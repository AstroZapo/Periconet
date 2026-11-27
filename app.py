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
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

import camera_credentials
import roof_alpaca
from cameras import (
    CamerasPayload,
    StoredCamera,
    check_unique,
    download_snapshot,
    prepare_cameras,
)
from clients.nina_client import NinaError
from monitoring import fingerprint
from paths import BASE_DIR, DATA_DIR, RESOURCE_DIR
from workers import NinaWorker, build_state, phd2_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("periconet")
logging.getLogger("httpx").setLevel(logging.WARNING)
CONFIG_PATH = Path(os.environ.get("PERICONET_CONFIG", DATA_DIR / "config.json"))
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


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
        if not all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
            for part in encoded.split(".")
        ):
            raise ValueError("Introduce una IP o nombre de equipo, sin http:// ni puerto")
        return encoded


class ExtraImageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2048)
    refresh_seconds: int = Field(default=60, ge=10, le=86400)
    # Añadido en la versión Mac: "page" muestra una página web completa (iframe).
    kind: Literal["image", "page"] = "image"
    size: Literal["small", "normal", "large", "side"] = "normal"

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
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None
    camera_settings: CamerasPayload | None = None
    # Versión Mac: techo Talon6 directo por Alpaca (vacío = solo vía NINA)
    roof_alpaca_url: str | None = Field(default=None, max_length=300)
    roof_alpaca_device: int | None = Field(default=None, ge=0, le=99)
    roof_alpaca_host: str | None = Field(default=None, max_length=255)

    @field_validator("roof_alpaca_url")
    @classmethod
    def roof_url(cls, value):
        if value:
            url = httpx.URL(value)
            if url.scheme not in ("http", "https") or not url.host:
                raise ValueError("La dirección Alpaca del techo debe ser como http://192.168.1.60:11111")
        return value

    @model_validator(mode="after")
    def unique(self):
        names = [s.name.casefold() for s in self.setups]
        ids = [s.id for s in self.setups if s.id]
        if len(names) != len(set(names)):
            raise ValueError("Cada equipo debe tener un nombre diferente")
        if len(ids) != len(set(ids)):
            raise ValueError("Identificadores de equipo duplicados")
        return self


DEFAULT_CONFIG = {
    "poll_interval_seconds": 5,
    "idle_poll_interval_seconds": 30,
    "hfr_history_points": 60,
    "observatory_name": "",
    "port": 8000,
    "log_level": "INFO",
    "setups": [],
    "extra_images": [],
    "cameras": [],
    "roof_alpaca_url": "",
    "roof_alpaca_device": 0,
    "roof_alpaca_host": "",
}
CONFIG = dict(DEFAULT_CONFIG)
if CONFIG_PATH.exists():
    try:
        CONFIG.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        parsed = SetupsPayload.model_validate(CONFIG)
        CONFIG["log_level"] = parsed.log_level or DEFAULT_CONFIG["log_level"]
        CONFIG["setups"] = [dict(s.model_dump(), id=s.id or uuid.uuid4().hex) for s in parsed.setups]
        try:
            cameras = TypeAdapter(list[StoredCamera]).validate_python(CONFIG["cameras"])
        except ValidationError as exc:
            details = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors(include_input=False))
            raise RuntimeError(f"Configuración de cámaras inválida en {CONFIG_PATH}: {details}") from None
        check_unique(cameras)
        CONFIG["cameras"] = [camera.model_dump() for camera in cameras]
        CONFIG["poll_interval_seconds"] = max(1, float(CONFIG["poll_interval_seconds"]))
        CONFIG["hfr_history_points"] = max(1, int(CONFIG["hfr_history_points"]))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Configuración inválida en {CONFIG_PATH}: {exc}") from exc

STATE = {s["id"]: build_state(s) for s in CONFIG["setups"]}
IMAGES = {}
WORKERS = {}
TASKS = {}
CAMERA_DOWNLOADS = {}
FRONTEND_CLIENTS = {}
ROOF = {}
ROOF_TASK = None
ROOF_WAKE = asyncio.Event()
SHUTDOWN_HOOK = None  # lo pone el lanzador (periconet.py)


def has_viewers():
    return bool(FRONTEND_CLIENTS)
CONFIG_LOCK = asyncio.Lock()
SERVER_PORT = CONFIG["port"]


