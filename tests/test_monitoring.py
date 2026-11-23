import asyncio
import copy
import datetime as dt
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from astronomy import night_at_connection, site_from_profile, solar_altitude
from clients.nina_client import NinaClient, NinaError
from clients.phd2_client import PHD2Client
from monitoring import CaptureTracker, active_target, exposure_state
from workers import NinaWorker, build_state


def sequence(name="M31", ra=1, status="RUNNING"):
    return [{"Name": "Imaging_Container", "Status": status, "Items": [
        {"Name": "Etiqueta editable", "Status": status,
         "Target": {"TargetName": name, "InputCoordinates": {"Coordinates": {"RA": ra, "Dec": 40}}},
         "Items": [{"Name": "TakeExposure", "Status": status, "ExposureTime": 120}]}]}]


def capture(name="M31", index=1, kind="LIGHT"):
    return {"TargetName": name, "Filename": f"{index}.fits", "Date": f"2026-11-15T20:00:{index:02d}+01:00",
            "ImageType": kind, "ExposureTime": 120, "Filter": "L", "HFR": 2}


def test_active_target_not_last_image_or_container_label():
    target = active_target(sequence())
    assert target["name"] == "M31" and target["ra"] == 1
    assert active_target(sequence(status="FINISHED")) is None
    assert active_target(sequence() + sequence("M42")) is None
    assert active_target([{"Status": "RUNNING"}]) is None


@pytest.mark.parametrize(("degrees", "negative", "expected_dec"), [
    (-5, True, -(5 + 23 / 60 + 28 / 3600)),
    (0, True, -0.5),
    (0, False, 0.5),
])
def test_active_target_from_serialized_nina_input_coordinates(degrees, negative, expected_dec):
    tree = sequence("M 42")
    inputs = {"RAHours": 5, "RAMinutes": 35, "RASeconds": 17.3,
              "DecDegrees": degrees, "DecMinutes": 23 if degrees else 30,
              "DecSeconds": 28 if degrees else 0, "NegativeDec": negative}
    tree[0]["Items"][0]["Target"]["InputCoordinates"] = inputs
    target = active_target(tree)
    assert target["name"] == "M 42"
    assert target["ra"] == pytest.approx(5 + 35 / 60 + 17.3 / 3600)
    assert target["dec"] == pytest.approx(expected_dec)
    assert target["ra_string"] and target["dec_string"]
    del inputs["RASeconds"]
    assert active_target(tree)["ra"] is None


def test_new_captures_target_switch_delayed_and_return():
    tracker = CaptureTracker()
    tracker.set_target(active_target(sequence()))
    tracker.update_history([capture(index=0)])
    assert tracker.latest() is None  # una imagen antigua no prueba el tramo activo
    tracker.update_history([capture(index=0), capture(index=1)])
    assert tracker.latest()[0] == 1
    tracker.update_history(tracker.history + [capture(index=2)])
    assert tracker.latest()[0] == 2
    tracker.set_target(active_target(sequence("M42")))
    assert tracker.latest() is None
    tracker.update_history(tracker.history + [capture(index=3), capture("M42", 4, "DARK")])
    assert tracker.latest() is None
    tracker.update_history(tracker.history + [capture("M42", 5)])
    assert tracker.latest()[0] == 5
    tracker.set_target(active_target(sequence()))
    assert tracker.latest() is None
    assert tracker.summary()["L"]["count"] == 4
    tracker.update_history(tracker.history + [capture(index=6)])
    assert tracker.latest()[0] == 6


def test_same_name_new_coordinates_is_a_new_target():
    tracker = CaptureTracker()
    tracker.set_target(active_target(sequence()))
    tracker.update_history([])
    tracker.update_history([capture()])
    assert tracker.latest()
    assert tracker.set_target(active_target(sequence(ra=2)))
    assert tracker.latest() is None


def test_exposure_keeps_the_first_observation_until_the_next_capture(monkeypatch):
    clock = iter((1_000_000_000, 9_000_000_000))
    monkeypatch.setattr("monitoring.time.time_ns", lambda: next(clock))
    camera = {"Connected": True, "IsExposing": True, "ExposureEndTime": "2026-11-15T23:00:00Z"}
    exp = exposure_state(camera)
    assert exp["duration"] == 0 and exp["is_exposing"]
    assert exp["observed_at_ms"] == 1000
    assert exposure_state(camera, exp) == exp

    camera["ExposureEndTime"] = "2026-11-15T23:02:00Z"
    newer = exposure_state(camera, exp)
    assert newer["observed_at_ms"] == 9000
    assert exposure_state({"Connected": True, "IsExposing": True}, newer)["observed_at_ms"] is None
    idle = exposure_state({**camera, "Connected": False}, newer)
    assert idle["is_exposing"] is False and idle["observed_at_ms"] is None


