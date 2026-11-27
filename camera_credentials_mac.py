"""Credenciales de cámara en el llavero (Keychain) de macOS.

Sustituye al Administrador de credenciales de Windows de la versión original.
Usa la herramienta del sistema /usr/bin/security, así que no necesita
dependencias extra. Cada entrada se guarda como contraseña genérica con
servicio «Periconet/camera/<id>».
"""

import json
import subprocess
import sys

SECURITY = "/usr/bin/security"
ACCOUNT = "periconet"
ITEM_NOT_FOUND = 44  # errSecItemNotFound devuelto como código de salida por `security`


def _service(credential_id):
    return f"Periconet/camera/{credential_id}"


def _run(args):
    if sys.platform != "darwin":
        raise OSError("El almacén de credenciales de cámara requiere macOS")
    return subprocess.run([SECURITY, *args], capture_output=True, text=True, timeout=15)


def _read(credential_id):
    result = _run(["find-generic-password", "-a", ACCOUNT, "-s", _service(credential_id), "-w"])
    if result.returncode == ITEM_NOT_FOUND:
        return None
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"security devolvió {result.returncode}")
    return json.loads(result.stdout.rstrip("\n"))


def _write(credential_id, value):
    # ensure_ascii=True evita que `security` devuelva la contraseña en hexadecimal
    data = json.dumps(value, ensure_ascii=True)
    result = _run(
        [
            "add-generic-password",
            "-U",
            "-a", ACCOUNT,
            "-s", _service(credential_id),
            "-l", f"Periconet cámara {credential_id[:8]}",
            "-w", data,
        ]
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"security devolvió {result.returncode}")


def read(credential_id):
    fields = _read(credential_id)
    if fields is None:
        return None
    if not isinstance(fields, list) or len(fields) != 2 or not all(isinstance(field, str) for field in fields):
        raise ValueError("Entrada de credenciales de cámara inválida")
    return tuple(fields)


def write(credential_id, username, password):
    _write(credential_id, (username, password))


def read_url(credential_id):
    fields = _read(credential_id)
    if fields is None:
        return None
    if not isinstance(fields, dict) or not isinstance(fields.get("url"), str) or not fields["url"]:
        raise ValueError("Entrada de URL protegida inválida")
    return fields["url"]


def write_url(credential_id, url):
    _write(credential_id, {"url": url})


def delete(credential_id):
    result = _run(["delete-generic-password", "-a", ACCOUNT, "-s", _service(credential_id)])
    if result.returncode not in (0, ITEM_NOT_FOUND):
        raise OSError(result.stderr.strip() or f"security devolvió {result.returncode}")
