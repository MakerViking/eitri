"""Failure isolation, action throttling and framing regressions."""

import asyncio
import copy

import pytest

from fakes import eventually
from helpers import TEST_MAP, build_map, fast_config, live_screen, ready_state
from klarity import actions as actions_mod
from klarity.actions import ActionRunner
from klarity.camera import CameraPreview
from klarity.daemon import Klarity
from klarity.moonraker import MoonrakerError
from klarity.protocol import (
    Cmd,
    FrameParser,
    Garbage,
    NumberEvent,
    ProtocolError,
    StringEvent,
    TouchEvent,
)
from klarity.pusher import Pusher
from klarity.screenmap import Action, ScreenMap
from klarity.transport import MemoryTransport

FF3 = b"\xff\xff\xff"


class StubScreen:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, command):
        self.sent.append(command)


class StubMoonraker:
    def __init__(self):
        self.calls: list[str] = []
        self.fail_next = False
        self.gate: asyncio.Event | None = None

    async def gcode(self, script):
        self.calls.append(script)
        if self.gate is not None:
            await self.gate.wait()

    def __getattr__(self, name):  # pause/resume/... are looked up eagerly by ActionRunner._print
        async def noop():
            return None

        return noop

    async def cancel(self):
        self.calls.append("cancel")
        if self.fail_next:
            self.fail_next = False
            raise MoonrakerError("link down")


def runner(mr=None):
    mr = mr or StubMoonraker()
    screen = StubScreen()
    return ActionRunner(screen, mr, ready_state(), build_map()), mr, screen


CANCEL = Action.parse(
    {
        "print": "cancel",
        "guard": {"source": "derived.state", "in": ["standby", "printing"]},
        "cooldown": 2.0,
    },
    "t",
)


# ------------------------------------------------------------------------- actions


async def test_failed_action_does_not_consume_its_cooldown():
    r, mr, screen = runner()
    mr.fail_next = True
    await r.run([CANCEL], {})
    assert mr.calls == ["cancel"] and screen.sent == [Cmd.beep(actions_mod.ERROR_BEEP_MS)]
    await r.run([CANCEL], {})  # immediate retry must reach Moonraker again
    assert mr.calls == ["cancel", "cancel"]
    await r.run([CANCEL], {})  # but a success still arms the cooldown
    assert mr.calls == ["cancel", "cancel"]


async def test_same_gcode_action_is_not_run_twice_concurrently():
    r, mr, _ = runner()
    mr.gate = asyncio.Event()
    g = Action.parse({"gcode": "G28"}, "t")
    first = asyncio.create_task(r.run([g], {}))
    await eventually(lambda: mr.calls == ["G28"])
    await r.run([g], {})  # repeat tap while the first is still running: ignored
    await r.run([g], {})
    assert mr.calls == ["G28"]
    mr.gate.set()
    await first
    await r.run([g], {})  # once finished it may run again
    assert mr.calls == ["G28", "G28"]


async def test_concurrent_action_runs_are_capped():
    r, mr, screen = runner()
    mr.gate = asyncio.Event()
    acts = [Action.parse({"gcode": f"G{i}"}, "t") for i in range(actions_mod.MAX_CONCURRENT_RUNS)]
    tasks = [asyncio.create_task(r.run([a], {})) for a in acts]
    await eventually(lambda: len(mr.calls) == len(acts))
    extra = Action.parse({"gcode": "M115"}, "t")
    await r.run([extra], {})
    assert "M115" not in mr.calls and screen.sent  # dropped with an error beep
    mr.gate.set()
    await asyncio.gather(*tasks)
    await r.run([extra], {})
    assert "M115" in mr.calls


async def test_gcode_has_a_finite_default_timeout(moonraker):
    from klarity.moonraker import GCODE_TIMEOUT, MoonrakerClient

    assert 0 < GCODE_TIMEOUT < float("inf")
    seen = {}
    client = MoonrakerClient("ws://127.0.0.1:1", ready_state())

    async def fake_call(method, params=None, timeout=-1):
        seen["timeout"] = timeout

    client.call = fake_call
    await client.gcode("G28")
    assert seen["timeout"] == GCODE_TIMEOUT


# ------------------------------------------------------------------------- daemon


def make_daemon():
    return Klarity(fast_config("ws://127.0.0.1:1"), smap=build_map(), transport=MemoryTransport())


async def test_supervisor_restarts_a_crashing_task_with_backoff():
    d = make_daemon()
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) < 3:
            raise AttributeError("boom")

    await asyncio.wait_for(d._supervise("flaky", flaky, backoff_min=0.01, backoff_max=0.02), 2)
    assert len(runs) == 3


async def test_supervisor_propagates_cancellation():
    d = make_daemon()
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(d._supervise("f", forever))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# -------------------------------------------------------------------------- pusher


