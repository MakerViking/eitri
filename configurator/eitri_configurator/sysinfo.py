"""Read-only facts about the host, for the Welcome step and the status chip."""
from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from . import __version__
from .settings import Settings


def _read(p: str) -> str:
    try:
        return Path(p).read_text().strip("\x00\n ")
    except OSError:
        return ""


def _os_release() -> dict[str, str]:
    out = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip('"')
    return out


def _meminfo() -> dict[str, int]:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.split():
            out[k] = int(v.split()[0]) // 1024
    return out


def _cpu_temp() -> float | None:
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        t = _read(str(zone / "temp"))
        if t.lstrip("-").isdigit():
            return round(int(t) / 1000, 1)
    return None


def collect(settings: Settings, lock: dict[str, Any]) -> dict[str, Any]:
    if settings.mock:
        return {
            "mock": True,
            "version": __version__,
            "release": lock["release_status"],
            "board": {"model": "Firefly ROC-RK3328-CC", "soc": "Rockchip RK3328", "cores": 4, "arch": "aarch64"},
            "os": {"name": "Eitri OS", "kernel": lock["os"]["linux"]["version"], "buildroot": lock["os"]["buildroot"]["version"]},
            "memory": {"total_mb": 976, "available_mb": 702},
            "storage": {"total_gb": 28.9, "free_gb": 26.4},
            "uptime_s": 94,
            "cpu_temp_c": 47.3,
        }
    mem = _meminfo()
    try:
        st = shutil.disk_usage(settings.data_dir)
        storage = {"total_gb": round(st.total / 1e9, 1), "free_gb": round(st.free / 1e9, 1)}
    except OSError:
        storage = None
    osr = _os_release()
    try:
        uptime = int(float(_read("/proc/uptime").split()[0]))
    except (IndexError, ValueError):
        uptime = None
    return {
        "mock": False,
        "version": __version__,
        "release": lock["release_status"],
        "board": {
            "model": _read("/proc/device-tree/model") or platform.node(),
            "soc": _read("/proc/device-tree/compatible").split("\x00")[-1] or platform.machine(),
            "cores": os.cpu_count(),
            "arch": platform.machine(),
        },
        "os": {"name": osr.get("PRETTY_NAME", osr.get("NAME", "Linux")), "kernel": platform.release(),
               "buildroot": osr.get("VERSION_ID", "")},
        "memory": {"total_mb": mem.get("MemTotal"), "available_mb": mem.get("MemAvailable")},
        "storage": storage,
        "uptime_s": uptime,
        "cpu_temp_c": _cpu_temp(),
    }


def klarity_status(settings: Settings, services_state: str) -> dict[str, Any]:
    """Klarity health.  The daemon may publish /run/klarity/status.json (contract TBD
    with the Klarity component); without it we can only report the unit state."""
    if settings.mock:
        return {
            "service": services_state, "connected": services_state == "active",
            "display": "TJC 800x480", "port": None, "baud": None,
            "version": "0.1.0", "source": "mock",
        }
    snap: dict[str, Any] = {}
    try:
        snap = json.loads(settings.klarity_status_file.read_text())
    except (OSError, ValueError):
        pass
    return {
        "service": services_state,
        "connected": bool(snap.get("connected")) if snap else None,   # None = unknown
        "display": snap.get("display"), "port": snap.get("port"), "baud": snap.get("baud"),
        "version": snap.get("version"), "source": "status-file" if snap else "unit-only",
    }
