import asyncio
import os
import pty

import pytest

from klarity.protocol import TERMINATOR
from klarity.screen import Screen
from klarity.transport import MemoryTransport, SerialTransport, TransportError


@pytest.fixture
def pty_pair():
    master, slave = pty.openpty()
    yield master, os.ttyname(slave)
    os.close(master)
    os.close(slave)


async def test_serial_transport_over_pty(pty_pair):
    master, slave_path = pty_pair
    transport = SerialTransport(slave_path, 115200)
    await transport.open()
    try:
        os.write(master, b"\x65\x01\x02\x01" + TERMINATOR)
        assert await asyncio.wait_for(transport.read(), 2) == b"\x65\x01\x02\x01" + TERMINATOR
        await transport.write(b"page 1" + TERMINATOR)
        await asyncio.sleep(0.1)
        assert os.read(master, 100) == b"page 1" + TERMINATOR
    finally:
        await transport.close()


async def test_screen_runs_over_pty(pty_pair):
    master, slave_path = pty_pair
    screen = Screen(SerialTransport(slave_path, 115200), reconnect_min=0.05)
    task = asyncio.create_task(screen.run())
    try:
        await asyncio.wait_for(screen.connected.wait(), 2)
        os.write(master, b"\x65\x04\x05\x01" + TERMINATOR)
        ev = await asyncio.wait_for(screen.events.get(), 2)
        assert (ev.page, ev.widget) == (4, 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_serial_open_failure_is_a_transport_error():
    with pytest.raises(TransportError, match="cannot open"):
        await SerialTransport("/dev/does-not-exist", 115200).open()


async def test_serial_io_before_open_raises():
    t = SerialTransport("/dev/null", 9600)
    with pytest.raises(TransportError):
        await t.read()
    with pytest.raises(TransportError):
        await t.write(b"x")


async def test_memory_transport_commands_roundtrip():
    t = MemoryTransport()
    await t.open()
    await t.write(b"page 1" + TERMINATOR + b't0.txt="a"' + TERMINATOR)
    assert t.commands() == ["page 1", 't0.txt="a"']
    await t.close()
    assert await t.read() == b""
