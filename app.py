"""Servidor local de monitorización Periconet."""
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from monitoring import active_target, fingerprint
from workers import NinaWorker, build_state, phd2_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("periconet")
logging.getLogger("httpx").setLevel(logging.WARNING)
BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("PERICONET_CONFIG", BASE_DIR / "config.json"))


class SetupInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    id: str | None = Field(default=None, pattern=r"^[\w-]+$", max_length=100)
    name: str = Field(min_length=1, max_length=100)
    nina_host: str = Field(min_length=1, max_length=253)
    nina_port: int = Field(default=1888, ge=1, le=65535)
    phd2_host: str = Field(default="", max_length=253)
    phd2_port: int = Field(default=4400, ge=1, le=65535)
    guide_axis_range: float = Field(default=3, gt=0, le=1000, allow_inf_nan=False)
    guide_axis_unit: Literal["arcsec", "px"] = "arcsec"

    @field_validator("nina_host", "phd2_host")
    @classmethod
    def host(cls, value):
        if not value:
            return value
        value = value.strip("[]")
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass
        encoded = value.encode("idna").decode("ascii")
        if not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part) for part in encoded.split(".")):
            raise ValueError("Introduce una IP o nombre de equipo, sin http:// ni puerto")
        return encoded


class ExtraImageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2048)
    refresh_seconds: int = Field(default=60, ge=10, le=86400)

    @field_validator("url")
    @classmethod
    def url_http(cls, value):
        if value:
            url = httpx.URL(value)
            if url.scheme not in ("http", "https") or not url.host:
                raise ValueError("La imagen necesita una URL http:// o https://")
        return value


class SetupsPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    observatory_name: str = Field(default="", max_length=200)
    setups: list[SetupInput]
    extra_images: list[ExtraImageInput] | None = None
    port: int | None = Field(default=None, ge=1024, le=65535)

    @model_validator(mode="after")
    def unique(self):
        names = [s.name.casefold() for s in self.setups]
        ids = [s.id for s in self.setups if s.id]
        if len(names) != len(set(names)):
            raise ValueError("Cada equipo debe tener un nombre diferente")
        if len(ids) != len(set(ids)):
            raise ValueError("Identificadores de equipo duplicados")
        return self


DEFAULT_CONFIG = {"poll_interval_seconds": 5, "hfr_history_points": 60,
                  "observatory_name": "", "port": 8000, "setups": [], "extra_images": []}
CONFIG = dict(DEFAULT_CONFIG)
if CONFIG_PATH.exists():
    try:
        CONFIG.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        parsed = SetupsPayload.model_validate(CONFIG)
        CONFIG["setups"] = [dict(s.model_dump(), id=s.id or uuid.uuid4().hex) for s in parsed.setups]
        CONFIG["poll_interval_seconds"] = max(1, float(CONFIG["poll_interval_seconds"]))
        CONFIG["hfr_history_points"] = max(1, int(CONFIG["hfr_history_points"]))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Configuración inválida en {CONFIG_PATH}: {exc}") from exc

STATE = {s["id"]: build_state(s) for s in CONFIG["setups"]}
IMAGES = {}
WORKERS = {}
TASKS = {}
FRONTEND_CLIENTS = {}
CONFIG_LOCK = asyncio.Lock()
SERVER_PORT = CONFIG["port"]


def save_config(config):
    # Sustituir el archivo solo cuando la escritura completa esté en disco.
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(config, output, indent=2, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def config_revision():
    return fingerprint(CONFIG)


async def broadcast(message):
    # Serializar ahora: las colas nunca comparten diccionarios mutables.
    raw = json.dumps(message, ensure_ascii=False, allow_nan=False)
    for ws, queue in tuple(FRONTEND_CLIENTS.items()):
        try:
            queue.put_nowait(raw)
        except asyncio.QueueFull:
            # El emisor cerrará este cliente y el navegador pedirá un snapshot.
            FRONTEND_CLIENTS.pop(ws, None)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)


def start_setup(setup):
    key = setup["id"]
    worker = NinaWorker(setup, STATE[key], broadcast, IMAGES, CONFIG)
    WORKERS[key] = worker
    tasks = [asyncio.create_task(worker.run())]
    if setup["phd2_host"]:
        tasks.append(asyncio.create_task(phd2_loop(setup, STATE[key], broadcast)))
    TASKS[key] = tasks


async def stop_setup(key):
    tasks = TASKS.pop(key, [])
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    WORKERS.pop(key, None)
    IMAGES.pop(key, None)


@asynccontextmanager
async def lifespan(app):
    if not CONFIG_PATH.exists():
        save_config(CONFIG)
    try:
        for setup in CONFIG["setups"]:
            start_setup(setup)
        yield
    finally:
        for key in list(TASKS):
            await stop_setup(key)


app = FastAPI(title="Periconet", lifespan=lifespan)


