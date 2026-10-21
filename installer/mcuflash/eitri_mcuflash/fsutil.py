# SPDX-License-Identifier: Apache-2.0
"""Crash-safe file writes and file hashing shared by eitri-mcuflash and eitri-update (stdlib only)."""
from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Optional


def atomic_write(path: str, text: str, *, mode: Optional[int] = None, prefix: str = ".tmp-") -> None:
    """Write *text* to *path* via a fsynced temp file and rename: a power cut leaves the old or the new file.

    The directory is created if missing. The file is private (0600) unless *mode* says otherwise.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def fsync_dir(path: str) -> None:
    """Make the directory entries of *path* durable (a new file, rename or symlink in it).  atomic_write fsyncs the
    file's data but not the directory that names it."""
    try:
        fd = os.open(path or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass                                     # some filesystems cannot fsync a directory; nothing more to do
    finally:
        os.close(fd)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
