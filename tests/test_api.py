import asyncio
import hashlib
import json
import re
from base64 import b64decode
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app
import cameras
from clients.nina_client import NinaClient
from workers import build_state


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app, "CONFIG", dict(app.DEFAULT_CONFIG))
    monkeypatch.setattr(app, "STATE", {})
    monkeypatch.setattr(app, "IMAGES", {})
    monkeypatch.setattr(app, "WORKERS", {})
    monkeypatch.setattr(app, "TASKS", {})
    monkeypatch.setattr(app, "CAMERA_DOWNLOADS", {})
    monkeypatch.setattr(app, "FRONTEND_CLIENTS", {})
    monkeypatch.setattr(app, "CONFIG_LOCK", asyncio.Lock())
    monkeypatch.setattr(app, "start_setup", lambda setup: None)
    monkeypatch.setattr(app, "stop_setup", AsyncMock())
    return app


def payload(name="Uno", **kwargs):
    return {"setups": [{"name": name, "nina_host": "127.0.0.1", "phd2_host": "", **kwargs}]}


@pytest.mark.asyncio
async def test_sequence_to_end_only_sends_one_order_to_the_selected_nina(isolated_app):
    requests = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def nina_api(request):
        requests.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json={"Success": True, "Response": "Skipped"})

    nina = NinaClient("nina-two", 1889)
    await nina.close()
    nina.http = httpx.AsyncClient(transport=httpx.MockTransport(nina_api))
    other = AsyncMock()
    app.WORKERS["one"] = other
    worker = SimpleNamespace(client=nina, sequence_end_lock=asyncio.Lock(), wake=asyncio.Event())
    app.WORKERS["two"] = worker
    url = "/api/setups/two/sequence/to-end"
    headers = {"X-Periconet-Action": "sequence-to-end"}
    async with nina.http, httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        assert (await client.get(url)).status_code == 405
        assert (await client.post(url)).status_code == 403
        assert (await client.post("/api/setups/missing/sequence/to-end", headers=headers)).status_code == 404
        order = asyncio.create_task(client.post(url, headers=headers))
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert (await client.post(url, headers=headers)).status_code == 409
        finally:
            release.set()
            response = await order
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert worker.wake.is_set()
    other.client.skip_sequence_to_end.assert_not_called()
    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", "http://nina-two:1889/v2/api/sequence/skip?type=ToEnd")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("nina_status", "body", "expected", "detail"), [
    (409, {"Success": False, "Error": "Sequence not running"}, 409,
     "NINA no tiene una secuencia avanzada en ejecución."),
    (409, {"Success": False, "Error": "Sequence is not initialized"}, 409,
     "El secuenciador avanzado de NINA no está inicializado."),
    (409, {"Success": False, "Error": "Otro motivo de NINA"}, 409, "Otro motivo de NINA"),
    (409, None, 409, "NINA rechazó el salto al bloque final de la secuencia."),
    (200, {"Success": False, "Error": "Skip rejected"}, 502, "Skip rejected"),
    (200, {}, 502, "Advanced API no confirmó la orden"),
])
async def test_sequence_to_end_reports_failure_when_nina_does_not_confirm(
    isolated_app, nina_status, body, expected, detail,
):
    nina = NinaClient("nina", 1888)
    await nina.close()
    transport = httpx.MockTransport(lambda request: httpx.Response(nina_status, json=body))
    nina.http = httpx.AsyncClient(transport=transport)
    worker = SimpleNamespace(client=nina, sequence_end_lock=asyncio.Lock(), invalidate_sequence_status=AsyncMock())
    app.WORKERS["one"] = worker
    async with nina.http, httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/api/setups/one/sequence/to-end", headers={"X-Periconet-Action": "sequence-to-end"})
    assert response.status_code == expected and response.json()["detail"] == detail
    assert worker.invalidate_sequence_status.await_count == (nina_status == 409)


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
    record = {"token": "old", "bytes": b"old", "hires": None, "index": 0, "item": item,
              "client": client, "lock": asyncio.Lock()}
    app.IMAGES["one"] = record
    worker = AsyncMock()
    worker.refresh_target.return_value = True
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


