"""Moonraker JSON-RPC websocket client with auto-reconnect and object subscription."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Callable, Iterable
from typing import Any

from . import __version__
from .state import DEFAULT_OBJECTS, PrinterState

log = logging.getLogger(__name__)


#: Upper bound for one G-code script (heat-up waits, PID tuning and meshes take minutes).
GCODE_TIMEOUT = 3600.0


class MoonrakerError(Exception):
    """Moonraker answered a request with a JSON-RPC error, or the link dropped."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class MoonrakerClient:
    def __init__(
        self,
        url: str,
        state: PrinterState,
        *,
        api_key: str = "",
        objects: Iterable[str] = (),
        request_timeout: float = 10.0,
        backoff_min: float = 1.0,
        backoff_max: float = 30.0,
        klippy_poll: float = 3.0,
    ) -> None:
        self.url = url
        self.state = state
        self.api_key = api_key
        self.wanted_objects = list(dict.fromkeys([*DEFAULT_OBJECTS, *objects]))
        self.request_timeout = request_timeout
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.klippy_poll = klippy_poll
        self.connected = asyncio.Event()
        self.on_gcode_response: Callable[[str], None] | None = None
        self._ws: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscribe_lock = asyncio.Lock()

    # ------------------------------------------------------------------ supervisor
    async def run(self) -> None:
        """Connect, serve, and reconnect with exponential backoff until cancelled."""
        from websockets.asyncio.client import connect

        delay = self.backoff_min
        headers = {"X-Api-Key": self.api_key} if self.api_key else None
        while True:
            try:
                async with connect(
                    self.url,
                    additional_headers=headers,
                    open_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    delay = self.backoff_min
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # OSError, websockets.WebSocketException, timeouts
                log.warning("moonraker link down: %s", exc)
            finally:
                self._ws = None
                self.connected.clear()
                self.state.set_connection(False)
                self._fail_pending(MoonrakerError("connection to Moonraker lost"))
            wait = delay * random.uniform(0.8, 1.2)
            log.info("reconnecting to Moonraker in %.1fs", wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, self.backoff_max)

    async def _session(self, ws: Any) -> None:
        reader = asyncio.create_task(self._read_loop(ws))
        try:
            self.state.set_connection(True)
            self.connected.set()
            log.info("connected to Moonraker at %s", self.url)
            await self._identify()
            init = asyncio.create_task(self._watch_klippy())
            try:
                await reader
            finally:
                init.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await init
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    async def _identify(self) -> None:
        try:
            await self.call(
                "server.connection.identify",
                {
                    "client_name": "klarity",
                    "version": __version__,
                    "type": "other",
                    "url": "https://github.com/MakerViking/eitri",
                },
            )
        except MoonrakerError as exc:
            # Identification is optional on trusted-client setups.
            log.debug("identify failed: %s", exc)

    async def _watch_klippy(self) -> None:
        """Subscribe as soon as Klipper is ready; poll until then (notifications can be missed)."""
        while True:
            try:
                info = await self.call("server.info")
                kstate = info.get("klippy_state", "disconnected")
                self.state.set_klippy(kstate)
                if kstate == "ready":
                    if not self.state.data:
                        await self.subscribe()
                    return
            except MoonrakerError as exc:
                log.debug("server.info failed: %s", exc)
            await asyncio.sleep(self.klippy_poll)

    async def subscribe(self) -> None:
        async with self._subscribe_lock:
            listing = await self.call("printer.objects.list")
            available = set(listing.get("objects", []))
            objects = {name: None for name in self.wanted_objects if name in available}
            missing = [n for n in self.wanted_objects if n not in available]
            if missing:
                log.info("not subscribing to absent printer objects: %s", ", ".join(missing))
            reply = await self.call("printer.objects.subscribe", {"objects": objects})
            self.state.apply(reply.get("status", {}))
            self.state.set_klippy("ready")

    # ------------------------------------------------------------------ receive path
    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                log.warning("non-JSON frame from Moonraker ignored")
                continue
            self._handle(msg)

    def _handle(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(
                    MoonrakerError(err.get("message", "unknown error"), err.get("code"))
                )
            else:
                fut.set_result(msg["result"])
            return
        method = msg.get("method", "")
        params = msg.get("params") or []
        if method == "notify_status_update" and params:
            self.state.apply(params[0])
        elif method == "notify_klippy_ready":
            self.state.set_klippy("ready")
            asyncio.get_running_loop().create_task(self._resubscribe())
        elif method == "notify_klippy_shutdown":
            self.state.set_klippy("shutdown")
        elif method == "notify_klippy_disconnected":
            self.state.set_klippy("disconnected")
        elif method == "notify_gcode_response" and params:
            line = str(params[0])
            self.state.add_gcode_response(line)
            if self.on_gcode_response:
                self.on_gcode_response(line)

    async def _resubscribe(self) -> None:
        try:
            await self.subscribe()
        except MoonrakerError as exc:
            log.warning("re-subscribe after klippy ready failed: %s", exc)

    # ------------------------------------------------------------------ requests
    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = -1
    ) -> Any:
        """Issue a JSON-RPC request.  ``timeout=None`` waits indefinitely (long G-code)."""
        ws = self._ws
        if ws is None:
            raise MoonrakerError("not connected to Moonraker")
        if timeout == -1:
            timeout = self.request_timeout
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        request: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params:
            request["params"] = params
        try:
            await ws.send(json.dumps(request))
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError:
            raise MoonrakerError(f"{method} timed out after {timeout}s") from None
        except MoonrakerError:
            raise
        except Exception as exc:
            raise MoonrakerError(f"{method} failed: {exc}") from exc
        finally:
            self._pending.pop(rid, None)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # -- convenience wrappers ----------------------------------------------------------
    async def gcode(self, script: str, timeout: float | None = GCODE_TIMEOUT) -> None:
        """Run G-code; returns when Klipper has finished it (can be minutes for G28/G29).

        The wait is generous but finite so a wedged Klipper cannot pin a pending request
        forever; pass ``timeout=None`` only for a caller that can cancel.
        """
        await self.call("printer.gcode.script", {"script": script}, timeout=timeout)

    async def pause(self) -> None:
        await self.call("printer.print.pause")

    async def resume(self) -> None:
        await self.call("printer.print.resume")

    async def cancel(self) -> None:
        await self.call("printer.print.cancel")

    async def start_print(self, filename: str) -> None:
        await self.call("printer.print.start", {"filename": filename})

    async def emergency_stop(self) -> None:
        await self.call("printer.emergency_stop")

    async def firmware_restart(self) -> None:
        await self.call("printer.firmware_restart")

    async def restart(self) -> None:
        await self.call("printer.restart")
