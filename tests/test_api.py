import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app
from workers import build_state


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app, "CONFIG", dict(app.DEFAULT_CONFIG))
    monkeypatch.setattr(app, "STATE", {})
    monkeypatch.setattr(app, "IMAGES", {})
    monkeypatch.setattr(app, "WORKERS", {})
    monkeypatch.setattr(app, "TASKS", {})
    monkeypatch.setattr(app, "FRONTEND_CLIENTS", {})
    monkeypatch.setattr(app, "CONFIG_LOCK", asyncio.Lock())
    monkeypatch.setattr(app, "start_setup", lambda setup: None)
    monkeypatch.setattr(app, "stop_setup", AsyncMock())
    return app


def payload(name="Uno", **kwargs):
    return {"setups": [{"name": name, "nina_host": "127.0.0.1", "phd2_host": "", **kwargs}]}


@pytest.mark.asyncio
async def test_config_rename_remove_and_connection_change(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        result = (await client.post("/api/setup", json=payload())).json()
        key = result["setups"][0]["id"]
        original = app.STATE[key]
        result = (await client.post("/api/setup", json=payload("Renombrado", id=key))).json()
        assert result["setups"][0]["id"] == key and app.STATE[key] is original
        assert original["name"] == "Renombrado"
        await client.post("/api/setup", json=payload("Renombrado", id=key, nina_port=1889))
        assert app.STATE[key] is not original
        await client.post("/api/setup", json={"setups": []})
        assert not app.STATE
        assert json.loads(app.CONFIG_PATH.read_text())["setups"] == []


@pytest.mark.asyncio
async def test_invalid_config_and_failed_write_leave_state_intact(isolated_app, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        for data in (payload(nina_port=0), payload(nina_host="http://localhost:1888"),
                     payload(guide_axis_range=-1), {"setups": payload()["setups"] * 2}):
            assert (await client.post("/api/setup", json=data)).status_code == 422
        assert app.CONFIG["setups"] == [] and not app.CONFIG_PATH.exists()
        app.save_config(app.CONFIG)
        before = app.CONFIG_PATH.read_bytes()
        def fail(*args):
            raise OSError("disk error")
        monkeypatch.setattr(app.os, "replace", fail)
        assert (await client.post("/api/setup", json=payload())).status_code == 500
        assert app.CONFIG_PATH.read_bytes() == before and app.CONFIG["setups"] == []
        assert not list(app.CONFIG_PATH.parent.glob(".config-*.tmp"))


@pytest.mark.asyncio
async def test_image_version_and_cache_cannot_reuse_old_target(isolated_app):
    app.IMAGES["one"] = {"token": "new", "bytes": b"\x89PNGfake", "hires": None}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        assert (await client.get("/api/image/one?v=old")).status_code == 404
        response = await client.get("/api/image/one?v=new")
        assert response.headers["content-type"] == "image/png"
        assert (await client.get("/api/image/one?v=new", headers={"If-None-Match": response.headers["etag"]})).status_code == 304
        app.IMAGES.clear()
        assert (await client.get("/api/image/one/hires?v=new")).status_code == 404


@pytest.mark.asyncio
async def test_hires_download_race_does_not_publish_previous_target(isolated_app):
    client = AsyncMock()
    item = {"Filename": "old.fits"}
    client.get_history_item.return_value = item
    client.get_sequence.return_value = []
    record = {"token": "old", "bytes": b"old", "hires": None, "index": 0, "item": item,
              "client": client, "lock": asyncio.Lock()}
    app.IMAGES["one"] = record
    worker = AsyncMock()
    worker.scheduler = None
    app.WORKERS["one"] = worker
    async def download(*args, **kwargs):
        app.IMAGES.pop("one")
        return b"old-hires"
    client.get_image_bytes.side_effect = download
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as http:
        assert (await http.get("/api/image/one/hires?v=old")).status_code == 409
    assert record["hires"] is None


@pytest.mark.asyncio
async def test_slow_browser_does_not_block_other_clients(isolated_app):
    slow, fast = object(), object()
    q1, q2 = asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=4)
    q1.put_nowait("pending")
    app.FRONTEND_CLIENTS.update({slow: q1, fast: q2})
    message = {"type": "test", "state": {"name": "original"}}
    await asyncio.wait_for(app.broadcast(message), 0.1)
    message["state"]["name"] = "changed"
    assert slow not in app.FRONTEND_CLIENTS and await q1.get() is None
    assert json.loads(await q2.get())["state"]["name"] == "original"


def test_websocket_snapshot_and_cleanup(isolated_app):
    app.STATE["one"] = build_state({"name": "Uno"})
    with TestClient(app.app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/dashboard.js").status_code == 200
        with client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "initial_state" and "one" in first["state"]
        assert not app.FRONTEND_CLIENTS
