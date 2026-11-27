"""
Cliente para el servidor de eventos nativo de PHD2. Puerto por defecto 4400.
Documentación oficial:
https://github.com/OpenPHDGuiding/phd2/wiki/EventMonitoring
"""

import asyncio
import json
import logging
from collections import deque

logger = logging.getLogger("phd2_client")


class PHD2Client:
    def __init__(self, host: str, port: int = 4400):
        self.host = host
        self.port = port
        self._reader = None
        self._writer = None
        self._pending = deque(maxlen=300)
        self._request_id = 0

    async def connect(self):
        self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 5)

    async def request(self, method: str, params: dict | None = None, req_id: int = 1) -> dict:
        if not self._writer or not self._reader:
            raise RuntimeError("Llama a connect() antes de request()")
        self._request_id += 1
        req_id = self._request_id
        payload = {"method": method, "id": req_id}
        if params:
            payload["params"] = params
        self._writer.write((json.dumps(payload) + "\r\n").encode())
        await self._writer.drain()
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            line = await asyncio.wait_for(
                self._reader.readline(),
                max(0.001, deadline - asyncio.get_running_loop().time()),
            )
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
            if "Event" in data:
                self._pending.append(data)

    async def get_pixel_scale(self) -> float | None:
        try:
            resp = await self.request("get_pixel_scale")
            return resp.get("result")
        except Exception:
            return None

    async def close(self):
        if self._writer:
            self._writer.close()
            try:
                await asyncio.wait_for(self._writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                pass

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
            if self._pending:
                yield self._pending.popleft()
                continue
            try:
                line = await asyncio.wait_for(self._reader.readline(), 30)
            except asyncio.TimeoutError:
                await self.request("get_app_state")
                continue
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Línea no-JSON de PHD2 ignorada: %r", line)
