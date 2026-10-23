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

    def verify_image(self) -> None:
        """The image must still hash to what was recorded when it was read from the board; a rotted or truncated copy
        must never be written back to a board."""
        p, want = self.app_path, self.meta.get("app_sha256")
        board = self.meta.get("board")
        if not p or not want or not os.path.isfile(p) or sha256_file(p) != want:
            raise FlashError("the saved firmware image for the %s board in %s is damaged (its checksum no longer matches)"
                             % (board, self.directory), code="backup_corrupt", step="rollback", board=board,
                             recovery="Use an older entry under the backup directory, or flash the board by hand (see docs/recovery.md).")


# The backup directory belongs to the service user, and the flasher may run as root (eitri-update, through sudo).
# So root never follows a symlink there: it walks the tree one directory at a time with O_NOFOLLOW directory
# handles, creates every file relative to such a handle with O_EXCL | O_NOFOLLOW, and checks that the entry
# directory it just made is still its own. A link planted by the service user can make a backup fail, but can
# never redirect root's writes to another file.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def _open_dir(name: str, dir_fd: Optional[int] = None, create: bool = False) -> int:
    if create:
        try:
            os.mkdir(name, 0o755, dir_fd=dir_fd)
        except FileExistsError:
            pass
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _unlink_at(dfd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dfd)          # removes a planted link itself, never what it points to
    except FileNotFoundError:
        pass


def _write_at(dfd: int, name: str, data: bytes, mode: int) -> None:
    """Write *name* inside the directory *dfd*: a fsynced temp file renamed into place."""
    tmp = ".%s.tmp" % name
    _unlink_at(dfd, tmp)
    fd = os.open(tmp, _NEW_FILE, mode, dir_fd=dfd)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        _unlink_at(dfd, tmp)
        raise


def _sync_dir(dfd: int) -> None:
    """Make a directory's entries durable (after the files in it were renamed into place)."""
    os.fsync(dfd)


def _read_at(dfd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


class BackupStore:
    def __init__(self, root: str) -> None:
        self.root = root

    def check_writable(self) -> None:
        try:
            os.makedirs(self.root, exist_ok=True)
            rfd = _open_dir(self.root)
            try:
                _unlink_at(rfd, ".write-test")
                fd = os.open(".write-test", _NEW_FILE, 0o600, dir_fd=rfd)
                os.write(fd, b"ok")
                os.close(fd)
                os.unlink(".write-test", dir_fd=rfd)
            finally:
                os.close(rfd)
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
        try:
            os.makedirs(self.root, exist_ok=True)
            rfd = _open_dir(self.root)
            try:
                bfd = _open_dir("backup", dir_fd=rfd, create=True)
                try:
                    return self._save_in(bfd, board, app, dictionary, info)
                finally:
                    os.close(bfd)
            finally:
                os.close(rfd)
        except OSError as exc:
            raise FlashError("cannot write the %s backup under %s: %s" % (board, self.root, exc),
                             code="backup_unwritable", step="backup", board=board,
                             recovery="Check that %s and its backup directory are ordinary directories (not links)." % self.root) from exc

    def _save_in(self, bfd: int, board: str, app: Optional[bytes], dictionary: Optional[Dict[str, Any]],
                 info: Dict[str, Any]) -> BackupEntry:
        while True:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            name = "%s-%s" % (stamp, board)
            try:
                os.mkdir(name, 0o755, dir_fd=bfd)
                break
            except FileExistsError:
                continue
        directory = os.path.join(self.root, "backup", name)
        dfd = os.open(name, _DIR_FLAGS, dir_fd=bfd)
        try:
            if os.fstat(dfd).st_uid != os.geteuid():
                raise FlashError("the new backup directory %s was replaced while it was being written" % directory,
                                 code="backup_unwritable", step="backup", board=board)
            meta: Dict[str, Any] = {"board": board, "created": stamp, **info}
            if app is not None:
                _write_at(dfd, "app.bin", app, 0o644)
                meta["app_file"] = "app.bin"
                meta["app_sha256"] = hashlib.sha256(app).hexdigest()
                meta["app_size"] = len(app)
                # Read it back: a backup that cannot be re-read is not a backup.
                if hashlib.sha256(_read_at(dfd, "app.bin")).hexdigest() != meta["app_sha256"]:
                    raise FlashError("backup file failed read-back verification", code="backup_unwritable", step="backup", board=board)
            if dictionary is not None:
                _write_at(dfd, "dictionary.json", json.dumps(dictionary, separators=(",", ":")).encode(), 0o644)
                meta["dictionary_file"] = "dictionary.json"
            # meta.json is what makes the entry findable (entries() skips a directory without a readable one), so it
            # is written atomically and the directory entries are made durable BEFORE the caller erases anything.
            _write_at(dfd, "meta.json", json.dumps(meta, indent=2, sort_keys=True).encode(), 0o644)
            _sync_dir(dfd)
            _sync_dir(bfd)
        finally:
            os.close(dfd)
        return BackupEntry(directory, meta)

    def damaged(self, board: str) -> List[str]:
        """Entries that hold an image but whose meta.json cannot be read (entries() cannot list them)."""
        base = os.path.join(self.root, "backup")
        out: List[str] = []
        if not os.path.isdir(base):
            return out
        for name in sorted(os.listdir(base)):
            if not name.endswith("-" + board):
                continue
            try:
                with open(os.path.join(base, name, "meta.json"), "r", encoding="utf-8") as fh:
                    json.load(fh)
            except (OSError, ValueError):
                out.append(os.path.join(base, name))
        return out

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
