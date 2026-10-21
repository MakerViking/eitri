"""A small fake Moonraker, started by `--mock`, so the *real* Moonraker client
and calibration code run unchanged on a developer machine.  It models just
enough of Klipper: homing, manual-probe (Z-offset), resonance testing, bed mesh."""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

from aiohttp import WSMsgType, web

from ..settings import Settings


class FakePrinter:
    def __init__(self, speed: float):
        self.speed = speed
        self.homed = False
        self.z = 10.0
        self.probe_active = False
        self.state = "standby"
        self.klipper_state = "ready"
        self.mesh: list[list[float]] | None = None
        self.profile = ""
        self.saved_pending = False
        self.sockets: set[web.WebSocketResponse] = set()
        self.log: list[str] = []

    async def sleep(self, s: float) -> None:
        if self.speed > 0:
            await asyncio.sleep(s / self.speed)

    async def say(self, *lines: str) -> None:
        for line in lines:
            self.log.append(line)
            for ws in list(self.sockets):
                if not ws.closed:
                    await ws.send_str(json.dumps({"jsonrpc": "2.0", "method": "notify_gcode_response", "params": [line]}))

    def make_mesh(self) -> list[list[float]]:
        rng = random.Random(4)
        n = 7
        rows = []
        for y in range(n):
            row = []
            for x in range(n):
                u, v = x / (n - 1) - 0.5, y / (n - 1) - 0.5
                # a gentle bowl with a tilt, plus a little noise: looks like a real PEI bed
                row.append(round(0.12 * (u * u + v * v) * 4 - 0.05 * u + 0.03 * v + rng.uniform(-0.012, 0.012) - 0.02, 4))
            rows.append(row)
        return rows

    async def run(self, script: str) -> None:
        for line in [s.strip() for s in script.replace("\r", "").split("\n") if s.strip()]:
            await self.run_one(line)

    async def run_one(self, line: str) -> None:
        cmd = line.split()[0].upper()
        args = dict(re.findall(r"(\w+)=(\S+)", line.upper()))
        if cmd == "G28":
            await self.say("// Homing X, Y, Z")
            await self.sleep(2.0)
            self.homed, self.z = True, 10.0
            await self.say("ok")
        elif cmd in ("G1", "G0"):
            if not self.homed:
                raise web.HTTPBadRequest(text=json.dumps({"error": {"message": "Must home axis first"}}), content_type="application/json")
            m = re.search(r"Z(-?[\d.]+)", line.upper())
            if m:
                self.z = float(m.group(1))
            await self.sleep(0.4)
        elif cmd in ("G90", "G91", "M400"):
            pass
        elif cmd == "PROBE_CALIBRATE":
            if not self.homed:
                raise web.HTTPBadRequest(text=json.dumps({"error": {"message": "Must home before probe"}}), content_type="application/json")
            self.probe_active, self.z = True, 3.0
            await self.say("// Z position: 3.000", "// Starting manual Z probe. Use TESTZ to adjust position, then ACCEPT or ABORT")
        elif cmd == "TESTZ":
            if not self.probe_active:
                raise web.HTTPBadRequest(text=json.dumps({"error": {"message": "Manual probe not active"}}), content_type="application/json")
            self.z = round(self.z + float(args.get("Z", "0")), 4)
            await self.say(f"// Z position: {self.z:.3f}")
        elif cmd == "ACCEPT":
            self.probe_active = False
            self.saved_pending = True
            await self.say(f"// probe: z_offset: {self.z:.3f}",
                           "// The SAVE_CONFIG command will update the printer config file with the above and restart the printer.")
        elif cmd == "ABORT":
            self.probe_active = False
            await self.say("// Manual probe aborted")
        elif cmd == "MEASURE_AXES_NOISE":
            await self.say("// Measuring noise ...")
            await self.sleep(2.5)
            await self.say("// Axes noise for xyz-axis accelerometer: 3.168 (x), 4.457 (y), 11.214 (z)")
        elif cmd == "SHAPER_CALIBRATE":
            await self.say("// Calibrating X axis ...")
            await self.sleep(3.0)
            await self.say(
                "// Fitted shaper 'zv' frequency = 58.6 Hz (vibrations = 7.1%, smoothing ~= 0.083)",
                "// Fitted shaper 'mzv' frequency = 53.8 Hz (vibrations = 1.9%, smoothing ~= 0.135)",
                "// Fitted shaper 'ei' frequency = 62.0 Hz (vibrations = 0.8%, smoothing ~= 0.152)",
                "// Recommended shaper_type_x = mzv, shaper_freq_x = 53.8 Hz",
                "// Calibrating Y axis ...",
            )
            await self.sleep(3.0)
            await self.say(
                "// Fitted shaper 'zv' frequency = 41.2 Hz (vibrations = 12.4%, smoothing ~= 0.167)",
                "// Fitted shaper 'mzv' frequency = 40.4 Hz (vibrations = 4.2%, smoothing ~= 0.239)",
                "// Fitted shaper 'ei' frequency = 48.6 Hz (vibrations = 2.3%, smoothing ~= 0.246)",
                "// Recommended shaper_type_y = ei, shaper_freq_y = 48.6 Hz",
                "// The SAVE_CONFIG command will update the printer config file with these parameters and restart the printer.",
            )
            self.saved_pending = True
        elif cmd == "BED_MESH_CALIBRATE":
            if not self.homed:
                raise web.HTTPBadRequest(text=json.dumps({"error": {"message": "Must home axis first"}}), content_type="application/json")
            await self.say("// Generating bed mesh ...")
            await self.sleep(5.0)
            self.mesh, self.profile = self.make_mesh(), "default"
            await self.say("// Mesh Bed Leveling Complete", "// Bed Mesh state has been saved to profile [default]")
        elif cmd == "BED_MESH_PROFILE":
            self.saved_pending = True
            await self.say("// Bed Mesh state has been saved to profile")
        elif cmd == "SAVE_CONFIG":
            self.saved_pending = False
            self.klipper_state = "startup"
            await self.say("// Saving configuration, restarting Klipper")
            asyncio.get_running_loop().call_later(max(0.3, 1.5 / self.speed) if self.speed else 0, self._ready)
        else:
            await self.say("ok")

    def _ready(self) -> None:
        self.klipper_state = "ready"


