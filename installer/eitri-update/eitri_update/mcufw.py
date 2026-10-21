# SPDX-License-Identifier: Apache-2.0
"""Delivering MCU firmware with a release: ``mcu-firmware-<version>-<flavor>.tar.zst`` into /data/eitri/mcu/firmware.

The archive unpacks to ``<flavor>/manifest.json`` and ``<flavor>/<board>/klipper.bin`` (installer/docs/BUNDLE.md).  The
archive's digest comes from the signed channel manifest.  It is still treated as hostile input: members are validated
before anything is written, only regular files and directories below ``<flavor>/`` are taken, nothing is extracted
through links, and the new tree replaces the old one with two renames; the old one is kept until the update is
confirmed (``finish``) or undone (``restore``), because a rolled-back host needs the firmware that matches it.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tarfile
from typing import Any, Dict, List

from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.fsutil import fsync_dir

MAX_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 256


def _bad(msg: str) -> FlashError:
    return FlashError("MCU firmware archive: " + msg, code="bad_archive", step="mcu-firmware")


def _clean(name: str) -> List[str]:
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if name.startswith("/") or ".." in parts or "\\" in name or "\0" in name:
        raise _bad("unsafe path %r" % name)
    return parts


def check_members(members: List[Any], flavor: str) -> None:
    """Refuse an archive that is not plain files and directories, is too big, or puts anything outside ``<flavor>/``
    (other flavors' trees are ignored, not an error: one archive may carry both)."""
    if len(members) > MAX_MEMBERS:
        raise _bad("more than %d members" % MAX_MEMBERS)
    total = 0
    mine = 0
    for m in members:
        parts = _clean(m.name)
        if not (m.isreg() or m.isdir()):
            raise _bad("%s is not a regular file or directory" % m.name)
        if not parts:
            continue
        if parts[0] == flavor:
            mine += 1
            if m.isreg():
                total += m.size
    if total > MAX_BYTES:
        raise _bad("unpacks to more than %d MiB" % (MAX_BYTES >> 20))
    if not mine:
        raise _bad("no %s/ tree in it" % flavor)


def _open_stream(archive: str):
    proc = subprocess.Popen(["zstd", "-dc", "--", archive], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc


def _members(archive: str) -> List[Any]:
    proc = _open_stream(archive)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            return list(tf)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise _bad("cannot read it: %s" % exc)
    finally:
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()


def stage(archive: str, flavor: str, dest: str) -> None:
    """Extract ``<flavor>/`` of *archive* into the new directory *dest* (which becomes ``<flavor>``)."""
    check_members(_members(archive), flavor)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    proc = _open_stream(archive)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for m in tf:
                parts = _clean(m.name)
                if not parts or parts[0] != flavor:
                    continue
                target = os.path.join(dest, *parts[1:])
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    raise _bad("cannot read %s" % m.name)
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(target, 0o755 if m.mode & stat.S_IXUSR else 0o644)
    except (tarfile.TarError, OSError, EOFError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise _bad("cannot unpack it: %s" % exc)
    except FlashError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()
    manifest = os.path.join(dest, "manifest.json")
    try:
        with open(manifest) as fh:
            doc: Dict[str, Any] = json.load(fh)
    except (OSError, ValueError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise _bad("%s/manifest.json is missing or not JSON (%s)" % (flavor, exc))
    if not isinstance(doc, dict) or doc.get("flavor") not in (None, flavor) or not isinstance(doc.get("boards"), dict) or not doc["boards"]:
        shutil.rmtree(dest, ignore_errors=True)
        raise _bad("%s/manifest.json does not describe %s boards" % (flavor, flavor))


def _old(fw_root: str, flavor: str) -> str:
    return os.path.join(fw_root, ".%s.release-old" % flavor)


def install(archive: str, flavor: str, fw_root: str) -> Dict[str, Any]:
    """Put the release's firmware at ``fw_root/<flavor>``; returns what restore() needs."""
    os.makedirs(fw_root, exist_ok=True)
    live, new, old = os.path.join(fw_root, flavor), os.path.join(fw_root, ".%s.release-new" % flavor), _old(fw_root, flavor)
    stage(archive, flavor, new)
    os.sync()
    if os.path.isdir(old):
        # An earlier attempt of the same update (interrupted before it was recorded) already set the original aside:
        # that is the tree to keep, whatever is live now.
        had_old = True
        shutil.rmtree(live, ignore_errors=True)
    else:
        had_old = os.path.isdir(live)
        if had_old:
            os.rename(live, old)
    os.rename(new, live)
    fsync_dir(fw_root)
    return {"had_old": had_old}


def restore(fw_root: str, flavor: str, had_old: bool) -> None:
    """Undo install(): the previous firmware tree comes back (or the directory goes away when there was none)."""
    live, old = os.path.join(fw_root, flavor), _old(fw_root, flavor)
    if had_old and os.path.isdir(old):
        shutil.rmtree(live, ignore_errors=True)
        os.rename(old, live)
    elif not had_old:
        shutil.rmtree(live, ignore_errors=True)
    fsync_dir(fw_root)


def finish(fw_root: str, flavor: str) -> None:
    shutil.rmtree(_old(fw_root, flavor), ignore_errors=True)
