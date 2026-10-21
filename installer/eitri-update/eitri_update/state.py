# SPDX-License-Identifier: Apache-2.0
"""Persistent client state under /data/eitri/state (atomic JSON)."""
from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List, Optional

from eitri_mcuflash.fsutil import atomic_write

DEFAULT_ROOT = "/data/eitri"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    def __init__(self, root: str = DEFAULT_ROOT) -> None:
        self.root = root
        self.path = os.path.join(root, "state", "update.json")

    # -- plain files other components read -------------------------------------
    def _text(self, name: str, default: str) -> str:
        try:
            with open(os.path.join(self.root, name)) as fh:
                return fh.read().strip() or default
        except OSError:
            return default

    @property
    def flavor(self) -> str:
        value = self._text("flavor", "klipper")
        return value if value in ("klipper", "kalico") else "klipper"

    @property
    def channel(self) -> str:
        value = self._text("channel", "eitri")
        return value if value in ("eitri", "upstream") else "eitri"

    def set_text(self, name: str, value: str) -> None:
        """Replace a small file; keep its owner. eitri/flavor and eitri/channel belong to the service user (the
        configurator rewrites them, and /data/eitri is root's so it cannot create them): a root-run client must
        not turn them into root-owned files."""
        path = os.path.join(self.root, name)
        try:
            st = os.stat(path)
        except OSError:
            st = None
        atomic_write(path, value + "\n")
        if st is not None and os.geteuid() == 0:
            os.chown(path, st.st_uid, st.st_gid)

    # -- update.json -------------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path) as fh:
                doc = json.load(fh)
                return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, doc: Dict[str, Any]) -> None:
        atomic_write(self.path, json.dumps(doc, indent=2, sort_keys=True) + "\n")

    def update(self, **changes: Any) -> Dict[str, Any]:
        doc = self.load()
        doc.update(changes)
        self.save(doc)
        return doc

    def add_history(self, event: str, **info: Any) -> None:
        doc = self.load()
        hist: List[Dict[str, Any]] = doc.get("history", [])
        hist.append({"at": _now(), "event": event, **info})
        doc["history"] = hist[-50:]
        self.save(doc)

    @property
    def installed_version(self) -> Optional[str]:
        return self.load().get("installed_version") or self._text("release", "") or None
