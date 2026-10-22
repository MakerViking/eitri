"""Adapter around the installer's `eitri-mcuflash` CLI (installer/mcuflash).

The CLI speaks the installer family's shared event stream (schema 1, JSON Lines;
see installer/mcuflash/eitri_mcuflash/events.py):

    {"schema":1,"ts":..,"run":..,"component":"mcuflash",
     "type":"plan|step|progress|log|prompt|result",
     "step":"probe|preflight|build|backup|roundtrip|flash|verify|rollback|...",
     "status":"start|ok|failed|skipped", "board":"mainboard|toolhead|null",
     "pct":0-100, "level":"debug|info|warn|error", "msg":"...", "data":{...}}

How this app uses it:

  detect  ->  `plan --json`   read-only: probes every UART, binds boards to ports by
                              evidence, runs the preflight checks.  Its final
                              `result.data` is the plan: {blocked, ports[], roles{},
                              boards{}, checks[], notes[]}.
  flash   ->  `flash --json --apply --yes`   one run for all selected boards: recovery
                              is proven for every board before any is erased.  Final
                              `result.data` carries results{board:{version,..}},
                              error{board,step,message,recovery} and recovery{board:[..]}.

Everything the rest of the app assumes about the CLI is in this file.  Unknown
events are surfaced as debug log lines, never dropped.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from .jobs import Job
from .settings import Settings

PORT_RE = re.compile(r"/dev/ttyS[0-9]{1,2}")          # the board UARTs; use fullmatch: `$` would admit a trailing newline
ROLES = ("mainboard", "toolhead")
# The phases the UI draws, in order.  CLI step names map onto them; steps that are
# not a phase (roundtrip, rollback, services) are surfaced as log lines instead.
PHASES = ("identify", "build", "backup", "flash", "verify")
PHASE_ALIASES = {"probe": "identify", "preflight": "identify", "build": "build", "backup": "backup", "flash": "flash", "verify": "verify"}
LOG_ONLY_STEPS = ("roundtrip", "rollback", "services")
STATUS = {"start": "start", "ok": "done", "failed": "fail", "skipped": "skip"}
METHOD_BOOTLOADER = {"katapult": "katapult", "sdcard-serial": "sdcard-serial", "sdcard-file": "sdcard-serial"}


def common_args(settings: Settings) -> list[str]:
    """Paths the CLI would otherwise default to /data for: keep it inside our data dir."""
    return [
        "--state-dir", str(settings.eitri_dir / "mcu" / "state"),
        "--firmware-root", str(settings.eitri_dir / "mcu" / "firmware"),
        "--klipper-dir", str(settings.data_dir / "firmware" / "klipper"),
        "--printer-cfg", str(settings.config_dir / "printer.cfg"),
    ]


def assign_args(assign: dict[str, str] | None) -> list[str]:
    out: list[str] = []
    for role in ROLES:
        port = (assign or {}).get(role)
        if port and PORT_RE.fullmatch(port):
            out += ["--assign", f"{role}={port}"]
    return out


def detect_args(settings: Settings, flavor: str, assign: dict[str, str] | None) -> list[str]:
    return ["plan", "--json", "--flavor", flavor, *common_args(settings), *assign_args(assign)]


def flash_args(settings: Settings, flavor: str, roles: list[str], assign: dict[str, str] | None) -> list[str]:
    only = [x for r in roles for x in ("--only", r)]
    return ["flash", "--json", "--apply", "--yes", "--flavor", flavor, *common_args(settings), *assign_args(assign), *only]


def _port_event(p: dict[str, Any]) -> dict[str, Any]:
    state = "found" if p.get("state") in ("klipper", "katapult") else "silent"
    return {"type": "detect.port", "port": p.get("port"), "baud": p.get("baud"), "state": state,
            "mcu_state": p.get("state"), "chip": p.get("mcu") or None,
            "version": (p.get("klipper") or {}).get("version")}


def normalize(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one raw CLI object onto zero or more UI events."""
    kind = str(raw.get("type") or raw.get("event") or "")
    board = raw.get("board")
    step = str(raw.get("step") or "")
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    msg = str(raw.get("msg") or raw.get("message") or "")
    if kind == "step":
        if step in LOG_ONLY_STEPS:
            return [{"type": "log", "board": board, "level": "warn" if raw.get("status") == "failed" else "info",
                     "message": f"{step}: {msg or STATUS.get(str(raw.get('status')), '')}".strip(": ")}]
        phase = PHASE_ALIASES.get(step, step)
        status = STATUS.get(str(raw.get("status")), "start")
        out = [{"type": "phase", "board": board, "phase": phase, "status": status, "message": msg,
                "recovery": data.get("recovery") or ""}]
        if step == "probe" and status == "done":
            out += [_port_event(p) for p in data.get("ports", []) if isinstance(p, dict)]
        return out
    if kind == "progress":
        pct = raw.get("pct", raw.get("percent", 0))
        return [{"type": "progress", "board": board, "phase": PHASE_ALIASES.get(step, "flash"),
                 "percent": max(0.0, min(100.0, float(pct or 0))), "message": msg}]
    if kind == "log":
        if step == "probe" and data.get("port"):
            return [{"type": "log", "board": board, "level": raw.get("level", "info"), "message": msg}, _port_event(data)]
        if step == "preflight" and data.get("id"):
            return [{"type": "check", "id": data["id"], "status": data.get("status"), "detail": data.get("detail", msg),
                     "board": data.get("board"), "remedy": data.get("remedy") or ""}]
        return [{"type": "log", "board": board, "level": raw.get("level", "info"), "message": msg}]
    if kind == "plan":
        return [{"type": "log", "board": board, "level": "info", "message": msg}]
    if kind == "prompt":
        return [{"type": "prompt", "board": board, "message": msg}]
    if kind == "result":
        return [{"type": "cli.result", "ok": bool(raw.get("ok")), "message": msg, "data": data}]
    return [{"type": "log", "board": board, "level": "debug", "message": f"unrecognised event: {json.dumps(raw)[:200]}"}]


