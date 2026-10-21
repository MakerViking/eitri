"""State -> screen push loop: change-diffing, coalescing and rate limiting."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .protocol import Cmd, ProtocolError
from .screen import Screen, ScreenError
from .screenmap import ScreenMap
from .state import PrinterState

log = logging.getLogger(__name__)


class TokenBucket:
    """Classic token bucket; ``acquire`` sleeps until a token is available."""

    def __init__(
        self,
        rate: float,
        burst: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.rate = rate
        self.capacity = burst
        self._tokens = burst
        self._clock = clock
        self._sleep = sleep
        self._stamp = clock()

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await self._sleep((1 - self._tokens) / self.rate)  # type: ignore[misc]


class Pusher:
    def __init__(
        self,
        screen: Screen,
        state: PrinterState,
        smap: ScreenMap,
        *,
        min_interval: float = 0.25,
        max_commands_per_second: float = 60.0,
        full_refresh: float = 30.0,
    ) -> None:
        self.screen = screen
        self.state = state
        self.smap = smap
        self.min_interval = min_interval
        self.full_refresh = full_refresh
        self._bucket = TokenBucket(max_commands_per_second, max(5.0, max_commands_per_second / 4))
        self._cache: dict[str, int | str] = {}
        self._dirty = asyncio.Event()
        self.passes = 0
        self.pushed = 0
        state.subscribe(self.kick)

    def kick(self) -> None:
        """Mark state as changed; the loop coalesces bursts into one pass."""
        self._dirty.set()

    def invalidate(self) -> None:
        """Forget what the panel shows (page change, reconnect) and repaint."""
        self._cache.clear()
        self.kick()

    async def run(self) -> None:
        last = 0.0
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), self.full_refresh)
            except TimeoutError:
                self._cache.clear()  # periodic heal against corrupted/lost writes
            wait = last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._dirty.clear()
            last = time.monotonic()
            try:
                await self.push_once()
            except ScreenError as exc:
                log.debug("push aborted: %s", exc)

    async def push_once(self) -> int:
        """Send every changed binding of the current page.  Returns commands sent."""
        if not self.screen.connected.is_set():
            return 0
        sent = 0
        self.passes += 1
        for item in self.smap.push_items(self.screen.current_page):
            value = item.binding.render(self.state)
            if value is None or self._cache.get(item.key) == value:
                continue
            attr = item.binding.attr
            try:
                command = (
                    Cmd.vis(item.obj, bool(value))
                    if attr == "vis"
                    else Cmd.set_attr(item.obj, attr, value)
                )
            except ProtocolError as exc:
                log.error("cannot build command for %s.%s: %s", item.obj, attr, exc)
                self._cache[item.key] = value  # do not retry a permanently invalid binding
                continue
            await self._bucket.acquire()
            await self.screen.send(command)
            self._cache[item.key] = value
            sent += 1
        self.pushed += sent
        return sent
