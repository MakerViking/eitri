"""The screen link: transport + frame parser + reconnect + request/response helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from .protocol import (
    FILE_TRANSFER_DONE,
    FILE_TRANSFER_READY,
    Cmd,
    Event,
    FrameParser,
    Garbage,
    MarkerEvent,
    NumberEvent,
    PageEvent,
    ProtocolError,
    StringEvent,
    TouchEvent,
    encode,
)
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

# Nextion "invalid variable name or attribute" return code, the reply to a bad ``get``.
STATUS_INVALID_VARIABLE = 0x1A


#: After a ``get`` times out, a number/string arriving this soon is most likely its late reply.
LATE_REPLY_GRACE = 2.0


class ScreenError(RuntimeError):
    """The panel rejected a request or did not answer."""


class ScreenTimeout(ScreenError):
    """The panel did not answer in time."""


class Screen:
    """Owns the serial link.  Decoded events are delivered through :attr:`events`."""

    def __init__(
        self,
        transport: Transport,
        *,
        encoding: str = "utf-8",
        quote_style: str = "backslash",
        frame_timeout: float = 0.5,
        reconnect_min: float = 0.5,
        reconnect_max: float = 15.0,
        single_byte_ids: tuple[int, ...] = (),
    ) -> None:
        self.transport = transport
        self.encoding = encoding
        self.frame_timeout = frame_timeout
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        Cmd.quote_style = quote_style
        self.parser = FrameParser(single_byte_ids)
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.connected = asyncio.Event()
        self.current_page: int | None = None
        self.on_connect: Callable[[], Awaitable[None]] | None = None
        self.on_page: Callable[[int | None], None] | None = None
        #: True for a ``0x71`` frame that is the stock keypad's unsolicited report (set by the
        #: daemon from the map).  Such a frame has the same bytes as a ``get`` reply, so it
        #: must never be handed to a pending ``get`` waiter.
        self.is_keypad_frame: Callable[[NumberEvent], bool] | None = None
        self._late_reply_until = 0.0
        self._waiters: list[tuple[Callable[[Event], bool], asyncio.Future[Event]]] = []
        self._get_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        """Open, read and (on failure) reopen the link until cancelled."""
        delay = self.reconnect_min
        while True:
            try:
                await self.transport.open()
            except TransportError as exc:
                log.warning("screen link unavailable: %s (retry in %.1fs)", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max)
                continue
            delay = self.reconnect_min
            self.parser = FrameParser(self.parser.single_byte_ids)  # fresh framing state per link
            self.connected.set()
            log.info("screen link up")
            hook = None
            if self.on_connect is not None:
                hook = asyncio.create_task(self._run_connect_hook())
            try:
                await self._read_loop()
            except TransportError as exc:
                log.warning("screen link lost: %s", exc)
            finally:
                self.connected.clear()
                if hook is not None:
                    hook.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await hook
                self._fail_waiters(ScreenError("screen link closed"))
                with contextlib.suppress(Exception):
                    await self.transport.close()
            log.warning("screen link down, reconnecting")
            await asyncio.sleep(self.reconnect_min)

    async def _run_connect_hook(self) -> None:
        assert self.on_connect is not None
        try:
            await self.on_connect()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("on_connect hook failed")

    async def _read_loop(self) -> None:
        while True:
            timeout = self.frame_timeout if self.parser.pending else None
            try:
                chunk = await asyncio.wait_for(self.transport.read(), timeout)
            except TimeoutError:
                for event in self.parser.flush():
                    self._dispatch(event)
                continue
            if not chunk:
                return
            for event in self.parser.feed(chunk):
                self._dispatch(event)

    # ------------------------------------------------------------------- inbound
    def _dispatch(self, event: Event) -> None:
        if isinstance(event, TouchEvent | PageEvent):
            self.set_page(event.page)

        for predicate, fut in list(self._waiters):
            if fut.done():
                continue
            if predicate(event):
                fut.set_result(event)
                self._waiters = [w for w in self._waiters if w[1] is not fut]
                return
            # Only a get-waiter cares about "invalid variable"; fail it fast, not by timeout.
            if (
                isinstance(event, MarkerEvent)
                and event.code == STATUS_INVALID_VARIABLE
                and getattr(predicate, "is_get_waiter", False)
            ):
                fut.set_exception(ScreenError("screen reported invalid variable"))
                self._waiters = [w for w in self._waiters if w[1] is not fut]
                return
        late = time.monotonic() < self._late_reply_until
        if (
            late
            and isinstance(event, NumberEvent | StringEvent)
            and not (isinstance(event, NumberEvent) and self._keypad(event))
        ):
            log.debug("dropped late reply to a timed-out get: %r", event)
            return
        if isinstance(event, Garbage):
            log.debug("dropped %d bytes: %s (%s)", len(event.raw), event.raw.hex(" "), event.reason)
        self.events.put_nowait(event)

    def _keypad(self, event: NumberEvent) -> bool:
        return self.is_keypad_frame is not None and self.is_keypad_frame(event)

    def set_page(self, page: int | None) -> None:
        if page != self.current_page:
            self.current_page = page
            if self.on_page is not None:
                try:
                    self.on_page(page)
                except Exception:
                    # A callback bug must not break the read loop and drop the link.
                    log.exception("on_page callback failed")

    def _fail_waiters(self, exc: Exception) -> None:
        for _, fut in self._waiters:
            if not fut.done():
                fut.set_exception(exc)
        self._waiters.clear()

    def expect(self, predicate: Callable[[Event], bool]) -> asyncio.Future[Event]:
        """Register interest in the next matching event *before* sending the request."""
        fut: asyncio.Future[Event] = asyncio.get_running_loop().create_future()
        self._waiters.append((predicate, fut))
        return fut

    def release(self, fut: asyncio.Future[Event]) -> None:
        """Drop a waiter and mark any stored exception as retrieved."""
        self._waiters = [w for w in self._waiters if w[1] is not fut]
        if not fut.cancel() and not fut.cancelled():
            fut.exception()

    async def resolve(self, fut: asyncio.Future[Event], timeout: float) -> Event:
        try:
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError:
            raise ScreenTimeout("timed out waiting for the screen") from None
        finally:
            self.release(fut)

    # ------------------------------------------------------------------ outbound
    async def send(self, command: str) -> None:
        """Send one instruction.  Raises :class:`ScreenError` if the link is down."""
        try:
            data = encode(command, self.encoding)
        except ProtocolError:
            log.error("refusing to send unsafe command %r", command)
            raise
        await self.send_raw(data)
        log.debug("-> %s", command)

    async def send_raw(self, data: bytes) -> None:
        async with self._write_lock:
            await self._write(data)

    async def _write(self, data: bytes) -> None:
        if not self.connected.is_set():
            raise ScreenError("screen not connected")
        try:
            await self.transport.write(data)
        except TransportError as exc:
            raise ScreenError(str(exc)) from exc

    async def goto(self, page: int | str) -> None:
        await self.send(Cmd.page(page))
        if isinstance(page, int):
            self.set_page(page)

    async def get(self, expr: str, timeout: float = 1.0) -> int | str:
        """Read an attribute from the panel.  Calls are serialised so replies cannot cross."""

        def is_reply(event: Event) -> bool:
            if isinstance(event, NumberEvent):
                return not self._keypad(event)
            return isinstance(event, StringEvent)

        is_reply.is_get_waiter = True  # type: ignore[attr-defined]
        async with self._get_lock:
            fut = self.expect(is_reply)
            try:
                await self.send(Cmd.get(expr))
                event = await self.resolve(fut, timeout)
            except ScreenTimeout:
                self._late_reply_until = time.monotonic() + LATE_REPLY_GRACE
                raise
            finally:
                self.release(fut)
        return event.text if isinstance(event, StringEvent) else event.value  # type: ignore[union-attr]

    async def send_file(
        self, path: str, data: bytes, *, chunk: int = 1024, timeout: float = 5.0
    ) -> None:
        """Transparent file write (``twfile``): announce, wait for 0xFE, stream, wait for 0xFD.

        TODO(hardware): confirm the TJC firmware speaks ``twfile`` (Nextion Enhanced) and its
        maximum chunk/ack behaviour on this panel; unverified against the stock .tft.
        """

        def marker(code: int) -> Callable[[Event], bool]:
            return lambda e: isinstance(e, MarkerEvent) and e.code == code

        ready = self.expect(marker(FILE_TRANSFER_READY))
        done = self.expect(marker(FILE_TRANSFER_DONE))
        # Hold the write lock for the whole transfer so no other instruction is interleaved
        # with the raw file bytes.
        async with self._write_lock:
            try:
                await self._write(encode(Cmd.twfile(path, len(data)), self.encoding))
                await self.resolve(ready, timeout)
                for start in range(0, len(data), chunk):
                    await self._write(data[start : start + chunk])
                await self.resolve(done, timeout + len(data) / 4000)
            finally:
                self.release(ready)
                self.release(done)
