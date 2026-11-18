"""Configuración y capturas HTTP de las cámaras del observatorio."""

import asyncio
import uuid
from typing import Annotated

import httpx
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

import camera_credentials

CameraId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
SECRET_QUERY_FIELDS = {
    "user", "username", "login", "pass", "password", "passwd", "pwd", "token", "access_token",
    "auth", "authorization", "apikey", "api_key", "key", "secret", "session", "sessionid", "signature",
}
MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024


class CameraSettings(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2048)]
    refresh_seconds: int = Field(default=10, ge=10, le=86400)

    @field_validator("url")
    @classmethod
    def snapshot_url(cls, value):
        try:
            url = httpx.URL(value)
        except (httpx.InvalidURL, ValueError):
            raise ValueError("Introduce una URL HTTP o HTTPS válida") from None
        if url.scheme not in ("http", "https") or not url.host or url.fragment:
            raise ValueError("Introduce una URL HTTP o HTTPS sin fragmento")
        if url.userinfo or any(key.casefold() in SECRET_QUERY_FIELDS for key in url.params):
            raise ValueError("Introduce las credenciales en sus propios campos, fuera de la URL")
        return str(url)


class StoredCamera(CameraSettings):
    id: CameraId
    credential_id: CameraId | None = None


class CameraInput(CameraSettings):
    id: CameraId | None = None
    username: Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)] = ""
    password: SecretStr = Field(default=SecretStr(""), max_length=256)
    remove_credentials: bool = False


def check_unique(cameras):
    names = [camera.name.casefold() for camera in cameras]
    ids = [camera.id for camera in cameras if camera.id]
    if len(names) != len(set(names)):
        raise ValueError("Cada cámara debe tener un nombre diferente")
    if len(ids) != len(set(ids)):
        raise ValueError("Identificadores de cámara duplicados")


class CamerasPayload(BaseModel):
    revision: str = Field(min_length=1, max_length=128)
    cameras: list[CameraInput]

    @model_validator(mode="after")
    def unique(self):
        check_unique(self.cameras)
        return self


def url_origin(value):
    url = httpx.URL(value)
    return url.scheme, url.host, url.port


def prepare_cameras(inputs, existing):
    cameras = []
    pending_credentials = {}
    for item in inputs:
        if item.id and item.id not in existing:
            raise ValueError("La cámara ya no existe. Recarga la configuración.")
        previous = existing.get(item.id)
        credential_id = previous["credential_id"] if previous else None
        username = item.username
        password = item.password.get_secret_value()
        if item.remove_credentials:
            if username or password:
                raise ValueError(f"{item.name}: elige entre sustituir o eliminar las credenciales.")
            credential_id = None
        elif password or username:
            if password and not username and credential_id:
                saved = camera_credentials.read(credential_id)
                username = saved[0] if saved else ""
            if not (username and password):
                raise ValueError(f"{item.name}: introduce el usuario y la contraseña de la cámara.")
            credential_id = uuid.uuid4().hex
            pending_credentials[credential_id] = (username, password)
        elif credential_id and url_origin(item.url) != url_origin(previous["url"]):
            raise ValueError(f"{item.name}: introduce las credenciales para la nueva dirección.")
        cameras.append({
            "id": item.id or uuid.uuid4().hex,
            "name": item.name,
            "url": item.url,
            "refresh_seconds": item.refresh_seconds,
            "credential_id": credential_id,
        })
    return cameras, pending_credentials


class CameraAuth(httpx.Auth):
    """Prefiere Digest y acepta Basic cuando la cámara lo solicita."""

    def __init__(self, username, password):
        self.digest = httpx.DigestAuth(username, password)
        self.basic = httpx.BasicAuth(username, password)

    def auth_flow(self, request):
        digest_flow = self.digest.auth_flow(request)
        response = yield next(digest_flow)
        if response.status_code != 401:
            return
        challenges = response.headers.get_list("www-authenticate")
        if any(header.lower().startswith("digest ") for header in challenges):
            yield digest_flow.send(response)
        elif any(header.lower().startswith("basic ") for header in challenges):
            yield next(self.basic.auth_flow(request))


async def download_snapshot(camera):
    try:
        async with asyncio.timeout(8):
            auth = None
            if camera["credential_id"]:
                credentials = camera_credentials.read(camera["credential_id"])
                if credentials is None:
                    return None
                auth = CameraAuth(*credentials)
            async with (
                httpx.AsyncClient(timeout=8, trust_env=False) as client,
                client.stream("GET", camera["url"], auth=auth) as response,
            ):
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_SNAPSHOT_BYTES:
                        return None
                    data.extend(chunk)
                if not data.startswith(b"\xff\xd8\xff"):
                    return None
                return bytes(data)
    except (OSError, httpx.HTTPError, ValueError, KeyError):
        return None