def test_solar_geometry_and_polar_nights():
    utc = dt.timezone.utc
    assert solar_altitude(dt.datetime(2026, 6, 21, 12, tzinfo=utc), 51.48, 0) == pytest.approx(61.96, abs=0.3)
    night = night_at_connection({"latitude": 40.4, "longitude": -3.7}, dt.datetime(2026, 11, 15, 20, tzinfo=utc))
    assert len(night["events"]) == 8
    assert [e["altitude"] for e in night["events"]] == [-0.833, -6, -12, -18, -18, -12, -6, -0.833]
    assert night["events"][3]["phase_after"] == "night"
    assert night["events"][-1]["phase_after"] == "day"
    summer = night_at_connection({"latitude": 89, "longitude": 0}, dt.datetime(2026, 6, 21, 20, tzinfo=utc))
    assert summer["events"] == [] and summer["initial_phase"] == "day"
    assert site_from_profile({"AstrometrySettings": {"Latitude": 0, "Longitude": 0}})
    assert site_from_profile({"AstrometrySettings": {"Latitude": "NaN", "Longitude": 0}}) is None


class FakeNina:
    base_url = "http://localhost:1888/v2/api"

    def __init__(self):
        self.target = sequence()
        self.history = []
        self.event_history = []
        self.profile_calls = 0
        self.downloads = []
        self.start = "session-1"
        self.down = False
        self.on_download = None
    async def get_camera_info(self):
        if self.down:
            raise OSError("sin conexión")
        return {"Connected": True, "IsExposing": True, "CameraState": "Exposing"}
    async def _get(self, path):
        assert path == "/application-start"
        return self.start
    async def get_event_history(self):
        return self.event_history
    async def get_profile(self):
        self.profile_calls += 1
        return {"AstrometrySettings": {"Latitude": 40, "Longitude": -3}}
    async def get_advanced_sequence(self):
        return self.target
    async def get_mount_info(self):
        return {"Connected": True}
    async def get_weather_info(self):
        raise OSError("sin estación")
    async def get_safety_info(self):
        return {"IsSafe": True}
    async def get_image_count(self):
        return len(self.history)
    async def get_image_history(self):
        return copy.deepcopy(self.history)
    async def get_history_item(self, index):
        return copy.deepcopy(self.history[index])
    async def get_image_bytes(self, index, **kwargs):
        self.downloads.append(index)
        if self.on_download:
            await self.on_download()
        return f"image-{index}".encode()


@pytest.fixture
async def worker():
    setup = {"id": "one", "name": "Uno", "nina_host": "localhost", "nina_port": 1888}
    state = build_state(setup)
    worker = NinaWorker(setup, state, AsyncMock(), {}, {"hfr_history_points": 3})
    await worker.client.close()
    worker.client = FakeNina()
    return worker


async def test_scheduler_target_and_images_use_events_without_sequence_queries(worker):
    worker.client.target = []
    worker.client.get_advanced_sequence = AsyncMock(side_effect=OSError("sin estado de secuencia"))
    await worker.poll()
    target_event = {"Event": "TS-NEWTARGETSTART", "TargetName": "Sh2 174",
                    "Coordinates": {"RA": 23.7803, "Dec": 80.9389}}
    await worker.handle_event(target_event)
    worker.wake.clear()
    await worker.handle_event({**target_event, "Event": "TS-TARGETSTART"})
    assert worker.state["sequence_running"] is True and not worker.wake.is_set()
    target_key = worker.state["target"]["key"]
    worker.client.history.append(capture("Sh2 174"))
    await worker.poll()
    image_id = worker.state["latest_image_id"]
    assert image_id and worker.state["target"]["ra"] == 23.7803

    await worker.refresh_advanced_sequence()
    await worker.poll()
    assert worker.state["target"]["key"] == target_key
    assert worker.state["latest_image_id"] == image_id

    worker.client.history.append(capture("Sh2 174", index=2))
    await worker.poll()
    assert worker.state["latest_image_id"] != image_id
    assert worker.images["one"]["index"] == 1
    assert worker.state["target"]["filter_summary"]["L"]["count"] == 2

    await worker.handle_event({"Event": "TS-WAITSTART"})
    await worker.poll()
    await worker.refresh_advanced_sequence()
    worker.client.get_advanced_sequence.assert_not_awaited()
    assert worker.state["sequence_running"] is True
    assert worker.state["target"]["key"] is None
    assert worker.state["latest_image_id"] is None and not worker.images
    await worker.handle_event({"Event": "SEQUENCE-FINISHED"})
    assert worker.state["sequence_running"] is False


