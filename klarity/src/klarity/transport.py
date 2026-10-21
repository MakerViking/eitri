"""Byte transports for the screen link.

The daemon only needs four operations, so they are a small :class:`Transport` protocol:
a real UART (:class:`SerialTransport`) and an in-memory loopback (:class:`MemoryTransport`)
used by tests and ``klarity --simulate``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol, runtime_checkable

from .protocol import TERMINATOR


class TransportError(OSError):
    """The link could not be opened or has failed."""


@runtime_checkable
class Transport(Protocol):
    async def open(self) -> None: ...

    async def read(self) -> bytes:
        """Return the next available chunk; ``b""`` means the link is closed."""
        ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


class SerialTransport:
    """Async UART via pyserial-asyncio-fast (imported lazily so tests need no pyserial)."""

    def __init__(self, port: str, baud: int) -> None:
        self.port = port
        self.baud = baud
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        import serial_asyncio_fast

        try:
            self._reader, self._writer = await serial_asyncio_fast.open_serial_connection(
                url=self.port, baudrate=self.baud
            )
        except Exception as exc:  # pyserial raises SerialException (an OSError subclass)
            raise TransportError(f"cannot open {self.port} @ {self.baud}: {exc}") from exc

    async def read(self) -> bytes:
        if self._reader is None:
            raise TransportError("serial port not open")
        try:
            return await self._reader.read(1024)
        except Exception as exc:
            raise TransportError(f"read failed on {self.port}: {exc}") from exc

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise TransportError("serial port not open")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:
            raise TransportError(f"write failed on {self.port}: {exc}") from exc

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class MemoryTransport:
    """In-process stand-in for the panel.

    ``inject`` plays bytes the screen would send; everything the host writes is collected
    in :attr:`sent` and can be read back as decoded instructions with :meth:`commands`.
    """

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent = bytearray()
        self.is_open = False
        self.open_count = 0
        self.fail_next_opens = 0
        self._wake = asyncio.Event()

    async def open(self) -> None:
        if self.fail_next_opens > 0:
            self.fail_next_opens -= 1
            raise TransportError("simulated open failure")
        self.is_open = True
        self.open_count += 1
        self._inbound = asyncio.Queue()

    async def read(self) -> bytes:
        if not self.is_open:
            return b""
        return await self._inbound.get()

    async def write(self, data: bytes) -> None:
        if not self.is_open:
            raise TransportError("memory transport closed")
        self.sent += data
        self._wake.set()

    async def close(self) -> None:
        self.is_open = False
        self._inbound.put_nowait(b"")

    # -- test helpers ---------------------------------------------------------------
    def inject(self, data: bytes) -> None:
        self._inbound.put_nowait(data)

    def drop_link(self) -> None:
        """Simulate the cable being pulled: the reader sees EOF."""
        self.is_open = False
        self._inbound.put_nowait(b"")

    def commands(self, encoding: str = "utf-8") -> list[str]:
        raw = bytes(self.sent)
        parts = raw.split(TERMINATOR)
        return [p.decode(encoding, errors="replace") for p in parts if p]

    def clear(self) -> None:
        self.sent.clear()

    async def wait_for_command(self, predicate, timeout: float = 2.0) -> str:  # noqa: ANN001
        """Block until a written instruction satisfies ``predicate`` (test convenience)."""
        async with asyncio.timeout(timeout):
            while True:
                for cmd in self.commands():
                    if predicate(cmd):
                        return cmd
                self._wake.clear()
                await self._wake.wait()
