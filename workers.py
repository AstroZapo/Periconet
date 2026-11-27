"""Conexiones independientes por equipo. Los fallos opcionales no cortan NINA."""
import asyncio
import datetime as dt
import itertools
import json
import logging
from time import perf_counter
from urllib.parse import urlsplit

import websockets

from astronomy import night_at_connection, number, site_from_profile
from clients.nina_client import NinaClient
from clients.phd2_client import PHD2Client
from monitoring import (
    CaptureTracker,
    active_target,
    exposure_state,
    fingerprint,
    mount_coordinates_for_capture,
    normalize,
    target_from_data,
)

logger = logging.getLogger("periconet")

SCHEDULER_TARGET_EVENTS = {"TS-NEWTARGETSTART", "TS-TARGETSTART"}
SCHEDULER_EVENTS = SCHEDULER_TARGET_EVENTS | {"TS-WAITSTART"}
SEQUENCE_EVENTS = SCHEDULER_EVENTS | {"SEQUENCE-STARTING", "SEQUENCE-FINISHED"}
SEQUENCE_CHECK_INTERVAL = 30

# ---- Alarmas (versión Mac/Windows) ------------------------------------------
# Errores graves que NINA anuncia por su WebSocket (los mismos que suelen
# acabar en una notificación de Pushover) y estrella perdida en PHD2.
ERROR_EVENTS = {
    "SEQUENCE-ENTITY-FAILED": "Fallo en la secuencia",
    "ERROR-AF": "Fallo del autoenfoque",
    "ERROR-PLATESOLVE": "Fallo del plate solving",
    "CAMERA-DOWNLOAD-TIMEOUT": "Timeout de descarga de la cámara",
}
# Solo cuentan si hay una secuencia en marcha (al recoger por la mañana es normal).
DISCONNECT_EVENTS = {
    "CAMERA-DISCONNECTED": "Cámara desconectada",
    "MOUNT-DISCONNECTED": "Montura desconectada",
    "GUIDER-DISCONNECTED": "Guiado desconectado",
    "FOCUSER-DISCONNECTED": "Enfocador desconectado",
    "FILTERWHEEL-DISCONNECTED": "Rueda de filtros desconectada",
    "ROTATOR-DISCONNECTED": "Rotador desconectado",
}
alert_ids = itertools.count(1)


