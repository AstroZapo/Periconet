"""Rutas de Periconet (Windows y macOS).

- BASE_DIR: carpeta del programa (o de la .app empaquetada).
- RESOURCE_DIR: donde están los recursos empaquetados (frontend/).
- DATA_DIR: donde se guardan config.json y logs/.
  * Ejecutando desde la carpeta del código: la propia carpeta (como en Windows).
  * Empaquetado como .app de macOS: ~/Library/Application Support/Periconet
    (dentro de una .app no se debe escribir).
  * Empaquetado como .exe de Windows: la carpeta del .exe (como el original).
  * Se puede forzar con la variable de entorno PERICONET_HOME.
"""

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
RESOURCE_DIR = Path(sys._MEIPASS) if FROZEN and hasattr(sys, "_MEIPASS") else BASE_DIR

if os.environ.get("PERICONET_HOME"):
    DATA_DIR = Path(os.environ["PERICONET_HOME"]).expanduser()
elif FROZEN and sys.platform == "darwin":
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Periconet"
else:
    DATA_DIR = BASE_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
