"""
Lanzador de Periconet. Punto de entrada para PyInstaller.
"""

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import pystray
import uvicorn
from PIL import Image, ImageDraw

HOST = "0.0.0.0"

LOG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Periconet"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "launcher.log"


def load_port() -> int:
    """Lee el puerto de config.json (por defecto 8000)."""
    try:
        import json
        cfg = json.loads((LOG_DIR / "config.json").read_text(encoding="utf-8"))
        port = int(cfg.get("port", 8000))
        if 1024 <= port <= 65535:
            return port
    except Exception:
        pass
    return 8000


PORT = load_port()
URL = f"http://localhost:{PORT}"


def log(message: str):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative


def make_tray_icon() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((6, 6, 58, 58), fill=(201, 70, 58, 255))
    draw.ellipse((22, 22, 42, 42), fill=(9, 9, 11, 255))
    return img


def run_server():
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w")

        import logging
        logging.basicConfig(
            level=logging.INFO,
            filename=str(LOG_PATH),
            filemode="a",
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

        sys.path.insert(0, str(resource_path("backend")))
        import app as periconet_app  # noqa: E402

        log("Arrancando uvicorn...")
        uvicorn.run(
            periconet_app.app,
            host=HOST,
            port=PORT,
            log_level="info",
            log_config=None,
        )
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
    icon.stop()
    sys.exit(0)


def main():
    log("--- Iniciando Periconet ---")
    server_thread = threading.Thread(target=run_server, daemon=True)
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
    icon.run()


if __name__ == "__main__":
    main()
