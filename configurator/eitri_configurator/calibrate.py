"""Guided calibration: thin, safety-minded wrappers over Klipper G-code, issued
through Moonraker.  Nothing here moves the printer without an explicit request
from the user, and nothing runs while a print is active."""
from __future__ import annotations

import re
from typing import Any

from .jobs import Job
from .moonraker import Moonraker, MoonrakerError

NOISE_RE = re.compile(r"Axes noise.*?:\s*([\d.]+)\s*\(x\),\s*([\d.]+)\s*\(y\),\s*([\d.]+)\s*\(z\)", re.I)
FIT_RE = re.compile(r"Fitted shaper '(\w+)' frequency = ([\d.]+) Hz \(vibrations = ([\d.]+)%, smoothing ~= ([\d.]+)\)")
REC_RE = re.compile(r"Recommended shaper_type_(\w) = (\w+), shaper_freq_\w = ([\d.]+) Hz")

# TESTZ steps the UI may request (mm).  Large downward steps are only allowed far above the bed.
NUDGES = {-1.0, -0.5, -0.1, -0.05, -0.025, -0.01, 0.01, 0.025, 0.05, 0.1, 0.5, 1.0}
SAFE_DESCENT_ABOVE_MM = 1.0
NOISE_LIMIT = 100.0       # Klipper docs: 1-100 is typical; above it the accelerometer is suspect


class CalibrationError(Exception):
    pass


def parse_shaper(lines: list[str]) -> dict[str, Any]:
    axes: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    noise = None
    for line in lines:
        if m := NOISE_RE.search(line):
            noise = {"x": float(m[1]), "y": float(m[2]), "z": float(m[3])}
        elif m := FIT_RE.search(line):
            pending.append({"type": m[1], "freq": float(m[2]), "vibrations": float(m[3]), "smoothing": float(m[4])})
        elif m := REC_RE.search(line):
            axes[m[1]] = {"recommended": {"type": m[2], "freq": float(m[3])}, "fits": pending}
            pending = []
    return {"noise": noise, "axes": axes}


def mesh_stats(matrix: list[list[float]]) -> dict[str, float]:
    flat = [v for row in matrix for v in row]
    if not flat:
        return {}
    mean = sum(flat) / len(flat)
    var = sum((v - mean) ** 2 for v in flat) / len(flat)
    return {"min": min(flat), "max": max(flat), "range": max(flat) - min(flat), "mean": mean, "stddev": var ** 0.5}


