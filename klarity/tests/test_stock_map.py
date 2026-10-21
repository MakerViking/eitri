"""The stock panel's numeric entries, offsets and derived values (shipped map behaviour)."""

import asyncio
import json

import pytest

from fakes import eventually
from klarity.config import packaged_map_path
from klarity.protocol import FrameParser, NumberEvent
from klarity.screenmap import Action, MapError, ScreenMap
from klarity.state import PrinterState
from test_e2e import FF3, klarity_running, touch


def parse_one(frame: bytes):
    (event,) = FrameParser().feed(frame)
    return event


def test_stock_number_frame_decodes_page_widget_and_u16():
    event = parse_one(bytes([0x71, 0x11, 0x03, 0x78, 0x00]) + FF3)
    assert isinstance(event, NumberEvent)
    assert event.stock == (0x11, 0x03, 120)
    event = parse_one(bytes([0x71, 0x23, 0x00, 0x72, 0x01]) + FF3)
    assert event.stock == (0x23, 0x00, 370)


def test_action_adjust_clamps_scales_and_allows_zero():
    a = Action.parse({"gcode": "M141 S{value}", "clamp": [45, 65], "zero_ok": True}, "t")
    assert [a.adjust(v) for v in (0, 30, 50, 90)] == [0, 45, 50, 65]
    fan = Action.parse({"gcode": "M106 S{value}", "clamp": [0, 100], "scale": 2.55}, "t")
    assert [fan.adjust(v) for v in (0, 40, 100, 500)] == [0, 102, 255, 255]
    with pytest.raises(MapError):
        Action.parse({"gcode": "x", "clamp": [5, 1]}, "t")
    with pytest.raises(MapError):
        Action.parse({"gcode": "x", "clamp": [1]}, "t")


def test_progress_bar_picture_offset_and_speed_value():
    m = ScreenMap.load(packaged_map_path())
    st = PrinterState()
    st.connected = True
    st.klippy_state = "ready"
    st.data = {
        "print_stats": {"state": "printing"},
        "virtual_sdcard": {"progress": 0.5},
        "gcode_move": {"speed": 6000.0, "speed_factor": 1.5},
    }
    items = {i.obj: i for i in m.push_items(0x11)}
    assert items["p0"].binding.attr == "pic"
    assert items["p0"].binding.render(st) == 188  # 50 % + 138
    assert items["n3"].binding.render(st) == 150  # 6000 mm/min * 1.5 / 60
    assert st.resolve("derived.speed_mms") == 150


async def test_typed_speed_is_clamped_and_sent(moonraker):
    async with klarity_running(moonraker, smap=ScreenMap.load(packaged_map_path())) as (_, t):
        t.inject(b"\x66\x11" + FF3)
        t.inject(bytes([0x71, 0x11, 0x03, 0x78, 0x00]) + FF3)  # 120 %
        await eventually(lambda: moonraker.scripts() == ["M220 S120"])
        t.inject(bytes([0x71, 0x11, 0x03, 0xC8, 0x00]) + FF3)  # 200 % -> 150
        await eventually(lambda: moonraker.scripts()[-1] == "M220 S150")


async def test_typed_nozzle_target_is_gated_by_state(moonraker):
    async with klarity_running(moonraker, smap=ScreenMap.load(packaged_map_path())) as (daemon, t):
        t.inject(b"\x66\x23" + FF3)
        t.inject(bytes([0x71, 0x23, 0x00, 0xD2, 0x00]) + FF3)  # 210
        await eventually(
            lambda: moonraker.scripts() == ["SET_HEATER_TEMPERATURE HEATER=extruder TARGET=210"]
        )
        await moonraker.push_status({"print_stats": {"state": "printing"}})
        await eventually(lambda: daemon.state.resolve("derived.state") == "printing")
        t.inject(bytes([0x71, 0x23, 0x00, 0x32, 0x00]) + FF3)  # 50 while printing -> floor 180
        await eventually(
            lambda: moonraker.scripts()[-1] == "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=180"
        )


async def test_stock_cancel_button_only_opens_confirmation(moonraker):
    async with klarity_running(moonraker, smap=ScreenMap.load(packaged_map_path())) as (daemon, t):
        await moonraker.push_status({"print_stats": {"state": "printing"}})
        await eventually(lambda: daemon.state.resolve("derived.state") == "printing")
        t.inject(b"\x66\x11" + FF3)
        t.inject(touch(0x11, 0))
        await eventually(lambda: "page 53" in t.commands())
        await daemon.wait_idle()
        assert "printer.print.cancel" not in moonraker.methods()


async def test_status_file_reports_link_state(moonraker, tmp_path):
    status = tmp_path / "status.json"
    async with klarity_running(moonraker, smap=ScreenMap.load(packaged_map_path())) as (d, _):
        d.cfg.status_file = str(status)
        task = asyncio.create_task(d._status_loop(interval=0.05))
        try:
            await eventually(status.exists, what="status file")
            snap = json.loads(status.read_text())
        finally:
            task.cancel()
    assert snap["connected"] is True and snap["display"] == "TJC 800x480"
    assert snap["moonraker"] is True and snap["version"]
