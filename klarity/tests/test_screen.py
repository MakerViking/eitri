import asyncio
import contextlib

import pytest

from fakes import eventually
from klarity.protocol import TERMINATOR, MarkerEvent, TouchEvent
from klarity.screen import Screen, ScreenError
from klarity.transport import MemoryTransport

FF3 = TERMINATOR


@contextlib.asynccontextmanager
async def running_screen(**kw):
    t = MemoryTransport()
    screen = Screen(t, reconnect_min=0.02, reconnect_max=0.05, frame_timeout=0.05, **kw)
    task = asyncio.create_task(screen.run())
    await asyncio.wait_for(screen.connected.wait(), 2)
    try:
        yield screen, t
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_events_and_page_tracking():
    async with running_screen() as (screen, t):
        pages = []
        screen.on_page = pages.append
        t.inject(b"\x65\x03\x07\x01" + FF3)
        ev = await asyncio.wait_for(screen.events.get(), 1)
        assert isinstance(ev, TouchEvent) and screen.current_page == 3 and pages == [3]
        t.inject(b"\x66\x04" + FF3)
        await eventually(lambda: screen.current_page == 4)


async def test_send_encodes_with_terminator():
    async with running_screen() as (screen, t):
        await screen.send('t0.txt="hi"')
        assert bytes(t.sent) == b't0.txt="hi"' + FF3


async def test_send_while_disconnected_raises():
    screen = Screen(MemoryTransport())
    with pytest.raises(ScreenError):
        await screen.send("page 0")


async def test_get_number_and_string_and_error():
    async with running_screen() as (screen, t):
        task = asyncio.create_task(screen.get("n0.val"))
        await t.wait_for_command(lambda c: c == "get n0.val")
        t.inject(b"\x71\x2a\x00\x00\x00" + FF3)
        assert await task == 42

        task = asyncio.create_task(screen.get("t0.txt"))
        await eventually(lambda: t.commands().count("get t0.txt") == 1)
        t.inject(b"\x70abc" + FF3)
        assert await task == "abc"

        task = asyncio.create_task(screen.get("bogus.txt"))
        await eventually(lambda: "get bogus.txt" in t.commands())
        t.inject(b"\x1a" + FF3)
        with pytest.raises(ScreenError, match="invalid variable"):
            await task


async def test_get_timeout():
    async with running_screen() as (screen, _t):
        with pytest.raises(ScreenError, match="timed out"):
            await screen.get("n0.val", timeout=0.05)


async def test_stale_partial_frame_is_flushed():
    async with running_screen() as (screen, t):
        t.inject(b"\x65\x01")  # truncated touch
        ev = await asyncio.wait_for(screen.events.get(), 1)  # Garbage after frame_timeout
        assert type(ev).__name__ == "Garbage"
        t.inject(b"\x65\x01\x02\x01" + FF3)
        ev = await asyncio.wait_for(screen.events.get(), 1)
        assert isinstance(ev, TouchEvent)


async def test_reconnects_and_runs_connect_hook():
    t = MemoryTransport()
    screen = Screen(t, reconnect_min=0.02, reconnect_max=0.05)
    hooks = []

    async def hook():
        hooks.append(1)
        await screen.send("sendme")

    screen.on_connect = hook
    task = asyncio.create_task(screen.run())
    try:
        await eventually(lambda: len(hooks) == 1)
        t.drop_link()
        await eventually(lambda: t.open_count == 2 and len(hooks) == 2, what="reopen")
        assert t.commands().count("sendme") == 2
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_open_failures_are_retried():
    t = MemoryTransport()
    t.fail_next_opens = 3
    screen = Screen(t, reconnect_min=0.01, reconnect_max=0.02)
    task = asyncio.create_task(screen.run())
    try:
        await asyncio.wait_for(screen.connected.wait(), 2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_send_file_handshake():
    async with running_screen() as (screen, t):
        data = bytes(range(200)) * 3
        task = asyncio.create_task(screen.send_file("ram/0.jpg", data, chunk=256))
        await t.wait_for_command(lambda c: c.startswith('twfile "ram/0.jpg",600'))
        t.inject(b"\xfe" + FF3)
        await eventually(lambda: len(t.sent) >= len(b'twfile "ram/0.jpg",600') + 3 + 600)
        assert bytes(t.sent).endswith(data)
        t.inject(b"\xfd" + FF3)
        await asyncio.wait_for(task, 1)


async def test_marker_events_pass_through():
    async with running_screen() as (screen, t):
        t.inject(b"\x91")
        ev = await asyncio.wait_for(screen.events.get(), 1)
        assert isinstance(ev, MarkerEvent) and ev.code == 0x91
