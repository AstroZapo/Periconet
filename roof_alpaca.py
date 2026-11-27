"""Versión Mac: techo Talon6 R.O.R. directo por ASCOM Alpaca (sin pasar por NINA).

API Alpaca (Dome): https://ascom-standards.org/api/
  GET  /api/v1/dome/{n}/connected | shutterstatus | slewing
  PUT  /api/v1/dome/{n}/connected (Connected=True) | openshutter | closeshutter | abortslew
ShutterStatus: 0 abierto, 1 cerrado, 2 abriendo, 3 cerrando, 4 error.
"""

import asyncio
import itertools
import logging
import os

import httpx

logger = logging.getLogger("periconet.roof")

SHUTTER = {0: "ShutterOpen", 1: "ShutterClosed", 2: "ShutterOpening", 3: "ShutterClosing", 4: "ShutterError"}
ACTIONS = {"open": "openshutter", "close": "closeshutter", "stop": "abortslew"}
CLIENT_ID = 1000 + os.getpid() % 30000
transaction_ids = itertools.count(1)


class AlpacaError(Exception):
    pass


class RoofAlpaca:
    def __init__(self, url, device=0, host_header=""):
        self.base = f"{url.rstrip('/')}/api/v1/dome/{int(device)}"
        headers = {"Host": host_header} if host_header else None
        self.http = httpx.AsyncClient(timeout=8, headers=headers, trust_env=False)

    async def close(self):
        await self.http.aclose()

    def _ids(self):
        return {"ClientID": CLIENT_ID, "ClientTransactionID": next(transaction_ids)}

    @staticmethod
    def _value(response):
        response.raise_for_status()
        data = response.json()
        if data.get("ErrorNumber"):
            raise AlpacaError(data.get("ErrorMessage") or f"Error Alpaca {data['ErrorNumber']}")
        return data.get("Value")

    async def get(self, prop):
        return self._value(await self.http.get(f"{self.base}/{prop}", params=self._ids()))

    async def put(self, method, **fields):
        data = {**{k: str(v) for k, v in fields.items()}, **self._ids()}
        return self._value(await self.http.put(f"{self.base}/{method}", data=data))

    async def ensure_connected(self):
        if not await self.get("connected"):
            logger.info("Techo Alpaca: conectando el dispositivo")
            await self.put("connected", Connected=True)

    async def status(self):
        await self.ensure_connected()
        shutter = await self.get("shutterstatus")
        try:
            slewing = bool(await self.get("slewing"))
        except (AlpacaError, httpx.HTTPError, ValueError):
            slewing = False
        return {"Connected": True, "ShutterStatus": SHUTTER.get(shutter, "ShutterError"), "Slewing": slewing}

    async def command(self, action):
        await self.ensure_connected()
        await self.put(ACTIONS[action])


def roof_settings(config):
    url = (config.get("roof_alpaca_url") or "").strip()
    if not url:
        return None
    return (url, int(config.get("roof_alpaca_device") or 0), (config.get("roof_alpaca_host") or "").strip())


async def roof_loop(config, state, broadcast, interval=3, idle_interval=30,
                    has_viewers=lambda: True, wake=None):
    """Mantiene state actualizado; state = {"source": "alpaca", ...} o vacío si no hay Alpaca."""
    client, current, failed = None, None, False
    try:
        while True:
            settings = roof_settings(config)
            if settings != current:
                if client:
                    await client.close()
                client = RoofAlpaca(*settings) if settings else None
                current, failed = settings, False
            previous = dict(state)
            if client is None:
                state.clear()
            else:
                try:
                    state.clear()
                    state.update(await client.status(), source="alpaca", error=None)
                    if failed:
                        logger.info("Techo Alpaca recuperado")
                    failed = False
                except Exception as exc:  # red, Alpaca o JSON
                    if not failed:
                        logger.warning("Techo Alpaca no disponible: %s: %s", type(exc).__name__, exc)
                    failed = True
                    state.clear()
                    state.update(source="alpaca", Connected=False, error=str(exc) or type(exc).__name__)
            if state != previous:
                await broadcast({"type": "roof_update", "roof": dict(state)})
            wait = interval if has_viewers() else idle_interval
            if wake is None:
                await asyncio.sleep(wait)
            else:
                try:
                    await asyncio.wait_for(wake.wait(), wait)
                except asyncio.TimeoutError:
                    pass
                wake.clear()
    finally:
        if client:
            await client.close()


async def roof_command(config, action):
    settings = roof_settings(config)
    if not settings:
        raise AlpacaError("Alpaca no configurado")
    client = RoofAlpaca(*settings)
    try:
        await client.command(action)
    finally:
        await client.close()