def save_config(config):
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
    raw = json.dumps(message, ensure_ascii=False, allow_nan=False)
    for ws, queue in tuple(FRONTEND_CLIENTS.items()):
        try:
            queue.put_nowait(raw)
        except asyncio.QueueFull:
            # Cliente demasiado lento: se le desconecta para no frenar al resto.
            FRONTEND_CLIENTS.pop(ws, None)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)


def start_setup(setup):
    key = setup["id"]
    worker = NinaWorker(setup, STATE[key], broadcast, IMAGES, CONFIG)
    worker.has_viewers = has_viewers
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
    logger.setLevel(CONFIG["log_level"])
    if not CONFIG_PATH.exists():
        save_config(CONFIG)
    for setup in CONFIG["setups"]:
        start_setup(setup)
    global ROOF_TASK
    ROOF_TASK = asyncio.create_task(
        roof_alpaca.roof_loop(CONFIG, ROOF, broadcast, has_viewers=has_viewers, wake=ROOF_WAKE)
    )
    try:
        yield
    finally:
        ROOF_TASK.cancel()
        await asyncio.gather(ROOF_TASK, return_exceptions=True)
        downloads = [task for _, task in CAMERA_DOWNLOADS.values()]
        for task in downloads:
            task.cancel()
        await asyncio.gather(*downloads, return_exceptions=True)
        CAMERA_DOWNLOADS.clear()
        for key in list(TASKS):
            await stop_setup(key)


app = FastAPI(title="Periconet", lifespan=lifespan)


@app.post("/api/setup")
async def save_setup(request: Request, response: Response):
    data = await read_setup_json(request)
    if isinstance(data, dict) and data.get("camera_settings") is not None:
        require_camera_admin(request)
    payload = validate_setup_payload(SetupsPayload, data)
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
        if payload.log_level is not None:
            new["log_level"] = payload.log_level
        for field in ("roof_alpaca_url", "roof_alpaca_device", "roof_alpaca_host"):
            if getattr(payload, field) is not None:
                new[field] = getattr(payload, field)
        if payload.extra_images is not None:
            new["extra_images"] = [e.model_dump() for e in payload.extra_images]
        retired_credentials = set()
        if payload.camera_settings is not None:
            retired_credentials = persist_camera_config(new, payload.camera_settings)
        else:
            try:
                save_config(new)
            except OSError as exc:
                logger.error("No se pudo guardar configuración: %s", exc)
                raise HTTPException(500, "No se pudo guardar la configuración en disco") from exc

        new_by_id = {s["id"]: s for s in setups}
        endpoints = ("nina_host", "nina_port", "phd2_host", "phd2_port")
        restart = {
            key
            for key, s in old.items()
            if key not in new_by_id or any(s.get(field) != new_by_id[key].get(field) for field in endpoints)
        }
        for key in restart:
            await stop_setup(key)
            STATE.pop(key, None)
        CONFIG.clear()
        CONFIG.update(new)
        logger.setLevel(new["log_level"])
        forget_camera_credentials(retired_credentials)
        for setup in setups:
            key = setup["id"]
            if key not in STATE:
                STATE[key] = build_state(setup)
                start_setup(setup)
            STATE[key]["name"] = setup["name"]
            if key in WORKERS:
                WORKERS[key].setup = setup
        await broadcast({"type": "config_changed", "revision": config_revision()})
        result = {
            "ok": True,
            "setups": setups,
            "restart_required": new["port"] != SERVER_PORT,
            "port": new["port"],
        }
        if payload.camera_settings is not None:
            response.headers["Cache-Control"] = "no-store"
            result["camera_settings"] = camera_admin_settings()
        return result


@app.get("/api/config")
async def get_config():
    return {key: CONFIG.get(key) for key in ("setups", "observatory_name", "extra_images", "port", "log_level",
                                                           "roof_alpaca_url", "roof_alpaca_device", "roof_alpaca_host")} | {
        "revision": config_revision(),
        "cameras": [{key: camera[key] for key in ("id", "name", "refresh_seconds")} for camera in CONFIG["cameras"]],
    }


def require_camera_admin(request: Request):
    origin = request.headers.get("origin")
    local_origin = f"{request.url.scheme}://{request.url.netloc}"
    if (
        request.client is None
        or request.client.host not in ("127.0.0.1", "::1")
        or request.url.hostname not in ("localhost", "127.0.0.1", "::1")
        or any(
            header in request.headers
            for header in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")
        )
        or (origin is not None and origin != local_origin)
        or request.headers.get("X-Periconet-Action") != "camera-setup"
    ):
        raise HTTPException(403, "Abre la configuración de cámaras desde localhost en el equipo de Periconet.")


