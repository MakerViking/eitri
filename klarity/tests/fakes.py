"""Test doubles: a fake Moonraker websocket server and small async helpers."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

DEFAULT_STATUS: dict[str, dict[str, Any]] = {
    "webhooks": {"state": "ready", "state_message": "Printer is ready"},
    "print_stats": {
        "state": "standby",
        "filename": "",
        "print_duration": 0.0,
        "message": "",
        "info": {"current_layer": None, "total_layer": None},
    },
    "virtual_sdcard": {"progress": 0.0, "is_active": False},
    "display_status": {"progress": 0.0, "message": None},
    "toolhead": {"position": [10.0, 20.0, 5.0, 0.0], "homed_axes": "xyz"},
    "extruder": {"temperature": 25.0, "target": 0.0},
    "heater_bed": {"temperature": 24.0, "target": 0.0},
    "heater_generic chamber": {"temperature": 26.0, "target": 0.0},
    "gcode_move": {"speed_factor": 1.0, "extrude_factor": 1.0},
    "idle_timeout": {"state": "Idle"},
    "pause_resume": {"is_paused": False},
}


class FakeMoonraker:
    def __init__(self, status: dict[str, dict[str, Any]] | None = None) -> None:
        self.status = copy.deepcopy(status or DEFAULT_STATUS)
        self.klippy_state = "ready"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.subscribed: list[str] = []
        self.fail_scripts: set[str] = set()
        self.connections: set[ServerConnection] = set()
        self.connect_count = 0
        self.server: Server | None = None
        self.port = 0
        self.headers: list[dict[str, str]] = []

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/websocket"

    async def start(self, port: int = 0) -> FakeMoonraker:
        self.server = await serve(self._handler, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def drop_connections(self) -> None:
        for ws in list(self.connections):
            await ws.close()

    def scripts(self) -> list[str]:
        return [p["script"] for m, p in self.calls if m == "printer.gcode.script"]

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    async def push_status(self, delta: dict[str, dict[str, Any]]) -> None:
        for obj, fields in delta.items():
            self.status.setdefault(obj, {}).update(fields)
        await self.notify("notify_status_update", [delta, time.monotonic()])

    async def notify(self, method: str, params: list[Any]) -> None:
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        for ws in list(self.connections):
            await ws.send(msg)

    async def _handler(self, ws: ServerConnection) -> None:
        self.connections.add(ws)
        self.connect_count += 1
        self.headers.append(dict(ws.request.headers))
        try:
            async for raw in ws:
                req = json.loads(raw)
                method, params = req["method"], req.get("params") or {}
                self.calls.append((method, params))
                try:
                    result = self._dispatch(method, params)
                    reply = {"jsonrpc": "2.0", "result": result, "id": req["id"]}
                except RuntimeError as exc:
                    reply = {
                        "jsonrpc": "2.0",
                        "error": {"code": -32601, "message": str(exc)},
                        "id": req["id"],
                    }
                await ws.send(json.dumps(reply))
        except ConnectionClosed:
            pass
        finally:
            self.connections.discard(ws)

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "server.connection.identify":
            return {"connection_id": 1}
        if method == "server.info":
            return {"klippy_state": self.klippy_state, "klippy_connected": True}
        if method == "printer.objects.list":
            return {"objects": list(self.status)}
        if method == "printer.objects.subscribe":
            self.subscribed = list(params["objects"])
            return {
                "status": {k: v for k, v in self.status.items() if k in params["objects"]},
                "eventtime": 1.0,
            }
        if method == "printer.gcode.script":
            if params["script"] in self.fail_scripts:
                raise RuntimeError("Must home axis first")
            return "ok"
        if method in (
            "printer.print.pause",
            "printer.print.resume",
            "printer.print.cancel",
            "printer.print.start",
            "printer.emergency_stop",
        ):
            return "ok"
        raise RuntimeError(f"Method not found: {method}")


async def eventually(predicate: Callable[[], Any], timeout: float = 3.0, what: str = "") -> Any:
    """Poll until ``predicate`` returns something truthy."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or predicate}")
        await asyncio.sleep(0.01)
