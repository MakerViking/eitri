#!/usr/bin/env python3
"""Stand-in for installer/mcuflash used by `--mock`.

Speaks the same CLI surface (`plan`, `probe`, `flash --apply --yes`, `--assign`,
`--only`) and the same shared event stream (schema 1, JSON Lines) as the real
tool, with believable timing and log lines, so the real subprocess + parser path
is exercised on a developer PC.  Env knobs: EITRI_MOCK_SPEED, EITRI_MOCK_FAIL
("<role>:<phase>"), EITRI_MOCK_FREEZE ("flash:<percent>" - stall there),
EITRI_MOCK_UNASSIGNED ("1": probing cannot tell which UART is which until
--assign is given).  Stdlib only: it is launched with `python -I`.
"""
import argparse
import datetime
import json
import os
import sys
import time

SPEED = float(os.environ.get("EITRI_MOCK_SPEED") or 1)
FAIL = os.environ.get("EITRI_MOCK_FAIL") or ""
FREEZE = os.environ.get("EITRI_MOCK_FREEZE") or ""
UNASSIGNED = os.environ.get("EITRI_MOCK_UNASSIGNED") == "1"
RUN = "mock%06x" % (int(time.time()) & 0xFFFFFF)


def emit(type_, **kw):
    ev = {"schema": 1, "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
          "run": RUN, "component": "mcuflash", "type": type_}
    ev.update({k: v for k, v in kw.items() if v is not None})
    print(json.dumps(ev, separators=(",", ":")), flush=True)


def nap(seconds):
    if SPEED > 0:
        time.sleep(seconds / SPEED)


# Mock topology.  Which UART is the mainboard is a documented unknown on real
# hardware; the mock simply picks one.
TRUTH = {
    "mainboard": dict(port="/dev/ttyS2", mcu="stm32f401xc", method="sdcard-serial"),
    "toolhead": dict(port="/dev/ttyS0", mcu="stm32f401xc", method="katapult"),
}
VERSION = "v0.12.0-2-g4b7d5c1"
ORDER = ("toolhead", "mainboard")          # registry order: toolhead first


def port_record(port):
    t = next(v for v in TRUTH.values() if v["port"] == port)
    return {"port": port, "state": "klipper", "baud": 500000, "mcu": t["mcu"], "detail": "",
            "klipper": {"app": "Klipper", "version": VERSION, "mcu": t["mcu"], "clock_freq": 84000000, "serial_baud": 500000}}


def probe_all():
    emit("step", step="probe", status="start", msg="identifying the MCUs on /dev/ttyS0, /dev/ttyS2")
    recs = []
    for port in ("/dev/ttyS0", "/dev/ttyS2"):
        nap(0.9)
        rec = port_record(port)
        recs.append(rec)
        emit("log", level="info", step="probe", msg=f"{port}: klipper ({rec['mcu']})", data=rec)
        nap(0.4)
    emit("step", step="probe", status="ok", data={"ports": recs})
    return recs


def make_plan(args, recs):
    assign = dict(a.split("=", 1) for a in args.assign)
    emit("step", step="preflight", status="start")
    roles, boards, checks, blocked = {}, {}, [], False
    if UNASSIGNED and not assign:
        checks.append({"id": "roles", "status": "block", "board": None,
                       "detail": "cannot tell which UART is the mainboard and which the toolhead: only the registry hint points that way",
                       "remedy": "Use --assign mainboard=<port> toolhead=<port> after checking printer.cfg."})
        blocked = True
    else:
        for name, t in TRUTH.items():
            port = assign.get(name, t["port"])
            roles[name] = {"board": name, "port": port, "confidence": "override" if name in assign else "mapped",
                           "evidence": [f"stock printer.cfg: {name} MCU on {port}"]}
            boards[name] = {"port": port, "method": t["method"], "steps": ["back up", "flash", "verify"],
                            "firmware": {"board": name, "flavor": args.flavor, "version": VERSION if False else "v0.13.0-813-ga08fc79", "mcu": t["mcu"]}}
            checks.append({"id": "port", "status": "ok", "board": name, "detail": f"{port} answers as klipper, MCU {t['mcu']}", "remedy": None})
            checks.append({"id": "firmware", "status": "ok", "board": name, "detail": f"{args.flavor} image matches the host version", "remedy": None})
            checks.append({"id": "recovery", "status": "ok" if name == "toolhead" else "warn", "board": name,
                           "detail": "recovery is proven at apply time: Katapult read-back first" if name == "toolhead"
                           else "stock qd_mcu.bin acknowledged as the floor", "remedy": None})
    for c in checks:
        emit("log", level={"ok": "info", "warn": "warn", "block": "error"}[c["status"]], board=c["board"], step="preflight",
             msg=f"{c['id']}: {c['detail']}", data=c)
    emit("step", step="preflight", status="failed" if blocked else "ok",
         msg="refusing to flash: 1 blocking issue(s)" if blocked else None)
    return {"flavor": args.flavor, "blocked": blocked, "ports": recs, "roles": roles, "boards": boards, "checks": checks, "notes": []}


