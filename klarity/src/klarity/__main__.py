"""Command line entry point: ``klarity [-c klarity.cfg] [--learn] [--simulate]``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from . import __version__
from .config import DEFAULT_CONFIG_PATH, ConfigError, KlarityConfig, load_config
from .daemon import Klarity
from .screenmap import MapError, ScreenMap
from .transport import MemoryTransport

EX_CONFIG = 78  # sysexits.h; systemd unit lists it in RestartPreventExitStatus


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="klarity", description="Open screen daemon for the QIDI X-Plus 4"
    )
    p.add_argument("-c", "--config", help=f"config file (default {DEFAULT_CONFIG_PATH})")
    p.add_argument("--map", help="screen map YAML (overrides [klarity] map)")
    p.add_argument("--serial", help="serial port of the panel (overrides [screen] serial_port)")
    p.add_argument("--baud", type=int, help="panel baud rate (overrides [screen] baud)")
    p.add_argument("--learn", action="store_true", help="log every screen event to the learn log")
    p.add_argument(
        "--allow-todo",
        action="store_true",
        help="ACT on map entries flagged todo (placeholder ids!). Dangerous on a real printer.",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="never send G-code or print control to Moonraker"
    )
    p.add_argument(
        "--simulate",
        action="store_true",
        help="no serial port: talk to an in-memory panel (development)",
    )
    p.add_argument("--check-map", action="store_true", help="validate the screen map and exit")
    p.add_argument("--log-level", help="DEBUG, INFO, WARNING or ERROR")
    p.add_argument("--version", action="version", version=f"klarity {__version__}")
    return p


def apply_overrides(cfg: KlarityConfig, args: argparse.Namespace) -> None:
    if args.map:
        cfg.map = args.map
    if args.serial:
        cfg.screen.serial_port = args.serial
    if args.baud:
        cfg.screen.baud = args.baud
    if args.allow_todo:
        cfg.allow_todo = True
    if args.log_level:
        cfg.log_level = args.log_level.upper()


async def _amain(cfg: KlarityConfig, args: argparse.Namespace) -> None:
    transport = MemoryTransport() if args.simulate else None
    daemon = Klarity(
        cfg, transport=transport, learn=True if args.learn else None, dry_run=args.dry_run
    )
    loop = asyncio.get_running_loop()
    main = asyncio.current_task()
    assert main is not None
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main.cancel)
    loop.add_signal_handler(signal.SIGUSR1, daemon.log_learn_summary)
    try:
        await daemon.run()
    finally:
        daemon.log_learn_summary()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        apply_overrides(cfg, args)
    except ConfigError as exc:
        print(f"klarity: config error: {exc}", file=sys.stderr)
        return EX_CONFIG
    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(levelname)s %(name)s: %(message)s",  # journald adds timestamps
    )
    try:
        if args.check_map:
            smap = ScreenMap.load(cfg.map_path, allow_todo=cfg.allow_todo)
            todo, total = smap.todo_summary()
            print(
                f"map OK: {len(smap.pages)} pages, {total} widgets, {todo} TODO "
                f"({'ignored' if not cfg.allow_todo else 'ACTIVE'})"
            )
            return 0
        with contextlib.suppress(asyncio.CancelledError):
            asyncio.run(_amain(cfg, args))
    except (ConfigError, MapError) as exc:
        logging.getLogger("klarity").error("%s", exc)
        return EX_CONFIG
    return 0


if __name__ == "__main__":
    sys.exit(main())
