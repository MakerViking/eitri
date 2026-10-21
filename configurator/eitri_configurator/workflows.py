"""The three multi-step operations behind the wizard: detect, apply, flash.

Each is a coroutine `(ctx, job, ...) -> result` run by the JobManager; progress is
reported as job events which the UI renders live."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import aiohttp

from . import layer, mcuflash, render, sysinfo
from . import options as opts
from .context import Context
from .jobs import Job
from .moonraker import MoonrakerError
from .state import atomic_write


async def _pause(ctx: Context, seconds: float) -> None:
    """Mock-mode pacing so the UI shows its transitions."""
    if ctx.settings.mock and ctx.settings.mock_speed > 0:
        await asyncio.sleep(seconds / ctx.settings.mock_speed)


# --------------------------------------------------------------------------
# Detect
# --------------------------------------------------------------------------

def analyze_boards(boards: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide whether flashing may proceed. Every blocker explains itself."""
    blockers: list[dict[str, str]] = []
    by_role: dict[str, list[dict]] = {"mainboard": [], "toolhead": []}
    for b in boards:
        if b["role"] in by_role:
            by_role[b["role"]].append(b)
        if b.get("port") and not mcuflash.PORT_RE.match(b["port"]):
            blockers.append({"code": "bad-port", "message": f"Unexpected serial device {b['port']}."})
    for role, found in by_role.items():
        if not found:
            blockers.append({"code": f"missing-{role}", "message": f"No {role} MCU answered on either serial port."})
        elif len(found) > 1:
            blockers.append({"code": f"ambiguous-{role}", "message": f"More than one port claims to be the {role}."})
    for b in boards:
        if b["role"] == "unknown":
            blockers.append({"code": "unknown-role", "message": f"{b.get('port')} answered but could not be identified as mainboard or toolhead."})
        elif b["bootloader"] == "stm32-rom":
            blockers.append({"code": "rom-only", "message": f"The {b['role']} has only the chip's built-in ROM bootloader, which needs the BOOT0 pin - hardware access. Flashing is blocked until a serial-reachable bootloader is present."})
        elif b["bootloader"] in ("unknown", "klipper"):
            blockers.append({"code": "no-recovery", "message": f"The {b['role']}'s bootloader could not be identified, so recovery after a failed flash is unproven. Flashing is blocked."})
    ports = [b["port"] for b in boards if b.get("port")]
    if len(ports) != len(set(ports)):
        blockers.append({"code": "dup-port", "message": "Two boards resolved to the same serial port."})
    return {"flash_ready": not blockers, "blockers": blockers}


async def _camera(ctx: Context) -> dict[str, Any]:
    if ctx.settings.mock:
        await _pause(ctx, 0.8)
        return {"present": True, "device": "/dev/eitri-cam", "model": "Sunplus UVC 1bcf:0b09", "streaming": True, "detail": "1920x1080 MJPEG"}
    present = Path("/dev/eitri-cam").exists()
    info: dict[str, Any] = {"present": present, "device": "/dev/eitri-cam" if present else None,
                            "model": "Sunplus UVC 1bcf:0b09" if present else None, "streaming": False, "detail": ""}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as http:
            async with http.get("http://127.0.0.1:8080/state") as r:
                res = (await r.json()).get("result", {})
                src = res.get("source", {})
                info.update(streaming=bool(src.get("online")), detail=f'{src.get("resolution", {}).get("width", "?")}x{src.get("resolution", {}).get("height", "?")}')
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        pass
    return info