def build_app(speed: float) -> web.Application:
    pr = FakePrinter(speed)

    async def info(_r):
        return web.json_response({"result": {"state": pr.klipper_state, "state_message": "Printer is ready" if pr.klipper_state == "ready" else "Klipper is restarting",
                                             "hostname": "eitri", "software_version": "v0.13.0-813-ga08fc79", "klipper_path": "/data/firmware/klipper"}})

    async def query(r):
        st: dict[str, Any] = {}
        if "print_stats" in r.query:
            st["print_stats"] = {"state": pr.state}
        if "manual_probe" in r.query:
            st["manual_probe"] = {"is_active": pr.probe_active, "z_position": pr.z,
                                  "z_position_lower": round(pr.z - 0.05, 3), "z_position_upper": round(pr.z + 0.05, 3)}
        if "toolhead" in r.query:
            st["toolhead"] = {"homed_axes": "xyz" if pr.homed else "", "position": [150, 150, pr.z, 0]}
        if "bed_mesh" in r.query:
            m = pr.mesh
            st["bed_mesh"] = {"profile_name": pr.profile, "mesh_min": [10, 10], "mesh_max": [290, 290],
                              "probed_matrix": m or [[]], "mesh_matrix": m or [[]]}
        if "configfile" in r.query:
            st["configfile"] = {"save_config_pending": pr.saved_pending}
        return web.json_response({"result": {"status": st, "eventtime": 1.0}})

    async def gcode(r):
        body = await r.json()
        await pr.run(body.get("script", ""))
        return web.json_response({"result": "ok"})

    async def ws(r):
        sock = web.WebSocketResponse()
        await sock.prepare(r)
        pr.sockets.add(sock)
        try:
            async for msg in sock:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            pr.sockets.discard(sock)
        return sock

    async def control(r):             # test/screenshot hook: POST {"state": "printing"}
        body = await r.json()
        pr.state = body.get("state", pr.state)
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes([
        web.get("/printer/info", info), web.get("/printer/objects/query", query),
        web.post("/printer/gcode/script", gcode), web.get("/websocket", ws),
        web.post("/_mock/state", control),
    ])
    return app


async def start(settings: Settings) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(build_app(settings.mock_speed), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}"
