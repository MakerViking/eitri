"""Hardware and service options.

The schema below is the single source of truth: it validates input, supplies
defaults, drives the template context *and* is served to the frontend, which
renders the Options step from it.  Adding a toggle means adding one entry here.
"""
from __future__ import annotations

import re
from typing import Any

FLAVORS = {
    "klipper": {
        "name": "Klipper",
        "tag": "Mainline",
        "summary": "The upstream project, untouched. Conservative and widely documented.",
        "pros": [
            "Reference implementation - every guide and macro targets it",
            "Slowest-moving, most predictable update cadence",
            "Largest community for troubleshooting",
        ],
        "cons": [
            "Fewer extras in the box (no built-in danger options, newer probe tooling lands later)",
        ],
        "recommended": True,
    },
    "kalico": {
        "name": "Kalico",
        "tag": "Feature fork",
        "summary": "KalicoCrew's fork (formerly Danger Klipper). Same core, more experimental features.",
        "pros": [
            "Extra features merged earlier than upstream",
            "Plugin loader and richer macro tooling",
            "Drop-in compatible with Klipper configs",
        ],
        "cons": [
            "Host and MCU firmware differ from mainline - switching later means reflashing both boards",
            "Smaller community, faster-moving surface",
        ],
        "recommended": False,
    },
}

_HOST_RE = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
_PORT_RE = re.compile(r"|/dev/tty[A-Za-z0-9_.-]{1,24}")     # matched with fullmatch: `$` admits a trailing newline


def _choice(value, label, help_=""):
    return {"value": value, "label": label, "help": help_}


SCHEMA: list[dict[str, Any]] = [
    {
        "id": "hardware", "title": "Hardware", "blurb": "What is actually fitted to your printer.",
        "fields": [
            {
                "id": "accelerometer", "type": "choice", "default": "adxl345",
                "label": "Input-shaper accelerometer",
                "help": "Used for resonance measurement in the calibration step. The toolhead ADXL345 ships on the stock printer.",
                "choices": [
                    _choice("adxl345", "Toolhead ADXL345", "As shipped"),
                    _choice("none", "None", "Skip input shaper calibration"),
                ],
            },
            {
                "id": "chamber_heater", "type": "bool", "default": False,
                "label": "Active chamber heater",
                "help": "Off by default. When enabled it is gated by a bed/chamber thermal interlock, and heater runaway protection stays active at all times.",
                "caution": True,
            },
            {
                "id": "chamber_ack", "type": "bool", "default": False,
                "label": "I understand the chamber heater is interlocked with the bed heater",
                "help": "Required to enable the chamber heater. Runaway protection cannot be disabled from here.",
                "depends_on": "chamber_heater",
            },
        ],
    },
    {
        "id": "printing", "title": "Printing", "blurb": "Behaviour that changes how jobs run.",
        "fields": [
            {
                "id": "power_loss_recovery", "type": "bool", "default": True,
                "label": "Power-loss recovery",
                "help": "Resume an interrupted print after a power cut.",
            },
        ],
    },
    {
        "id": "services", "title": "Services", "blurb": "What runs alongside Klipper.",
        "fields": [
            {
                "id": "klarity", "type": "bool", "default": True,
                "label": "Klarity screen daemon",
                "help": "Drives the stock touchscreen over its serial link. The display firmware is never reflashed.",
            },
            {
                "id": "klarity_port", "type": "text", "default": "/dev/ttyS1",
                "label": "Screen serial port",
                "help": "The UART the display is wired to. The stock software uses /dev/ttyS1; that is not confirmed on the mainline kernel until probed on the printer. Klarity stays idle while this is empty.",
                "depends_on": "klarity", "mono": True,
            },
            {
                "id": "camera", "type": "bool", "default": True,
                "label": "Webcam stream (1920x1080 MJPEG)",
                "help": "ustreamer at full resolution on :8080, shown in Fluidd/Mainsail and available as MJPEG or RTSP.",
            },
            {
                "id": "go2rtc", "type": "bool", "default": False,
                "label": "go2rtc re-streamer",
                "help": "Adds WebRTC / RTSP outputs for external viewers or streaming setups.",
                "depends_on": "camera",
            },
            {
                "id": "ui", "type": "choice", "default": "fluidd",
                "label": "Web interface",
                "help": "Served on port 80. You can switch later.",
                "choices": [
                    _choice("fluidd", "Fluidd", "Compact, fast"),
                    _choice("mainsail", "Mainsail", "Roomier, more panels"),
                ],
            },
        ],
    },
    {
        "id": "access", "title": "Access", "blurb": "Who may control the printer from the network.",
        "fields": [
            {
                "id": "api_access", "type": "choice", "default": "lan",
                "label": "Network access to the printer",
                "help": "Anyone Moonraker trusts can start prints, edit the configuration and, through Klipper's shell commands, run programs on the printer. Moonraker itself only listens on the printer; the web interface on port 80 is the way in.",
                "choices": [
                    _choice("lan", "Trust my home network", "Devices on private addresses need no sign-in"),
                    _choice("signin", "Require sign-in", "Only the printer itself is trusted; create a user over SSH first"),
                ],
            },
        ],
    },
    {
        "id": "updates", "title": "Updates", "blurb": "How Eitri keeps itself current.",
        "fields": [
            {
                "id": "channel", "type": "choice", "default": "eitri",
                "label": "Update channel",
                "help": "Eitri releases are tested bundles with matching MCU firmware and one-click rollback. Upstream mode follows git directly and needs a ~300 MB compiler add-on.",
                "choices": [
                    _choice("eitri", "Eitri releases", "Tested, signed, rollback-able"),
                    _choice("upstream", "Follow upstream", "git pull, any branch"),
                ],
            },
        ],
    },
]


def _fields() -> dict[str, dict]:
    return {f["id"]: f for g in SCHEMA for f in g["fields"]}


FIELDS = _fields()


def defaults() -> dict[str, Any]:
    return {fid: f["default"] for fid, f in FIELDS.items()}


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def validate(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Merge `raw` over defaults, rejecting unknown keys and bad values."""
    raw = dict(raw or {})
    out = defaults()
    errors: dict[str, str] = {}
    for key, val in raw.items():
        f = FIELDS.get(key)
        if f is None:
            errors[key] = "unknown option"
            continue
        if f["type"] == "bool":
            if not isinstance(val, bool):
                errors[key] = "must be true or false"
                continue
        elif f["type"] == "choice":
            if val not in {c["value"] for c in f["choices"]}:
                errors[key] = "not an available choice"
                continue
        elif f["type"] == "text":
            if not isinstance(val, str) or not _PORT_RE.fullmatch(val):
                errors[key] = "must be empty or a /dev/tty* path"
                continue
        out[key] = val
    if out["chamber_heater"] and not out["chamber_ack"]:
        errors["chamber_ack"] = "acknowledge the thermal interlock to enable the chamber heater"
    if not out["chamber_heater"]:
        out["chamber_ack"] = False
    if not out["camera"]:
        out["go2rtc"] = False
    if errors:
        raise ValidationError(errors)
    return out


def validate_hostname(name: str) -> str:
    name = (name or "").strip().lower()
    if not _HOST_RE.fullmatch(name):
        raise ValidationError({"hostname": "use 1-63 letters, digits or hyphens (no leading/trailing hyphen)"})
    return name
