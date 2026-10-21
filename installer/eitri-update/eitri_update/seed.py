# SPDX-License-Identifier: Apache-2.0
"""The data seed on the consumer side: manifest checks and the wrapper around ``eitri-seed-install``.

The seed (``data-seed-<version>-<flavor>.tar.zst``, specification in ``seed/SPEC.md``) is the application layer of
/data: pinned Klipper or Kalico and Moonraker clones, their Python environments, Klarity and the qd_* extras.

This module is standard library only and keeps to Python 3.7 grammar, because ``install.sh`` imports it on the stock
printer (Debian 10) to check a downloaded seed before the point of no return, and ``eitri-update`` uses it on Eitri to
deliver a new seed with a release or to switch flavor.

What is trusted, and where it comes from: the archive's SHA-256 and size come from a *signed* document (``install.json``
for the in-place conversion, the channel ``manifest.json`` for updates).  The seed's own manifest is untrusted by
itself; it is checked against the signed values (the archive digest, the release version, the flavor's pinned
commit) and its content is only used for extra sanity checks and for sizing, never to widen what is accepted.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tarfile
from typing import Any, Dict, List, Optional, Tuple

from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.fsutil import sha256_file
from eitri_mcuflash.proc import Runner, run

FLAVORS = ("klipper", "kalico")
LAYOUT = 1
KIND = "eitri-data-seed"
# The components of seed/SPEC.md section 3, in the order eitri-seed-install places them.  A leftover
# ".<name>.seed-old" next to one of them means an update is waiting for `finish` or `rollback`.
COMPONENTS = ("firmware/klipper", "firmware/moonraker", "venv/klippy", "venv/moonraker", "eitri/klarity/venv",
              "eitri/extras", "eitri/klipper-layer", "eitri/seed")
RECORD = "eitri/state/seed-installed.env"
TOOL_PATHS = ("/usr/libexec/eitri/eitri-seed-install",)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_~-]*$")
_ABI = re.compile(r"^cp3\d{1,2}$")


def archive_name(version: str, flavor: str) -> str:
    return "data-seed-%s-%s.tar.zst" % (version, flavor)


def manifest_name(version: str, flavor: str) -> str:
    return "data-seed-%s-%s.manifest.json" % (version, flavor)


def safe_version(version: str) -> bool:
    return bool(_VERSION.match(version or ""))


def _bad(msg: str, code: str = "seed_manifest") -> FlashError:
    return FlashError("data seed: " + msg, code=code, step="seed-check")


def _glibc() -> Optional[Tuple[int, ...]]:
    try:
        lib, ver = platform.libc_ver()
    except Exception:  # noqa: BLE001 - platform probing must never decide an install
        return None
    if lib != "glibc" or not re.match(r"^\d+(\.\d+)+$", ver or ""):
        return None
    return tuple(int(p) for p in ver.split("."))


def _numtuple(text: str) -> Optional[Tuple[int, ...]]:
    return tuple(int(p) for p in text.split(".")) if re.match(r"^\d+(\.\d+)+$", text or "") else None


def load_manifest(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise _bad("cannot read the seed manifest %s: %s" % (path, exc))
    if not isinstance(doc, dict):
        raise _bad("the seed manifest is not a JSON object")
    return doc


def check_manifest(
    doc: Dict[str, Any],
    *,
    flavor: str,
    version: Optional[str] = None,
    archive: Optional[Dict[str, Any]] = None,
    release_flavor: Optional[Dict[str, Any]] = None,
    expect_abi: Optional[str] = None,
    check_host_libc: bool = False,
) -> Dict[str, Any]:
    """Check a seed manifest against what the signed documents say.  Returns what callers need
    (``commit``, ``describe``, ``uncompressed_size``, ``version``); raises FlashError(code=seed_manifest) otherwise.

    *archive* is ``{"name", "sha256", "size"}`` of the archive as announced by the signed document (and, once it is
    on disk, as measured); *release_flavor* is that release's ``flavors[<flavor>]`` entry (``commit``, ``describe``);
    *expect_abi* the interpreter ABI of the running image (``cp314``); *check_host_libc* compares the glibc the seed
    was linked against with the one this system runs."""
    if flavor not in FLAVORS:
        raise _bad("flavor must be klipper or kalico")
    if doc.get("schema") != 1 or doc.get("kind") != KIND:
        raise _bad("not an Eitri data seed manifest (schema %r, kind %r)" % (doc.get("schema"), doc.get("kind")))
    if doc.get("flavor") != flavor:
        raise _bad("this is a %s seed, a %s seed was asked for" % (doc.get("flavor"), flavor))
    if doc.get("layout") != LAYOUT:
        raise _bad("seed layout %r is not understood (this tool knows layout %d)" % (doc.get("layout"), LAYOUT), "seed_layout")
    seed_version = doc.get("version")
    if not isinstance(seed_version, str) or not _VERSION.match(seed_version):
        raise _bad("the manifest has no valid version")
    if version is not None and seed_version != version:
        raise _bad("the seed is version %s but the release is %s; one release ships exactly its own seed" % (seed_version, version))
    arc = doc.get("archive")
    if not isinstance(arc, dict):
        raise _bad("the manifest has no archive section")
    if arc.get("name") != archive_name(seed_version, flavor):
        raise _bad("archive name %r does not follow data-seed-<version>-<flavor>.tar.zst" % (arc.get("name"),))
    if arc.get("compression") != "zstd" or arc.get("tar_format") != "gnu":
        raise _bad("the archive must be a GNU tar compressed with zstd")
    if not _SHA256.match(str(arc.get("sha256", ""))):
        raise _bad("the manifest's archive sha256 is not 64 lowercase hex digits")
    for key in ("size", "uncompressed_size"):
        if not isinstance(arc.get(key), int) or isinstance(arc.get(key), bool) or arc[key] <= 0:
            raise _bad("archive.%s is missing" % key)
    if archive is not None:
        for key in ("name", "sha256", "size"):
            if key in archive and archive[key] != arc.get(key):
                raise _bad("archive.%s in the manifest (%r) does not match the signed value (%r)" % (key, arc.get(key), archive[key]))
    target = doc.get("target") or {}
    if target.get("arch") != "aarch64":
        raise _bad("the seed is built for %r, this is an aarch64 printer" % (target.get("arch"),))
    abi = str(target.get("python_abi", ""))
    if not _ABI.match(abi):
        raise _bad("the manifest has no valid python_abi")
    if expect_abi and abi != expect_abi:
        raise _bad("the seed's Python environments are for %s but this system runs %s: it needs a seed built for this image"
                   % (abi, expect_abi), "seed_abi")
    if check_host_libc:
        need, have = _numtuple(str(target.get("glibc_needed", ""))), _glibc()
        if need is not None and have is not None and have < need:
            raise _bad("the seed needs glibc %s, this system has %s" % (target.get("glibc_needed"), ".".join(map(str, have))), "seed_abi")
    fw = (doc.get("sources") or {}).get("firmware") or {}
    if fw.get("name") != flavor:
        raise _bad("the seed's firmware tree is %r, not %s" % (fw.get("name"), flavor))
    commit, describe = str(fw.get("commit", "")), str(fw.get("describe", ""))
    if not _COMMIT.match(commit) or not describe:
        raise _bad("the manifest does not name the firmware commit and its git describe string")
    if release_flavor:
        want_commit, want_describe = str(release_flavor.get("commit") or ""), str(release_flavor.get("describe") or "")
        # The release pins the host firmware that the MCU images were built from: the seed must carry that exact tree.
        if want_commit and not commit.startswith(want_commit.lower()):
            raise _bad("the seed carries %s commit %s but the release pins %s; host and MCU firmware would not match"
                       % (flavor, commit[:12], want_commit[:12]), "seed_commit")
        if want_describe and describe != want_describe:
            raise _bad("the seed's firmware version is %s but the release pins %s" % (describe, want_describe), "seed_commit")
    return {"version": seed_version, "commit": commit, "describe": describe, "uncompressed_size": arc["uncompressed_size"],
            "python_abi": abi, "archive": arc}


def check_archive_file(path: str, expected_sha256: str, expected_size: Optional[int] = None) -> None:
    """The file on disk is the one the signed document announced (size, then SHA-256)."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise _bad("cannot read %s: %s" % (path, exc), "seed_integrity")
    if expected_size is not None and size != expected_size:
        raise _bad("%s is %d bytes, the signed document says %d" % (os.path.basename(path), size, expected_size), "seed_integrity")
    if sha256_file(path) != expected_sha256:
        raise _bad("%s does not match the signed SHA-256" % os.path.basename(path), "seed_integrity")


