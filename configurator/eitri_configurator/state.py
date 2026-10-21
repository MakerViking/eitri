"""Wizard progress, persisted so a page reload (or switching from PC to phone)
resumes exactly where the user left off."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STEPS = [
    "welcome", "network", "detect", "flavor", "options",
    "review", "flash", "calibrate", "done",
]


def default_state() -> dict[str, Any]:
    return {
        "step": "welcome",
        "furthest": "welcome",
        "flavor": "klipper",
        "options": None,            # None -> defaults
        "hostname": None,
        "assign": None,             # manual role->port override {mainboard, toolhead}
        "detect": None,             # last detection result
        "applied": None,            # summary of last config write
        "flash": None,              # per-board outcome of the last flash
        "calibration": {},          # zoffset/shaper/mesh -> done|skipped
        "completed": False,
    }


def fsync_dir(path: Path) -> None:
    """Make a rename or a new file in `path` durable (best effort: some filesystems refuse a directory fsync)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, data: str | bytes, mode: int = 0o644) -> None:
    """Write via tmp+rename so a power cut never leaves a torn file on /data.

    The data is fsynced before the rename and the directory afterwards, so after a power cut
    the file is either entirely the old content or entirely the new content.  The temporary
    file is created 0600 by mkstemp and only receives its final mode once it is complete.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class StateStore:
    # Keys the client may set directly; everything else is server-owned.
    CLIENT_KEYS = {"step", "flavor", "hostname", "calibration", "options"}

    def __init__(self, path: Path):
        self.path = path
        self.data = default_state()
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if k in self.data})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data, indent=2))

    def update(self, **kw: Any) -> None:
        self.data.update(kw)
        if kw.get("step") in STEPS:
            cur = STEPS.index(self.data["furthest"]) if self.data["furthest"] in STEPS else 0
            self.data["furthest"] = STEPS[max(cur, STEPS.index(kw["step"]))]
        self.save()

    def reset(self) -> None:
        self.data = default_state()
        self.save()