@app.post("/api/setup")
async def save_setup(payload: SetupsPayload):
    async with CONFIG_LOCK:
        old = {s["id"]: s for s in CONFIG["setups"]}
        setups = []
        for model in payload.setups:
            data = model.model_dump()
            if model.id and model.id not in old:
                raise HTTPException(422, "El equipo ya no existe; recarga la configuración")
            data["id"] = model.id or uuid.uuid4().hex
            setups.append(data)
        new = dict(CONFIG, setups=setups, observatory_name=payload.observatory_name)
        if payload.port is not None:
            new["port"] = payload.port
        if payload.extra_images is not None:
            new["extra_images"] = [e.model_dump() for e in payload.extra_images]
        try:
            save_config(new)
        except OSError as exc:
            logger.error("No se pudo guardar configuración: %s", exc)
            raise HTTPException(500, "No se pudo guardar la configuración en disco") from exc
        new_by_id = {s["id"]: s for s in setups}
        endpoints = ("nina_host", "nina_port", "phd2_host", "phd2_port")
        restart = {key for key, s in old.items() if key not in new_by_id or
                   any(s.get(field) != new_by_id[key].get(field) for field in endpoints)}
        for key in restart:
            await stop_setup(key)
            STATE.pop(key, None)
        CONFIG.clear()
        CONFIG.update(new)
        for setup in setups:
            key = setup["id"]
            if key not in STATE:
                STATE[key] = build_state(setup)
                start_setup(setup)
            STATE[key]["name"] = setup["name"]
            if key in WORKERS:
                WORKERS[key].setup = setup
        await broadcast({"type": "config_changed", "revision": config_revision()})
        return {"ok": True, "setups": setups, "restart_required": new["port"] != SERVER_PORT,
                "port": new["port"]}


@app.get("/api/config")
async def get_config():
    return {key: CONFIG.get(key) for key in ("setups", "observatory_name", "extra_images", "port")} | {"revision": config_revision()}


@app.get("/api/setups")
async def get_setups():
    return [{key: s[key] for key in ("id", "name", "guide_axis_range", "guide_axis_unit")} for s in CONFIG["setups"]]


@app.get("/api/state")
async def get_all_state():
    return STATE


@app.get("/api/state/{setup_id}")
async def get_state(setup_id: str):
    return STATE.get(setup_id, {})


def image_response(data, request):
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    kind = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return Response(data, media_type=kind, headers=headers)


@app.get("/api/image/{setup_id}")
async def get_latest_image(setup_id: str, request: Request):
    record = IMAGES.get(setup_id)
    if not record or request.query_params.get("v", record["token"]) != record["token"]:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return image_response(record["bytes"], request)


@app.get("/api/image/{setup_id}/hires")
async def get_latest_hires_image(setup_id: str, request: Request):
    record, worker = IMAGES.get(setup_id), WORKERS.get(setup_id)
    if not record or not worker or request.query_params.get("v", record["token"]) != record["token"]:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    try:
        async with record["lock"]:
            if not record["hires"]:
                client = record["client"]
                if await client.get_history_item(record["index"]) != record["item"]:
                    return Response(status_code=409)
                data = await client.get_image_bytes(record["index"], quality=65, resize=False)
                sequence = await client.get_sequence()
                await worker.set_target(active_target(sequence, worker.scheduler))
                if IMAGES.get(setup_id) is not record or await client.get_history_item(record["index"]) != record["item"]:
                    return Response(status_code=409)
                if not data:
                    return Response(status_code=404)
                record["hires"] = data
            if IMAGES.get(setup_id) is not record:
                return Response(status_code=409)
            return image_response(record["hires"], request)
    except Exception as exc:
        logger.debug("Imagen HD %s no disponible: %s", setup_id, exc)
        return Response(status_code=502)


@app.get("/api/extra-image/{index}")
async def get_extra_image(index: int, request: Request):
    images = CONFIG.get("extra_images", [])
    if not 0 <= index < len(images) or not images[index].get("url"):
        return Response(status_code=404)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            result = await client.get(images[index]["url"])
            result.raise_for_status()
        response = image_response(result.content, request)
        response.headers["content-type"] = result.headers.get("content-type", "image/jpeg")
        return response
    except httpx.HTTPError:
        return Response(status_code=502)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue = asyncio.Queue(maxsize=64)
    FRONTEND_CLIENTS[websocket] = queue
    queue.put_nowait(json.dumps({"type": "initial_state", "state": STATE, "revision": config_revision()}))
    async def send():
        while True:
            try:
                raw = await asyncio.wait_for(queue.get(), 5)
            except asyncio.TimeoutError:
                raw = '{"type":"heartbeat"}'
            if raw is None:
                return
            await asyncio.wait_for(websocket.send_text(raw), 2)
    async def receive():
        while True:
            await websocket.receive_text()
    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, OSError):
        pass
    finally:
        FRONTEND_CLIENTS.pop(websocket, None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(websocket.close(), 2)
        except Exception:
            pass


frontend_dir = BASE_DIR / "frontend"
if not frontend_dir.exists() and getattr(sys, "frozen", False):
    frontend_dir = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "frontend"
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(frontend_dir / ("index.html" if CONFIG["setups"] else "setup.html"), headers={"Cache-Control": "no-cache"})


@app.get("/setup")
async def setup_page():
    return FileResponse(frontend_dir / "setup.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, access_log=False)
