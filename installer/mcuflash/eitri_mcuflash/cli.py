# SPDX-License-Identifier: Apache-2.0
"""eitri-mcuflash command line.

    eitri-mcuflash probe                 identify what answers on each UART
    eitri-mcuflash plan                  probe + preflight + show what flash would do
    eitri-mcuflash backup [--apply --yes] prove recovery: save each board's firmware, flash nothing
    eitri-mcuflash flash [--apply --yes] dry run by default; --apply really flashes
    eitri-mcuflash rollback BOARD        restore the firmware saved before the last flash
    eitri-mcuflash backups               list saved firmware images

Machine consumers (the configurator) pass --json: one event per line on stdout,
ending with a {"type":"result"} event.  Exit codes: 0 ok, 1 failed, 2 usage,
3 blocked by preflight (nothing touched), 4 operator action required.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

from . import __version__
from .errors import FlashError
from .events import EventSink
from .firmware import DEFAULT_FIRMWARE_ROOT, DEFAULT_KLIPPER_DIR, FLAVORS
from .methods import FlashContext
from .proc import Runner, run
from .registry import load_registry
from .runner import EX_BLOCKED, EX_FAIL, EX_OK, EX_USAGE, Flasher, Options
from .guidance import recovery_steps
from .transport import open_transport

CONFIRM_PHRASE = "FLASH"
# The service user's directory (backups, board map); its parent /data/eitri/mcu and the delivered firmware/ next to it
# stay root's, so the account that runs the web-facing programs cannot redirect what root writes or flashes.
DEFAULT_STATE_DIR = "/data/eitri/mcu/state"
DEFAULT_FLAVOR_FILE = "/data/eitri/flavor"


def _kv(values: Optional[List[str]], what: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit("error: %s expects BOARD=VALUE, got %r" % (what, item))
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _default_flavor() -> str:
    try:
        with open(DEFAULT_FLAVOR_FILE) as fh:
            value = fh.read().strip()
            if value in FLAVORS:
                return value
    except OSError:
        pass
    return "klipper"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eitri-mcuflash", description="Probe, back up, flash and verify the X-Plus 4 MCUs.")
    p.add_argument("--version", action="version", version="eitri-mcuflash " + __version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="emit JSON-lines events on stdout")
    common.add_argument("--events-file", help="also append JSON events to this file")
    common.add_argument("--flavor", choices=FLAVORS, default=None, help="firmware flavor (default: /data/eitri/flavor or klipper)")
    common.add_argument("--firmware-root", default=DEFAULT_FIRMWARE_ROOT, help="directory holding <flavor>/manifest.json + images")
    common.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="where backups and the board map are kept")
    common.add_argument("--klipper-dir", default=DEFAULT_KLIPPER_DIR, help="host Klipper/Kalico clone (version lock, spi_flash.py, builds)")
    common.add_argument("--klippy-python", help="python with klippy's deps, for spi_flash.py")
    common.add_argument("--katapult-tool", help="path to Katapult scripts/flashtool.py (used when pyserial is available)")
    common.add_argument("--katapult-backend", choices=("auto", "native", "flashtool"), default="auto")
    common.add_argument("--port", action="append", default=[], help="UART to probe (repeatable; default: the board registry hints)")
    common.add_argument("--assign", action="append", default=[], metavar="BOARD=PORT", help="force a board to a port (overrides probing)")
    common.add_argument("--printer-cfg", action="append", default=[], help="printer.cfg used as wiring evidence (repeatable)")
    common.add_argument("--only", action="append", default=[], metavar="BOARD", help="restrict to these boards")
    common.add_argument("--stop-services", action="store_true", help="stop klipper (and --service units) while talking to the MCUs, restart afterwards")
    common.add_argument("--service", action="append", default=[], help="additional systemd unit to stop (repeatable)")
    common.add_argument("--registry", help="alternative boards.json")
    common.add_argument("--commit", help="the source commit the firmware must come from (release.lock pin); mismatch blocks")
    common.add_argument("--no-roundtrip", action="store_true", help="skip the write-back-the-backup proof")
    common.add_argument("--build", action="store_true", help="build the firmware on-device (upstream mode) instead of using the bundle")
    common.add_argument("--build-config-dir", help="directory with <board>.config files for --build")
    common.add_argument("--no-version-lock", action="store_true", help="do not require the image to match the host Klipper commit")
    common.add_argument("--stock-bin", action="append", default=[], metavar="BOARD=FILE", help="keep a stock image for boards that cannot be read back")
    common.add_argument("--accept-no-mcu-backup", action="store_true", help="proceed on an SD-bootloader board without a stock image")
    common.add_argument("--sd-mount", help="mounted microSD card for the sdcard-file method")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", parents=[common], help="identify what is on each UART (read-only)")
    sub.add_parser("plan", parents=[common], help="probe + preflight; show what flash would do (read-only)")
    f = sub.add_parser("flash", parents=[common], help="flash the selected flavor's firmware (dry run unless --apply)")
    f.add_argument("--apply", action="store_true", help="really back up and flash (default is a dry run)")
    f.add_argument("--yes", action="store_true", help="skip the typed confirmation (required with --json)")
    bk = sub.add_parser("backup", parents=[common], help="read back and keep each board's current firmware; flash nothing")
    bk.add_argument("--apply", action="store_true", help="really reset the boards into their bootloaders (default is a dry run)")
    bk.add_argument("--yes", action="store_true")
    bk.add_argument("--origin", choices=("stock",), help="tag the saved images as the printer's ORIGINAL firmware (revert.sh restores these)")
    r = sub.add_parser("rollback", parents=[common], help="restore the firmware saved before the last flash")
    r.add_argument("board")
    r.add_argument("--original", action="store_true", help="restore the firmware the printer shipped with, not the one saved last")
    r.add_argument("--apply", action="store_true")
    r.add_argument("--yes", action="store_true")
    sub.add_parser("backups", parents=[common], help="list saved images")
    return p


def _confirm(args: argparse.Namespace, what: str) -> bool:
    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        sys.stderr.write("error: --apply needs --yes when not run from a terminal\n")
        return False
    sys.stderr.write("\n%s\nType %s to continue: " % (what, CONFIRM_PHRASE))
    sys.stderr.flush()
    return sys.stdin.readline().strip() == CONFIRM_PHRASE


class ServiceGuard:
    """Stop services around MCU access, restart exactly those we stopped."""

    def __init__(self, units: List[str], runner: Runner, events: EventSink) -> None:
        self.units, self.runner, self.events = units, runner, events
        self.stopped: List[str] = []

    def __enter__(self) -> "ServiceGuard":
        for unit in self.units:
            active = self.runner(["systemctl", "is-active", "--quiet", unit])
            if active.returncode == 0:
                res = self.runner(["systemctl", "stop", unit])
                if res.returncode == 0:
                    self.stopped.append(unit)
                    self.events.info("stopped %s" % unit, step="services")
                else:
                    self.events.warn("could not stop %s: %s" % (unit, res.output.strip()), step="services")
        return self

    def __exit__(self, *exc) -> None:
        for unit in reversed(self.stopped):
            self.runner(["systemctl", "start", unit])
            self.events.info("restarted %s" % unit, step="services")


def make_flasher(args: argparse.Namespace, events: EventSink, runner: Runner = run, opener=open_transport):
    registry = load_registry(args.registry)
    ctx = FlashContext(
        registry=registry,
        opener=opener,
        events=events,
        runner=runner,
        klipper_dir=args.klipper_dir,
        klippy_python=args.klippy_python,
        katapult_tool=args.katapult_tool,
        katapult_backend=args.katapult_backend,
        sd_mount=getattr(args, "sd_mount", None),
        work_dir=os.path.join(args.state_dir) if os.path.isdir(args.state_dir) else "/tmp",
    )
    opt = Options(
        flavor=args.flavor or _default_flavor(),
        firmware_root=args.firmware_root,
        ports=list(args.port),
        assign=_kv(args.assign, "--assign"),
        printer_cfgs=list(args.printer_cfg),
        state_dir=args.state_dir,
        stop_services=([ "klipper.service"] + list(args.service)) if args.stop_services else list(args.service),
        roundtrip=not getattr(args, "no_roundtrip", False),
        stock_bins=_kv(getattr(args, "stock_bin", []), "--stock-bin"),
        accept_no_mcu_backup=getattr(args, "accept_no_mcu_backup", False),
        build=getattr(args, "build", False),
        build_config_dir=getattr(args, "build_config_dir", None),
        check_version_lock=not getattr(args, "no_version_lock", False),
        commit=args.commit,
        only=list(args.only),
        origin=getattr(args, "origin", "") or "",
        sd_tooling=args.cmd != "backup",
    )
    return Flasher(ctx, opt), ctx


def print_plan(plan, out=sys.stdout) -> None:
    out.write("MCU ports\n")
    for p in plan.probes:
        out.write("  %-12s %-9s %s\n" % (p.path, p.state, p.mcu or p.detail))
    out.write("Boards\n")
    for b in plan.boards:
        out.write("  %-10s %s (%s)  method=%s\n" % (b.board.name, b.role.port, b.role.confidence, b.method or "-"))
        for s in b.steps():
            out.write("      - %s\n" % s)
    out.write("Checks\n")
    for c in plan.all_checks():
        out.write("  [%-5s] %s%s: %s\n" % (c.status, (c.board + " ") if c.board else "", c.id, c.detail))
        if c.status == "block" and c.remedy:
            out.write("          -> %s\n" % c.remedy)
    for n in plan.notes:
        out.write("note: %s\n" % n)


def main(argv: Optional[List[str]] = None, *, runner: Runner = run, opener=open_transport, context_hook=None) -> int:
    args = build_parser().parse_args(argv)
    events = EventSink(
        component="mcuflash",
        json_out=sys.stdout if args.json else None,
        human_out=None if args.json else sys.stderr,
        events_file=args.events_file,
    )
    try:
        flasher, ctx = make_flasher(args, events, runner, opener)
        if context_hook:
            context_hook(ctx)
        if args.cmd == "backups":
            return _cmd_backups(flasher, args)
        with ServiceGuard(flasher.opt.stop_services, runner, events):
            if args.cmd == "probe":
                probes = flasher.probe_all()
                events.result(True, data={"ports": [p.to_dict() for p in probes]})
                if not args.json:
                    for p in probes:
                        print("%-12s %-9s %s" % (p.path, p.state, p.mcu or p.detail))
                return EX_OK
            plan = flasher.make_plan(flasher.probe_all())
            if args.cmd == "plan":
                if not args.json:
                    print_plan(plan)
                events.result(not plan.blocked, data=plan.to_dict())
                return EX_BLOCKED if plan.blocked else EX_OK
            if args.cmd == "flash":
                if args.apply and not plan.blocked:
                    if not _confirm(args, "This will erase and reflash the firmware of: %s" % ", ".join(b.board.name for b in plan.boards)):
                        events.result(False, msg="not confirmed")
                        return EX_USAGE
                code, _ = flasher.execute(plan, apply=args.apply)
                if not args.json:
                    print_plan(plan)
                return code
            if args.cmd == "backup":
                if args.apply and not plan.blocked:
                    if not _confirm(args, "This resets %s into the bootloader and back to read its firmware." % ", ".join(b.board.name for b in plan.boards)):
                        events.result(False, msg="not confirmed")
                        return EX_USAGE
                code, _ = flasher.backup_only(plan, apply=args.apply)
                if not args.json:
                    print_plan(plan)
                return code
            if args.cmd == "rollback":
                return _cmd_rollback(flasher, plan, args)
    except FlashError as exc:
        events.error(str(exc))
        events.result(False, msg=exc.message, data=exc.to_dict())
        return EX_FAIL
    finally:
        events.close()
    return EX_USAGE


def _cmd_backups(flasher: Flasher, args: argparse.Namespace) -> int:
    out = {}
    for b in flasher.ctx.registry.ordered():
        out[b.name] = [
            {"dir": e.directory, "created": e.meta.get("created"), "restorable": e.restorable, "version": (e.meta.get("dictionary") or {}).get("version")}
            for e in flasher.store.entries(b.name)
        ]
    flasher.events.result(True, data=out)
    if not args.json:
        print(json.dumps(out, indent=2))
    return EX_OK


def _cmd_rollback(flasher: Flasher, plan, args: argparse.Namespace) -> int:
    bps = [b for b in plan.boards if b.board.name == args.board]
    if not bps:
        flasher.events.result(False, msg="board %s not found/identified" % args.board, data=plan.to_dict())
        return EX_BLOCKED
    bp = bps[0]
    entry = flasher.store.original_restorable(args.board) if args.original else flasher.store.latest_restorable(args.board)
    if entry is None and not (flasher.store.stock_image(args.board) or flasher.opt.stock_bins.get(args.board)):
        flasher.events.result(False, msg="no saved image for %s" % args.board)
        return EX_FAIL
    # The dry run answers the question revert.sh asks before it flashes ANY board: can this one really go back?
    problems = flasher.rollback_problems(bp, original=args.original)
    if problems:
        recovery = " | ".join(recovery_steps(bp.board, bp.method or "", False))
        flasher.events.result(False, msg="%s cannot be rolled back automatically: %s" % (args.board, "; ".join(problems)),
                              data={"problems": problems, "recovery": recovery})
        return EX_FAIL
    if not args.apply:
        flasher.events.result(True, msg="dry run: would restore %s from %s" % (args.board, entry.directory if entry else "stock image"))
        return EX_OK
    if not _confirm(args, "This will reflash the %s board with its previous firmware." % args.board):
        return EX_USAGE
    try:
        flasher.rollback_board(bp, original=args.original)
    except FlashError as exc:
        flasher.events.result(False, msg=exc.message, data=exc.to_dict())
        return EX_FAIL
    flasher.events.result(True, msg="%s restored" % args.board)
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