async def raise_alert(setup, state, broadcast, kind, title, detail=""):
    alerts = state.setdefault("alerts", [])
    if kind == "star" and any(alert["kind"] == "star" for alert in alerts):
        return
    alerts.append(
        {
            "id": next(alert_ids),
            "kind": kind,
            "title": title,
            "detail": detail,
            "time": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    )
    del alerts[:-10]
    logger.warning("ALARMA %s: %s %s", setup["name"], title, detail)
    await broadcast({"type": "alerts", "setup_id": setup["id"], "alerts": list(alerts)})



def empty_target():
    return {
        "name": "Sin objetivo identificado",
        "key": None,
        "ra": None,
        "dec": None,
        "ra_string": "",
        "dec_string": "",
        "filter_summary": {},
    }


def build_state(setup):
    return {
        "name": setup["name"],
        "connected": False,
        "camera": None,
        "mount": None,
        "weather": None,
        "safety": None,
        "site": None,
        "night": None,
        "sequence_running": None,
        "hfr_history": [],
        "target": empty_target(),
        "exposure": exposure_state(None),
        "latest_image_id": None,
        "latest_image_url": None,
        "guide_steps": [],
        "guide_seq": 0,
        "guiding_status": "desconectado",
        "pixel_scale": None,
        "dome": None,
        "alerts": [],
    }


class NinaWorker:
    def __init__(self, setup, state, broadcast, images, config):
        self.setup, self.state, self.broadcast, self.images, self.config = setup, state, broadcast, images, config
        self.client = NinaClient(setup["nina_host"], setup["nina_port"])
        self.sequence_end_lock = asyncio.Lock()
        self.sequence_status_version = 0
        self.tracker = CaptureTracker()
        self.wake = asyncio.Event()
        self.sequence_wake = asyncio.Event()
        self.sequence_event = None
        self.socket_connected = False
        self.event_history_pending = True
        self.last_history_event = None
        self.start_time = None
        self.failures = set()
        # Versión Mac: sin nadie mirando el dashboard se sondea menos (ahorro de datos Starlink).
        self.has_viewers = lambda: True

    def report(self, name, error=None):
        if error is not None:
            logger.debug("%s: fallo en %s", self.setup["name"], name, exc_info=error)
            if name not in self.failures:
                logger.warning(
                    "%s: %s no disponible: %s: %s",
                    self.setup["name"], name, type(error).__name__, str(error) or "sin mensaje",
                )
                self.failures.add(name)
        elif name in self.failures:
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
        state = {
            k: v
            for k, v in self.state.items()
            if k not in ("guide_steps", "guide_seq", "guiding_status", "pixel_scale")
        }
        await self.broadcast({"type": "nina_update", "setup_id": self.setup["id"], "state": state})

    def clear_image(self):
        self.images.pop(self.setup["id"], None)
        self.state.update(latest_image_id=None, latest_image_url=None)

    def set_target(self, target):
        if target:
            previous = self.state.get("target", {})
            if previous.get("key") == target.get("key"):
                target["filter_summary"] = previous.get("filter_summary", {})
        changed = self.tracker.set_target(target)
        self.state["target"] = target or empty_target()
        if changed:
            self.clear_image()
            self.state["hfr_history"] = []
        return changed

    async def reset(self, *, restarted=False):
        self.sequence_status_version += 1
        self.tracker = CaptureTracker()
        if restarted:
            self.sequence_event = None
        self.event_history_pending = True
        self.last_history_event = None
        self.clear_image()
        self.state.update(
            connected=False, camera=None, mount=None, weather=None, safety=None,
            site=None, night=None, sequence_running=None, hfr_history=[],
            target=empty_target(), exposure=exposure_state(None), dome=None,
        )
        await self.publish()

    async def invalidate_sequence_status(self):
        await self.handle_event({"Event": "SEQUENCE-FINISHED"})

    async def refresh_advanced_sequence(self):
        # Target Scheduler gestiona su propio objetivo mediante eventos.
        if self.sequence_event in SCHEDULER_EVENTS:
            return
        version = self.sequence_status_version
        sequence = await self.optional("secuencia avanzada", self.client.get_advanced_sequence())
        if version != self.sequence_status_version:
            return
        running = None
        if isinstance(sequence, list):
            running = any(
                isinstance(block, dict) and str(block.get("Status", "")).casefold() == "running"
                for block in sequence
            )
        changed = running != self.state["sequence_running"]
        self.state["sequence_running"] = running
        target = active_target(sequence) if running else None
        explicit_target = self.tracker.target and not self.tracker.target.get("from_capture")
        if (
            target
            or (running is True and explicit_target)
            or (running is False and self.sequence_event != "SEQUENCE-STARTING")
        ):
            changed = self.set_target(target) or changed
        if changed:
            await self.publish()

    async def sequence_updates(self):
        while True:
            try:
                await asyncio.wait_for(self.sequence_wake.wait(), SEQUENCE_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self.sequence_wake.clear()
            if not self.state["connected"]:
                continue
            if self.event_history_pending or not self.socket_connected:
                self.event_history_pending = False
                if not await self.restore_target_events():
                    self.event_history_pending = True
            idle = self.state["sequence_running"] is False and self.sequence_event in (None, "SEQUENCE-FINISHED")
            if idle and (self.socket_connected or self.sequence_event == "SEQUENCE-FINISHED"):
                continue
            await self.refresh_advanced_sequence()

    async def poll(self):
        camera = await self.client.get_camera_info()
        if not isinstance(camera, dict):
            raise ValueError("Información de cámara no válida")
        start = await self.optional("inicio de NINA", self.client._get("/application-start"))
        if start and self.start_time and start != self.start_time:
            await self.reset(restarted=True)
        if start:
            self.start_time = start
        reconnected = not self.state["connected"]
        self.state.update(
            connected=True,
            camera=camera,
            exposure=exposure_state(camera, self.state["exposure"]),
        )
        if reconnected:
            self.sequence_wake.set()
        if self.state["site"] is None:
            profile = await self.optional("ubicación", self.client.get_profile())
            site = site_from_profile(profile)
            if site:
                self.state["site"] = site
                self.state["night"] = night_at_connection(site)
        mount, weather, safety, dome = await asyncio.gather(
            self.optional("montura", self.client.get_mount_info()),
            self.optional("meteorología", self.client.get_weather_info()),
            self.optional("seguridad", self.client.get_safety_info()),
            self.optional("cúpula", self.client.get_dome_info()),
        )
        self.state.update(mount=mount, weather=weather, safety=safety, dome=dome)
        sequence_version = self.sequence_status_version
        previous_count = len(self.tracker.history)
        had_history = self.tracker.initialized
        history = await self.optional("historial", self.read_history())
        if history is not None:
            reset = self.tracker.update_history(history)
            if reset:
                self.clear_image()
            if had_history and not reset and sequence_version == self.sequence_status_version:
                self.update_target_from_captures(history, previous_count)
            self.state["target"]["filter_summary"] = self.tracker.summary()
            limit = max(1, int(self.config.get("hfr_history_points", 60)))
            self.state["hfr_history"] = [item for item in history if self.tracker.matching(item)][-limit:]
            candidate = self.tracker.latest()
            if candidate and self.has_viewers():
                await self.optional("imagen", self.load_image(*candidate))
        await self.publish()

    def update_target_from_captures(self, history, first_index):
        if self.sequence_event in SCHEDULER_EVENTS or (
            self.sequence_event != "SEQUENCE-STARTING" and self.state["sequence_running"] is not True
        ):
            return
        if self.tracker.target and not self.tracker.target.get("from_capture"):
            return
        for index in range(len(history) - 1, first_index - 1, -1):
            item = history[index]
            if str(item.get("ImageType", "")).upper() != "LIGHT":
                continue
            target = target_from_data(item, "capture")
            if not target:
                continue
            previous = self.tracker.target
            same_target = previous and normalize(target["name"]) == normalize(previous["name"])
            if (
                same_target
                and previous["ra"] is not None
                and (previous.get("coordinates_source") != "mount" or target["ra"] is None)
            ):
                return
            if target["ra"] is None:
                coords = mount_coordinates_for_capture(self.state["mount"], item.get("Date"))
                if coords is not None:
                    target = target_from_data({**item, "Coordinates": coords}, "capture")
                    target["coordinates_source"] = "mount"
                    logger.debug(
                        "%s: coordenadas aproximadas de montura para %s",
                        self.setup["name"], target["name"],
                    )
            target["from_capture"] = True
            if same_target:
                if target["ra"] is not None:
                    target["key"] = previous["key"]
                    self.set_target(target)
                return
            self.set_target(target)
            self.tracker.floor = index
            return

    async def read_history(self):
        count = await self.client.get_image_count()
        old = self.tracker.history
        if not self.tracker.initialized or count < len(old) or count - len(old) > 10:
            result = await self.client.get_image_history()
            return result
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
        if not self.image_target_is_current(generation, item):
            return
        token = fingerprint([self.start_time, generation, index, item])
        if self.state["latest_image_id"] == token:
            return
        data = await self.client.get_image_bytes(index)
        if not data:
            return
        if not self.image_target_is_current(generation, item):
            return
        if await self.client.get_history_item(index) != item:
            return
        if not self.image_target_is_current(generation, item):
            return
        self.images[self.setup["id"]] = {
            "token": token,
            "index": index,
            "item": item,
            "generation": generation,
            "bytes": data,
            "hires": None,
            "client": self.client,
            "lock": asyncio.Lock(),
        }
        self.state.update(
            latest_image_id=token,
            latest_image_url=f"/api/image/{self.setup['id']}?v={token}",
        )

    def image_target_is_current(self, generation, item):
        running = self.state["sequence_running"] is True or (
            self.sequence_event == "SEQUENCE-STARTING" and self.socket_connected
        )
        return (
            self.state["connected"]
            and running
            and generation == self.tracker.generation
            and self.tracker.matching(item)
        )

    async def handle_event(self, event, *, replay=False):
        name = event.get("Event")
        if not replay and isinstance(name, str):
            if name in ERROR_EVENTS:
                detail = " · ".join(str(event[k]) for k in ("Entity", "Error") if event.get(k))
                await raise_alert(self.setup, self.state, self.broadcast, "error", ERROR_EVENTS[name], detail)
            elif name in DISCONNECT_EVENTS and self.state["sequence_running"] is True:
                await raise_alert(self.setup, self.state, self.broadcast, "error", DISCONNECT_EVENTS[name],
                                  "con la secuencia en marcha")
        if not isinstance(name, str) or (name not in SEQUENCE_EVENTS and name != "IMAGE-SAVE"):
            return
        logger.debug("%s: evento NINA %s", self.setup["name"], name)
        if name == "IMAGE-SAVE":
            self.wake.set()
            return
        self.sequence_status_version += 1
        self.sequence_event = name
        running = None if name == "SEQUENCE-STARTING" else name in SCHEDULER_EVENTS
        changed = self.state["sequence_running"] != running
        self.state["sequence_running"] = running
        target = None
        if name in SCHEDULER_TARGET_EVENTS:
            project = normalize(event.get("ProjectName"))
            target = target_from_data(event, f"scheduler/{project}")
            if target:
                target["project"] = project
                previous = self.tracker.target
                if (
                    target["ra"] is None
                    and previous
                    and previous.get("ra") is not None
                    and previous.get("project") == project
                    and normalize(previous["name"]) == normalize(target["name"])
                ):
                    target = previous
        target_changed = self.set_target(target)
        if changed or target_changed:
            self.wake.set()
            logger.debug(
                "%s: estado de secuencia por eventos (en curso=%s, objetivo=%s)",
                self.setup["name"], running, self.state["target"]["name"],
            )
            await self.publish()
        if name == "SEQUENCE-STARTING" and not replay:
            self.sequence_wake.set()

    async def restore_target_events(self):
        version = self.sequence_status_version
        history = await self.optional("eventos anteriores", self.client.get_event_history())
        if not isinstance(history, list):
            return False
        if version != self.sequence_status_version:
            return True
        for event in reversed(history):
            if not isinstance(event, dict):
                continue
            name = event.get("Event")
            if not isinstance(name, str) or name not in SEQUENCE_EVENTS:
                continue
            if event != self.last_history_event or self.state["sequence_running"] is None:
                self.last_history_event = event
                logger.debug(
                    "%s: recuperando el último evento de secuencia (%s de %d eventos)",
                    self.setup["name"], name, len(history),
                )
                await self.handle_event(event, replay=True)
            return True
        return True

    async def events(self):
        url = urlsplit(self.client.base_url)
        socket_url = f"ws://{url.netloc}/v2/socket"
        while True:
            try:
                logger.debug("%s: abriendo WebSocket NINA %s", self.setup["name"], socket_url)
                async with websockets.connect(socket_url, open_timeout=5, ping_interval=20, ping_timeout=10) as ws:
                    self.socket_connected = True
                    self.event_history_pending = True
                    self.sequence_wake.set()
                    logger.debug("%s: WebSocket NINA conectado", self.setup["name"])
                    self.report("eventos NINA")
                    async for raw in ws:
                        event = json.loads(raw).get("Response")
                        if isinstance(event, dict):
                            await self.handle_event(event)
                    logger.debug("%s: WebSocket NINA cerrado", self.setup["name"])
            except Exception as exc:
                self.report("eventos NINA", exc)
            if self.socket_connected:
                self.socket_connected = False
                self.sequence_status_version += 1
                self.state["sequence_running"] = None
                self.sequence_wake.set()
                await self.publish()
            await asyncio.sleep(5)

    async def run(self):
        logger.debug("%s: iniciando sondeo de %s", self.setup["name"], self.client.base_url)
        background = (asyncio.create_task(self.events()), asyncio.create_task(self.sequence_updates()))
        try:
            while True:
                self.wake.clear()
                started = perf_counter()
                try:
                    await self.poll()
                    self.report("NINA")
                except Exception as exc:
                    self.report("NINA", exc)
                    if self.state["connected"]:
                        if self.state["sequence_running"] is True:
                            await raise_alert(self.setup, self.state, self.broadcast, "error",
                                              "NINA no responde", "con la secuencia en marcha")
                        await self.reset()
                logger.debug(
                    "%s: sondeo terminado en %.1f ms (conectado=%s, sondeo pendiente=%s)",
                    self.setup["name"], (perf_counter() - started) * 1000,
                    self.state["connected"], self.wake.is_set(),
                )
                try:
                    interval = (
                        self.config.get("poll_interval_seconds", 5)
                        if self.has_viewers()
                        else self.config.get("idle_poll_interval_seconds", 30)
                    )
                    await asyncio.wait_for(self.wake.wait(), max(1, interval))
                    logger.debug("%s: siguiente sondeo adelantado por un evento u orden", self.setup["name"])
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await self.client.close()


async def phd2_loop(setup, state, broadcast):
    failed = False

    async def status(value):
        state["guiding_status"] = value
        await broadcast(
            {
                "type": "guiding_status",
                "setup_id": setup["id"],
                "status": value,
                "pixel_scale": state["pixel_scale"],
                "guide_seq": state["guide_seq"],
                "reset": value == "desconectado",
            }
        )

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
                    await status("detenido" if event.get("Status") else "guiando")
                elif name == "GuideStep":
                    if settling:
                        continue
                    ra, dec = number(event.get("RADistanceRaw")), number(event.get("DECDistanceRaw"))
                    if ra is None or dec is None:
                        continue
                    scale = state["pixel_scale"]
                    state["guide_seq"] += 1
                    point = {
                        "t": event.get("Timestamp"),
                        "seq": state["guide_seq"],
                        "ra_distance_px": ra,
                        "dec_distance_px": dec,
                        "ra_distance_arcsec": ra * scale if scale else None,
                        "dec_distance_arcsec": dec * scale if scale else None,
                    }
                    state["guide_steps"].append(point)
                    del state["guide_steps"][:-300]
                    state["guiding_status"] = "guiando"
                    await broadcast(
                        {"type": "guide_step", "setup_id": setup["id"], "point": point, "pixel_scale": scale}
                    )
                else:
                    value = {
                        "StarLost": "estrella perdida",
                        "GuidingStopped": "detenido",
                        "Paused": "detenido",
                        "GuidingStarted": "guiando",
                        "StartGuiding": "guiando",
                        "StartCalibration": "calibrando",
                        "LoopingExposures": "en bucle",
                        "LoopingExposuresStopped": "detenido",
                    }.get(name)
                    if name == "StarLost":
                        await raise_alert(setup, state, broadcast, "star", "Estrella perdida (PHD2)")
                    if name == "AppState":
                        value = {
                            "Guiding": "guiando",
                            "LostLock": "estrella perdida",
                            "Stopped": "detenido",
                            "Paused": "detenido",
                            "Looping": "en bucle",
                            "Calibrating": "calibrando",
                        }.get(event.get("State"), "conectado")
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
