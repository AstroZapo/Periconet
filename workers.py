"""Conexiones independientes por equipo. Los fallos opcionales no cortan NINA."""
import asyncio
import json
import logging
from urllib.parse import urlsplit

import websockets

from astronomy import night_at_connection, number, site_from_profile
from clients.nina_client import NinaClient
from clients.phd2_client import PHD2Client
from monitoring import CaptureTracker, active_target, exposure_state, fingerprint, target_from_data

logger = logging.getLogger("periconet")


def empty_target():
    return {"name": "Sin objetivo identificado", "key": None, "ra": None, "dec": None,
            "ra_string": "", "dec_string": "", "filter_summary": {}}


def build_state(setup):
    return {"name": setup["name"], "connected": False, "camera": None, "mount": None,
            "weather": None, "safety": None, "site": None, "night": None, "sequence_running": False,
            "hfr_history": [], "target": empty_target(), "exposure": exposure_state(None),
            "latest_image_id": None, "latest_image_url": None,
            "guide_steps": [], "guide_seq": 0, "guiding_status": "desconectado", "pixel_scale": None}


class NinaWorker:
    def __init__(self, setup, state, broadcast, images, config):
        self.setup, self.state, self.broadcast, self.images, self.config = setup, state, broadcast, images, config
        self.client = NinaClient(setup["nina_host"], setup["nina_port"])
        self.sequence_end_lock = asyncio.Lock()
        self.sequence_status_version = 0
        self.tracker = CaptureTracker()
        self.wake = asyncio.Event()
        self.scheduler = None
        self.start_time = None
        self.failures = set()

    def report(self, name, error=None):
        if error and name not in self.failures:
            logger.warning("%s: %s no disponible: %s", self.setup["name"], name, error)
            self.failures.add(name)
        elif not error and name in self.failures:
            logger.info("%s: %s recuperado", self.setup["name"], name)
            self.failures.remove(name)

    async def optional(self, name, operation):
        try:
            result = await operation
            self.report(name)
            return result
        except Exception as exc:
            self.report(name, exc)
            return None

    async def publish(self):
        # El guiado tiene mensajes propios, con contador monotónico.
        state = {k: v for k, v in self.state.items() if k not in ("guide_steps", "guide_seq", "guiding_status", "pixel_scale")}
        await self.broadcast({"type": "nina_update", "setup_id": self.setup["id"], "state": state})

    def clear_image(self):
        self.images.pop(self.setup["id"], None)
        self.state.update(latest_image_id=None, latest_image_url=None)

    async def set_target(self, target):
        if target:
            previous = self.state.get("target", {})
            if previous.get("key") == target.get("key"):
                target["filter_summary"] = previous.get("filter_summary", {})
        if self.tracker.set_target(target):
            self.clear_image()
            self.state["hfr_history"] = []
            self.state["target"] = target or empty_target()
            await self.publish()
        self.state["target"] = target or empty_target()

    async def reset(self):
        self.tracker = CaptureTracker()
        self.scheduler = None
        self.clear_image()
        self.state.update(connected=False, camera=None, mount=None, weather=None, safety=None,
                          site=None, night=None, sequence_running=False,
                          hfr_history=[], target=empty_target(), exposure=exposure_state(None))
        await self.publish()

    async def invalidate_sequence_status(self):
        self.sequence_status_version += 1
        self.wake.set()
        if self.state["sequence_running"]:
            self.state["sequence_running"] = False
            await self.publish()

    async def poll(self):
        camera = await self.client.get_camera_info()
        if not isinstance(camera, dict):
            raise ValueError("Información de cámara no válida")
        start = await self.optional("inicio de NINA", self.client._get("/application-start"))
        if start and self.start_time and start != self.start_time:
            await self.reset()
        if start:
            self.start_time = start
        self.state.update(connected=True, camera=camera, exposure=exposure_state(camera))
        if self.state["site"] is None:
            profile = await self.optional("ubicación", self.client.get_profile())
            site = site_from_profile(profile)
            if site:
                self.state["site"] = site
                self.state["night"] = night_at_connection(site)
        sequence_status_version = self.sequence_status_version
        sequence, mount, weather, safety = await asyncio.gather(
            self.optional("secuencia", self.client.get_sequence()),
            self.optional("montura", self.client.get_mount_info()),
            self.optional("meteorología", self.client.get_weather_info()),
            self.optional("seguridad", self.client.get_safety_info()))
        self.state.update(mount=mount, weather=weather, safety=safety)
        if sequence_status_version == self.sequence_status_version:
            # /sequence/state devuelve los bloques de la secuencia avanzada y sus estados.
            self.state["sequence_running"] = isinstance(sequence, list) and any(
                isinstance(block, dict) and str(block.get("Status", "")).casefold() == "running"
                for block in sequence
            )
        await self.set_target(active_target(sequence, self.scheduler))
        history = await self.optional("historial", self.read_history())
        if history is not None:
            if self.tracker.update_history(history):
                self.clear_image()
            self.state["target"]["filter_summary"] = self.tracker.summary()
            limit = max(1, int(self.config.get("hfr_history_points", 60)))
            self.state["hfr_history"] = [item for item in history if self.tracker.matching(item)][-limit:]
            candidate = self.tracker.latest()
            if candidate:
                await self.optional("imagen", self.load_image(*candidate))
        await self.publish()

    async def read_history(self):
        count = await self.client.get_image_count()
        old = self.tracker.history
        if not self.tracker.initialized or count < len(old) or count - len(old) > 10:
            result = await self.client.get_image_history()
        else:
            # Verifica el último registro incluso si el contador no cambia:
            # un reinicio puede reutilizar los índices del historial.
            if old and await self.client.get_history_item(len(old) - 1) != old[-1]:
                return await self.client.get_image_history()
            result = list(old)
            for index in range(len(old), count):
                item = await self.client.get_history_item(index)
                if not isinstance(item, dict):
                    raise ValueError("Captura no disponible")
                result.append(item)
        return result

    async def load_image(self, index, item):
        generation = self.tracker.generation
        token = fingerprint([self.start_time, generation, index, item])
        if self.state["latest_image_id"] == token:
            return
        data = await self.client.get_image_bytes(index)
        if not data:
            return
        # El objetivo puede haber cambiado durante la descarga. Verificar antes
        # de publicar, también si no hay WebSocket disponible.
        sequence = await self.client.get_sequence()
        target = active_target(sequence, self.scheduler)
        await self.set_target(target)
        if generation != self.tracker.generation or not self.tracker.matching(item):
            return
        if await self.client.get_history_item(index) != item:
            return
        if generation != self.tracker.generation or not self.tracker.matching(item):
            return
        self.images[self.setup["id"]] = {"token": token, "index": index, "item": item,
                                          "bytes": data, "hires": None, "client": self.client,
                                          "lock": asyncio.Lock()}
        self.state.update(latest_image_id=token, latest_image_url=f"/api/image/{self.setup['id']}?v={token}")

    async def handle_event(self, event):
        name = event.get("Event")
        if name in ("TS-NEWTARGETSTART", "TS-TARGETSTART"):
            self.scheduler = event
            await self.set_target(target_from_data(event, "scheduler"))
        elif name in ("SEQUENCE-FINISHED", "SEQUENCE-STARTING"):
            self.scheduler = None
            await self.invalidate_sequence_status()
            await self.set_target(None)
        if name in ("IMAGE-SAVE", "TS-NEWTARGETSTART", "TS-TARGETSTART"):
            self.wake.set()

    async def events(self):
        url = urlsplit(self.client.base_url)
        while True:
            try:
                async with websockets.connect(f"ws://{url.netloc}/v2/socket", open_timeout=5,
                                               ping_interval=20, ping_timeout=10) as ws:
                    self.report("eventos NINA")
                    # Recuperar el contexto de Target Scheduler al abrir el panel
                    # durante una secuencia ya iniciada.
                    history = await self.optional("eventos anteriores", self.client._get("/event-history"))
                    if isinstance(history, list):
                        for event in reversed(history):
                            if event.get("Event") in ("TS-NEWTARGETSTART", "TS-TARGETSTART", "SEQUENCE-FINISHED", "SEQUENCE-STARTING"):
                                self.scheduler = event if event["Event"].startswith("TS-") else None
                                break
                    async for raw in ws:
                        event = json.loads(raw).get("Response")
                        if isinstance(event, dict):
                            await self.handle_event(event)
            except Exception as exc:
                self.report("eventos NINA", exc)
                self.scheduler = None
                self.wake.set()
            await asyncio.sleep(5)

    async def run(self):
        listener = asyncio.create_task(self.events())
        try:
            while True:
                self.wake.clear()
                try:
                    await self.poll()
                    self.report("NINA")
                except Exception as exc:
                    self.report("NINA", exc)
                    if self.state["connected"]:
                        await self.reset()
                try:
                    await asyncio.wait_for(self.wake.wait(), max(1, self.config.get("poll_interval_seconds", 5)))
                except asyncio.TimeoutError:
                    pass
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            await self.client.close()


