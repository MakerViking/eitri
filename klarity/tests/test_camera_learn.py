import asyncio
import http.server
import io
import json
import threading

import pytest
from PIL import Image

from helpers import build_map, live_screen, ready_state
from klarity.camera import CameraError, CameraPreview, SnapshotFetcher, resize_jpeg, validate_jpeg
from klarity.learn import LearnRecorder
from klarity.protocol import MarkerEvent, TouchEvent, UnknownFrame
from klarity.screenmap import ScreenMap


def make_jpeg(size=(1920, 1080)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def snapshot_server():
    jpeg = make_jpeg((640, 360))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/snapshot":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(jpeg)
            elif self.path == "/junk":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html>not a jpeg</html>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


async def test_fetch_snapshot(snapshot_server):
    data = await SnapshotFetcher(snapshot_server + "/snapshot").fetch()
    assert data.startswith(b"\xff\xd8")


async def test_fetch_rejects_non_jpeg_404_and_dead_port(snapshot_server):
    with pytest.raises(CameraError, match="JPEG"):
        await SnapshotFetcher(snapshot_server + "/junk").fetch()
    with pytest.raises(CameraError, match="failed"):
        await SnapshotFetcher(snapshot_server + "/nope").fetch()
    with pytest.raises(CameraError):
        await SnapshotFetcher("http://127.0.0.1:9/snapshot", timeout=0.5).fetch()
    with pytest.raises(CameraError, match="http"):
        SnapshotFetcher("file:///etc/passwd")


def test_validate_and_resize():
    with pytest.raises(CameraError):
        validate_jpeg(b"GIF89a")
    with pytest.raises(CameraError):
        validate_jpeg(b"\xff\xd8truncated")
    small = resize_jpeg(make_jpeg(), 480, 270)
    img = Image.open(io.BytesIO(small))
    assert img.size == (480, 270)
    assert not img.info.get("progressive")
    assert len(small) < 40_000
    with pytest.raises(CameraError, match="decode"):
        resize_jpeg(b"\xff\xd8garbage\xff\xd9", 10, 10)


def camera_map() -> ScreenMap:
    import copy

    from helpers import TEST_MAP

    raw = copy.deepcopy(TEST_MAP)
    raw["pages"]["cam"] = {"id": 3, "widgets": {}}
    raw["camera"] = {
        "page": "cam",
        "obj": "p_cam",
        "picture": "ram/0.jpg",
        "interval": 0.05,
        "width": 320,
        "height": 180,
    }
    return ScreenMap.from_dict(raw)


async def test_preview_pushes_frame_only_on_camera_page(snapshot_server):
    smap = camera_map()
    async with live_screen() as (screen, t):
        preview = CameraPreview(
            screen, smap, SnapshotFetcher(snapshot_server + "/snapshot"), push_enabled=True
        )
        screen.set_page(1)
        assert not preview.active()
        screen.set_page(3)
        assert preview.active()
        task = asyncio.create_task(preview.push_frame(smap.camera))
        await t.wait_for_command(lambda c: c.startswith('twfile "ram/0.jpg",'))
        t.inject(b"\xfe\xff\xff\xff")
        await asyncio.sleep(0.1)
        t.inject(b"\xfd\xff\xff\xff")
        await asyncio.wait_for(task, 3)
        assert bytes(t.sent).endswith(b'p_cam.path="ram/0.jpg"\xff\xff\xff')
        assert preview.frames_pushed == 1


async def test_preview_disabled_push_sends_nothing(snapshot_server):
    smap = camera_map()
    async with live_screen() as (screen, t):
        preview = CameraPreview(
            screen, smap, SnapshotFetcher(snapshot_server + "/snapshot"), push_enabled=False
        )
        await preview.push_frame(smap.camera)
        assert not t.sent and preview.frames_pushed == 0


# -------------------------------------------------------------------------------- learn


async def test_learn_logs_known_and_unknown(tmp_path):
    log = tmp_path / "sub" / "learn.jsonl"
    rec = LearnRecorder(build_map(), ready_state(), str(log), window=0.05)
    rec.record(TouchEvent(b"", 1, 10, 0), 1)  # mapped
    rec.record(TouchEvent(b"", 1, 77, 1), 1)  # unmapped press
    rec.record(TouchEvent(b"", 9, 3, 0), 9)  # unmapped page
    rec.record(MarkerEvent(b"\xf9", 0xF9), 1)  # unmapped marker
    rec.record(MarkerEvent(b"\x91", 0x91), 1)  # mapped marker
    rec.record(UnknownFrame(b"\xa7\x01", 0xA7, b"\x01"), 1)
    await asyncio.sleep(0.15)
    rec.close()
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    touches = [line for line in lines if line["kind"] == "touch"]
    assert [(t["page"], t["widget"], t["known"]) for t in touches] == [
        (1, 10, True),
        (1, 77, False),
        (9, 3, False),
    ]
    markers = [line for line in lines if line["kind"] == "marker"]
    assert [m["known"] for m in markers] == [False, True]
    assert any(line["kind"] == "unknown" and not line["known"] for line in lines)


async def test_learn_effect_probe_records_state_change_and_gcode(tmp_path):
    log = tmp_path / "learn.jsonl"
    st = ready_state()
    rec = LearnRecorder(build_map(), st, str(log), window=0.05)
    rec.record(TouchEvent(b"", 1, 77, 0), 1)
    st.apply(
        {"extruder": {"target": 210.0, "temperature": 99.0}}
    )  # temperature is noise, target is not
    st.add_gcode_response("ok")
    await asyncio.sleep(0.15)
    rec.close()
    effect = next(json.loads(line) for line in log.read_text().splitlines() if '"effect"' in line)
    assert effect["of"] == {"page": 1, "widget": 77, "type": 0}
    assert effect["state_changes"] == {"extruder.target": [0.0, 210.0]}
    assert effect["gcode_responses"] == ["ok"]


def test_learn_summary_yaml_skeleton(tmp_path):
    import yaml

    rec = LearnRecorder(build_map(), ready_state(), None)
    rec.seen[(1, 10, 0)] += 2  # mapped: excluded
    rec.seen[(5, 8, 1)] += 3
    rec.seen[(5, 8, 0)] += 3
    rec.unknown_markers[0xFA] += 1
    out = yaml.safe_load(rec.summary())
    assert list(out["pages"]) == ["page_5"]
    assert out["pages"]["page_5"]["widgets"]["widget_8"]["seen"] == {"press": 3, "release": 3}
    assert 0xFA in out["markers"]


def test_learn_unwritable_log_does_not_crash():
    rec = LearnRecorder(build_map(), ready_state(), "/proc/nope/learn.jsonl")
    rec.record(TouchEvent(b"", 1, 1, 1), 1)  # logs to journal only
