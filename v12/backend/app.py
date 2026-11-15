import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx

from clients.nina_client import NinaClient
from clients.phd2_client import PHD2Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("periconet")

if getattr(sys, "frozen", False):
    config_dir = Path(os.environ.get("APPDATA", Path.home())) / "Periconet"
    config_dir.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH = config_dir / "config.json"
else:
    CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {"poll_interval_seconds": 5, "hfr_history_points": 60, "observatory_name": "", "setups": []}

if CONFIG_PATH.exists():
    CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
else:
    CONFIG = dict(DEFAULT_CONFIG)
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")


def build_state(setup: dict) -> dict:
    return {
        "name": setup["name"],
        "connected": False,
        "camera": None,
        "mount": None,
        "weather": None,
        "safety": None,
        "hfr_history": [],
        "latest_image_url": f"/api/image/{setup['id']}",
        "guide_steps": [],
        "guiding_status": "desconocido",
    }


STATE: dict[str, dict] = {s["id"]: build_state(s) for s in CONFIG.get("setups", [])}
IMAGE_CACHE: dict[str, bytes] = {}
FRONTEND_CLIENTS: set[WebSocket] = set()
GUIDE_HISTORY_MAX = 300


async def broadcast(message: dict):
    dead = []
    for ws in FRONTEND_CLIENTS:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        FRONTEND_CLIENTS.discard(ws)


async def poll_nina_loop(setup: dict):
    client = NinaClient(setup["nina_host"], setup["nina_port"])
    setup_id = setup["id"]
    interval = CONFIG.get("poll_interval_seconds", 5)

    while True:
        try:
            camera = await client.get_camera_info()
            mount = await client.get_mount_info()
            weather = await client.get_weather_info()
            safety = await client.get_safety_info()
            history = await client.get_image_history(CONFIG.get("hfr_history_points", 60))

            image_bytes = None
            if history:
                last_index = len(history) - 1
                image_bytes = await client.get_image_bytes(last_index)

            STATE[setup_id].update({
                "connected": True,
                "camera": camera,
                "mount": mount,
                "weather": weather,
                "safety": safety,
                "hfr_history": history,
                "latest_image_url": f"/api/image/{setup_id}",
            })
            if image_bytes:
                IMAGE_CACHE[setup_id] = image_bytes

            await broadcast({"type": "nina_update", "setup_id": setup_id, "state": STATE[setup_id]})

        except Exception as e:
            logger.warning("NINA %s no disponible (%s): %s", setup_id, setup["nina_host"], e)
            STATE[setup_id]["connected"] = False

        await asyncio.sleep(interval)


async def phd2_listener_loop(setup: dict):
    """Se conecta al socket de eventos de PHD2 y reenvía el guiado en vivo."""
    setup_id = setup["id"]

    while True:
        client = PHD2Client(setup["phd2_host"], setup["phd2_port"])
        try:
            await client.connect()
            logger.info("Conectado a PHD2 de %s en %s:%s", setup_id, setup["phd2_host"], setup["phd2_port"])

            pixel_scale = await client.get_pixel_scale()
            if pixel_scale:
                logger.info("Escala de píxel de %s: %.3f arcsec/px", setup_id, pixel_scale)
            else:
                logger.warning(
                    "No se pudo obtener la escala de píxel de %s; el guiado se mostrará en píxeles crudos",
                    setup_id,
                )
            scale = pixel_scale or 1.0

            # Mientras esté en "settling" (justo tras un dither), el salto
            # de posición NO es error de guiado real -- es el propio
            # movimiento intencional del dither. PHD2 excluye esos puntos
            # de su propia estadística de RMS; hacemos lo mismo, para no
            # inflar el RMS ni la gráfica con algo que no es un fallo.
            is_settling = False

            async for event in client.events():
                event_name = event.get("Event")

                if event_name == "Settling":
                    is_settling = True

                elif event_name == "SettleDone":
                    is_settling = False

                elif event_name == "GuideStep":
                    if is_settling:
                        continue  # descartamos: es el salto del dither, no un error de guiado

                    ra_px = event.get("RADistanceRaw") or 0
                    dec_px = event.get("DECDistanceRaw") or 0
                    point = {
                        "t": event.get("Timestamp"),
                        "ra_distance_px": ra_px,
                        "dec_distance_px": dec_px,
                        "ra_distance_arcsec": ra_px * scale,
                        "dec_distance_arcsec": dec_px * scale,
                        "hfd": event.get("HFD"),
                        "snr": event.get("SNR"),
                    }
                    steps = STATE[setup_id]["guide_steps"]
                    steps.append(point)
                    if len(steps) > GUIDE_HISTORY_MAX:
                        del steps[0]
                    STATE[setup_id]["guiding_status"] = "guiando"
                    await broadcast({"type": "guide_step", "setup_id": setup_id, "point": point})

                elif event_name == "StarLost":
                    STATE[setup_id]["guiding_status"] = "estrella perdida"
                    await broadcast({"type": "guiding_status", "setup_id": setup_id, "status": "estrella perdida"})

                elif event_name in ("GuidingStopped", "Paused"):
                    STATE[setup_id]["guiding_status"] = "detenido"
                    await broadcast({"type": "guiding_status", "setup_id": setup_id, "status": "detenido"})

                elif event_name == "GuidingStarted":
                    STATE[setup_id]["guiding_status"] = "guiando"
                    await broadcast({"type": "guiding_status", "setup_id": setup_id, "status": "guiando"})

        except (ConnectionRefusedError, OSError) as e:
            logger.warning("PHD2 %s no disponible (%s): %s", setup_id, setup["phd2_host"], e)
        except Exception:
            logger.exception("Error en el listener de PHD2 para %s", setup_id)
        finally:
            await client.close()

        await asyncio.sleep(5)


