"""Entry point: `python -m eitri_configurator [--mock]`."""
from __future__ import annotations

import argparse
import logging
import os
import pwd
import sys
import tempfile
from pathlib import Path

from . import __version__
from . import auth
from .settings import DEFAULT_REPO_ROOT, Settings, parse_cmd
from .state import StateStore


def build_settings(argv: list[str] | None = None) -> Settings:
    ap = argparse.ArgumentParser(prog="eitri-configurator", description="Eitri first-run web configurator")
    ap.add_argument("--mock", action="store_true", help="develop on a PC: fake MCUs, fake Moonraker, scratch /data")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--data-dir", type=Path, help="app data root (default /data; mock: a temp dir)")
    ap.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    ap.add_argument("--layer-dir", type=Path, help="klipper-layer directory (default: <repo>/klipper-layer, then /data/eitri/klipper-layer)")
    ap.add_argument("--web-dir", type=Path, help="frontend directory (default: auto-detect; env EITRI_CONFIGURATOR_WEB)")
    ap.add_argument("--moonraker-url", default="http://127.0.0.1:7125")
    ap.add_argument("--mcuflash-cmd", type=parse_cmd, help="argv prefix of the installer's mcuflash")
    ap.add_argument("--mock-speed", type=float, default=1.0, help="mock pacing multiplier; 0 = instant")
    ap.add_argument("--mock-fail", help="make a mock flash fail, e.g. toolhead:flash")
    ap.add_argument("--mock-freeze", help="stall a mock stage, e.g. flash:62")
    ap.add_argument("--mock-unassigned", action="store_true", help="mock probing cannot tell which UART is which")
    ap.add_argument("--mock-toolhead-unresolved", action="store_true",
                    help="mock flasher refuses the toolhead, as the real one does today (a printer installed without tools)")
    ap.add_argument("--retire-when-locked", action=argparse.BooleanOptionalAction, default=None,
                    help="exit cleanly once setup is complete and the configurator is locked, at start-up and after the finishing "
                         "browser's grace period (frees its memory; `eitri-configurator unlock` starts it again). "
                         "Default: on, except in --mock mode; --no-retire-when-locked keeps it running")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    if a.mock:
        data = a.data_dir or Path(tempfile.mkdtemp(prefix="eitri-mock-"))
        s = Settings.for_mock(data, mock_speed=a.mock_speed, mock_fail=a.mock_fail, mock_freeze=a.mock_freeze, mock_unassigned=a.mock_unassigned,
                               mock_toolhead_unresolved=a.mock_toolhead_unresolved)
        from .mock.seed import seed
        seed(data)
    else:
        s = Settings(data_dir=a.data_dir or Path("/data"), moonraker_url=a.moonraker_url)
        if a.mcuflash_cmd:
            s.mcuflash_cmd = a.mcuflash_cmd
    s.host = a.host or ("127.0.0.1" if a.mock else "0.0.0.0")
    s.port = a.port
    s.retire_when_locked = (not a.mock) if a.retire_when_locked is None else a.retire_when_locked
    s.repo_root = a.repo_root
    s.layer_dir = a.layer_dir
    if a.web_dir:
        s.web_dir = a.web_dir
    return s


ADMIN_COMMANDS = ("pairing", "unlock", "lock")
SERVICE_USER = "eitri"


def drop_to_service_user() -> bool:
    """When started as root (`sudo eitri-configurator unlock`), become the service user before touching any file.

    The flag and pairing files live in a directory the eitri user controls, so root acting on them could be steered
    with symlinks. As eitri nothing needs root: starting the service goes through polkit, which allows exactly that.
    A fixed account name (not the owner of the directory) so nothing the user controls picks the identity.
    """
    if os.geteuid() != 0:
        return False
    try:
        pw = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        return False             # a development machine without the account: stay as we are
    os.initgroups(SERVICE_USER, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    return True


def admin(argv: list[str]) -> int:
    """`eitri-configurator pairing|unlock|lock [--data-dir DIR]`: run on the printer, never through the API."""
    ap = argparse.ArgumentParser(prog="eitri-configurator", description="Pairing and lock control for the first-run configurator")
    ap.add_argument("command", choices=ADMIN_COMMANDS, help="pairing: show the code; unlock: allow pairing after setup and start the "
                                                             "configurator if it retired; lock: close it again")
    ap.add_argument("--data-dir", type=Path, default=Path("/data"))
    ap.add_argument("--no-start", action="store_true", help="unlock: only create the flag, do not start the service or wait for the code")
    ap.add_argument("--wait", type=float, default=None, help="unlock: seconds to wait for the pairing code (default: 20 on a terminal, 0 otherwise)")
    a = ap.parse_args(argv)
    drop_to_service_user()
    if a.command == "pairing":
        info = auth.show_pairing(a.data_dir)
        if info.get("code"):
            print(f"Pairing code: {info['code']}")
        else:
            print(f"No code is shown ({info.get('state', 'unknown')}). After setup, run `eitri-configurator unlock` to pair again.")
    elif a.command == "unlock":
        # Only the real /data is managed through systemd; any other --data-dir is a development or test tree.
        if a.no_start or a.data_dir != Path("/data"):
            auth.unlock(a.data_dir)
            print("Unlocked: browsers can pair again with the code on the printer screen (`eitri-configurator pairing`).\n"
                  "It locks again when you finish the wizard, or run `eitri-configurator lock`.\n"
                  "If the service was retired, start it: systemctl start eitri-configurator")
            return 0
        wait = a.wait if a.wait is not None else (20.0 if sys.stdout.isatty() else 0.0)
        res = auth.unlock_and_start(a.data_dir, timeout=wait)
        if res["error"]:
            print(f"Unlocked, but the configurator could not be started: {res['error']}\n"
                  "Start it yourself: systemctl start eitri-configurator", file=sys.stderr)
            return 3
        code = res["pairing"].get("code") if res["pairing"].get("state") == "pairing" else None
        if code:
            print(f"Unlocked. Open the printer's address in a browser (port 8099) and enter the pairing code {code}.\n"
                  "The same code is on the printer screen. The configurator locks again when you finish the wizard or run `eitri-configurator lock`.")
        else:
            print("Unlocked, and the configurator is starting. The pairing code is not shown yet: run `eitri-configurator pairing` in a few seconds.")
    else:
        auth.lock(a.data_dir)
        print("Locked: the maintenance flag is removed (the configurator exits by itself once nobody is paired).")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ADMIN_COMMANDS:
        sys.exit(admin(args))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from aiohttp import web                 # only the server needs it: pairing/unlock/lock run on the image's plain python
    from .app import create_app
    s = build_settings(args)
    if s.retire_when_locked and StateStore(s.state_file).data.get("completed") and not (s.configurator_dir / "unlock").exists():
        logging.info("setup is complete and the configurator is locked: exiting (--retire-when-locked)")
        return
    if s.mock:
        logging.info("MOCK mode: scratch data dir %s", s.data_dir)
    web.run_app(create_app(s), host=s.host, port=s.port, access_log=None, print=lambda m: logging.info(m))


if __name__ == "__main__":
    main()
