"""
Cliente para el plugin "Advanced API" de N.I.N.A. (christian-photo).

Todos los endpoints de abajo están confirmados en vivo contra una
instalación real, incluida la imagen (GET /v2/api/image/{index}, que
devuelve un PNG codificado en base64 dentro de "Response").
"""

import base64

import httpx


class NinaClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.base_url = f"http://{host}:{port}/v2/api"
        self.timeout = timeout

    async def _get(self, path: str, params: dict | None = None):
        """
        Todas las respuestas de la Advanced API vienen envueltas como
        {"Response": ..., "Error": "", "StatusCode": 200, "Success": true}.
        Nos quedamos directamente con el contenido de "Response".
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}{path}", params=params)
            r.raise_for_status()
            data = r.json()
            return data.get("Response", data)

    async def get_camera_info(self):
        return await self._get("/equipment/camera/info")

    async def get_mount_info(self):
        return await self._get("/equipment/mount/info")

    async def get_weather_info(self):
        return await self._get("/equipment/weather/info")

    async def get_safety_info(self):
        return await self._get("/equipment/safetymonitor/info")

    async def get_image_history(self, count: int = 60):
        return await self._get("/image-history", params={"all": "true"})

    async def get_image_bytes(self, index: int, quality: int = 80) -> bytes | None:
        b64_data = await self._get(
            f"/image/{index}",
            params={"quality": quality, "resize": "true"},
        )
        if not b64_data or not isinstance(b64_data, str):
            return None
        try:
            return base64.b64decode(b64_data)
        except Exception:
            return None