def peek_archive(path: str) -> Dict[str, bytes]:
    """``eitri/flavor`` and ``eitri/seed/seed-info.env`` read out of the archive, which is streamed only until both have
    been seen (they sort near the front).  Nothing is written to disk."""
    wanted = ("eitri/flavor", "eitri/seed/seed-info.env")
    found: Dict[str, bytes] = {}
    try:
        proc = subprocess.Popen(["zstd", "-dc", "--", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise _bad("cannot run zstd to look into the archive: %s" % exc, "seed_integrity")
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for member in tf:
                if member.name in wanted and member.isreg() and member.size <= 65536:
                    fh = tf.extractfile(member)
                    found[member.name] = fh.read() if fh is not None else b""
                    if len(found) == len(wanted):
                        break
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise _bad("cannot read %s: %s" % (os.path.basename(path), exc), "seed_integrity")
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    return found


def check_archive_contents(path: str, flavor: str, info: Dict[str, Any]) -> None:
    """What the archive says about itself (flavor file, seed-info.env) agrees with the manifest that was checked against
    the signed documents.  Catches an archive that is really another flavor or another build."""
    found = peek_archive(path)
    if "eitri/flavor" not in found or "eitri/seed/seed-info.env" not in found:
        raise _bad("the archive lacks eitri/flavor or eitri/seed/seed-info.env", "seed_integrity")
    if found["eitri/flavor"].decode("utf-8", "replace").strip() != flavor:
        raise _bad("the archive's eitri/flavor is not %s" % flavor, "seed_integrity")
    env: Dict[str, str] = {}
    for line in found["eitri/seed/seed-info.env"].decode("utf-8", "replace").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k] = v
    expect = {"SEED_KIND": KIND, "SEED_FLAVOR": flavor, "SEED_LAYOUT": str(LAYOUT), "SEED_VERSION": info["version"],
              "SEED_FIRMWARE_COMMIT": info["commit"]}
    for key, want in sorted(expect.items()):
        if env.get(key) != want:
            raise _bad("%s inside the archive is %r, the manifest says %r" % (key, env.get(key), want), "seed_integrity")


# ---- the installed state -------------------------------------------------------------------------------------
def read_record(data_root: str) -> Dict[str, str]:
    """``/data/eitri/state/seed-installed.env`` (KEY=value lines), or {} when no seed was ever installed."""
    out: Dict[str, str] = {}
    try:
        with open(os.path.join(data_root, RECORD)) as fh:
            for line in fh:
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.rstrip("\n").partition("=")
                    out[k] = v
    except OSError:
        pass
    return out


def installed_flavor(data_root: str) -> Optional[str]:
    """The flavor of the seed that is actually installed (the record), not of the flag file, which a front end may
    already have rewritten on its way to asking for a switch."""
    flavor = read_record(data_root).get("SEED_FLAVOR")
    return flavor if flavor in FLAVORS else None


def leftovers(data_root: str) -> List[str]:
    """Components of an unfinished ``--keep-old`` update (a kept ``.<name>.seed-old`` tree)."""
    found = []
    for comp in COMPONENTS:
        parent, name = os.path.split(os.path.join(data_root, comp))
        if os.path.lexists(os.path.join(parent, ".%s.seed-old" % name)):
            found.append(comp)
    return found


def find_tool(explicit: Optional[str] = None) -> str:
    """eitri-seed-install: an explicit path, $EITRI_SEED_INSTALL, the image's libexec copy, or a checkout's seed/bin."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [explicit, os.environ.get("EITRI_SEED_INSTALL")] + list(TOOL_PATHS) + [
        os.path.join(here, "..", "..", "..", "seed", "bin", "eitri-seed-install")]
    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.abspath(cand)
    raise FlashError("eitri-seed-install is not installed (looked in %s)" % ", ".join(TOOL_PATHS), code="seed_tool",
                     recovery="Reinstall the system image; /usr/libexec/eitri/eitri-seed-install ships with it.")


_EXIT = {3: "seed_integrity", 4: "seed_conflict", 5: "no_space", 6: "seed_failed"}


class SeedTool:
    """``eitri-seed-install`` through the runner (never a shell; every argument is a separate argv entry)."""

    def __init__(self, runner: Runner = run, path: Optional[str] = None, data_root: str = "/data") -> None:
        self.runner, self.data_root = runner, data_root
        self._path = path
        self.path = path or ""

    def _argv(self, *args: str) -> List[str]:
        if not self.path:
            self.path = find_tool(self._path)
        return [self.path, "--data", self.data_root] + list(args)

    def _call(self, *args: str) -> str:
        res = self.runner(self._argv(*args))
        if res.returncode != 0:
            tail = "\n".join(res.output.strip().splitlines()[-6:])
            raise FlashError("eitri-seed-install failed (exit %d): %s" % (res.returncode, tail or "no output"),
                             code=_EXIT.get(res.returncode, "seed_failed"), step="seed")
        return res.output

    def dry_run(self, archive: str, sha256: str, flavor: str, *, mode: str = "update", switch_flavor: bool = False) -> str:
        return self._call("--archive", archive, "--sha256", sha256, "--flavor", flavor, "--mode", mode, "--dry-run",
                          *(["--switch-flavor"] if switch_flavor else []))

    def update(self, archive: str, sha256: str, flavor: str, *, switch_flavor: bool = False, keep_old: bool = True) -> str:
        """Swap the installed trees for the seed's, keeping the replaced ones until finish() or rollback()."""
        return self._call("--archive", archive, "--sha256", sha256, "--flavor", flavor, "--mode", "update", "--owner", "1000:1000",
                          *(["--switch-flavor"] if switch_flavor else []), *(["--keep-old"] if keep_old else []))

    def finish(self) -> None:
        self._call("--mode", "finish")

    def rollback(self) -> None:
        self._call("--mode", "rollback")


def default_overlay_command(data_root: str) -> List[str]:
    layer = os.path.join(data_root, "eitri", "klipper-layer")
    return [os.path.join(layer, "scripts", "overlay-install.sh"), "--klipper-dir", os.path.join(data_root, "firmware", "klipper"),
            "--extras-dir", os.path.join(data_root, "eitri", "extras"), "--config-dir", os.path.join(layer, "config"),
            "--printer-data", os.path.join(data_root, "printer_data"), "--owner", "eitri", "-q"]
