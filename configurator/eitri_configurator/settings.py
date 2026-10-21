"""Runtime settings.

Everything the app touches on disk or over the network is derived from one
`Settings` object so that mock mode can redirect it all into a scratch dir and
tests can build isolated instances.
"""
from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CONFIGURATOR_DIR = PACKAGE_DIR.parent
# configurator/ lives at <repo>/configurator
DEFAULT_REPO_ROOT = CONFIGURATOR_DIR.parent


def default_web_dir() -> Path:
    """Source checkout first; installed layouts (venv in /data) fall back to known locations."""
    env = os.environ.get("EITRI_CONFIGURATOR_WEB")
    for cand in ([Path(env)] if env else []) + [
        CONFIGURATOR_DIR / "web", Path("/data/eitri/configurator/web"), Path("/usr/share/eitri/configurator/web"),
    ]:
        if (cand / "index.html").is_file():
            return cand
    return CONFIGURATOR_DIR / "web"


@dataclass
class Settings:
    mock: bool = False
    host: str = "0.0.0.0"
    port: int = 8099

    repo_root: Path = DEFAULT_REPO_ROOT
    web_dir: Path = field(default_factory=default_web_dir)
    data_dir: Path = Path("/data")

    moonraker_url: str = "http://127.0.0.1:7125"
    # argv prefix for the installer's MCU flasher (installer/mcuflash); sub-commands are appended.
    mcuflash_cmd: list[str] = field(default_factory=lambda: ["eitri-mcuflash"])
    # Optional override for the config template directory (*.tpl files); replaces the klipper-layer.
    template_dir: Path | None = None
    # Optional override for the klipper-layer directory (default: <repo_root>/klipper-layer, then /data/eitri/klipper-layer).
    layer_dir: Path | None = None

    # Mock-mode knobs (ignored otherwise).
    mock_speed: float = 1.0          # >1 faster, 0 = instant
    mock_fail: str | None = None     # e.g. "toolhead:flash" -> fail that phase
    mock_freeze: str | None = None   # e.g. "flash:62" -> stall flashing at 62 % (screenshots)
    mock_unassigned: bool = False    # probing cannot bind UARTs to boards until the user assigns them

    # --- derived paths -------------------------------------------------
    @property
    def printer_data(self) -> Path:
        return self.data_dir / "printer_data"

    @property
    def config_dir(self) -> Path:
        return self.printer_data / "config"

    @property
    def eitri_dir(self) -> Path:
        return self.data_dir / "eitri"

    @property
    def state_file(self) -> Path:
        return self.eitri_dir / "configurator" / "state.json"

    @property
    def flavor_file(self) -> Path:
        return self.eitri_dir / "flavor"

    @property
    def release_lock(self) -> Path:
        return self.repo_root / "release.lock"

    @property
    def klarity_status_file(self) -> Path:
        # Written by the Klarity daemon ([klarity] status_file): connected, display, port, baud, version.
        return Path("/run/klarity/status.json")

    @classmethod
    def for_mock(cls, data_dir: Path, **kw) -> "Settings":
        s = cls(mock=True, data_dir=data_dir, **kw)
        s.mcuflash_cmd = [sys.executable, "-I", str(PACKAGE_DIR / "mock" / "mcuflash.py")]
        return s


def parse_cmd(value: str) -> list[str]:
    return shlex.split(value)