def plan_to_detect(data: dict[str, Any]) -> dict[str, Any]:
    """Turn the CLI's plan into what the wizard shows: boards, blockers, warnings."""
    ports = {p["port"]: p for p in data.get("ports", []) if p.get("port")}
    boards: list[dict[str, Any]] = []
    bound: set[str] = set()
    for name, role in (data.get("roles") or {}).items():
        port = role.get("port")
        p = ports.get(port, {})
        method = ((data.get("boards") or {}).get(name) or {}).get("method")
        boot = METHOD_BOOTLOADER.get(method or "") or ("katapult" if p.get("state") == "katapult" else "unknown")
        boards.append({
            "role": name if name in ROLES else "unknown", "port": port, "baud": p.get("baud"), "state": "found",
            "chip": p.get("mcu") or None, "bootloader": boot, "method": method,
            "version": (p.get("klipper") or {}).get("version"),
            "confidence": role.get("confidence"), "evidence": role.get("evidence") or [],
        })
        bound.add(port)
    for port, p in ports.items():           # a board answered but could not be bound to a role
        if p.get("state") in ("klipper", "katapult") and port not in bound:
            boards.append({"role": "unknown", "port": port, "baud": p.get("baud"), "state": "found", "chip": p.get("mcu") or None,
                           "bootloader": "katapult" if p["state"] == "katapult" else "unknown", "method": None,
                           "version": (p.get("klipper") or {}).get("version"), "confidence": None, "evidence": []})
    checks = data.get("checks") or []
    return {
        "boards": boards,
        "ports": [{"port": k, "state": v.get("state"), "chip": v.get("mcu") or None} for k, v in ports.items()],
        "cli_blocked": bool(data.get("blocked")),
        "blockers": [{"code": c.get("id", "check"), "message": c.get("detail", ""), "remedy": c.get("remedy") or "", "board": c.get("board")}
                     for c in checks if c.get("status") == "block"],
        "warnings": [{"code": c.get("id", "check"), "message": c.get("detail", ""), "board": c.get("board")} for c in checks if c.get("status") == "warn"],
        "notes": data.get("notes") or [],
        "needs_assignment": any(c.get("id") == "roles" and c.get("status") == "block" for c in checks),
    }


def final_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((e for e in reversed(events) if e["type"] == "cli.result"), None)


def _env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    if settings.mock:
        env["EITRI_MOCK_SPEED"] = str(settings.mock_speed)
        env["EITRI_MOCK_FAIL"] = settings.mock_fail or ""
        env["EITRI_MOCK_FREEZE"] = settings.mock_freeze or ""
        env["EITRI_MOCK_UNASSIGNED"] = "1" if settings.mock_unassigned else ""
    return env


async def run(settings: Settings, job: Job, args: list[str]) -> dict[str, Any]:
    """Run the CLI, forwarding each normalized event into `job`.

    Returns {"rc": int, "events": [...normalized...]}."""
    argv = [*settings.mcuflash_cmd, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL, env=_env(settings),
        )
    except OSError as exc:
        job.emit("log", level="error", message=f"cannot start mcuflash ({argv[0]}): {exc}")
        return {"rc": 127, "events": []}
    seen: list[dict[str, Any]] = []

    async def pump_stderr() -> None:
        assert proc.stderr
        async for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                job.emit("log", level="debug", message=text)

    err_task = asyncio.create_task(pump_stderr())
    try:
        assert proc.stdout
        async for line in proc.stdout:
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
                if not isinstance(raw, dict):
                    raise ValueError
            except ValueError:
                job.emit("log", level="info", message=text)     # tolerate plain-text lines
                continue
            for ev in normalize(raw):
                seen.append(dict(ev))
                job.emit(ev.pop("type"), **ev)
        rc = await proc.wait()
    except asyncio.CancelledError:
        proc.terminate()
        raise
    finally:
        await asyncio.gather(err_task, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
    return {"rc": rc, "events": seen}
