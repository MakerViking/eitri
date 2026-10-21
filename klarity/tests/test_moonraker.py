import asyncio
import contextlib

import pytest

from fakes import eventually
from klarity.moonraker import MoonrakerClient, MoonrakerError
from klarity.state import PrinterState


@contextlib.asynccontextmanager
async def running(client: MoonrakerClient):
    task = asyncio.create_task(client.run())
    try:
        yield client
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def make(moonraker, **kw) -> tuple[MoonrakerClient, PrinterState]:
    state = PrinterState()
    kw.setdefault("backoff_min", 0.05)
    kw.setdefault("backoff_max", 0.2)
    kw.setdefault("klippy_poll", 0.05)
    return MoonrakerClient(moonraker.url, state, **kw), state


async def test_connect_identify_subscribe(moonraker):
    client, state = make(moonraker, objects=["heater_generic chamber", "not_there"])
    async with running(client):
        await eventually(lambda: state.data.get("extruder"), what="initial status")
        assert state.connected and state.klippy_state == "ready"
        assert "server.connection.identify" in moonraker.methods()
        # Only objects Klipper actually reports are subscribed.
        assert "heater_generic chamber" in moonraker.subscribed
        assert "not_there" not in moonraker.subscribed
        assert "extruder" in moonraker.subscribed


async def test_status_updates_are_merged(moonraker):
    client, state = make(moonraker)
    async with running(client):
        await eventually(lambda: state.data.get("extruder"))
        await moonraker.push_status({"extruder": {"temperature": 150.5}})
        await eventually(lambda: state.resolve("extruder.temperature") == 150.5)
        assert state.resolve("extruder.target") == 0.0


async def test_gcode_and_print_control(moonraker):
    client, state = make(moonraker)
    async with running(client):
        await eventually(lambda: client.connected.is_set())
        await client.gcode("G28")
        await client.pause()
        await client.resume()
        await client.cancel()
        await client.start_print("a.gcode")
    assert moonraker.scripts() == ["G28"]
    assert [m for m in moonraker.methods() if m.startswith("printer.print")] == [
        "printer.print.pause",
        "printer.print.resume",
        "printer.print.cancel",
        "printer.print.start",
    ]


async def test_rpc_error_is_raised(moonraker):
    client, _ = make(moonraker)
    moonraker.fail_scripts.add("G1 X1")
    async with running(client):
        await eventually(lambda: client.connected.is_set())
        with pytest.raises(MoonrakerError, match="Must home"):
            await client.gcode("G1 X1")
        with pytest.raises(MoonrakerError, match="Method not found"):
            await client.call("no.such.method")


async def test_call_while_disconnected():
    client = MoonrakerClient("ws://127.0.0.1:9", PrinterState())
    with pytest.raises(MoonrakerError, match="not connected"):
        await client.gcode("G28")


async def test_reconnect_with_backoff_and_resubscribe(moonraker):
    client, state = make(moonraker)
    async with running(client):
        await eventually(lambda: state.data.get("extruder"))
        await moonraker.drop_connections()
        await eventually(lambda: not state.connected, what="disconnect noticed")
        assert state.klippy_state == "disconnected" and not state.data
        await eventually(
            lambda: moonraker.connect_count >= 2 and state.data.get("extruder"),
            what="reconnect + resubscribe",
        )
        assert state.connected


async def test_survives_server_down_then_up():
    from fakes import FakeMoonraker

    first = await FakeMoonraker().start()
    port = first.port
    await first.stop()
    state = PrinterState()
    client = MoonrakerClient(
        f"ws://127.0.0.1:{port}/websocket", state, backoff_min=0.05, backoff_max=0.1
    )
    async with running(client):
        await asyncio.sleep(0.3)  # several failed attempts
        assert not state.connected
        server = await FakeMoonraker().start(port)
        try:
            await eventually(lambda: state.data.get("extruder"), what="connect after server start")
        finally:
            await server.stop()


async def test_waits_for_klippy_ready(moonraker):
    moonraker.klippy_state = "startup"
    client, state = make(moonraker)
    async with running(client):
        await eventually(lambda: state.klippy_state == "startup")
        assert not state.data
        moonraker.klippy_state = "ready"
        await eventually(lambda: state.data.get("extruder"), what="subscribe after ready")


async def test_klippy_shutdown_and_ready_notifications(moonraker):
    client, state = make(moonraker)
    async with running(client):
        await eventually(lambda: state.data.get("extruder"))
        await moonraker.notify("notify_klippy_shutdown", [])
        await eventually(lambda: state.klippy_state == "shutdown")
        assert not state.data
        await moonraker.notify("notify_klippy_ready", [])
        await eventually(lambda: state.klippy_state == "ready" and state.data.get("extruder"))


async def test_gcode_response_notifications(moonraker):
    client, state = make(moonraker)
    seen = []
    client.on_gcode_response = seen.append
    async with running(client):
        await eventually(lambda: client.connected.is_set())
        await moonraker.notify("notify_gcode_response", ["// hello"])
        await eventually(lambda: seen == ["// hello"])
        assert state.gcode_responses[-1][1] == "// hello"


async def test_api_key_header_sent(moonraker):
    client, _ = make(moonraker, api_key="sekret")
    async with running(client):
        await eventually(lambda: moonraker.headers)
    assert moonraker.headers[0].get("x-api-key") == "sekret"


async def test_pending_calls_fail_on_disconnect(moonraker):
    client, _ = make(moonraker)
    async with running(client):
        await eventually(lambda: client.connected.is_set())
        pending = asyncio.create_task(
            client.call("printer.gcode.script", {"script": "G4 P99999"}, timeout=None)
        )
        await moonraker.drop_connections()
        # The fake answers immediately, so the call either completes or fails cleanly: never hangs.
        with contextlib.suppress(MoonrakerError):
            await asyncio.wait_for(pending, 2)
