"""Read pinned component versions from release.lock (TOML) for display.

The configurator never *acts* on these pins - the installer does.  We only show the user which
flavor build they are choosing.  A missing or broken file yields ``{}`` and the UI shows dashes.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def version(lock: dict[str, Any], *keys: str) -> str:
    """lock[keys...]["version"] as text, or "-" when any level is missing."""
    node: Any = lock
    for key in keys:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return str(node.get("version") or "-") if isinstance(node, dict) else "-"


def flavor_card(lock: dict[str, Any], flavor: str) -> dict[str, str]:
    fw = lock.get("firmware", {}).get(flavor, {})
    return {"version": str(fw.get("version") or "-"), "commit": str(fw.get("commit") or "")[:10]}
