"""
Lanzador de Periconet (Windows y macOS).

Arranca el servidor web en segundo plano, abre el dashboard en el navegador
y pone un icono en la bandeja de Windows / barra de menús de macOS con las opciones
«Abrir dashboard» y «Salir». Si pystray no está disponible, funciona igual
sin icono: se cierra con Ctrl+C en la ventana de Terminal.
"""

import json
import logging
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

from paths import DATA_DIR  # noqa: E402

HOST = "0.0.0.0"
server = None
server_thread = None
stop_requested = threading.Event()

LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "launcher.log"

MAX_LOG_BYTES = 15 * 1024 * 1024
log_handler = RotatingFileHandler(LOG_PATH, maxBytes=MAX_LOG_BYTES, backupCount=1, encoding="utf-8")
log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
    root_logger.addHandler(log_handler)

logger = logging.getLogger("launcher")


def load_port() -> int:
    try:
        cfg_path = DATA_DIR / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
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
    if sys.stdout is not None:  # con pythonw.exe (Windows) no hay consola
        print(message, flush=True)


def make_tray_icon():
    from PIL import Image, ImageDraw

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
        import app as periconet_app

        periconet_app.SHUTDOWN_HOOK = request_stop

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
    finally:
        stop_requested.set()


def request_stop():
    """Cierre pedido desde el dashboard («cerrar Periconet»)."""
    log("Cierre solicitado desde el dashboard.")
    stop_requested.set()
    if server is not None:
        server.should_exit = True


def show_alert(message):
    """Aviso visible aunque no haya consola."""
    try:
        if sys.platform == "darwin":
            import subprocess

            subprocess.run(
                ["osascript", "-e", f'display alert "Periconet" message "{message}" as warning'],
                timeout=120,
            )
        elif os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Periconet", 0x30)
    except Exception:
        pass


def existing_instance():
    """'ok' si ya hay un Periconet respondiendo, 'stuck' si el puerto está ocupado sin respuesta."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/setups", timeout=3) as response:
            if response.status == 200:
                return "ok"
    except Exception:
        pass
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return "stuck"
    except OSError:
        return None


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_requested.is_set():
            return False
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


def run_tray():
    """Icono en la barra de menús. Devuelve False si no se puede usar."""
    try:
        import pystray
    except Exception:
        log("pystray no disponible: Periconet sigue funcionando sin icono en la barra de menús.\n"
            + traceback.format_exc())
        return False
    log(f"Icono de bandeja: pystray backend {getattr(pystray.Icon, '__module__', '?')}")

    icon = pystray.Icon(
        "periconet",
        make_tray_icon(),
        "Periconet",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir dashboard", open_dashboard, default=True),
            pystray.MenuItem("Salir", quit_app),
        ),
    )

    # Solo icono en la barra de menús, sin icono de Python en el Dock.
    if sys.platform == "darwin":
        try:
            import AppKit

            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory
            )
        except Exception:
            pass

    def watch_server(icon):
        try:
            icon.visible = True
            log("Icono de bandeja mostrado.")
        except Exception:
            log("No se pudo mostrar el icono:\n" + traceback.format_exc())
        stop_requested.wait()
        icon.stop()

    # En macOS el icono tiene que ejecutarse en el hilo principal.
    try:
        icon.run(setup=watch_server)
    except Exception:
        log("El icono de bandeja falló; Periconet sigue funcionando sin él.\n" + traceback.format_exc())
        return False
    return True


def main():
    global server_thread
    log("--- Iniciando Periconet ---")
    log(f"Configuración y logs en: {DATA_DIR}")
    running = existing_instance()
    if running == "ok":
        log("Periconet ya estaba abierto: se abre el dashboard.")
        open_dashboard()
        return
    if running == "stuck":
        log(f"El puerto {PORT} está ocupado y no responde.")
        show_alert(
            f"Hay otro Periconet abierto que no responde (puerto {PORT}). "
            "Ciérralo y vuelve a abrir Periconet. En Mac: Terminal → pkill -f periconet.py · "
            "En Windows: Administrador de tareas → pythonw / Periconet → Finalizar tarea."
        )
        return
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    if wait_for_port("127.0.0.1", PORT):
        log(f"Servidor listo en {URL}, abriendo navegador.")
        open_dashboard()
    else:
        log("El servidor no respondió a tiempo; revisa el error de arriba si lo hay.")

    try:
        if not run_tray():
            log("Sin icono: se cierra con «cerrar Periconet» en el dashboard o con Ctrl+C.")
            while server_thread.is_alive() and not stop_requested.is_set():
                server_thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_requested.set()
        if server is not None:
            server.should_exit = True
        server_thread.join(timeout=15)


if __name__ == "__main__":
    main()
