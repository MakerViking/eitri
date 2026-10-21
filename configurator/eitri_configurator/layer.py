"""The Eitri klipper-layer: where it is, how printer.cfg is rendered from it, how it is installed.

The layer (``klipper-layer/``) owns everything Klipper-specific: the ``printer.cfg`` template and
its variables, the macros, the ``qd_*`` extras and ``scripts/overlay-install.sh``, which links
them into the (pristine) Klipper or Kalico clone.  The configurator only decides the *values*
(which MCU port is which, whether the chamber heater is fitted, ...) and shows the result for
review; it never invents Klipper configuration of its own.
"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

from .settings import Settings

#: Optional per-printer overrides, merged over the layer defaults on every render.  A TOML
#: file with a ``[vars]`` table (see klipper-layer/config/eitri-variables.toml for the names).
USER_VARS = "eitri-vars.toml"


class LayerError(RuntimeError):
    """The klipper-layer is missing or unusable; the message tells the user what to do."""


def find_layer(settings: Settings) -> Path | None:
    candidates = [settings.layer_dir] if settings.layer_dir else []
    candidates += [settings.eitri_dir / "klipper-layer", settings.repo_root / "klipper-layer"]
    for cand in candidates:
        if cand and (cand / "config" / "printer.cfg.tmpl").is_file() and (cand / "scripts" / "render-config.py").is_file():
            return cand
    return None


def require_layer(settings: Settings) -> Path:
    layer = find_layer(settings)
    if layer is None:
        raise LayerError(
            "The Eitri klipper-layer was not found (looked in /data/eitri/klipper-layer and next to the "
            "configurator), so printer.cfg cannot be generated."
        )
    return layer


def _renderer(layer: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("eitri_layer_render_config", layer / "scripts" / "render-config.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def variables(settings: Settings, layer: Path, options: dict[str, Any], detect: dict | None) -> dict[str, Any]:
    """Layer defaults, then what the wizard knows, then the user's own overrides."""
    mod = _renderer(layer)
    values = mod.load_vars(layer / "config" / "eitri-variables.toml")
    ports = {b["role"]: b["port"] for b in (detect or {}).get("boards", []) if b.get("role") in ("mainboard", "toolhead") and b.get("port")}
    # Which UART carries which board is only ever taken from detection (the layer's defaults are the
    # reference unit's wiring and just a starting point).
    if "toolhead" in ports:
        values["mcu_toolhead_serial"] = ports["toolhead"]
    if "mainboard" in ports:
        values["mcu_mainboard_serial"] = ports["mainboard"]
    values["printer_data"] = str(settings.printer_data)
    values["chamber_enabled"] = bool(options["chamber_heater"] and options["chamber_ack"])
    values["plr_enabled"] = bool(options["power_loss_recovery"])
    values["adxl_enabled"] = options["accelerometer"] == "adxl345"
    user = settings.config_dir / USER_VARS
    if user.is_file():
        try:
            values.update(mod.load_vars(user))
        except (mod.RenderError, OSError, tomllib.TOMLDecodeError) as exc:
            raise LayerError(f"{USER_VARS} is not valid: {exc}") from exc
    return values


def render_printer_cfg(settings: Settings, header: str, options: dict[str, Any], detect: dict | None) -> str:
    layer = require_layer(settings)
    mod = _renderer(layer)
    values = variables(settings, layer, options, detect)
    try:
        body = mod.render((layer / "config" / "printer.cfg.tmpl").read_text(encoding="utf-8"), values)
    except mod.RenderError as exc:
        raise LayerError(f"printer.cfg template: {exc}") from exc
    return header + body


def overlay_command(settings: Settings, layer: Path) -> list[str]:
    """Link the layer's extras and config set into the Klipper/Kalico clone and printer_data."""
    return [
        "bash", str(layer / "scripts" / "overlay-install.sh"), "--quiet",
        "--klipper-dir", str(settings.data_dir / "firmware" / "klipper"),
        "--printer-data", str(settings.printer_data),
        "--flavor-file", str(settings.flavor_file),
        "--owner", "eitri:eitri",
    ]
