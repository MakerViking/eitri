"""End-to-end: fake panel (in-memory UART) <-> Klarity <-> fake Moonraker (real websocket)."""

import asyncio
import contextlib
import json

import pytest

from fakes import eventually
from helpers import build_map, fast_config
from klarity.daemon import Klarity
from klarity.transport import MemoryTransport

FF3 = b"\xff\xff\xff"


def touch(page: int, widget: int, typ: int = 0) -> bytes:
    return bytes([0x65, page, widget, typ]) + FF3


@contextlib.asynccontextmanager
async def klarity_running(moonraker, *, smap=None, **cfg_kw):
    transport = MemoryTransport()
    cfg = fast_config(moonraker.url, **cfg_kw)
    daemon = Klarity(cfg, smap=smap or build_map(), transport=transport)
    task = asyncio.create_task(daemon.run())
    try:
        await eventually(
            lambda: transport.is_open and "sendme" in transport.commands(), what="screen init"
        )
        await eventually(lambda: daemon.state.data.get("extruder"), what="moonraker state")
        yield daemon, transport
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def show_home(t: MemoryTransport):
    """The panel answers ``sendme`` with its current page."""
    t.clear()
    t.inject(b"\x66\x01" + FF3)
    await eventually(lambda: 't_noz.txt="25 / 0"' in t.commands(), what="initial paint")