@pytest.fixture
def camera_store(monkeypatch):
    credentials = {}
    monkeypatch.setattr(app.camera_credentials, "read", credentials.get)
    monkeypatch.setattr(app.camera_credentials, "write", lambda key, user, password: credentials.update({key: (user, password)}))
    monkeypatch.setattr(app.camera_credentials, "read_url", credentials.get)
    monkeypatch.setattr(app.camera_credentials, "write_url", lambda key, url: credentials.update({key: url}))
    monkeypatch.setattr(app.camera_credentials, "delete", lambda key: credentials.pop(key, None))
    return credentials


@pytest.mark.asyncio
async def test_setup_serves_the_current_script_and_saves_cameras_with_other_settings(isolated_app, camera_store):
    transport = httpx.ASGITransport(app=app.app)
    headers = {"X-Periconet-Action": "camera-setup"}
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        for path in ("/", "/setup"):
            page = await client.get(path)
            script_url = re.search(r'src="(/static/setup\.js\?v=[a-f0-9]+)"', page.text)[1]
            script = await client.get(script_url)
            assert script_url.endswith(hashlib.sha256(script.content).hexdigest()[:16])
            assert page.text.index('id="add-camera"') < page.text.index('id="save-settings"')

        revision = (await client.get("/api/setup/cameras", headers=headers)).json()["revision"]
        settings = payload("Artec 250E") | {
            "observatory_name": "Menelmacar",
            "camera_settings": {"revision": revision, "cameras": [{
                "name": "Monturas", "url": "http://camera/picture",
                "username": "visor", "password": "test-secret",
            }]},
        }
        assert (await client.post("/api/setup", json=settings)).status_code == 403
        plain = await client.post("/api/setup", content=json.dumps(payload()), headers={"Content-Type": "text/plain"})
        assert plain.status_code == 422
        invalid = dict(settings, setups=None)
        rejected = await client.post("/api/setup", headers=headers, json=invalid)
        assert rejected.status_code == 422 and "test-secret" not in rejected.text
        assert not app.CONFIG_PATH.exists() and not camera_store

        saved = await client.post("/api/setup", headers=headers, json=settings)
        assert saved.status_code == 200 and "test-secret" not in saved.text
        assert saved.json()["camera_settings"]["cameras"][0]["has_credentials"]
        assert list(camera_store.values()) == [("visor", "test-secret")]
        config = json.loads(app.CONFIG_PATH.read_bytes())
        assert config["setups"][0]["name"] == "Artec 250E"
        assert config["cameras"][0]["name"] == "Monturas"
        assert "test-secret" not in app.CONFIG_PATH.read_text(encoding="utf-8")

        unrelated = await client.post("/api/setup", json={"setups": saved.json()["setups"]})
        assert unrelated.status_code == 200
        assert app.CONFIG["cameras"] == config["cameras"]