async def run_detect(ctx: Context, job: Job) -> dict[str, Any]:
    stage = lambda name, status, **kw: job.emit("detect.stage", stage=name, status=status, **kw)   # noqa: E731
    st = ctx.state.data

    stage("host", "start")
    info = sysinfo.collect(ctx.settings, ctx.lock)
    await _pause(ctx, 0.7)
    stage("host", "done", detail=f'{info["board"]["model"]}, {info["memory"]["total_mb"]} MB')

    stage("mcus", "start")
    # Klipper holds the board UARTs; stop it for the probe and bring it back afterwards.
    was_active = (await ctx.services.status()).get("klipper") == "active"
    if was_active:
        await ctx.services.act("stop", "klipper")
    try:
        out = await mcuflash.run(ctx.settings, job, mcuflash.detect_args(ctx.settings, st["flavor"], st.get("assign")))
    finally:
        if was_active:
            await ctx.services.act("start", "klipper")
    res = mcuflash.final_result(out["events"])
    if res and "ports" in res["data"]:
        det = mcuflash.plan_to_detect(res["data"])
        verdict = analyze_boards(det["boards"])
        blockers = det["blockers"] + [b for b in verdict["blockers"] if b["code"] in ("bad-port", "dup-port")]
        flash_ready = not det["cli_blocked"] and not blockers and verdict["flash_ready"]
        if not flash_ready and not blockers:
            blockers = verdict["blockers"]
        det.update(blockers=blockers, flash_ready=flash_ready)
    else:
        det = {"boards": [], "ports": [], "warnings": [], "notes": [], "needs_assignment": False, "cli_blocked": True, "flash_ready": False,
               "blockers": [{"code": "probe-failed", "message": (res or {}).get("message") or "The serial probe failed to run. See the log for details.", "remedy": ""}]}
    stage("mcus", "done" if det["flash_ready"] else "warn", detail=f"{len(det['boards'])} board(s) identified")

    stage("camera", "start")
    camera = await _camera(ctx)
    stage("camera", "done" if camera["present"] else "warn", detail=camera.get("detail") or ("not found" if not camera["present"] else ""))

    stage("screen", "start")
    await _pause(ctx, 0.6)
    state = (await ctx.services.status()).get("klarity", "unknown")
    screen = sysinfo.klarity_status(ctx.settings, state)
    stage("screen", "done" if screen.get("connected") else "warn", detail=screen.get("display") or "")

    result = {"ok": True, "host": info["board"], "camera": camera, "screen": screen, **det}
    ctx.state.update(detect=result)
    return result


# --------------------------------------------------------------------------
# Apply (write configuration)
# --------------------------------------------------------------------------

async def _run_hook(ctx: Context, job: Job, argv: list[str], what: str) -> bool:
    if ctx.settings.mock or not (shutil.which(argv[0]) or os.access(argv[0], os.X_OK)):
        job.emit("log", level="info", message=f"{what}: helper not installed here; choice recorded for the installer")
        return True
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    assert proc.stdout
    async for line in proc.stdout:
        job.emit("log", level="info", message=line.decode(errors="replace").rstrip())
    return await proc.wait() == 0


def current_context(ctx: Context) -> dict[str, Any]:
    st = ctx.state.data
    return render.build_context(
        flavor=st["flavor"], options=opts.validate(st["options"]), detect=st["detect"],
        lock=ctx.lock, hostname=st["hostname"],
    )


def preview(ctx: Context) -> dict[str, Any]:
    files = render.plan(ctx.settings, render.render_all(ctx.settings, current_context(ctx)))
    prev_flavor = ctx.settings.flavor_file.read_text().strip() if ctx.settings.flavor_file.is_file() else None
    flavor = ctx.state.data["flavor"]
    options = opts.validate(ctx.state.data["options"])
    _, source = render.template_root(ctx.settings)
    actions = []
    if prev_flavor and prev_flavor != flavor:
        actions.append({"kind": "flavor-switch", "text": f"Switch the host firmware from {opts.FLAVORS.get(prev_flavor, {}).get('name', prev_flavor)} to {opts.FLAVORS[flavor]['name']}; both MCUs must be reflashed."})
    actions.append({"kind": "flash", "text": f"Flash {opts.FLAVORS[flavor]['name']} firmware to the mainboard and toolhead MCUs (next step)."})
    if options["channel"] == "upstream":
        actions.append({"kind": "toolchain", "text": "Install the compiler add-on (about 300 MB) for upstream mode."})
    if ctx.state.data["hostname"]:
        actions.append({"kind": "hostname", "text": f"Set the hostname to {ctx.state.data['hostname']}."})
    return {"files": render.public(files), "actions": actions, "template_source": source,
            "stale_detect": ctx.state.data["detect"] is None, "_plan": files}