async def test_startup_sequence_and_initial_paint(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        cmds = t.commands()
        assert cmds[0] == "bkcmd=0"
        assert "dim=100" in cmds and cmds[-1] == "sendme"
        await show_home(t)
        cmds = t.commands()
        assert 't_state.txt="Idle"' in cmds and 't_bed.txt="24 / 0"' in cmds
        assert "vis b_pause,0" in cmds


async def test_state_changes_are_pushed_incrementally(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.clear()
        await moonraker.push_status({"extruder": {"temperature": 150.4, "target": 200.0}})
        await eventually(lambda: 't_noz.txt="150 / 200"' in t.commands())
        await asyncio.sleep(0.1)
        assert t.commands() == ['t_noz.txt="150 / 200"']  # nothing else resent

        t.clear()
        await moonraker.push_status(
            {"print_stats": {"state": "printing"}, "virtual_sdcard": {"progress": 0.5}}
        )
        await eventually(lambda: "j_prog.val=50" in t.commands())
        cmds = t.commands()
        assert 't_state.txt="Printing"' in cmds and "vis b_pause,1" in cmds


async def test_pause_button_calls_moonraker_only_when_printing(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.inject(touch(1, 10, 1))  # press: no action mapped
        t.inject(touch(1, 10, 0))  # release while idle: guard blocks + beeps
        await eventually(lambda: "beep 50" in t.commands())
        assert "printer.print.pause" not in moonraker.methods()

        await moonraker.push_status({"print_stats": {"state": "printing"}})
        await eventually(lambda: daemon.state.resolve("derived.state") == "printing")
        t.inject(touch(1, 10, 0))
        await eventually(lambda: "printer.print.pause" in moonraker.methods())


async def test_resume_cancel_and_cooldown(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        await moonraker.push_status({"print_stats": {"state": "paused"}})
        await eventually(lambda: daemon.state.resolve("derived.state") == "paused")
        t.inject(touch(1, 11))
        await eventually(lambda: "printer.print.resume" in moonraker.methods())
        t.inject(touch(1, 12))
        t.inject(touch(1, 12))  # double tap within the cooldown
        await eventually(lambda: "printer.print.cancel" in moonraker.methods())
        await daemon.wait_idle()
        assert moonraker.methods().count("printer.print.cancel") == 1


async def test_gcode_button_and_guard(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.inject(touch(1, 13))
        await eventually(lambda: moonraker.scripts() == ["G28"])


async def test_page_navigation_and_page_specific_paint(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.clear()
        t.inject(touch(1, 14))  # "to move"
        await eventually(lambda: "page 2" in t.commands())
        t.inject(touch(2, 20))  # x+ on the move page (page learned from the touch itself)
        await eventually(lambda: any("G1 X10" in s for s in moonraker.scripts()))
        assert daemon.screen.current_page == 2
        assert 't_z.txt="5.0"' in t.commands()


def tap(page: int, widget: int) -> bytes:
    """The stock X-Plus 4 program's touch frame: no type byte."""
    return bytes([0x65, page, widget]) + FF3


async def test_a_stock_tap_runs_the_release_handler(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.inject(tap(1, 13))
        await eventually(lambda: moonraker.scripts() == ["G28"])


async def test_numeric_read_back_feeds_gcode(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.clear()
        t.inject(touch(1, 15))
        await eventually(lambda: "get n_noz.val" in t.commands())
        t.inject(b"\x71\xd2\x00\x00\x00" + FF3)  # 210
        await eventually(
            lambda: moonraker.scripts() == ["SET_HEATER_TEMPERATURE HEATER=extruder TARGET=210"]
        )


async def test_text_read_back_rejects_gcode_injection(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.clear()
        t.inject(touch(1, 16))
        await eventually(lambda: "get t_name.txt" in t.commands())
        t.inject(b"\x70x\nM112" + FF3)
        await eventually(lambda: "beep 300" in t.commands(), what="error beep")
        assert moonraker.scripts() == []


async def test_unsolicited_number_event_routed_to_page_handler(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.inject(touch(1, 14))
        await eventually(lambda: daemon.screen.current_page == 2)
        t.inject(b"\x71\x07\x00\x00\x00" + FF3)
        await eventually(lambda: moonraker.scripts() == ["M117 7"])


async def test_marker_navigates_home(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        daemon.screen.set_page(2)
        t.inject(b"\x91")
        await eventually(lambda: daemon.screen.current_page == 1)


async def test_moonraker_outage_is_shown_and_recovers(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.clear()
        await moonraker.drop_connections()
        await eventually(lambda: 't_state.txt="Offline"' in t.commands(), what="offline status")
        t.clear()
        # Values that did not change are not resent; the status word flips back.
        await eventually(
            lambda: 't_state.txt="Idle"' in t.commands(), what="status after reconnect"
        )
        assert daemon.state.connected and 't_noz.txt="25 / 0"' not in t.commands()


async def test_screen_link_drop_reinitialises_and_repaints(moonraker):
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.clear()
        t.drop_link()
        await eventually(
            lambda: t.open_count == 2 and "sendme" in t.commands(), what="reconnect init"
        )
        t.inject(b"\x66\x01" + FF3)
        await eventually(lambda: 't_noz.txt="25 / 0"' in t.commands())


async def test_failed_gcode_beeps_and_daemon_keeps_running(moonraker):
    moonraker.fail_scripts.add("G28")
    async with klarity_running(moonraker) as (daemon, t):
        await show_home(t)
        t.clear()
        t.inject(touch(1, 13))
        await eventually(lambda: "beep 300" in t.commands())
        await moonraker.push_status({"extruder": {"temperature": 99.0}})
        await eventually(lambda: 't_noz.txt="99 / 0"' in t.commands())


async def test_placeholder_map_is_inert_but_logs_in_learn_mode(moonraker, tmp_path):
    from klarity.config import packaged_map_path
    from klarity.screenmap import ScreenMap

    smap = ScreenMap.load(packaged_map_path())  # all TODO
    log = tmp_path / "learn.jsonl"
    async with klarity_running(moonraker, smap=smap, log_path=str(log)) as (daemon, t):
        t.clear()
        t.inject(b"\x66\x01" + FF3)  # page 1 = placeholder home
        t.inject(touch(1, 12, 1))  # placeholder "cancel": must do nothing
        t.inject(touch(1, 12, 0))
        t.inject(b"\xf9")
        await eventually(
            lambda: log.exists() and len(log.read_text().splitlines()) >= 4, what="learn log"
        )
        await asyncio.sleep(0.2)
        assert not [m for m in moonraker.methods() if m.startswith("printer.print")]
        assert moonraker.scripts() == []
        assert not [c for c in t.commands() if "txt" in c]  # nothing painted from placeholder ids
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    touches = [r for r in rows if r["kind"] == "touch"]
    assert [(r["page"], r["widget"], r["type"], r["known"]) for r in touches] == [
        (1, 12, 1, False),
        (1, 12, 0, False),
    ]
    assert any(r["kind"] == "marker" and r["code"] == 0xF9 and not r["known"] for r in rows)


async def test_daemon_refuses_to_start_without_serial_port():
    from klarity.config import ConfigError, KlarityConfig

    with pytest.raises(ConfigError, match="serial_port"):
        Klarity(KlarityConfig(), smap=build_map())
