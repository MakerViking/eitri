"""Loader for ``klarity.cfg`` (Klipper-style INI, ``#`` comments)."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/data/printer_data/config/klarity.cfg")


class ConfigError(ValueError):
    """klarity.cfg is invalid."""


def packaged_map_path() -> Path:
    return Path(str(resources.files("klarity") / "maps" / "default.yaml"))


@dataclass(slots=True)
class ScreenConfig:
    # The stock software uses /dev/ttyS1 at 115200, but that is not confirmed live on mainline.
    # The shipped klarity.cfg sets it; with no config file Klarity still refuses to start.
    serial_port: str = ""
    baud: int = 115200
    encoding: str = "utf-8"
    quote_style: str = "backslash"
    bkcmd: int = 0
    frame_timeout: float = 0.5


@dataclass(slots=True)
class MoonrakerConfig:
    url: str = "ws://127.0.0.1:7125/websocket"
    api_key: str = ""
    request_timeout: float = 10.0
    backoff_min: float = 1.0
    backoff_max: float = 30.0


@dataclass(slots=True)
class PushConfig:
    min_interval: float = 0.25
    max_commands_per_second: float = 60.0
    full_refresh: float = 30.0


@dataclass(slots=True)
class CameraConfig:
    enabled: bool = False
    snapshot_url: str = "http://127.0.0.1:8080/snapshot"
    timeout: float = 3.0
    push_enabled: bool = False  # stays off until the file-transfer path is verified on hardware


@dataclass(slots=True)
class LearnConfig:
    enabled: bool = False
    log_path: str = "/data/printer_data/logs/klarity-learn.jsonl"
    window: float = 2.0


@dataclass(slots=True)
class UnlockConfig:
    """The "Unlock setup" screen action (reopens the configurator's pairing, shows the code)."""

    enabled: bool = True
    # Privileged helper (narrow sudoers rule); run without a shell.
    command: str = "/usr/bin/sudo -n /usr/bin/eitri-configurator unlock"
    # The configurator exits after setup; this brings it back (polkit lets the eitri user do it).
    start_command: str = "/usr/bin/systemctl --no-block start eitri-configurator.service"
    # Written by the configurator: {"state": "pairing"|"locked", "code": "dddd-dddd"}.
    pairing_file: str = "/data/eitri/configurator/pairing.json"
    # Address shown to the user: empty = the printer's own IPv4 address (default route).
    host: str = ""
    port: int = 8099
    # Seconds to wait for each command, and for the code to appear.
    timeout: float = 10.0
    wait: float = 15.0


@dataclass(slots=True)
class KlarityConfig:
    map: str = ""
    allow_todo: bool = False
    log_level: str = "INFO"
    # Small JSON file with the link state, read by the Eitri configurator.  Empty = disabled.
    status_file: str = "/run/klarity/status.json"
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    moonraker: MoonrakerConfig = field(default_factory=MoonrakerConfig)
    push: PushConfig = field(default_factory=PushConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    learn: LearnConfig = field(default_factory=LearnConfig)
    unlock: UnlockConfig = field(default_factory=UnlockConfig)

    @property
    def map_path(self) -> Path:
        return Path(self.map) if self.map else packaged_map_path()


_SECTIONS: dict[str, str | None] = {
    "klarity": None,  # top level fields
    "screen": "screen",
    "moonraker": "moonraker",
    "push": "push",
    "camera": "camera",
    "learn": "learn",
    "unlock": "unlock",
}


def _convert(raw: str, default: Any, where: str) -> Any:
    try:
        if isinstance(default, bool):
            lowered = raw.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected true/false")
        if isinstance(default, int):
            return int(raw, 0)
        if isinstance(default, float):
            return float(raw)
        return raw.strip()
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc} (got {raw!r})") from exc


def _fill(target: Any, items: dict[str, str], section: str) -> None:
    for key, raw in items.items():
        if key not in target.__slots__ or key.startswith("_"):
            raise ConfigError(f"[{section}] unknown option {key!r}")
        if section == "klarity" and key in _SECTIONS:
            raise ConfigError(f"[{section}] unknown option {key!r}")
        setattr(target, key, _convert(raw, getattr(target, key), f"[{section}] {key}"))


def parse_config(text: str) -> KlarityConfig:
    parser = configparser.ConfigParser(
        delimiters=(":", "="), inline_comment_prefixes=("#",), interpolation=None, strict=True
    )
    parser.optionxform = str.lower  # type: ignore[assignment]
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise ConfigError(str(exc)) from exc

    cfg = KlarityConfig()
    for section in parser.sections():
        if section not in _SECTIONS:
            raise ConfigError(f"unknown section [{section}]")
        attr = _SECTIONS[section]
        target = cfg if attr is None else getattr(cfg, attr)
        _fill(target, dict(parser.items(section)), section)

    cfg.log_level = cfg.log_level.upper()
    if cfg.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"[klarity] log_level: unsupported {cfg.log_level!r}")
    if cfg.screen.bkcmd not in (0, 1, 2, 3):
        raise ConfigError("[screen] bkcmd must be 0..3")
    if cfg.screen.quote_style not in ("backslash", "replace"):
        raise ConfigError("[screen] quote_style must be 'backslash' or 'replace'")
    if cfg.screen.baud <= 0:
        raise ConfigError("[screen] baud must be positive")
    if cfg.push.min_interval < 0.02 or cfg.push.max_commands_per_second <= 0:
        raise ConfigError("[push] min_interval must be >= 0.02 and max_commands_per_second > 0")
    if not 1 <= cfg.unlock.port <= 65535:
        raise ConfigError("[unlock] port must be 1..65535")
    if cfg.unlock.timeout <= 0 or cfg.unlock.wait < 0:
        raise ConfigError("[unlock] timeout must be > 0 and wait >= 0")
    return cfg


def load_config(path: str | Path | None = None) -> KlarityConfig:
    """Load ``path`` (default ``/data/printer_data/config/klarity.cfg``).

    A missing *default* file yields defaults (the daemon then complains about the unset
    serial port); a missing explicitly requested file is an error.
    """
    explicit = path is not None
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"config file not found: {p}") from None
        return KlarityConfig()
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    return parse_config(text)