def cmd_probe(args):
    emit("result", ok=True, data={"ports": probe_all()})


def cmd_plan(args):
    plan = make_plan(args, probe_all())
    emit("result", ok=not plan["blocked"], data=plan)
    sys.exit(3 if plan["blocked"] else 0)


def cmd_flash(args):
    plan = make_plan(args, probe_all())
    if plan["blocked"]:
        emit("result", ok=False, msg="blocked by preflight", data=plan)
        sys.exit(3)
    roles = [r for r in ORDER if (not args.only or r in args.only)]
    results = {}

    def log(board, msg, level="info"):
        emit("log", level=level, board=board, msg=msg)

    # Phase A: build + back up + prove recovery for every board before erasing any
    for r in roles:
        emit("step", step="build", status="start", board=r)
        for pct in (35, 70, 100):
            emit("progress", step="build", board=r, pct=pct)
            nap(0.3)
        log(r, f"using release bundle firmware {r}-{args.flavor}.bin (matches host)")
        emit("step", step="build", status="ok", board=r)
        emit("step", step="backup", status="start", board=r)
        nap(0.8)
        log(r, f"read the current firmware; stored under /data/eitri/mcu/{r}/")
        emit("step", step="backup", status="ok", board=r)
        if TRUTH[r]["method"] == "katapult":
            emit("step", step="roundtrip", status="start", board=r, msg="writing the saved image back to prove the reflash path")
            nap(0.8)
            emit("step", step="roundtrip", status="ok", board=r)

    # Phase B: flash each board
    for r in roles:
        t = TRUTH[r]
        port = plan["roles"][r]["port"]
        emit("step", step="flash", status="start", board=r, msg=f"v0.13.0 via {t['method']}")
        log(r, "Katapult connected @ 250000, protocol 1.0.0" if t["method"] == "katapult" else "SD-card serial bootloader signature found @ 250000")
        total = 28160 if r == "toolhead" else 31744
        freeze_at = float(FREEZE.split(":", 1)[1]) if FREEZE.startswith("flash:") else None
        pct = 0.0
        while pct < 100:
            pct = min(100.0, pct + 4.1)
            if FAIL == f"{r}:flash" and pct > 26:
                msg = f"Write to {port} stopped at block 118/442: no acknowledgement from the bootloader"
                recovery = "Power-cycle the printer, then press Retry. The bootloader was not touched; the previous firmware was restored automatically."
                emit("log", level="warn", board=r, step="flash", msg="pass 3/3 failed: no acknowledgement")
                emit("step", step="flash", status="failed", board=r, msg=msg, data={"code": "write", "message": msg, "step": "flash", "board": r, "port": port, "recovery": recovery})
                emit("step", step="rollback", status="start", board=r)
                nap(0.6)
                emit("step", step="rollback", status="ok", board=r, msg="previous firmware restored")
                emit("result", ok=False, msg=msg, data={"error": {"code": "write", "message": msg, "step": "flash", "board": r, "port": port, "recovery": recovery},
                                                        "results": results, "recovery": {x: [recovery] for x in roles}})
                sys.exit(1)
            emit("progress", step="flash", board=r, pct=round(pct, 1), msg=f"{int(total * pct / 100)}/{total} bytes")
            if freeze_at is not None and pct >= freeze_at:
                log(r, f"(mock) holding at {pct:.0f}%")
                while True:
                    time.sleep(3600)
            nap(0.2)
        emit("step", step="verify", status="start", board=r, msg="waiting for the firmware to answer")
        nap(1.0)
        version = "v0.13.0-813-ga08fc79" if args.flavor == "klipper" else "v2026.10.00"
        emit("step", step="verify", status="ok", board=r, msg=f"running {version}")
        emit("step", step="flash", status="ok", board=r)
        results[r] = {"port": port, "mcu": t["mcu"], "version": version, "flavor": args.flavor, "method": t["method"], "attempts": 1}
    emit("result", ok=True, msg="all MCUs flashed and verified", data={"results": results})


def main():
    ap = argparse.ArgumentParser(prog="mcuflash-mock")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true")
    common.add_argument("--flavor", default="klipper")
    common.add_argument("--assign", action="append", default=[])
    common.add_argument("--only", action="append", default=[])
    common.add_argument("--printer-cfg", action="append", default=[])
    common.add_argument("--state-dir")
    common.add_argument("--firmware-root")
    common.add_argument("--klipper-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", parents=[common]).set_defaults(fn=cmd_probe)
    sub.add_parser("plan", parents=[common]).set_defaults(fn=cmd_plan)
    f = sub.add_parser("flash", parents=[common])
    f.add_argument("--apply", action="store_true")
    f.add_argument("--yes", action="store_true")
    f.set_defaults(fn=cmd_flash)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
