"""The camera's RTSP sign-in, for the separate "Camera sign-in" page (never part of the wizard flow).

go2rtc's RTSP output is password protected.  `camera/bin/eitri-go2rtc-auth ensure` generates the
credentials on go2rtc's first start into `<config>/go2rtc-auth.env` (`EITRI_RTSP_USER`,
`EITRI_RTSP_PASS`; mode 0600, readable by the eitri user, which is who the configurator runs as).
This module only reads that file; it never creates or changes the password.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

PORT = 8554
STREAM = "eitri_cam"


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1]
    return raw


def read_credentials(path: Path) -> dict[str, str] | None:
    """{"user", "password"} from the env file, or None while go2rtc has not generated it yet."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    found: dict[str, str] = {}
    for line in lines:
        key, sep, val = line.partition("=")
        if sep and key in ("EITRI_RTSP_USER", "EITRI_RTSP_PASS"):
            found[key] = _value(val)            # the last assignment wins, as in the shell helper
    user, password = found.get("EITRI_RTSP_USER", ""), found.get("EITRI_RTSP_PASS", "")
    if not user or not password or any(ord(c) < 32 or ord(c) == 127 for c in user + password):
        return None
    return {"user": user, "password": password}


def describe(path: Path, camera: bool, go2rtc: bool, data_dir: Path | None = None) -> dict[str, Any]:
    creds = read_credentials(path)
    shown = path
    if data_dir is not None:
        try:
            shown = Path("/data") / path.relative_to(data_dir)       # always the on-printer name, also for a relocated data dir (mock)
        except ValueError:
            pass
    return {
        "configured": creds is not None,
        "user": creds["user"] if creds else None,
        "password": creds["password"] if creds else None,
        "port": PORT, "stream": STREAM,
        "camera_enabled": camera, "gateway_enabled": camera and go2rtc,
        "file": str(shown),
    }
