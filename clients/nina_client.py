"""Cliente de solo lectura para Advanced API v2 de N.I.N.A."""
import math
import httpx


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

    async def _get(self, path, params=None):
        response = await self.http.get(self.base_url + path, params=params)
        return self._read_json(response)

    @staticmethod
    def _read_json(response):
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise NinaError("Respuesta JSON no válida")
        if isinstance(data, dict) and data.get("Success") is False:
            raise NinaError(str(data.get("Error") or "Advanced API rechazó la consulta"))
        response.raise_for_status()
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

    async def get_sequence(self):
        return await self._get("/sequence/state")

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
        response = await self.http.get(f"{self.base_url}/image/{index}", params={
            "quality": quality, "resize": "true" if resize else "false", "stream": "true"})
        if response.headers.get("content-type", "").startswith("image/"):
            response.raise_for_status()
            return response.content or None
        self._read_json(response)
        raise NinaError("Advanced API no devolvió una imagen")
