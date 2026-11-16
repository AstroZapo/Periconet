"""
Lanzador de Periconet. Punto de entrada para PyInstaller.
"""

import logging
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
import uvicorn
from PIL import Image, ImageDraw

HOST = "0.0.0.0"
server = None
server_thread = None
stop_requested = threading.Event()


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


BASE_DIR = get_base_dir()
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "launcher.log"

# Manejador con rotación automática a un máximo de 15 MB (1 backup)
MAX_LOG_BYTES = 15 * 1024 * 1024  # 15 MB
log_handler = RotatingFileHandler(
    LOG_PATH,
    maxBytes=MAX_LOG_BYTES,
    backupCount=1,
    encoding="utf-8",
)
log_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
    root_logger.addHandler(log_handler)

logger = logging.getLogger("launcher")


def load_port() -> int:
    """Lee el puerto de config.json en la carpeta local (por defecto 8000)."""
    try:
        import json
        cfg_path = BASE_DIR / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            port = int(cfg.get("port", 8000))
            if 1024 <= port <= 65535:
                return port
    except Exception:
        pass
    return 8000


PORT = load_port()
URL = f"http://localhost:{PORT}"


def log(message: str):
    logger.info(message)


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    return base / relative


def make_tray_icon() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((6, 6, 58, 58), fill=(201, 70, 58, 255))
    draw.ellipse((22, 22, 42, 42), fill=(9, 9, 11, 255))
    return img


def run_server():
    global server
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w")

        sys.path.insert(0, str(resource_path("backend")))
        sys.path.insert(0, str(BASE_DIR))
        import app as periconet_app  # noqa: E402

        log("Arrancando uvicorn...")
        config = uvicorn.Config(
            periconet_app.app,
            host=HOST,
            port=PORT,
            log_level="info",
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        if not stop_requested.is_set():
            server.run()
    except Exception:
        log("ERROR al arrancar el servidor:\n" + traceback.format_exc())


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def open_dashboard(icon=None, item=None):
    webbrowser.open(URL)


def quit_app(icon, item):
    stop_requested.set()
    if server is not None:
        server.should_exit = True
    icon.stop()


def main():
    global server_thread
    log("--- Iniciando Periconet ---")
    server_thread = threading.Thread(target=run_server)
    server_thread.start()

    if wait_for_port("127.0.0.1", PORT):
        log("Servidor listo, abriendo navegador.")
        open_dashboard()
    else:
        log("El servidor no respondió a tiempo; revisa el error de arriba si lo hay.")

    icon = pystray.Icon(
        "periconet",
        make_tray_icon(),
        "Periconet",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir dashboard", open_dashboard, default=True),
            pystray.MenuItem("Salir", quit_app),
        ),
    )
    try:
        icon.run()
    finally:
        stop_requested.set()
        if server is not None:
            server.should_exit = True
        server_thread.join(timeout=15)


if __name__ == "__main__":
    main()
