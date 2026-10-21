"""Read pinned component versions from release.lock (TOML) for display.

The configurator never *acts* on these pins — the installer does.  We only
show the user which flavor build they are choosing.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

# Shown when release.lock cannot be read (e.g. a dev checkout without it), flagged
# `source: "fallback"`.  Keys mirror release.lock's own tables.
FALLBACK: dict[str, Any] = {
    "firmware": {
        "klipper": {"version": "master-a08fc79", "commit": "a08fc79fcfa342e5b95ac02a9a8b271f453523e9"},
        "kalico": {"version": "v2026.10.00", "commit": "3526de3bfbdc64f40001a4070b95d28b79fa4147"},
    },
    "moonraker": {"version": "v0.11.0"},
    "python": {"version": "3.14"},
    "os": {"buildroot": {"version": "2026.02.3"}, "linux": {"version": "6.18.57"}},
    "ui": {"fluidd": {"version": "v1.37.7"}, "mainsail": {"version": "v2.19.0"}},
    "camera": {"ustreamer": {"version": "v6.67"}, "go2rtc": {"version": "v1.9.14"}},
    "release_status": "pre-alpha",
}


def load(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {**FALLBACK, "source": "fallback"}
    # Fill gaps from the fallback so the UI never has to special-case missing keys.
    merged: dict[str, Any] = {}
    for key, default in FALLBACK.items():
        got = data.get(key)
        merged[key] = _deep_merge(default, got) if isinstance(default, dict) and isinstance(got, dict) else (got if got is not None else default)
    merged["source"] = "release.lock"
    return merged


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def flavor_card(lock: dict[str, Any], flavor: str) -> dict[str, str]:
    fw = lock["firmware"].get(flavor, {})
    return {
        "version": str(fw.get("version") or "-"),
        "commit": str(fw.get("commit") or "")[:10],
    }