def camera_admin_settings():
    cameras = []
    for camera in CONFIG["cameras"]:
        credential_id = camera["credential_id"]
        protected_url = camera["url"] is None
        has_credentials = False
        if credential_id:
            try:
                read_secret = camera_credentials.read_url if protected_url else camera_credentials.read
                has_credentials = read_secret(credential_id) is not None
            except (OSError, ValueError):
                pass
        cameras.append(
            {key: camera[key] for key in ("id", "name", "url", "refresh_seconds")}
            | {
                "protected_url": protected_url,
                "has_credentials": has_credentials,
                "credentials_missing": bool(credential_id and not has_credentials),
            }
        )
    return {"cameras": cameras, "revision": fingerprint(CONFIG["cameras"])}


@app.get("/api/setup/cameras", dependencies=[Depends(require_camera_admin)])
async def get_camera_setup(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return camera_admin_settings()


def forget_camera_credentials(ids):
    for credential_id in ids:
        try:
            camera_credentials.delete(credential_id)
        except OSError:
            logger.warning("No se pudo eliminar una credencial de cámara de %s", camera_credentials.STORE_NAME)


async def read_setup_json(request):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type and content_type != "application/json":
        if not content_type.startswith("application/") or not content_type.endswith("+json"):
            raise HTTPException(422, "La configuración debe enviarse como JSON.")
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(422, "La configuración no es válida.") from None


def validate_setup_payload(model, data):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        raise HTTPException(422, errors) from None


def persist_camera_config(config, payload):
    if payload.revision != fingerprint(CONFIG["cameras"]):
        raise HTTPException(409, "La configuración de cámaras ha cambiado. Recarga la página.")
    existing = {camera["id"]: camera for camera in CONFIG["cameras"]}
    try:
        cameras, pending = prepare_cameras(payload.cameras, existing)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except OSError:
        raise HTTPException(500, f"No se pudo leer {camera_credentials.STORE_NAME}.") from None
    written = []
    try:
        config["cameras"] = cameras
        for credential_id, secret in pending.items():
            if isinstance(secret, str):
                camera_credentials.write_url(credential_id, secret)
            else:
                camera_credentials.write(credential_id, *secret)
            written.append(credential_id)
        save_config(config)
    except (OSError, ValueError):
        forget_camera_credentials(written)
        raise HTTPException(
            500, f"No se pudieron guardar las cámaras o sus credenciales en {camera_credentials.STORE_NAME}."
        ) from None
    old_ids = {camera["credential_id"] for camera in existing.values() if camera["credential_id"]}
    new_ids = {camera["credential_id"] for camera in cameras}
    return old_ids - new_ids


@app.post("/api/setup/cameras", dependencies=[Depends(require_camera_admin)])
async def save_camera_setup(request: Request, response: Response):
    payload = validate_setup_payload(CamerasPayload, await read_setup_json(request))
    async with CONFIG_LOCK:
        new = dict(CONFIG)
        retired_credentials = persist_camera_config(new, payload)
        CONFIG["cameras"] = new["cameras"]
        forget_camera_credentials(retired_credentials)
        await broadcast({"type": "config_changed", "revision": config_revision()})
        response.headers["Cache-Control"] = "no-store"
        return camera_admin_settings()


@app.get("/api/setups")
async def get_setups():
    return [
        {key: s[key] for key in ("id", "name", "guide_axis_range", "guide_axis_unit")}
        for s in CONFIG["setups"]
    ]


@app.post("/api/setups/{setup_id}/sequence/to-end")
async def skip_sequence_to_end(setup_id: str, request: Request):
    if request.headers.get("X-Periconet-Action") != "sequence-to-end":
        raise HTTPException(403, "Petición de control no válida")
    worker = WORKERS.get(setup_id)
    if worker is None:
        raise HTTPException(404, "El equipo no está disponible")
    if worker.sequence_end_lock.locked():
        raise HTTPException(409, "Ya hay una orden en curso para este equipo")
    async with worker.sequence_end_lock:
        try:
            logger.debug("NINA %s: solicitando el salto al bloque final", setup_id)
            await worker.client.skip_sequence_to_end()
            logger.debug("NINA %s: salto al bloque final confirmado", setup_id)
        except httpx.TimeoutException as exc:
            raise HTTPException(
                504, "NINA no respondió a tiempo. Comprueba la secuencia antes de repetir la orden."
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                await worker.invalidate_sequence_status()
                try:
                    body = exc.response.json()
                except ValueError:
                    body = None
                error = body.get("Error") if isinstance(body, dict) else None
                error = error.strip() if isinstance(error, str) else ""
                detail = {
                    "Sequence is not initialized": "El secuenciador avanzado de NINA no está inicializado.",
                    "Sequence not running": "NINA no tiene una secuencia avanzada en ejecución.",
                }.get(error, error or "NINA rechazó el salto al bloque final de la secuencia.")
                raise HTTPException(409, detail) from exc
            raise HTTPException(502, f"NINA respondió con un error HTTP {exc.response.status_code}") from exc
        except NinaError as exc:
            raise HTTPException(502, str(exc)) from exc
        except httpx.RequestError as exc:
            raise HTTPException(502, "No se pudo confirmar la orden con NINA. Comprueba la conexión.") from exc
        worker.wake.set()
    return {"ok": True}


ROOF_ACTIONS = {"open": "abrir", "close": "cerrar", "stop": "parar"}


@app.post("/api/roof/{action}")
async def roof_command(action: str, request: Request):
    """Versión Mac: control del techo (Talon6) a través de la cúpula ASCOM de NINA.

    Talon6 sigue aplicando sus propias protecciones (monturas aparcadas, sensores).
    """
    if request.headers.get("X-Periconet-Action") != "roof-control":
        raise HTTPException(403, "Petición de control no válida")
    if action not in ROOF_ACTIONS:
        raise HTTPException(404, "Orden desconocida")
    errors = []
    if roof_alpaca.roof_settings(CONFIG):
        try:
            logger.info("Techo: orden %s enviada directa a Talon6 (Alpaca)", action)
            await roof_alpaca.roof_command(CONFIG, action)
            for other in WORKERS.values():
                other.wake.set()
            ROOF_WAKE.set()
            return {"ok": True, "via": "Talon6 (Alpaca)"}
        except roof_alpaca.AlpacaError as exc:
            # Talon6 respondió y rechazó la orden (p. ej. monturas sin aparcar): no se reintenta.
            logger.warning("Techo: Talon6 rechazó %s: %s", action, exc)
            raise HTTPException(409, f"Talon6 rechazó la orden: {exc}") from None
        except Exception as exc:
            logger.warning("Techo: Alpaca falló (%s); se intenta vía NINA", exc)
            errors.append(f"Alpaca: {exc or type(exc).__name__}")
    candidates = [
        (key, worker)
        for key, worker in WORKERS.items()
        if STATE.get(key, {}).get("connected") and (STATE[key].get("dome") or {}).get("Connected")
    ]
    if not candidates:
        raise HTTPException(409, " · ".join(errors + ["Ningún NINA tiene el techo (cúpula) conectado."]))
    last_error = "No se pudo enviar la orden al techo."
    # STOP se intenta por todos los equipos hasta que uno lo acepte.
    for key, worker in candidates if action == "stop" else candidates[:1]:
        name = STATE[key]["name"]
        try:
            logger.info("Techo: orden %s enviada vía %s", action, name)
            await worker.client.dome_command(action)
        except httpx.TimeoutException:
            last_error = f"NINA ({name}) no respondió a tiempo. Comprueba el techo."
            continue
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
            except ValueError:
                body = None
            detail = body.get("Error") if isinstance(body, dict) else None
            last_error = f"NINA ({name}) rechazó la orden: {detail or exc.response.status_code}"
            continue
        except NinaError as exc:
            last_error = f"NINA ({name}) rechazó la orden: {exc}"
            continue
        except httpx.RequestError:
            last_error = f"No se pudo contactar con NINA ({name})."
            continue
        for other in WORKERS.values():
            other.wake.set()
        return {"ok": True, "via": name}
    raise HTTPException(502, " · ".join(errors + [last_error]))


async def clear_alerts(setup_id):
    state = STATE.get(setup_id)
    if state is None:
        return
    state["alerts"] = []
    await broadcast({"type": "alerts", "setup_id": setup_id, "alerts": []})


@app.post("/api/alerts/clear")
async def clear_all_alerts(request: Request, setup_id: str | None = None):
    """Desactiva el parpadeo de un equipo (?setup_id=) o de todos."""
    if request.headers.get("X-Periconet-Action") != "alerts-clear":
        raise HTTPException(403, "Petición no válida")
    for key in [setup_id] if setup_id else list(STATE):
        await clear_alerts(key)
    return {"ok": True}


@app.post("/api/shutdown")
async def shutdown(request: Request):
    """Cierra Periconet desde el dashboard; solo desde el propio equipo."""
    if (
        request.client is None
        or request.client.host not in ("127.0.0.1", "::1")
        or request.headers.get("X-Periconet-Action") != "shutdown"
    ):
        raise HTTPException(403, "Solo se puede cerrar Periconet desde el propio equipo.")
    if SHUTDOWN_HOOK is None:
        raise HTTPException(409, "Este Periconet no se puede cerrar desde aquí.")
    asyncio.get_running_loop().call_later(0.5, SHUTDOWN_HOOK)
    return {"ok": True}


@app.get("/api/roof")
async def get_roof():
    return ROOF


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
            index, item, generation = record["index"], record["item"], record["generation"]
            if IMAGES.get(setup_id) is not record or not worker.image_target_is_current(generation, item):
                return Response(status_code=409)
            if not record["hires"]:
                client = record["client"]
                if await client.get_history_item(index) != item:
                    return Response(status_code=409)
                if IMAGES.get(setup_id) is not record or not worker.image_target_is_current(generation, item):
                    return Response(status_code=409)
                data = await client.get_image_bytes(index, quality=65, resize=False)
                if not data:
                    return Response(status_code=404)
                if await client.get_history_item(index) != item:
                    return Response(status_code=409)
                if IMAGES.get(setup_id) is not record or not worker.image_target_is_current(generation, item):
                    return Response(status_code=409)
                record["hires"] = data
            return image_response(record["hires"], request)
    except Exception:
        logger.debug("Imagen HD %s no disponible", setup_id, exc_info=True)
        return Response(status_code=502)


@app.get("/api/extra-image/{index}")
async def get_extra_image(index: int, request: Request):
    images = CONFIG.get("extra_images", [])
    if (
        not 0 <= index < len(images)
        or not images[index].get("url")
        or images[index].get("kind", "image") != "image"
    ):
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


async def capture_camera(camera):
    try:
        return await download_snapshot(camera)
    finally:
        CAMERA_DOWNLOADS.pop(camera["id"], None)


@app.get("/api/cameras/{camera_id}/snapshot")
async def get_camera_snapshot(camera_id: str):
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    camera = next((item for item in CONFIG["cameras"] if item["id"] == camera_id), None)
    if camera is None:
        return Response(status_code=404, headers=headers)
    active = CAMERA_DOWNLOADS.get(camera_id)
    if active is None:
        task = asyncio.create_task(capture_camera(camera))
        CAMERA_DOWNLOADS[camera_id] = (camera, task)
    else:
        active_camera, task = active
        if active_camera != camera:
            return Response(status_code=409, headers=headers)
    data = await asyncio.shield(task)
    if camera not in CONFIG["cameras"]:
        return Response(status_code=409, headers=headers)
    if data is None:
        return Response(status_code=502, headers=headers)
    return Response(data, media_type="image/jpeg", headers=headers)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue = asyncio.Queue(maxsize=64)
    FRONTEND_CLIENTS[websocket] = queue
    # Alguien abre el dashboard: ponerse al día enseguida.
    for worker in WORKERS.values():
        worker.wake.set()
    ROOF_WAKE.set()
    queue.put_nowait(json.dumps({"type": "initial_state", "state": STATE, "revision": config_revision()}))
    queue.put_nowait(json.dumps({"type": "roof_update", "roof": ROOF}))

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
if not frontend_dir.exists():
    frontend_dir = RESOURCE_DIR / "frontend"
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
async def root():
    if CONFIG["setups"] or CONFIG["cameras"]:
        return FileResponse(frontend_dir / "index.html", headers={"Cache-Control": "no-cache"})
    return await setup_page()


@app.get("/setup")
async def setup_page():
    script_version = hashlib.sha256((frontend_dir / "setup.js").read_bytes()).hexdigest()[:16]
    html = (frontend_dir / "setup.html").read_text(encoding="utf-8")
    html = html.replace('src="/static/setup.js"', f'src="/static/setup.js?v={script_version}"', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, access_log=False)