class Calibrator:
    def __init__(self, mr: Moonraker):
        self.mr = mr

    async def preflight(self) -> None:
        ok, why = await self.mr.ready()
        if not ok:
            raise CalibrationError(why)
        state = await self.mr.print_state()
        if state in ("printing", "paused"):
            raise CalibrationError(f"A print is {state}. Calibration only runs while the printer is idle.")

    async def _homed(self) -> str:
        st = await self.mr.query({"toolhead": ["homed_axes"]})
        return st.get("toolhead", {}).get("homed_axes", "")

    async def _ensure_homed(self, job: Job, console: list[str]) -> None:
        if self._sorted(await self._homed()) == "xyz":
            return
        job.emit("step", id="home", title="Homing", status="start")
        await self.mr.gcode("G28")
        job.emit("step", id="home", title="Homing", status="done")

    @staticmethod
    def _sorted(s: str) -> str:
        return "".join(sorted(s))

    # --- Z offset ------------------------------------------------------------
    async def zoffset_start(self, job: Job) -> dict[str, Any]:
        await self.preflight()
        lines: list[str] = []
        sink = lambda l: (lines.append(l), job.emit("log", level="info", message=l))   # noqa: E731
        async with self.mr.console(sink):
            await self._ensure_homed(job, lines)
            # Lift first: starting a probe routine with the nozzle on the bed would trip
            # the probe's pre-triggered check (which we deliberately never suppress).
            job.emit("step", id="lift", title="Lifting the nozzle clear of the bed", status="start")
            await self.mr.gcode("G90\nG1 Z10 F600\nM400")
            job.emit("step", id="lift", title="Lifting the nozzle clear of the bed", status="done")
            await self.mr.gcode("PROBE_CALIBRATE")
        mp = (await self.mr.query({"manual_probe": None})).get("manual_probe", {})
        return {"ok": True, "active": bool(mp.get("is_active")), "z": mp.get("z_position")}

    async def zoffset_nudge(self, delta: float) -> dict[str, Any]:
        delta = round(float(delta), 3)
        if delta not in NUDGES:
            raise CalibrationError("Unsupported step size.")
        mp = (await self.mr.query({"manual_probe": None})).get("manual_probe", {})
        if not mp.get("is_active"):
            raise CalibrationError("Z-offset calibration is not active. Start it first.")
        if delta < -0.1 and (mp.get("z_position") or 0) <= SAFE_DESCENT_ABOVE_MM:
            raise CalibrationError("Close to the bed: use steps of 0.1 mm or smaller when lowering.")
        await self.mr.gcode(f"TESTZ Z={delta:+g}")
        mp = (await self.mr.query({"manual_probe": None})).get("manual_probe", {})
        return {"ok": True, "z": mp.get("z_position")}

    async def zoffset_accept(self) -> dict[str, Any]:
        await self.mr.gcode("ACCEPT")
        return {"ok": True, "save_pending": True}

    async def zoffset_abort(self) -> dict[str, Any]:
        await self.mr.gcode("ABORT")
        return {"ok": True}

    # --- Input shaper ----------------------------------------------------------
    async def shaper(self, job: Job) -> dict[str, Any]:
        await self.preflight()
        lines: list[str] = []
        def sink(l: str) -> None:
            lines.append(l)
            job.emit("log", level="info", message=l)
        async with self.mr.console(sink):
            await self._ensure_homed(job, lines)
            job.emit("step", id="noise", title="Checking accelerometer noise", status="start")
            await self.mr.gcode("MEASURE_AXES_NOISE")
            noise = parse_shaper(lines)["noise"]
            if noise and max(noise.values()) > NOISE_LIMIT:
                job.emit("step", id="noise", title="Checking accelerometer noise", status="fail")
                raise CalibrationError(
                    f"Accelerometer noise is {max(noise.values()):.0f} (should be under {NOISE_LIMIT:.0f}). "
                    "Check the sensor wiring and that nothing is vibrating (fans off), then try again.")
            job.emit("step", id="noise", title="Checking accelerometer noise", status="done", noise=noise)
            job.emit("step", id="resonance", title="Measuring resonances (about 2 minutes)", status="start")
            await self.mr.gcode("SHAPER_CALIBRATE")
            job.emit("step", id="resonance", title="Measuring resonances", status="done")
        result = parse_shaper(lines)
        if len(result["axes"]) < 2:
            raise CalibrationError("Klipper finished without recommending a shaper for both axes. See the log.")
        return {"ok": True, **result, "save_pending": True}

    # --- Bed mesh --------------------------------------------------------------
    async def mesh_state(self) -> dict[str, Any]:
        bm = (await self.mr.query({"bed_mesh": None})).get("bed_mesh", {})
        matrix = bm.get("probed_matrix") or []
        if not matrix or not matrix[0]:
            return {"ok": True, "matrix": None}
        return {"ok": True, "matrix": matrix, "min": bm.get("mesh_min"), "max": bm.get("mesh_max"),
                "profile": bm.get("profile_name"), "stats": mesh_stats(matrix)}

    async def mesh(self, job: Job) -> dict[str, Any]:
        await self.preflight()
        async with self.mr.console(lambda l: job.emit("log", level="info", message=l)):
            await self._ensure_homed(job, [])
            job.emit("step", id="probe", title="Probing the bed", status="start")
            await self.mr.gcode("BED_MESH_CALIBRATE")
            job.emit("step", id="probe", title="Probing the bed", status="done")
        return await self.mesh_state()

    # --- Persist ------------------------------------------------------------
    async def save(self, job: Job) -> dict[str, Any]:
        """SAVE_CONFIG writes the results into printer.cfg and restarts Klipper."""
        import asyncio
        job.emit("step", id="save", title="Writing results and restarting Klipper", status="start")
        try:
            await self.mr.gcode("SAVE_CONFIG")
        except MoonrakerError:
            pass      # Klipper may drop the connection while restarting
        saw_down = False
        for i in range(60):
            await asyncio.sleep(0.5)
            ok, _why = await self.mr.ready()
            saw_down = saw_down or not ok
            # Klipper can still answer "ready" for a moment before it restarts: require
            # a dip first (or a few seconds without one) so we do not report too early.
            if ok and (saw_down or i >= 8):
                job.emit("step", id="save", title="Klipper restarted", status="done")
                return {"ok": True}
        raise CalibrationError("Klipper did not come back after SAVE_CONFIG within 30 s. Check the Klipper log.")
