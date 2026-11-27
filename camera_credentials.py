"""Credenciales de cámara: Administrador de credenciales en Windows, llavero en macOS."""
import os

if os.name == "nt":
    from camera_credentials_win import delete, read, read_url, write, write_url  # noqa: F401

    STORE_NAME = "el Administrador de credenciales de Windows"
else:
    from camera_credentials_mac import delete, read, read_url, write, write_url  # noqa: F401

    STORE_NAME = "el llavero de macOS"
