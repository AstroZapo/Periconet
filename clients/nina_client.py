"""Cliente para Advanced API v2 de N.I.N.A."""
import logging
import math
from itertools import count
from time import perf_counter

import httpx

logger = logging.getLogger("periconet.nina")
request_ids = count(1)


class NinaError(Exception):
    pass


def clean_numbers(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: clean_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_numbers(item) for item in value]
    return value


class NinaClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self.base_url = f"http://{host}:{port}/v2/api"
        self.http = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self.http.aclose()

    async def _request(self, path, params=None, headers=None):
        url = self.base_url + path
        if not logger.isEnabledFor(logging.DEBUG):
            return await self.http.get(url, params=params, headers=headers)
        request_id = next(request_ids)
        started = perf_counter()
        logger.debug("NINA HTTP #%d GET %s parámetros=%s", request_id, url, params)
        try:
            response = await self.http.get(url, params=params, headers=headers)
        except Exception as exc:
            logger.debug("NINA HTTP #%d GET %s falló en %.1f ms (%s: %s)", request_id, url,
                         (perf_counter() - started) * 1000, type(exc).__name__, str(exc) or "sin mensaje")
            raise
        logger.debug("NINA HTTP #%d GET %s respondió HTTP %d en %.1f ms (%d bytes, %s)", request_id, url,
                     response.status_code, (perf_counter() - started) * 1000,
                     len(response.content), response.headers.get("content-type", "sin Content-Type"))
        return response

    async def _get(self, path, params=None):
        response = await self._request(path, params)
        return self._read_json(response)

    @staticmethod
    def _read_json(response, *, require_success=False):
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise NinaError("Respuesta JSON no válida")
        if isinstance(data, dict) and data.get("Success") is False:
            raise NinaError(str(data.get("Error") or "Advanced API rechazó la petición"))
        response.raise_for_status()
        if require_success and (not isinstance(data, dict) or data.get("Success") is not True):
            raise NinaError("Advanced API no confirmó la orden")
        return clean_numbers(data.get("Response", data) if isinstance(data, dict) else data)

    async def get_camera_info(self):
        return await self._get("/equipment/camera/info")

    async def get_mount_info(self):
        return await self._get("/equipment/mount/info")

    async def get_weather_info(self):
        return await self._get("/equipment/weather/info")

    async def get_safety_info(self):
        return await self._get("/equipment/safetymonitor/info")

    async def get_profile(self):
        return await self._get("/profile/show", {"active": "true"})

    async def get_advanced_sequence(self):
        return await self._get("/sequence/json")

    async def get_event_history(self):
        return await self._get("/event-history")

    async def skip_sequence_to_end(self):
        response = await self._request(
            "/sequence/skip",
            params={"type": "ToEnd"},
            headers={"Cache-Control": "no-cache, no-store"},
        )
        if response.status_code == 409:
            response.raise_for_status()
        self._read_json(response, require_success=True)

    async def get_history(self, **params):
        try:
            return await self._get("/image-history", params)
        except NinaError as exc:
            if "no images" in str(exc).lower():
                return [] if params.get("all") else None
            raise

    async def get_image_history(self):
        result = await self.get_history(all="true")
        return result if isinstance(result, list) else []

    async def get_history_item(self, index):
        result = await self.get_history(index=index)
        return result[0] if isinstance(result, list) and result else result

    async def get_image_count(self):
        return int(await self.get_history(count="true") or 0)

    async def get_image_bytes(self, index, quality=80, resize=True):
        response = await self._request(f"/image/{index}", params={
            "quality": quality, "resize": "true" if resize else "false", "stream": "true"})
        if response.headers.get("content-type", "").startswith("image/"):
            response.raise_for_status()
            return response.content or None
        self._read_json(response)
        raise NinaError("Advanced API no devolvió una imagen")
