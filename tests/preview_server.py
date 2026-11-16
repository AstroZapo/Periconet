"""Panel con dos equipos simulados para revisar la interfaz, sin tocar config.json.

Ejecutar: python tests/preview_server.py  (http://127.0.0.1:8765)
"""
import asyncio
from contextlib import asynccontextmanager
import datetime as dt
import io
import math
import os
from pathlib import Path
import random
import sys
import tempfile

from PIL import Image, ImageDraw
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run():
    with tempfile.TemporaryDirectory(prefix="periconet-preview-") as directory:
        os.environ["PERICONET_CONFIG"] = str(Path(directory) / "config.json")
        import app
        from astronomy import night_at_connection
        from workers import build_state

        setups = [app.SetupInput(id=key, name=name, nina_host="localhost").model_dump()
                  for key, name in (("one", "Equipo simulado 1"), ("two", "Equipo simulado 2"))]
        app.CONFIG.update(setups=setups, observatory_name="Periconet · Prueba de interfaz")
        site = {"latitude": 40.4, "longitude": -3.7}
        for setup in setups:
            state = build_state(setup)
            state.update(connected=True, site=site, night=night_at_connection(site), pixel_scale=1.2,
                         guiding_status="guiando", weather={"Temperature": 12, "SkyTemperature": -22, "Humidity": 45, "WindSpeed": 2},
                         safety={"IsSafe": True}, mount={"Connected": True, "TimeToMeridianFlip": 1.5})
            app.STATE[setup["id"]] = state
            app.WORKERS[setup["id"]] = object()

        async def simulate():
            count = 0
            while True:
                for index, setup in enumerate(setups):
                    key, state = setup["id"], app.STATE[setup["id"]]
                    target_number = count // 30
                    target_key = f"{key}-{target_number}"
                    state["target"] = {"key": target_key, "name": "M31" if target_number % 2 == 0 else "M42",
                                       "ra": 0.71 if target_number % 2 == 0 else 5.59, "dec": 41.3 if target_number % 2 == 0 else -5.4,
                                       "ra_string": "00h 42m", "dec_string": "+41° 18′", "filter_summary": {"L": {"count": count // 5, "total_seconds": count * 12}}}
                    state["exposure"] = {"is_exposing": True, "camera_state": "Exposing", "duration": 0,
                                         "end_time": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60-count % 60)).isoformat()}
                    if count % 30 < 5:
                        state.update(latest_image_id=None, latest_image_url=None)
                        app.IMAGES.pop(key, None)
                    elif count % 5 == 0:
                        image = Image.new("RGB", (900, 600), (10 + index * 8, 10, 20))
                        draw = ImageDraw.Draw(image)
                        rng = random.Random(index)
                        for _ in range(120):
                            x, y = rng.randrange(900), rng.randrange(600)
                            draw.ellipse((x, y, x+2, y+2), fill="white")
                        draw.text((25, 25), f"SIMULACION - {state['target']['name']} - TOMA {count // 5}", fill="white")
                        buffer = io.BytesIO()
                        image.save(buffer, format="PNG")
                        token = f"{target_key}-{count}"
                        app.IMAGES[key] = {"token": token, "bytes": buffer.getvalue(), "hires": buffer.getvalue(), "lock": asyncio.Lock()}
                        state.update(latest_image_id=token, latest_image_url=f"/api/image/{key}?v={token}")
                    state["guide_seq"] += 1
                    state["guide_steps"].append({"seq": state["guide_seq"], "ra_distance_px": math.sin(count)*0.4,
                                                "dec_distance_px": math.cos(count)*0.3, "ra_distance_arcsec": math.sin(count)*0.48,
                                                "dec_distance_arcsec": math.cos(count)*0.36})
                    del state["guide_steps"][:-300]
                    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
                    state["hfr_history"] = [
                        {"HFR": 2.1, "TargetName": "M31 · Galaxia de Andrómeda · Mosaico exterior",
                         "Date": (now - dt.timedelta(minutes=5)).isoformat()},
                        {"HFR": 2.3, "TargetName": "M31 · Galaxia de Andrómeda · Mosaico exterior",
                         "Date": now.isoformat()},
                    ]
                    await app.broadcast({"type": "nina_update", "setup_id": key, "state": state})
                count += 1
                await asyncio.sleep(1)

        @asynccontextmanager
        async def preview_lifespan(application):
            task = asyncio.create_task(simulate())
            yield
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        app.app.router.lifespan_context = preview_lifespan
        uvicorn.run(app.app, host="127.0.0.1", port=8765, access_log=False)


if __name__ == "__main__":
    run()