async def test_scheduler_recovers_altitude_coordinates_from_events(worker):
    name = "IC 5146 HOS"
    ra = 21 + 48 / 60 + 6 / 3600
    start = {"Event": "TS-TARGETSTART", "TargetName": name, "Coordinates": {"RA": [], "Dec": []}}
    worker.client.event_history = [start, {"Event": "IMAGE-SAVE"}]
    worker.client.history = [capture(name)]
    await worker.restore_target_events()
    await worker.poll()
    assert worker.state["target"]["name"] == name and worker.state["target"]["ra"] is None
    assert worker.state["sequence_running"] is True

    await worker.handle_event({**start, "Coordinates": {"RA": ra, "Dec": 40}})
    await worker.poll()
    target = worker.state["target"]
    assert target["name"] == name and (target["ra"], target["dec"]) == (ra, 40)
    assert worker.state["site"] and worker.state["night"]
    assert worker.state["latest_image_id"] is None and worker.client.downloads == []

    await worker.handle_event(start)
    assert worker.state["target"]["key"] == target["key"]


async def test_scheduler_context_survives_socket_failure_and_replays_boundaries(worker, monkeypatch):
    worker.client.target = []
    start = {"Event": "TS-TARGETSTART", "TargetName": "Sh2 174"}
    worker.client.event_history = [start, {"Event": "IMAGE-SAVE"}]
    await worker.restore_target_events()

    disconnected = asyncio.Event()

    def fail_socket(*args, **kwargs):
        disconnected.set()
        raise OSError("sin WebSocket")

    monkeypatch.setattr("workers.websockets.connect", fail_socket)
    listener = asyncio.create_task(worker.events())
    try:
        await asyncio.wait_for(disconnected.wait(), 1)
        await worker.poll()
        assert worker.state["target"]["name"] == "Sh2 174"
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

    for event in ("TS-WAITSTART", "SEQUENCE-FINISHED", "SEQUENCE-STARTING"):
        worker.client.event_history = [start, {"Event": event}, {"Event": "IMAGE-SAVE"}]
        await worker.restore_target_events()
        await worker.poll()
        assert worker.state["target"]["key"] is None
        await worker.handle_event(start)


async def test_sequence_updates_recovers_a_running_scheduler_without_reading_the_sequence(worker):
    recovered = asyncio.Event()

    async def broadcast(message):
        if message["state"]["sequence_running"] is True:
            recovered.set()

    worker.broadcast = broadcast
    worker.socket_connected = True
    worker.client.get_event_history = AsyncMock(return_value=[
        {"Event": "SEQUENCE-FINISHED"},
        {"Event": "SEQUENCE-STARTING"},
        {"Event": "TS-TARGETSTART", "TargetName": "M31", "Coordinates": {"RA": 1, "Dec": 40}},
        {"Event": "IMAGE-SAVE"},
    ])
    worker.client.get_advanced_sequence = AsyncMock()
    task = asyncio.create_task(worker.sequence_updates())
    try:
        await worker.poll()
        await asyncio.wait_for(recovered.wait(), 1)
        assert worker.state["target"]["name"] == "M31"
        worker.client.get_event_history.assert_awaited_once()
        worker.client.get_advanced_sequence.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_capture_names_ordinary_target_when_legacy_sequence_has_only_block_status(worker):
    worker.client.target = [{"Name": "Imaging_Container", "Status": "RUNNING", "Items": []}]
    worker.client.history.append(capture("Objetivo anterior"))
    await worker.poll()
    await worker.refresh_advanced_sequence()
    assert worker.state["latest_image_id"] is None

    worker.client.history.append(capture("M31", index=2))
    await worker.poll()
    assert worker.state["target"]["name"] == "M31"
    assert worker.state["target"]["ra"] is None
    assert worker.images["one"]["index"] == 1


