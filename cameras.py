"""Configuración y capturas HTTP de las cámaras del observatorio."""

import asyncio
import logging
import ssl
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

logger = logging.getLogger("periconet.cameras")

CameraId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
SECRET_QUERY_FIELDS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "key",
    "login", "pass", "passwd", "password", "pwd", "secret", "session",
    "sessionid", "signature", "token", "user", "username",
}
MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024
MAX_CAMERA_URL_LENGTH = 2048


def require_url_without_credentials(value):
    url = httpx.URL(value)
    if url.userinfo or any(key.casefold() in SECRET_QUERY_FIELDS for key in url.params):
        raise ValueError("Marca «La URL contiene credenciales» o introdúcelas en sus propios campos.")


class CameraSettings(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_CAMERA_URL_LENGTH)] | None = Field(
        default=None, repr=False
    )
    refresh_seconds: int = Field(default=10, ge=10, le=86400)

    @field_validator("url")
    @classmethod
    def snapshot_url(cls, value):
        if not value:
            return None
        try:
            url = httpx.URL(value)
        except (httpx.InvalidURL, ValueError):
            raise ValueError("Introduce una URL HTTP o HTTPS válida") from None
        if url.scheme not in ("http", "https") or not url.host or url.fragment:
            raise ValueError("Introduce una URL HTTP o HTTPS sin fragmento")
        return str(url)


class StoredCamera(CameraSettings):
    id: CameraId
    credential_id: CameraId | None = None

    @model_validator(mode="after")
    def stored_access(self):
        if self.url is not None:
            require_url_without_credentials(self.url)
        elif not self.credential_id:
            raise ValueError("La cámara necesita una URL de captura o una referencia a su URL protegida.")
        return self


class CameraInput(CameraSettings):
    id: CameraId | None = None
    username: Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)] = ""
    password: SecretStr = Field(default=SecretStr(""), max_length=256)
    remove_credentials: bool = False
    protected_url: bool = False

    @model_validator(mode="after")
    def validate_access(self):
        if self.protected_url:
            if self.username or self.password.get_secret_value():
                raise ValueError("Deja vacíos Usuario y Contraseña al usar una URL protegida.")
            if self.url and len(self.url.encode("utf-8")) > MAX_CAMERA_URL_LENGTH:
                raise ValueError("La URL protegida supera el tamaño permitido.")
            return self
        if self.url:
            require_url_without_credentials(self.url)
            return self
        raise ValueError("Introduce la URL de captura de la cámara.")


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


def camera_access(item, previous):
    was_protected = previous is not None and previous["url"] is None
    if item.protected_url:
        if item.url:
            return None, uuid.uuid4().hex, item.url
        if was_protected:
            return None, previous["credential_id"], None
        raise ValueError(f"{item.name}: introduce la URL protegida de la cámara.")

    credential_id = previous["credential_id"] if previous and not was_protected else None
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
        if not username or not password:
            raise ValueError(f"{item.name}: introduce el usuario y la contraseña de la cámara.")
        return item.url, uuid.uuid4().hex, (username, password)
    elif credential_id and url_origin(item.url) != url_origin(previous["url"]):
        raise ValueError(f"{item.name}: introduce las credenciales para la nueva dirección.")
    return item.url, credential_id, None


def prepare_cameras(inputs, existing):
    cameras = []
    pending_credentials = {}
    for item in inputs:
        if item.id and item.id not in existing:
            raise ValueError("La cámara ya no existe. Recarga la configuración.")
        url, credential_id, new_secret = camera_access(item, existing.get(item.id))
        if new_secret is not None:
            pending_credentials[credential_id] = new_secret
        cameras.append(
            {
                "id": item.id or uuid.uuid4().hex,
                "name": item.name,
                "url": url,
                "refresh_seconds": item.refresh_seconds,
                "credential_id": credential_id,
            }
        )
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


def _is_ssl_error(exc):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _legacy_ssl_context():
    """Contexto TLS permisivo para cámaras locales con certificados propios o TLS antiguo."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
    try:
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    except (AttributeError, ssl.SSLError, ValueError):
        pass
    for ciphers in ("DEFAULT:@SECLEVEL=0", "DEFAULT:@SECLEVEL=1", "DEFAULT"):
        try:
            context.set_ciphers(ciphers)
            break
        except ssl.SSLError:
            continue
    return context


async def _fetch_snapshot(url, auth, verify=True):
    async with (
        httpx.AsyncClient(timeout=8, trust_env=False, verify=verify) as client,
        client.stream("GET", url, auth=auth) as response,
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


async def download_snapshot(camera):
    try:
        async with asyncio.timeout(8):
            url = camera["url"]
            credential_id = camera["credential_id"]
            auth = None
            if url is None:
                url = camera_credentials.read_url(credential_id)
                if url is None:
                    return None
            elif credential_id:
                credentials = camera_credentials.read(credential_id)
                if credentials is None:
                    return None
                auth = CameraAuth(*credentials)
            try:
                return await _fetch_snapshot(url, auth)
            except (OSError, httpx.HTTPError) as exc:
                if not _is_ssl_error(exc):
                    raise
                logger.debug("Reintentando captura sin verificación SSL estricta (%s): %s", camera.get("name", url), exc)
                return await _fetch_snapshot(url, auth, verify=_legacy_ssl_context())
    except (OSError, httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError):
        return None