async def phd2_loop(setup, state, broadcast):
    failed = False
    async def status(value):
        state["guiding_status"] = value
        await broadcast({"type": "guiding_status", "setup_id": setup["id"], "status": value,
                         "pixel_scale": state["pixel_scale"], "guide_seq": state["guide_seq"],
                         "reset": value == "desconectado"})
    while True:
        client = PHD2Client(setup["phd2_host"], setup["phd2_port"])
        try:
            await client.connect()
            if failed:
                logger.info("PHD2 %s recuperado", setup["name"])
            failed = False
            scale = number(await client.get_pixel_scale())
            state["pixel_scale"] = scale if scale and scale > 0 else None
            await status("conectado")
            settling = False
            async for event in client.events():
                name = event.get("Event")
                if name in ("Settling", "GuidingDithered"):
                    settling = True
                    await status("estabilizando")
                elif name == "SettleDone":
                    settling = False
                    await status("guiando" if not event.get("Status") else "detenido")
                elif name == "GuideStep":
                    if settling:
                        continue
                    ra, dec = number(event.get("RADistanceRaw")), number(event.get("DECDistanceRaw"))
                    if ra is None or dec is None:
                        continue
                    scale = state["pixel_scale"]
                    state["guide_seq"] += 1
                    point = {"t": event.get("Timestamp"), "seq": state["guide_seq"],
                             "ra_distance_px": ra, "dec_distance_px": dec,
                             "ra_distance_arcsec": ra * scale if scale else None,
                             "dec_distance_arcsec": dec * scale if scale else None}
                    state["guide_steps"].append(point)
                    del state["guide_steps"][:-300]
                    state["guiding_status"] = "guiando"
                    await broadcast({"type": "guide_step", "setup_id": setup["id"], "point": point,
                                     "pixel_scale": scale})
                else:
                    value = {"StarLost": "estrella perdida", "GuidingStopped": "detenido",
                             "Paused": "detenido", "GuidingStarted": "guiando",
                             "StartGuiding": "guiando", "StartCalibration": "calibrando",
                             "LoopingExposures": "en bucle", "LoopingExposuresStopped": "detenido"}.get(name)
                    if name == "AppState":
                        value = {"Guiding": "guiando", "LostLock": "estrella perdida", "Stopped": "detenido",
                                 "Paused": "detenido", "Looping": "en bucle", "Calibrating": "calibrando"}.get(event.get("State"), "conectado")
                    if value:
                        await status(value)
            raise ConnectionError("PHD2 cerró la conexión")
        except Exception as exc:
            if not failed:
                logger.warning("PHD2 %s no disponible: %s", setup["name"], exc)
            failed = True
        finally:
            await client.close()
            state["guide_steps"].clear()
            state["pixel_scale"] = None
            await status("desconectado")
        await asyncio.sleep(5)
