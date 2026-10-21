# SPDX-License-Identifier: Apache-2.0
"""eitri-update command line.

    eitri-update check                      is a newer release published? (signed manifest)
    eitri-update status                     installed release, flavor, channel mode, slots
    eitri-update upgrade [--dry-run --yes]  download, verify, install via RAUC, reboot; the release's data seed and
                                            MCU firmware follow after the reboot (before Klipper starts)
    eitri-update switch-flavor klipper|kalico [--dry-run --yes]
                                            run the other firmware flavor: its data seed + MCU firmware from the installed
                                            release, both MCUs re-flashed, no reboot; rolled back if it does not come up
    eitri-update post-boot [--stage prepare|verify]
                                            (systemd, every boot) finish an update, verify health, commit or roll back
    eitri-update rollback                   boot the previous system on next start
    eitri-update mcu-sync                   flash MCU firmware that does not match this host
    eitri-update upstream enable|disable|status
                                            follow upstream Klipper/Kalico with on-device builds

Exit codes: 0 ok, 1 failed, 2 usage, 3 nothing to do / refused by policy.

Two disk layouts are handled behind one interface (bootenv.py).  The in-place conversion of a stock printer
(installer/install.sh, boot state in /boot/eitri/boot.env) is the one users get.  The flash-once GPT image built by os/
(U-Boot state in a raw sector, via rauc-bootloader) exists for bench boards, microSD cards and CI only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from eitri_mcuflash import privs
from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.events import EventSink
from eitri_mcuflash.proc import Runner, run

from . import __version__, fetch
from .client import CONF_PATH, Config, Updater
from .upstream import UpstreamMode, UpstreamPaths


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eitri-update", description="Eitri release channel client.",
        epilog="Disk layouts: the in-place conversion of a stock printer (installer/install.sh, boot state in /boot/eitri/boot.env) is "
               "the one users get. The flash-once GPT disk image (raw U-Boot state sector) is for bench boards, microSD cards and CI only.")
    p.add_argument("--version", action="version", version="eitri-update " + __version__)
    p.add_argument("--json", action="store_true", help="JSON-lines events on stdout")
    p.add_argument("--events-file")
    p.add_argument("--config", default=CONF_PATH)
    p.add_argument("--manifest-url", help="override the channel manifest URL (https:// or file://)")
    p.add_argument("--key-dir", action="append", help="trusted minisign public key file or directory (repeatable)")
    p.add_argument("--data-root", default="/data")
    p.add_argument("--allow-insecure", action="store_true", help="permit http:// URLs (testing only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("status")
    up = sub.add_parser("upgrade")
    up.add_argument("--version", dest="target", help="install this release instead of the latest")
    up.add_argument("--dry-run", action="store_true")
    up.add_argument("--yes", action="store_true")
    up.add_argument("--no-reboot", action="store_true")
    up.add_argument("--allow-downgrade", action="store_true")
    pb = sub.add_parser("post-boot", help="after a reboot: finish an update, verify health, commit or roll back")
    pb.add_argument("--stage", choices=("all", "prepare", "verify"), default="all",
                    help="prepare runs before klipper.service, verify after it (the systemd units); all does both")
    sw = sub.add_parser("switch-flavor", help="run the other firmware flavor (klipper <-> kalico): data seed + MCU firmware")
    sw.add_argument("flavor", choices=("klipper", "kalico"))
    sw.add_argument("--dry-run", action="store_true", help="show the plan; downloads and changes nothing")
    sw.add_argument("--yes", action="store_true", help="do not ask (required without a terminal)")
    sub.add_parser("rollback")
    sub.add_parser("confirm", help="mark the running system good")
    sub.add_parser("mcu-sync")
    u = sub.add_parser("upstream")
    usub = u.add_subparsers(dest="action", required=True)
    en = usub.add_parser("enable")
    en.add_argument("--dry-run", action="store_true")
    en.add_argument("--no-restart", action="store_true")
    dis = usub.add_parser("disable")
    dis.add_argument("--keep-toolchain", action="store_true")
    dis.add_argument("--dry-run", action="store_true")
    dis.add_argument("--no-restart", action="store_true")
    usub.add_parser("status")
    return p


# The eitri account may run eitri-update as root through sudo (etc/sudoers.d/eitri, needed for switch-flavor from the
# screen and for updates from a shell).  That account also runs the network-facing services, so through sudo nothing may
# change WHAT is trusted or WHERE root writes: no other config, manifest, keys, data root, plain http, downgrade, or
# event log.  Root itself (systemd units, a root shell with the SUDO_* variables unset) keeps every option.
DEFAULT_DATA_ROOT = "/data"
DEFAULT_EVENTS_FILE = "/data/eitri/logs/update.jsonl"


def invoked_through_sudo(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return privs.is_root() and any(env.get(k) for k in ("SUDO_UID", "SUDO_USER", "SUDO_COMMAND"))


def sudo_refusals(args) -> List[str]:
    """The options of *args* that a sudo caller may not use (empty: allowed)."""
    bad = []
    if args.config != CONF_PATH:
        bad.append("--config")
    if args.manifest_url:
        bad.append("--manifest-url")
    if args.key_dir:
        bad.append("--key-dir")
    if args.data_root != DEFAULT_DATA_ROOT:
        bad.append("--data-root")
    if args.allow_insecure:
        bad.append("--allow-insecure")
    if args.events_file not in (None, DEFAULT_EVENTS_FILE):
        bad.append("--events-file (only %s)" % DEFAULT_EVENTS_FILE)
    if getattr(args, "allow_downgrade", False):
        bad.append("--allow-downgrade")
    return bad


def main(argv: Optional[List[str]] = None, *, runner: Runner = run, opener=fetch._open, updater_factory=None, environ=None) -> int:
    args = build_parser().parse_args(argv)
    if invoked_through_sudo(environ):
        bad = sudo_refusals(args)
        if bad:
            sys.stderr.write("eitri-update: refusing %s through sudo: these options change what is trusted or where root "
                             "writes. Run them as root itself (a root shell without the SUDO_* variables).\n" % ", ".join(bad))
            return 2
    events = EventSink(component="update", json_out=sys.stdout if args.json else None,
                       human_out=None if args.json else sys.stderr, events_file=args.events_file)
    cfg = Config.load(args.config)
    cfg.data_root = args.data_root
    cfg.allow_insecure = args.allow_insecure
    if args.manifest_url:
        cfg.manifest_url = args.manifest_url
    if args.key_dir:
        cfg.key_paths = list(args.key_dir)
    cfg.mcu_firmware_root = cfg.eitri_root + "/mcu/firmware"
    cfg.mcu_state_dir = cfg.eitri_root + "/mcu"
    updater = updater_factory(cfg, events) if updater_factory else Updater(cfg, events, runner=runner, opener=opener)
    try:
        if args.cmd == "check":
            res = updater.check()
            if not args.json:
                print("installed: %s\nlatest:    %s%s" % (res["installed"] or "unknown", res["latest"],
                                                         "  (update available)" if res["update_available"] else "  (up to date)"))
            return 0
        if args.cmd == "status":
            return _status(updater, args)
        if args.cmd == "upgrade":
            return _upgrade(updater, args)
        if args.cmd == "post-boot":
            if args.stage == "prepare":
                updater.prepare()
            elif args.stage == "verify":
                updater.verify()
            else:
                updater.post_boot()
            return 0
        if args.cmd == "switch-flavor":
            return _switch_flavor(updater, args)
        if args.cmd == "rollback":
            res = updater.rollback()
            events.result(True, data=res, msg="reboot to switch to slot %s" % res["to_slot"])
            return 0
        if args.cmd == "confirm":
            slot = updater.bootctl.current_slot()
            updater.rauc.mark("good")
            updater.bootctl.set_state(slot, "good")
            events.result(True, msg="slot %s marked good" % slot)
            return 0
        if args.cmd == "mcu-sync":
            events.result(True, data=updater.mcu_sync())
            return 0
        if args.cmd == "upstream":
            return _upstream(updater, args, runner, opener)
    except FlashError as exc:
        events.error(str(exc))
        events.result(False, msg=exc.message, data=exc.to_dict())
        return 3 if exc.code in ("up_to_date", "downgrade", "upstream_mode", "printing") else 1
    finally:
        events.close()
    return 2


def _status(updater: Updater, args) -> int:
    st = updater.state
    doc = st.load()
    try:
        rauc = updater.rauc.status()
        slots = [{"slot": s.bootname, "state": s.state, "boot_status": s.boot_status, "version": s.version} for s in rauc.slots]
        booted = rauc.booted
    except FlashError:
        slots, booted = [], None
    info = {"installed": st.installed_version, "flavor": st.flavor, "channel_mode": st.channel, "booted_slot": booted, "slots": slots,
            "pending": doc.get("pending"), "switch": doc.get("switch"), "last_check": doc.get("last_check"), "latest_seen": doc.get("latest_seen")}
    updater.events.result(True, data=info)
    if not args.json:
        print(json.dumps(info, indent=2))
    return 0


def _upgrade(updater: Updater, args) -> int:
    if not args.dry_run and not args.yes:
        if args.json or not sys.stdin.isatty():
            sys.stderr.write("error: upgrade needs --yes when not run from a terminal (or use --dry-run)\n")
            return 2
        updater.fetch_manifest()
        sys.stderr.write("Install the update and reboot? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            return 3
    updater.upgrade(target=args.target, dry_run=args.dry_run, allow_downgrade=args.allow_downgrade, reboot=not args.no_reboot)
    return 0


def _switch_flavor(updater: Updater, args) -> int:
    if not args.dry_run and not args.yes:
        if args.json or not sys.stdin.isatty():
            sys.stderr.write("error: switch-flavor needs --yes when not run from a terminal (or use --dry-run)\n")
            return 2
        sys.stderr.write("Klipper restarts, both MCUs are re-flashed (a few minutes) and %s becomes the firmware flavor. Continue? [y/N] " % args.flavor)
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            return 3
    updater.switch_flavor(args.flavor, dry_run=args.dry_run)
    return 0


def _upstream(updater: Updater, args, runner: Runner, opener) -> int:
    paths = UpstreamPaths(args.data_root, args.data_root + "/printer_data")
    mode = UpstreamMode(updater.state, updater.events, paths, runner=runner, opener=opener)
    if args.action == "status":
        info = mode.status()
        updater.events.result(True, data=info)
        if not args.json:
            print(json.dumps(info, indent=2))
        return 0
    if args.action == "enable":
        manifest = None
        if not mode.toolchain_installed():
            manifest = updater.fetch_manifest()
        else:
            try:
                manifest = updater.fetch_manifest()
            except FlashError as exc:
                updater.events.warn("manifest unavailable (%s); using the installed toolchain and default upstream repositories" % exc.message)
        if manifest is None:
            from .manifest import Manifest
            manifest = Manifest("local", 0, "", None, [], {}, {}, {})
        mode.enable(manifest, dry_run=args.dry_run, restart=not args.no_restart)
    else:
        mode.disable(remove_toolchain=not args.keep_toolchain, dry_run=args.dry_run, restart=not args.no_restart)
    updater.events.result(True, data=mode.status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
