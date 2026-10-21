# SPDX-License-Identifier: Apache-2.0
"""MCU backups: what was on the board before we touched it.

Katapult boards can be read back verbatim.  SD-card boards cannot, so for those
we record the identity of what was running (data dictionary) and keep any stock
image the operator supplies.  Either way nothing is erased without an entry here.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .errors import FlashError
from .fsutil import sha256_file

MIN_FREE_BYTES = 8 * 1024 * 1024


@dataclass
class BackupEntry:
    directory: str
    meta: Dict[str, Any]

    @property
    def app_path(self) -> Optional[str]:
        name = self.meta.get("app_file")
        return os.path.join(self.directory, name) if name else None

    @property
    def restorable(self) -> bool:
        p = self.app_path
        return bool(p and os.path.isfile(p))


class BackupStore:
    def __init__(self, root: str) -> None:
        self.root = root

    def check_writable(self) -> None:
        try:
            os.makedirs(self.root, exist_ok=True)
            probe = os.path.join(self.root, ".write-test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.unlink(probe)
            free = shutil.disk_usage(self.root).free
        except OSError as exc:
            raise FlashError(
                "cannot write MCU backups to %s: %s" % (self.root, exc),
                code="backup_unwritable",
                step="backup",
                recovery="Free space or choose another --state-dir; flashing is refused without a backup location.",
            ) from exc
        if free < MIN_FREE_BYTES:
            raise FlashError(
                "only %d KiB free in %s" % (free // 1024, self.root), code="backup_unwritable", step="backup"
            )

    def save(
        self,
        board: str,
        *,
        app: Optional[bytes],
        dictionary: Optional[Dict[str, Any]],
        info: Dict[str, Any],
    ) -> BackupEntry:
        # Microsecond resolution and exist_ok=False: two backups taken in the same second must never overwrite
        # each other (the name also sorts the entries oldest to newest).
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        directory = os.path.join(self.root, "backup", "%s-%s" % (stamp, board))
        os.makedirs(os.path.dirname(directory), exist_ok=True)
        while True:
            try:
                os.mkdir(directory)
                break
            except FileExistsError:
                stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                directory = os.path.join(self.root, "backup", "%s-%s" % (stamp, board))
        meta: Dict[str, Any] = {"board": board, "created": stamp, **info}
        if app is not None:
            path = os.path.join(directory, "app.bin")
            with open(path, "wb") as fh:
                fh.write(app)
                fh.flush()
                os.fsync(fh.fileno())
            meta["app_file"] = "app.bin"
            meta["app_sha256"] = hashlib.sha256(app).hexdigest()
            meta["app_size"] = len(app)
            # Read it back: a backup that cannot be re-read is not a backup.
            if sha256_file(path) != meta["app_sha256"]:
                raise FlashError("backup file failed read-back verification", code="backup_unwritable", step="backup", board=board)
        if dictionary is not None:
            with open(os.path.join(directory, "dictionary.json"), "w", encoding="utf-8") as fh:
                json.dump(dictionary, fh, separators=(",", ":"))
            meta["dictionary_file"] = "dictionary.json"
        with open(os.path.join(directory, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
        return BackupEntry(directory, meta)

    def entries(self, board: str) -> List[BackupEntry]:
        base = os.path.join(self.root, "backup")
        out: List[BackupEntry] = []
        if not os.path.isdir(base):
            return out
        for name in sorted(os.listdir(base)):
            if not name.endswith("-" + board):
                continue
            try:
                with open(os.path.join(base, name, "meta.json"), "r", encoding="utf-8") as fh:
                    out.append(BackupEntry(os.path.join(base, name), json.load(fh)))
            except (OSError, ValueError):
                continue
        return out

    def latest_restorable(self, board: str) -> Optional[BackupEntry]:
        for entry in reversed(self.entries(board)):
            if entry.restorable:
                return entry
        return None

    def original_restorable(self, board: str) -> Optional[BackupEntry]:
        """The backup of the firmware the board shipped with.

        Entries tagged origin=stock (written by the installer on the untouched printer) win; otherwise the
        OLDEST restorable entry is the best available guess - later ones hold Eitri's own firmware."""
        restorable = [e for e in self.entries(board) if e.restorable]
        for entry in restorable:
            if entry.meta.get("origin") == "stock":
                return entry
        return restorable[0] if restorable else None

    # -- operator-supplied stock images (e.g. QIDI's qd_mcu.bin) ---------------
    def add_stock_image(self, board: str, source: str) -> str:
        if not os.path.isfile(source):
            raise FlashError("stock firmware image %s not found" % source, code="stock_image", board=board)
        dest_dir = os.path.join(self.root, "stock")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "%s.bin" % board)
        shutil.copyfile(source, dest)
        h = sha256_file(dest)
        with open(dest + ".sha256", "w") as fh:
            fh.write("%s  %s.bin\n" % (h, board))
        return dest

    def stock_image(self, board: str) -> Optional[str]:
        p = os.path.join(self.root, "stock", "%s.bin" % board)
        return p if os.path.isfile(p) else None