async def test_exoplanet_uses_recent_tracking_position_without_changing_capture_identity(worker):
    worker.client.target = [{"Name": "Imaging_Container", "Status": "RUNNING", "Items": [
        {"Name": "ExoPlanet Object Container_Container", "Status": "RUNNING"},
    ]}]
    mount = {"Connected": True, "TrackingEnabled": True, "Slewing": True,
             "Coordinates": {"RA": 2.5, "Dec": -10, "Epoch": "JNOW"}}
    worker.client.get_mount_info = AsyncMock(return_value=mount)
    await worker.poll()
    await worker.refresh_advanced_sequence()

    def fresh_capture(index, name="TOI-1728 b"):
        return {**capture(name, index=index), "Date": dt.datetime.now(dt.timezone.utc).isoformat()}

    worker.client.history.append(fresh_capture(1))
    await worker.poll()
    original_key = worker.state["target"]["key"]
    assert worker.state["target"]["ra"] is None

    mount["Slewing"] = False
    worker.client.history.append(fresh_capture(2))
    await worker.poll()
    target = worker.state["target"]
    assert target["key"] == original_key and target["name"] == "TOI-1728 b"
    assert (target["ra"], target["dec"], target["epoch"]) == (2.5, -10, "JNOW")
    assert target["coordinates_source"] == "mount" and target["filter_summary"]["L"]["count"] == 2
    assert worker.images["one"]["index"] == 1

    worker.client.history.append(capture("Otro objetivo", index=3))
    await worker.poll()
    assert worker.state["target"]["name"] == "Otro objetivo"
    assert worker.state["target"]["ra"] is None


@pytest.mark.asyncio
async def test_advanced_sequence_state_requires_a_running_block_and_is_invalidated_on_finish(worker):
    assert worker.state["sequence_running"] is None
    worker.client.target = [{"GlobalTriggers": [{"Status": "RUNNING"}]}, {"Name": "Inicio", "Status": "CREATED"}]
    await worker.poll()
    await worker.refresh_advanced_sequence()
    assert worker.state["connected"] and worker.state["sequence_running"] is False

    worker.client.target[1]["Status"] = "RUNNING"
    await worker.refresh_advanced_sequence()
    assert worker.state["sequence_running"] is True
    assert worker.state["target"]["key"] is None

    async def stale_poll():
        await worker.handle_event({"Event": "SEQUENCE-FINISHED"})
        return worker.client.target

    worker.client.get_advanced_sequence = stale_poll
    await worker.refresh_advanced_sequence()
    assert worker.state["sequence_running"] is False
    assert worker.wake.is_set()

    await worker.handle_event({"Event": "SEQUENCE-STARTING"})
    worker.client.get_advanced_sequence = AsyncMock(return_value=worker.client.target)
    await worker.refresh_advanced_sequence()
    assert worker.state["sequence_running"] is True
    worker.client.get_advanced_sequence.side_effect = OSError("sin estado de secuencia")
    await worker.refresh_advanced_sequence()
    assert worker.state["connected"] and worker.state["sequence_running"] is None


@pytest.mark.asyncio
async def test_worker_updates_every_capture_caches_site_and_survives_weather(worker):
    await worker.poll()
    await worker.refresh_advanced_sequence()
    assert worker.state["connected"] and worker.state["weather"] is None
    assert worker.state["latest_image_id"] is None
    night = worker.state["night"]
    tokens = []
    for index in range(10):
        worker.client.history.append(capture(index=index))
        await worker.poll()
        tokens.append(worker.state["latest_image_id"])
    assert len(set(tokens)) == 10
    assert worker.client.downloads == list(range(10))
    await worker.poll()
    assert len(worker.client.downloads) == 10
    assert worker.client.profile_calls == 1 and worker.state["night"] is night
    assert len(worker.state["hfr_history"]) == 3
    assert worker.state["target"]["filter_summary"]["L"]["count"] == 10
    worker.client.target = sequence("M42")
    await worker.refresh_advanced_sequence()
    await worker.poll()
    assert worker.state["latest_image_id"] is None and not worker.images
    worker.client.history.append(capture(index=10))  # respuesta tardía del objeto anterior
    await worker.poll()
    assert worker.state["latest_image_id"] is None
    worker.client.history.append(capture("M42", 11))
    await worker.poll()
    assert worker.images["one"]["bytes"] == b"image-11"


@pytest.mark.asyncio
async def test_target_changes_during_download_are_discarded(worker):
    await worker.poll()
    await worker.handle_event({"Event": "TS-TARGETSTART", "TargetName": "M31"})
    worker.client.history.append(capture())
    async def change():
        await worker.handle_event({"Event": "TS-TARGETSTART", "TargetName": "M42"})
    worker.client.on_download = change
    await worker.poll()
    assert worker.state["target"]["name"] == "M42"
    assert worker.state["latest_image_id"] is None and not worker.images


@pytest.mark.asyncio
async def test_reconnect_and_fast_nina_restart_recompute_once(worker):
    await worker.poll()
    await worker.reset()
    await worker.poll()
    assert worker.client.profile_calls == 2
    worker.client.start = "session-2"
    await worker.poll()
    await worker.poll()
    assert worker.client.profile_calls == 3


