"""
Cliente para el servidor de eventos nativo de PHD2. Puerto por defecto 4400.
Documentación oficial:
https://github.com/OpenPHDGuiding/phd2/wiki/EventMonitoring
"""

import asyncio
import json
import logging

logger = logging.getLogger("phd2_client")


class PHD2Client:
    def __init__(self, host: str, port: int = 4400):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self):
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def request(self, method: str, params: dict | None = None, req_id: int = 1) -> dict:
        if not self._writer or not self._reader:
            raise RuntimeError("Llama a connect() antes de request()")
        payload = {"method": method, "id": req_id}
        if params:
            payload["params"] = params
        self._writer.write((json.dumps(payload) + "\r\n").encode())
        await self._writer.drain()
        while True:
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("PHD2 cerró la conexión mientras esperábamos respuesta")
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("id") == req_id:
                return data

    async def get_pixel_scale(self) -> float | None:
        try:
            resp = await self.request("get_pixel_scale")
            return resp.get("result")
        except Exception:
            return None

    async def close(self):
        if self._writer:
            self._writer.close()

    async def events(self):
        """
        Generador async: produce un dict por cada evento JSON que llega de
        PHD2 -- incluye GuideStep, StarLost, GuidingStarted/Stopped, y
        también "Settling"/"SettleDone" (durante y al terminar un dither),
        que usamos para descartar puntos de guiado que no son error real,
        sino el propio salto intencional del dither.
        """
        if not self._reader:
            raise RuntimeError("Llama a connect() antes de iterar events()")
        while True:
            line = await self._reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Línea no-JSON de PHD2 ignorada: %r", line)