RUNNING_TASKS: list[asyncio.Task] = []


def start_polling_tasks():
    for setup in CONFIG.get("setups", []):
        RUNNING_TASKS.append(asyncio.create_task(poll_nina_loop(setup)))
        RUNNING_TASKS.append(asyncio.create_task(phd2_listener_loop(setup)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_polling_tasks()
    yield
    for t in RUNNING_TASKS:
        t.cancel()


app = FastAPI(title="Periconet", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class SetupInput(BaseModel):
    name: str
    nina_host: str
    nina_port: int = 1888
    phd2_host: str
    phd2_port: int = 4400
    guide_axis_range: float = 3.0
    guide_axis_unit: str = "arcsec"


class ExtraImageInput(BaseModel):
    title: str = ""
    url: str = ""
    refresh_seconds: int = 60


class SetupsPayload(BaseModel):
    observatory_name: str = ""
    setups: list[SetupInput]
    extra_images: list[ExtraImageInput] | None = None
    port: int | None = None


def slugify_id(name: str, index: int) -> str:
    slug = "".join(c.lower() if c.isalnum() else "" for c in name)
    return slug or f"setup{index}"


@app.post("/api/setup")
async def save_setup(payload: SetupsPayload):
    new_setups = []
    for i, s in enumerate(payload.setups):
        new_setups.append({
            "id": slugify_id(s.name, i),
            "name": s.name,
            "nina_host": s.nina_host,
            "nina_port": s.nina_port,
            "phd2_host": s.phd2_host,
            "phd2_port": s.phd2_port,
            "guide_axis_range": s.guide_axis_range,
            "guide_axis_unit": s.guide_axis_unit,
        })

    CONFIG["setups"] = new_setups
    CONFIG["observatory_name"] = payload.observatory_name
    if payload.port is not None and 1024 <= payload.port <= 65535:
        CONFIG["port"] = payload.port
    if payload.extra_images is not None:
        CONFIG["extra_images"] = [
            {"title": e.title.strip(), "url": e.url.strip(), "refresh_seconds": max(10, e.refresh_seconds)}
            for e in payload.extra_images
        ]
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "setups": new_setups}


@app.get("/api/config")
async def get_config():
    return {
        "setups": CONFIG.get("setups", []),
        "observatory_name": CONFIG.get("observatory_name", ""),
        "extra_images": CONFIG.get("extra_images", []),
        "port": CONFIG.get("port", 8000),
    }


@app.get("/api/extra-image/{index}")
async def get_extra_image(index: int):
    """Descarga la imagen externa (seeing, all-sky...) desde el servidor y la
    reenvía al navegador. Así funciona aunque la URL solo sea alcanzable desde
    este PC (p. ej. una VPN) y evita problemas de hotlinking/CORS."""
    images = CONFIG.get("extra_images", [])
    if index < 0 or index >= len(images) or not images[index].get("url"):
        return Response(status_code=404)
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(images[index]["url"], headers={"User-Agent": "Periconet"})
            r.raise_for_status()
        return Response(
            content=r.content,
            media_type=r.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        logger.warning("Imagen extra %s no disponible: %s", index, e)
        return Response(status_code=502)


@app.get("/api/setups")
async def get_setups():
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "guide_axis_range": s.get("guide_axis_range", 3.0),
            "guide_axis_unit": s.get("guide_axis_unit", "arcsec"),
        }
        for s in CONFIG.get("setups", [])
    ]


@app.get("/api/state/{setup_id}")
async def get_state(setup_id: str):
    return STATE.get(setup_id, {})


@app.get("/api/state")
async def get_all_state():
    return STATE


@app.get("/api/image/{setup_id}")
async def get_latest_image(setup_id: str):
    image_bytes = IMAGE_CACHE.get(setup_id)
    if not image_bytes:
        return Response(status_code=404)
    return Response(content=image_bytes, media_type="image/png")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    FRONTEND_CLIENTS.add(websocket)
    try:
        await websocket.send_json({"type": "initial_state", "state": STATE})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        FRONTEND_CLIENTS.discard(websocket)


frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
async def root():
    if not CONFIG.get("setups"):
        return FileResponse(frontend_dir / "setup.html")
    return FileResponse(frontend_dir / "index.html")


@app.get("/setup")
async def setup_page():
    return FileResponse(frontend_dir / "setup.html")
