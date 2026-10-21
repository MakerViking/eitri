"""Entry point: `python -m eitri_configurator [--mock]`."""
from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

from aiohttp import web

from . import __version__
from .app import create_app
from .settings import DEFAULT_REPO_ROOT, Settings, parse_cmd


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
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    if a.mock:
        data = a.data_dir or Path(tempfile.mkdtemp(prefix="eitri-mock-"))
        s = Settings.for_mock(data, mock_speed=a.mock_speed, mock_fail=a.mock_fail, mock_freeze=a.mock_freeze, mock_unassigned=a.mock_unassigned)
        from .mock.seed import seed
        seed(data)
    else:
        s = Settings(data_dir=a.data_dir or Path("/data"), moonraker_url=a.moonraker_url)
        if a.mcuflash_cmd:
            s.mcuflash_cmd = a.mcuflash_cmd
    s.host = a.host or ("127.0.0.1" if a.mock else "0.0.0.0")
    s.port = a.port
    s.repo_root = a.repo_root
    s.layer_dir = a.layer_dir
    if a.web_dir:
        s.web_dir = a.web_dir
    return s


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = build_settings(argv)
    if s.mock:
        logging.info("MOCK mode: scratch data dir %s", s.data_dir)
    web.run_app(create_app(s), host=s.host, port=s.port, access_log=None, print=lambda m: logging.info(m))


if __name__ == "__main__":
    main()