async def run_apply(ctx: Context, job: Job) -> dict[str, Any]:
    st = ctx.state.data
    step = lambda id_, title, status, **kw: job.emit("step", id=id_, title=title, status=status, **kw)   # noqa: E731
    pv = preview(ctx)
    files = pv["_plan"]

    step("config", "Backing up and writing configuration", "start")
    res = render.write(ctx.settings, files)
    await _pause(ctx, 0.5)
    step("config", "Configuration written", "done", written=res["written"], backup=res["backup"])

    step("overlay", "Linking the Eitri Klipper extras and macros", "start")
    layer_dir = layer.find_layer(ctx.settings)
    clone = ctx.settings.data_dir / "firmware" / "klipper"
    if layer_dir is None or not (clone / "klippy").is_dir():
        job.emit("log", level="warn", message=f"No Klipper/Kalico clone at {clone} yet; the extras are linked once it exists (re-run Apply, or the installer does it).")
    elif not await _run_hook(ctx, job, layer.overlay_command(ctx.settings, layer_dir), "klipper-layer overlay"):
        step("overlay", "Linking the Klipper extras failed", "fail")
        return {"ok": False, "error": "overlay-install.sh failed; see the log"}
    step("overlay", "Extras and macros linked", "done")

    step("flavor", "Recording firmware flavor", "start")
    prev = ctx.settings.flavor_file.read_text().strip() if ctx.settings.flavor_file.is_file() else None
    options = opts.validate(st["options"])
    atomic_write(ctx.settings.flavor_file, st["flavor"] + "\n")
    atomic_write(ctx.settings.eitri_dir / "channel", options["channel"] + "\n")
    atomic_write(ctx.settings.eitri_dir / "ui", options["ui"] + "\n")
    if prev and prev != st["flavor"]:
        step("flavor", f"Switching host firmware to {opts.FLAVORS[st['flavor']]['name']}", "start")
        if not await _run_hook(ctx, job, ["/usr/libexec/eitri/switch-flavor", st["flavor"]], "switch-flavor"):
            step("flavor", "Switching the host firmware failed", "fail")
            return {"ok": False, "error": "switch-flavor helper failed; see the log"}
    if options["channel"] == "upstream":
        await _run_hook(ctx, job, ["/usr/libexec/eitri/toolchain", "install"], "toolchain")
    await _pause(ctx, 0.4)
    step("flavor", "Flavor recorded", "done")

    if st["hostname"]:
        step("hostname", "Setting hostname", "start")
        await ctx.network.set_hostname(st["hostname"])
        step("hostname", "Hostname set", "done")

    step("services", "Applying service choices", "start")
    await ctx.services.act("restart" if options["klarity"] else "stop", "klarity")
    await ctx.services.act("restart" if options["camera"] else "stop", "eitri-camera")
    await ctx.services.act("restart" if options["go2rtc"] else "stop", "go2rtc")
    await _pause(ctx, 0.4)
    step("services", "Services updated", "done")

    summary = {"ok": True, **res, "flavor": st["flavor"]}
    ctx.state.update(applied=summary)
    return summary


# --------------------------------------------------------------------------
# Flash
# --------------------------------------------------------------------------

FLASH_ORDER = ("mainboard", "toolhead")


async def run_flash(ctx: Context, job: Job, roles: list[str]) -> dict[str, Any]:
    st = ctx.state.data
    outcome: dict[str, dict[str, Any]] = dict((st.get("flash") or {}))
    roles = [r for r in FLASH_ORDER if r in roles]

    job.emit("step", id="prepare", title="Stopping Klipper so the serial ports are free", status="start")
    await ctx.services.act("stop", "klipper")
    job.emit("step", id="prepare", title="Klipper stopped", status="done")
    try:
        out = await mcuflash.run(ctx.settings, job, mcuflash.flash_args(ctx.settings, st["flavor"], roles, st.get("assign")))
    finally:
        job.emit("step", id="restart", title="Restarting Klipper", status="start")
        await ctx.services.act("start", "klipper")
        job.emit("step", id="restart", title="Klipper restarted", status="done")

    res = mcuflash.final_result(out["events"])
    data = (res or {}).get("data", {})
    results = data.get("results") or {}
    err = data.get("error") or {}
    recovery = data.get("recovery") or {}
    blocked_msg = "" if (results or err) else ((res or {}).get("message") or "mcuflash exited without reporting a result")
    for role in roles:
        if role in results:
            outcome[role] = {"ok": True, "version": results[role].get("version")}
            job.emit("board.result", board=role, ok=True, version=results[role].get("version"), message="", recovery="")
        elif err.get("board") == role or (not err and blocked_msg):
            advice = err.get("recovery") or " ".join(recovery.get(role, [])) or ""
            outcome[role] = {"ok": False}
            job.emit("board.result", board=role, ok=False, message=err.get("message") or blocked_msg, recovery=advice, phase=PHASE_ALIAS.get(err.get("step"), err.get("step")))
        else:
            outcome[role] = {"ok": False, "skipped": True}
            job.emit("board.result", board=role, ok=False, skipped=True,
                     message="Not flashed. " + ("Nothing was changed on this board." if not results else "An earlier board failed first."))
    ctx.state.update(flash=outcome)
    complete = all(outcome.get(r, {}).get("ok") for r in FLASH_ORDER)
    ok = out["rc"] == 0 and all(outcome[r]["ok"] for r in roles)
    return {"ok": ok, "complete": complete, "boards": outcome, **({"error": err} if err else {})}


PHASE_ALIAS = mcuflash.PHASE_ALIASES


async def flash_guard(ctx: Context) -> str | None:
    """Return a reason to refuse flashing, or None."""
    try:
        state = await ctx.moonraker.print_state()
    except MoonrakerError:
        return None        # Moonraker/Klipper down: nothing can be printing
    if state in ("printing", "paused"):
        return f"A print is {state}. Wait for it to finish before flashing the MCUs."
    return None
