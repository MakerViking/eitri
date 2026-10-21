"""Minimal Moonraker client: gcode scripts, object queries, and a websocket tap
for console output (so long calibrations can stream their messages)."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp


class MoonrakerError(Exception):
    pass


class Moonraker:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self._http: aiohttp.ClientSession | None = None

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            # Calibrations (SHAPER_CALIBRATE, mesh) can run for minutes: no total timeout.
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=3, sock_connect=3))
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        try:
            async with self.http.request(method, self.base + path, **kw) as r:
                body = await r.json(content_type=None)
                if r.status >= 400:
                    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
                    raise MoonrakerError(err.get("message") or f"Moonraker returned HTTP {r.status}")
                return body.get("result", body) if isinstance(body, dict) else body
        except aiohttp.ClientConnectionError as exc:
            raise MoonrakerError("Moonraker is not reachable - is moonraker.service running?") from exc

    async def printer_info(self) -> dict[str, Any]:
        return await self._json("GET", "/printer/info")

    async def query(self, objects: dict[str, list[str] | None]) -> dict[str, Any]:
        params = "&".join(f"{o}" + (f"={','.join(f)}" if f else "") for o, f in objects.items())
        res = await self._json("GET", f"/printer/objects/query?{params}")
        return res.get("status", {})

    async def gcode(self, script: str) -> None:
        """Run a script; returns when Klipper has finished it (can take minutes)."""
        await self._json("POST", "/printer/gcode/script", json={"script": script})

    async def print_state(self) -> str:
        st = await self.query({"print_stats": ["state"]})
        return st.get("print_stats", {}).get("state", "standby")

    async def ready(self) -> tuple[bool, str]:
        try:
            info = await self.printer_info()
        except MoonrakerError as exc:
            return False, str(exc)
        if info.get("state") != "ready":
            return False, info.get("state_message") or f"Klipper is {info.get('state')}"
        return True, ""

    @contextlib.asynccontextmanager
    async def console(self, on_line: Callable[[str], None]) -> AsyncIterator[None]:
        """Forward `notify_gcode_response` lines to `on_line` while the block runs."""
        ws_url = self.base.replace("http", "ws", 1) + "/websocket"
        stop = asyncio.Event()

        async def pump() -> None:
            try:
                async with self.http.ws_connect(ws_url, heartbeat=20) as ws:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        if data.get("method") == "notify_gcode_response":
                            for line in data.get("params", []):
                                on_line(str(line))
                        if stop.is_set():
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass  # console tap is best-effort; the HTTP call still reports success/failure

        task = asyncio.create_task(pump())
        await asyncio.sleep(0.05)   # let the socket subscribe before the script starts
        try:
            yield
        finally:
            await asyncio.sleep(0.1)   # drain trailing responses
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