@pytest.mark.asyncio
async def test_api_envelope_error_even_with_http_200():
    client = NinaClient("localhost", 1888)
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"Success": False, "Error": "No camera"})))
    with pytest.raises(NinaError, match="No camera"):
        await client.get_camera_info()
    await client.close()


@pytest.mark.asyncio
async def test_nina_history_index_matches_binary_image_request():
    item = {"TargetName": "M31", "ImageType": "LIGHT"}
    jpeg = b"\xff\xd8test\xff\xd9"

    def reply(request):
        params = request.url.params
        if request.url.path.endswith("/image-history"):
            result = 1 if params.get("count") == "true" else [item]
            assert params.get("count") == "true" or params.get("index") == "0"
            return httpx.Response(200, json={"Success": True, "Response": result})
        assert request.url.path.endswith("/image/0")
        assert params.get("stream") == "true"
        assert params.get("resize") == "false"
        return httpx.Response(200, content=jpeg, headers={"Content-Type": "image/jpeg"})

    client = NinaClient("localhost", 1888)
    await client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as mocked:
        client.http = mocked
        assert await client.get_image_count() == 1
        assert await client.get_history_item(0) == item
        assert await client.get_image_bytes(0, resize=False) == jpeg


@pytest.mark.asyncio
async def test_phd_events_arriving_during_rpc_are_preserved():
    async def handler(reader, writer):
        request = json.loads(await reader.readline())
        for message in ({"Event": "AppState", "State": "LostLock"}, {"id": request["id"], "result": 1.5},
                        {"Event": "GuideStep", "RADistanceRaw": 0.5, "DECDistanceRaw": -0.5}):
            writer.write((json.dumps(message) + "\r\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    client = PHD2Client("127.0.0.1", server.sockets[0].getsockname()[1])
    try:
        await client.connect()
        assert await client.get_pixel_scale() == 1.5
        events = [event async for event in client.events()]
        assert [e["Event"] for e in events] == ["AppState", "GuideStep"]
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_phd_long_run_units_settling_and_cleanup(monkeypatch):
    import workers
    ready = asyncio.Event()
    closed = False
    class FakePHD:
        def __init__(self, *args):
            pass
        async def connect(self):
            pass
        async def get_pixel_scale(self):
            return None
        async def close(self):
            nonlocal closed
            closed = True
        async def events(self):
            yield {"Event": "Settling"}
            yield {"Event": "GuideStep", "RADistanceRaw": 99, "DECDistanceRaw": 99}
            yield {"Event": "SettleDone", "Status": 0}
            yield {"Event": "StarLost"}
            for _ in range(1000):
                yield {"Event": "GuideStep", "RADistanceRaw": 0.5, "DECDistanceRaw": -0.2}
            ready.set()
            await asyncio.Future()
    monkeypatch.setattr(workers, "PHD2Client", FakePHD)
    setup = {"id": "one", "name": "Uno", "phd2_host": "localhost", "phd2_port": 4400}
    state = build_state(setup)
    messages = []
    async def publish(message):
        messages.append(message)
    task = asyncio.create_task(workers.phd2_loop(setup, state, publish))
    await asyncio.wait_for(ready.wait(), 2)
    assert state["guiding_status"] == "guiando"
    assert len(state["guide_steps"]) == 300 and state["guide_seq"] == 1000
    assert all(point["ra_distance_arcsec"] is None for point in state["guide_steps"])
    assert all(point["ra_distance_px"] == 0.5 for point in state["guide_steps"])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert closed and state["guide_steps"] == [] and state["guiding_status"] == "desconectado"


@pytest.mark.asyncio
async def test_independent_setups_and_recovery(worker):
    setup = dict(worker.setup, id="two", name="Dos")
    second = NinaWorker(setup, build_state(setup), AsyncMock(), worker.images, worker.config)
    await second.client.close()
    second.client = FakeNina()
    second.client.target = sequence("M42")
    await asyncio.gather(worker.poll(), second.poll())
    await asyncio.gather(worker.refresh_advanced_sequence(), second.refresh_advanced_sequence())
    worker.client.history.append(capture())
    second.client.history.append(capture("M42"))
    await asyncio.gather(worker.poll(), second.poll())
    assert set(worker.images) == {"one", "two"}
    await worker.reset()
    assert "one" not in worker.images and "two" in worker.images
    assert second.state["connected"] and second.state["target"]["name"] == "M42"
    await worker.poll()
    assert worker.client.profile_calls == 2 and second.client.profile_calls == 1