async def test_pusher_survives_a_binding_that_cannot_render():
    st = ready_state()
    async with live_screen() as (screen, t):
        smap = build_map()
        pusher = Pusher(screen, st, smap, min_interval=0.02)
        screen.set_page(1)
        binding = smap.push_items(1)[1].binding  # nozzle text
        object.__setattr__(binding, "format", "{0.nope}")  # bypass load-time validation
        sent = await pusher.push_once()  # must not raise
        assert sent == 4
        assert not any("t_noz" in c for c in t.commands())
        await pusher.push_once()  # and keeps going on later passes


# --------------------------------------------------------------------------- camera


async def test_camera_loop_survives_protocol_error():
    from test_camera_learn import camera_map

    smap = camera_map()
    async with live_screen() as (screen, _t):
        screen.set_page(3)
        calls = []
        preview = CameraPreview(screen, smap, fetcher=None, push_enabled=True)

        async def bad_frame(spec):
            calls.append(1)
            raise ProtocolError("bad path")

        preview.push_frame = bad_frame
        task = asyncio.create_task(preview.run())
        await eventually(lambda: len(calls) >= 1)
        await asyncio.sleep(0.2)
        assert not task.done()
        task.cancel()


def test_resize_rejects_non_image_data_cleanly():
    from klarity.camera import CameraError, resize_jpeg

    with pytest.raises(CameraError):
        resize_jpeg(b"\xff\xd8 not really a jpeg", 100, 100)


# --------------------------------------------------------------------------- screen


def keypad_map() -> ScreenMap:
    raw = copy.deepcopy(TEST_MAP)
    raw["pages"]["home"]["widgets"]["flow"] = {
        "id": 30,
        "on_number": [{"gcode": "M221 S{value}"}],
    }
    return ScreenMap.from_dict(raw)


async def test_get_waiter_does_not_swallow_a_stock_keypad_frame():
    d = Klarity(fast_config("ws://127.0.0.1:1"), smap=keypad_map(), transport=MemoryTransport())
    async with live_screen() as (screen, t):
        screen.is_keypad_frame = d._is_keypad_frame  # the daemon's map-driven predicate
        got = asyncio.create_task(screen.get("n0.val", timeout=1.0))
        await t.wait_for_command(lambda c: c == "get n0.val")
        t.inject(bytes([0x71, 1, 30, 0x78, 0x00]) + FF3)  # stock keypad report, widget 30
        ev = await asyncio.wait_for(screen.events.get(), 1)
        assert isinstance(ev, NumberEvent) and ev.stock == (1, 30, 120)
        assert not got.done()  # the waiter did not take it
        t.inject(bytes([0x71, 7, 0, 0, 0]) + FF3)  # the real reply
        assert await got == 7


async def test_late_get_reply_is_dropped_not_acted_on():
    async with live_screen() as (screen, t):
        with pytest.raises(Exception, match="timed out"):
            await screen.get("n0.val", timeout=0.05)
        t.inject(b"\x71\x2a\x00\x00\x00" + FF3)  # the reply, now too late
        t.inject(b"\x70late" + FF3)
        await asyncio.sleep(0.15)
        assert screen.events.empty()


async def test_on_page_callback_error_does_not_kill_the_read_loop():
    async with live_screen() as (screen, t):
        screen.on_page = lambda page: 1 / 0
        t.inject(b"\x66\x04" + FF3)
        await eventually(lambda: screen.current_page == 4)
        t.inject(b"\x70ok" + FF3)
        while not isinstance(ev := await asyncio.wait_for(screen.events.get(), 1), StringEvent):
            pass
        assert ev.text == "ok"


# -------------------------------------------------------------------------- protocol


def test_stray_byte_before_a_valid_frame_does_not_swallow_it():
    p = FrameParser()
    events = p.feed(b"\x41" + b"\x65\x01\x03\x01" + FF3)
    assert [type(e) for e in events] == [Garbage, TouchEvent]
    assert (events[1].page, events[1].widget, events[1].type) == (1, 3, 1)


def test_stray_byte_before_number_frame_with_ff_payload():
    p = FrameParser()
    events = p.feed(b"\x41" + b"\x71\xff\xff\xff\xff" + FF3)
    # The number's own FF bytes mean the first terminator is not the frame's: left to the
    # generic unknown-frame path rather than guessed at.
    assert events and not any(isinstance(e, TouchEvent) for e in events)


def test_unknown_frame_with_embedded_fixed_looking_bytes_is_kept_whole():
    p = FrameParser()
    # 0x41 ... terminator, where the payload only coincidentally starts like a fixed frame
    # but its own terminator is not the first one.
    events = p.feed(b"\x41\x65\xff\xff\xff" + b"\x65\x01\x02\x00" + FF3)
    assert any(isinstance(e, TouchEvent) for e in events)
