"""Credenciales de cámara en el almacén local de la cuenta de Windows."""

import ctypes
import json
import os
from ctypes import wintypes
from functools import lru_cache

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


@lru_cache(maxsize=1)
def credential_api():
    if os.name != "nt":
        raise OSError("El almacén de credenciales de cámara requiere Windows")
    api = ctypes.WinDLL("Advapi32", use_last_error=True)
    pointer = ctypes.POINTER(Credential)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _read(credential_id):
    api = credential_api()
    entry = ctypes.POINTER(Credential)()
    if not api.CredReadW(f"Periconet/camera/{credential_id}", CRED_TYPE_GENERIC, 0, ctypes.byref(entry)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(error)
    try:
        value = entry.contents
        return json.loads(ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize))
    finally:
        api.CredFree(entry)


def _write(credential_id, value):
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    entry = Credential(
        Type=CRED_TYPE_GENERIC,
        TargetName=f"Periconet/camera/{credential_id}",
        CredentialBlobSize=len(blob),
        CredentialBlob=blob,
        Persist=CRED_PERSIST_LOCAL_MACHINE,
    )
    if not credential_api().CredWriteW(ctypes.byref(entry), 0):
        raise ctypes.WinError(ctypes.get_last_error())


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
    if not credential_api().CredDeleteW(f"Periconet/camera/{credential_id}", CRED_TYPE_GENERIC, 0):
        error = ctypes.get_last_error()
        if error != ERROR_NOT_FOUND:
            raise ctypes.WinError(error)