@pytest.mark.asyncio
async def test_camera_configuration_keeps_credentials_private_and_rotates_atomically(isolated_app, camera_store, monkeypatch):
    transport = httpx.ASGITransport(app=app.app)
    endpoint = "/api/setup/cameras"
    headers = {"X-Periconet-Action": "camera-setup"}
    camera = {"name": "Monturas", "url": "http://camera/ISAPI/Streaming/channels/1/picture",
              "username": "viewer-test", "password": "fake-password", "refresh_seconds": 12}
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        revision = (await client.get(endpoint, headers=headers)).json()["revision"]
        assert (await client.get(endpoint)).status_code == 403
        assert (await client.get(endpoint, headers=headers | {"Origin": "http://otro-sitio"})).status_code == 403
        assert (await client.get(endpoint, headers=headers | {"X-Forwarded-Host": "localhost"})).status_code == 403
        assert (await client.get("http://periconet.lan" + endpoint, headers=headers)).status_code == 403
        async def save(*items):
            return await client.post(endpoint, headers=headers, json={"revision": revision, "cameras": items})

        invalid = await save(dict(camera, url="http://viewer-test:fake-password@camera/snapshot"))
        assert invalid.status_code == 422 and "fake-password" not in invalid.text
        assert (await save(camera, camera)).status_code == 422
        created = await save(camera)
        assert created.status_code == 200
        info = created.json()["cameras"][0]
        assert info["has_credentials"] and "fake-password" not in created.text
        assert "viewer-test" not in created.text
        assert (await save(camera)).status_code == 409
        on_disk = app.CONFIG_PATH.read_text(encoding="utf-8")
        assert "fake-password" not in on_disk and "viewer-test" not in on_disk
        assert list(camera_store.values()) == [("viewer-test", "fake-password")]
        assert (await client.get("/api/config")).json()["cameras"] == [
            {"id": info["id"], "name": "Monturas", "refresh_seconds": 12}
        ]
        assert "observatory-title" in (await client.get("/")).text

        revision = created.json()["revision"]
        edited = {key: info[key] for key in ("id", "name", "url", "refresh_seconds")}
        assert (await save(dict(edited, url="http://different-camera/snapshot"))).status_code == 422
        retained = await save(dict(edited, name="Techo"))
        assert retained.status_code == 200 and list(camera_store.values()) == [("viewer-test", "fake-password")]
        revision = retained.json()["revision"]
        edited["name"] = "Techo"

        def fail_save(*args):
            raise OSError("disk error")

        with monkeypatch.context() as failed_write:
            failed_write.setattr(app.os, "replace", fail_save)
            response = await save(dict(edited, password="next-password"))
            assert response.status_code == 500 and "next-password" not in response.text
        assert list(camera_store.values()) == [("viewer-test", "fake-password")]
        rotated = await save(dict(edited, password="next-password"))
        assert rotated.status_code == 200
        assert list(camera_store.values()) == [("viewer-test", "next-password")]
        revision = rotated.json()["revision"]
        deleted = await save()
        assert deleted.status_code == 200 and deleted.json()["cameras"] == [] and not camera_store

    remote = httpx.ASGITransport(app=app.app, client=("192.0.2.20", 8000))
    async with httpx.AsyncClient(transport=remote, base_url="http://localhost:8000") as client:
        assert (await client.get(endpoint, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_protected_camera_url_is_write_only_and_can_be_rotated(isolated_app, camera_store, monkeypatch):
    transport = httpx.ASGITransport(app=app.app)
    headers = {"X-Periconet-Action": "camera-setup"}
    endpoint = "/api/setup/cameras"
    url = "http://camera/snapshot?cmd=Snap&user=visor&password=initial-test-secret%2B"
    replacement_url = "http://camera/snapshot?token=replacement-test-secret"
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        revision = (await client.get(endpoint, headers=headers)).json()["revision"]

        async def save(camera):
            return await client.post(endpoint, headers=headers, json={"revision": revision, "cameras": [camera]})

        draft = {"name": "Monturas", "url": url}
        incompatible = await save(draft)
        assert incompatible.status_code == 422 and "initial-test-secret" not in incompatible.text
        mixed = await save(dict(draft, protected_url=True, username="visor", password="separate-test-secret"))
        assert mixed.status_code == 422 and "separate-test-secret" not in mixed.text
        assert (await save({"name": "Monturas", "protected_url": True})).status_code == 422
        assert not camera_store and not app.CONFIG_PATH.exists()

        created = await save(dict(draft, protected_url=True))
        assert created.status_code == 200 and "initial-test-secret" not in created.text
        revision = created.json()["revision"]
        camera = created.json()["cameras"][0]
        assert camera["protected_url"] and camera["has_credentials"] and camera["url"] is None
        assert (await client.get(endpoint, headers=headers)).json()["cameras"] == [camera]
        public = (await client.get("/api/config")).json()["cameras"]
        assert public == [{"id": camera["id"], "name": "Monturas", "refresh_seconds": 10}]
        original_id = app.CONFIG["cameras"][0]["credential_id"]
        assert camera_store == {original_id: url}
        config = json.loads(app.CONFIG_PATH.read_bytes())
        assert cameras.StoredCamera.model_validate(config["cameras"][0]).url is None
        assert "initial-test-secret" not in app.CONFIG_PATH.read_text(encoding="utf-8")

        edit = {"id": camera["id"], "name": "Techo", "url": "", "protected_url": True}
        retained = await save(edit)
        assert retained.status_code == 200 and camera_store == {original_id: url}
        revision = retained.json()["revision"]

        def fail_save(*args):
            raise OSError("disk error")

        with monkeypatch.context() as failed_write:
            failed_write.setattr(app.os, "replace", fail_save)
            failed = await save(dict(edit, url=replacement_url))
            assert failed.status_code == 500 and "replacement-test-secret" not in failed.text
        assert camera_store == {original_id: url}
        rotated = await save(dict(edit, url=replacement_url))
        assert rotated.status_code == 200
        assert original_id not in camera_store and list(camera_store.values()) == [replacement_url]
        assert "replacement-test-secret" not in app.CONFIG_PATH.read_text(encoding="utf-8")

        revision = rotated.json()["revision"]
        public_url = "http://camera/snapshot?width=640"
        unprotected = await save(dict(edit, url=public_url, protected_url=False))
        assert unprotected.status_code == 200 and not camera_store
        assert unprotected.json()["cameras"][0]["url"] == public_url


@pytest.mark.asyncio
async def test_camera_snapshot_shares_one_download_and_is_independent_of_other_cameras(isolated_app, monkeypatch):
    first = {"id": "1" * 32, "name": "Lenta", "url": "http://camera-one/snapshot", "credential_id": None}
    second = {"id": "2" * 32, "name": "Disponible", "url": "http://camera-two/snapshot", "credential_id": None}
    app.CONFIG["cameras"] = [first, second]
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def download(camera):
        calls.append(camera["id"])
        if camera is first:
            started.set()
            await release.wait()
            return None
        return b"\xff\xd8\xfftest"

    monkeypatch.setattr(app, "download_snapshot", download)
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://periconet.lan") as client:
        first_url = f"/api/cameras/{first['id']}/snapshot"
        pending = asyncio.create_task(client.get(first_url))
        await asyncio.wait_for(started.wait(), 1)
        second_viewer = asyncio.create_task(client.get(first_url))
        try:
            ready = await asyncio.wait_for(client.get(f"/api/cameras/{second['id']}/snapshot"), 1)
            assert ready.status_code == 200 and ready.headers["cache-control"] == "no-store"
            assert ready.headers["content-type"] == "image/jpeg"
            assert calls == [first["id"], second["id"]]
        finally:
            release.set()
            results = await asyncio.gather(pending, second_viewer)
        assert [response.status_code for response in results] == [502, 502]
        assert not app.CAMERA_DOWNLOADS


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["Basic", "Digest"])
async def test_camera_download_responds_to_auth_challenge_and_requires_a_jpeg(camera_store, monkeypatch, scheme):
    reference = "1" * 32
    camera_store[reference] = ("visor", "clave-prueba")
    challenge = 'Basic realm="camera"'
    if scheme == "Digest":
        challenge = 'Digest realm="camera", nonce="test", qop="auth", algorithm=MD5'
    response_bytes = b"\xff\xd8\xfftest"
    calls = []

    def camera_api(request):
        authorization = request.headers.get("Authorization")
        calls.append(authorization)
        if not authorization:
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        return httpx.Response(200, content=response_bytes)

    client = httpx.AsyncClient
    transport = httpx.MockTransport(camera_api)
    monkeypatch.setattr(cameras.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))
    camera = {"url": "http://camera/snapshot", "credential_id": reference}
    assert await cameras.download_snapshot(camera) == response_bytes
    assert len(calls) == 2 and calls[0] is None and calls[1].startswith(scheme + " ")
    if scheme == "Basic":
        assert b64decode(calls[1].split(" ", 1)[1]) == b"visor:clave-prueba"
    else:
        assert 'username="visor"' in calls[1] and "clave-prueba" not in calls[1]
    response_bytes = b"<html>Inicio de sesion</html>"
    assert await cameras.download_snapshot(camera) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "protected", "expected_userinfo"), [
    ("http://camera/snapshot?width=640", False, False),
    ("http://camera/snapshot?cmd=Snap&user=demo&password=x%2By+z&channel=0", True, False),
    ("http://demo:test-secret@camera/snapshot", True, True),
])
async def test_camera_download_uses_direct_url_without_separate_http_auth(
    camera_store, monkeypatch, url, protected, expected_userinfo,
):
    reference = "1" * 32 if protected else None
    if protected:
        camera_store[reference] = url
    requests = []

    def camera_api(request):
        requests.append(request)
        return httpx.Response(200, content=b"\xff\xd8\xfftest")

    client = httpx.AsyncClient
    transport = httpx.MockTransport(camera_api)
    monkeypatch.setattr(cameras.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))
    camera = {"url": None if protected else url, "credential_id": reference}
    assert await cameras.download_snapshot(camera) == b"\xff\xd8\xfftest"
    assert len(requests) == 1
    assert requests[0].url.raw_path == httpx.URL(url).raw_path
    authorization = requests[0].headers.get("Authorization")
    if expected_userinfo:
        assert b64decode(authorization.split(" ", 1)[1]) == b"demo:test-secret"
    else:
        assert authorization is None
