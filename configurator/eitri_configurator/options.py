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
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DNS_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
MAX_EXTRA_HOSTNAMES = 16


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
                "id": "klarity_port", "type": "text", "default": "/dev/ttyS1", "check": "tty", "placeholder": "/dev/ttyS1",
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
        "id": "access", "title": "Web interface sign-in", "blurb": "Who may use the printer's web interface and slicer connection from the network.",
        "fields": [
            {
                "id": "api_access", "type": "choice", "default": "lan",
                "label": "Who may use the web interface",
                "help": "Anyone trusted here can start prints, edit the configuration and, through Klipper's shell commands, run programs on the printer. Everything, slicers and apps included, connects on port 80 (Fluidd or Mainsail); Moonraker itself only listens on the printer. SSH is a separate, optional step at the end.",
                "choices": [
                    _choice("lan", "Trust my home network", "Devices on private addresses need no sign-in"),
                    _choice("signin", "Require sign-in", "Only the printer itself is trusted; needs a user created over SSH first (see Remote access)"),
                ],
            },
            {
                "id": "extra_hostnames", "type": "text", "default": "", "check": "hostnames", "placeholder": "printer.home.arpa",
                "label": "Other names for this printer",
                "help": "The web interface answers to the printer's IP address, single names such as eitri, and names ending in .local. If your router gives it another name, list it here (separate several with spaces); any other name is refused, which stops web pages from reaching the printer through a look-alike name. Saved as eitri-hosts.txt with the configuration files.",
                "mono": True,
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
    {
        "id": "reports", "title": "Crash reports", "blurb": "Whether Eitri may send a report when it recovers from a crash.",
        "fields": [
            {
                "id": "crash_reports", "type": "choice", "default": "off",
                "label": "After a crash",
                "help": "A report holds the end of the system and Klipper logs from before the crash. Addresses, names, serial numbers, keys and file names are removed on the printer before anything leaves it, and it goes to the Eitri developers at tinkeratlas.com, without an account. You can change this any time with the EITRI_CRASH_REPORTS macro, or in eitri-reports.txt with the configuration files.",
                "choices": [
                    _choice("off", "Never send", "Nothing is sent. You can still make a report yourself with the EITRI_REPORT macro"),
                    _choice("ask", "Ask me first", "The web interface asks before each report is sent, and shows where to read it"),
                    _choice("always", "Send automatically", "Reports are sent without asking"),
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
            if not isinstance(val, str):
                errors[key] = "must be text"
                continue
            if f["check"] == "tty":
                if not _PORT_RE.fullmatch(val):
                    errors[key] = "must be empty or a /dev/tty* path"
                    continue
            elif f["check"] == "hostnames":
                try:
                    val = normalize_hostnames(val)
                except ValueError as exc:
                    errors[key] = str(exc)
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


def normalize_hostnames(text: str) -> str:
    """Space-separated, lowercase DNS names; the same rules as /usr/libexec/eitri/eitri-hosts applies."""
    names: list[str] = []
    for entry in re.split(r"[\s,]+", text.strip()):
        if not entry:
            continue
        name = entry.lower().rstrip(".")
        if len(name) > 253 or not _DNS_RE.fullmatch(name):
            raise ValueError(f"{entry[:40]!r} is not a host name (letters, digits, hyphens and dots)")
        if name not in names:
            names.append(name)
    if len(names) > MAX_EXTRA_HOSTNAMES:
        raise ValueError(f"at most {MAX_EXTRA_HOSTNAMES} names")
    return " ".join(names)


def validate_hostname(name: str) -> str:
    name = (name or "").strip().lower()
    if not _HOST_RE.fullmatch(name):
        raise ValidationError({"hostname": "use 1-63 letters, digits or hyphens (no leading/trailing hyphen)"})
    return name
