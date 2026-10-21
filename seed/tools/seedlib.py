# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the Eitri data-seed build and its checks.

Pure standard library; runs under any Python >= 3.11 (the host), never on the printer.
Everything that must be reproducible (tree digest, archive layout) lives here so that the builder, the
verifier and the tests cannot disagree.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tomllib
from pathlib import Path
from typing import Iterable, Iterator

SEED_SCHEMA = 1          # manifest.json and seed-info.json schema
LAYOUT_VERSION = 1       # /data layout version (matches data-init's LAYOUT_VERSION)
TAR_UID = 1000           # the "eitri" user and group inside the image
TAR_GID = 1000
FLAVORS = ("klipper", "kalico")

# Components the archive may contain. Everything else in an archive is refused by the consumers.
# Paths are relative to /data. "move" components are installed (or swapped) as a unit.
COMPONENTS = (
    "firmware/klipper",
    "firmware/moonraker",
    "venv/klippy",
    "venv/moonraker",
    "eitri/klarity/venv",
    "eitri/extras",
    "eitri/klipper-layer",
    "eitri/seed",
)
# Plain files (not directories) the archive may contain.
FILES = ("eitri/flavor",)
# Directories that only exist to hold components (created by the archive as 0755 eitri:eitri).
PARENTS = ("firmware", "venv", "eitri", "eitri/klarity")

ARCHIVE_RE = re.compile(r"^data-seed-(?P<version>[0-9A-Za-z.+_~-]+)-(?P<flavor>klipper|kalico)\.tar\.zst$")
MANIFEST_RE = re.compile(r"^data-seed-(?P<version>[0-9A-Za-z.+_~-]+)-(?P<flavor>klipper|kalico)\.manifest\.json$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_~-]*$")


class SeedError(Exception):
    pass


# --- files and digests -------------------------------------------------------------------------------

def sha256_file(path: str | os.PathLike, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: str | os.PathLike, obj: object) -> None:
    """Canonical, human-readable JSON (sorted keys, 2 spaces, trailing newline)."""
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: str | os.PathLike) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- release.lock ------------------------------------------------------------------------------------

def load_lock(repo: str | os.PathLike) -> dict:
    with open(Path(repo) / "release.lock", "rb") as f:
        return tomllib.load(f)


def pins(repo: str | os.PathLike, flavor: str) -> dict:
    """Everything the seed build takes from release.lock for one flavor."""
    if flavor not in FLAVORS:
        raise SeedError(f"unknown flavor {flavor!r}")
    lock = load_lock(repo)
    fw = lock["firmware"][flavor]
    mr = lock["moonraker"]
    for name, entry in (("firmware." + flavor, fw), ("moonraker", mr)):
        if not COMMIT_RE.match(entry.get("commit", "")):
            raise SeedError(f"release.lock [{name}] has no 40-hex commit")
    default_origin = {
        "klipper": ("https://github.com/Klipper3d/klipper.git", "master"),
        "kalico": ("https://github.com/KalicoCrew/kalico.git", "main"),
    }[flavor]
    return {
        "flavor": flavor,
        "firmware": {
            "name": flavor,
            "version": fw["version"],
            "commit": fw["commit"],
            # A tag the commit descends from (Klipper: the last release tag, 813 commits before the pin;
            # Kalico: the release tag itself). Needed so `git describe` works on the shallow clone.
            "tag": fw.get("last_tag", fw["version"]),
            "tag_commit": fw.get("last_tag_commit", fw["commit"]),
            "origin": default_origin[0],
            "branch": default_origin[1],
        },
        "moonraker": {
            "name": "moonraker",
            "version": mr["version"],
            "commit": mr["commit"],
            "tag": mr["version"],
            "tag_commit": mr["commit"],
            "origin": "https://github.com/Arksine/moonraker.git",
            "branch": "master",
        },
        "python": lock["python"]["version"],
        "uv": lock["uv"]["version"],
        "klarity_version": lock["components"]["klarity"]["version"],
        "layer_version": lock["components"]["klipper-layer"]["version"],
        "release_status": lock.get("release_status", ""),
    }


# --- tree digest -------------------------------------------------------------------------------------

def _entry_line(rel: str, st: os.stat_result, path: str) -> str:
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        return f"d {mode:04o} - - {rel}\n"
    if stat.S_ISLNK(st.st_mode):
        return f"l {mode:04o} - {sha256_bytes(os.readlink(path).encode())} {rel}\n"
    if stat.S_ISREG(st.st_mode):
        return f"f {mode:04o} {st.st_size} {sha256_file(path)} {rel}\n"
    raise SeedError(f"{rel}: unsupported file type (only files, directories and symlinks are allowed)")


def walk_sorted(root: str | os.PathLike) -> Iterator[tuple[str, str]]:
    """Yield (relative path, absolute path) for every entry below root, parents before children, sorted by
    byte order of the relative path (so the order never depends on the file system)."""
    root = str(root)
    rels: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            rels.append(os.path.relpath(os.path.join(dirpath, name), root))
    for rel in sorted(rels, key=lambda r: r.encode()):
        yield rel, os.path.join(root, rel)


# Written after the digest is known, so the digest cannot cover them.
DIGEST_EXCLUDE = ("eitri/seed/seed-info.json", "eitri/seed/seed-info.env")


def tree_digest(root: str | os.PathLike, exclude: Iterable[str] = DIGEST_EXCLUDE,
                include_prefixes: Iterable[str] | None = None) -> tuple[str, int]:
    """SHA-256 over a canonical listing of the tree: type, permission bits, size, content digest (link-target
    digest for symlinks) and path of every entry. Owners and mtimes are deliberately not part of it (they are
    fixed by the archive and checked separately). The `exclude` paths (seed-info.*) are skipped.
    Returns (digest, number of entries counted)."""
    skip = set(exclude)
    prefixes = tuple(include_prefixes) if include_prefixes is not None else None
    h = hashlib.sha256()
    n = 0
    for rel, path in walk_sorted(root):
        if rel in skip:
            continue
        if prefixes is not None and not any(rel == p or rel.startswith(p + "/") for p in prefixes):
            continue
        h.update(_entry_line(rel, os.lstat(path), path).encode())
        n += 1
    return h.hexdigest(), n


def tree_size(root: str | os.PathLike, exclude: Iterable[str] = DIGEST_EXCLUDE) -> int:
    skip = set(exclude)
    total = 0
    for rel, path in walk_sorted(root):
        if rel in skip:
            continue
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total


def normalize_tree(root: str | os.PathLike, epoch: int) -> None:
    """Set every mtime below root (root included) to epoch. Permissions are left as they are, but group/other
    write is removed and the group/other read/execute bits follow the owner's, so the result does not depend
    on the builder's umask."""
    root = str(root)
    paths = [root] + [p for _r, p in walk_sorted(root)]
    for p in paths:
        st = os.lstat(p)
        if not stat.S_ISLNK(st.st_mode):
            mode = stat.S_IMODE(st.st_mode)
            want = 0
            if mode & 0o400:
                want |= 0o444
            if mode & 0o200:
                want |= 0o200
            if mode & 0o100:
                want |= 0o111
            if want != mode:
                os.chmod(p, want)
        os.utime(p, (epoch, epoch), follow_symlinks=False)


# --- deterministic tar -------------------------------------------------------------------------------

def _tarinfo(rel: str, path: str, owner: tuple[int, int]) -> tarfile.TarInfo:
    st = os.lstat(path)
    ti = tarfile.TarInfo(rel)
    ti.mtime = int(st.st_mtime)
    ti.uid, ti.gid = owner
    ti.uname = ti.gname = ""
    ti.mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        ti.type = tarfile.DIRTYPE
        ti.size = 0
    elif stat.S_ISLNK(st.st_mode):
        ti.type = tarfile.SYMTYPE
        ti.linkname = os.readlink(path)
        ti.size = 0
    elif stat.S_ISREG(st.st_mode):
        ti.type = tarfile.REGTYPE
        ti.size = st.st_size
    else:
        raise SeedError(f"{rel}: unsupported file type")
    return ti


def write_tar(root: str | os.PathLike, out: io.RawIOBase | io.BufferedIOBase, owner: tuple[int, int] = (TAR_UID, TAR_GID)) -> int:
    """Write the tree as an uncompressed GNU tar: sorted, numeric owner, no hard links, no extended attributes.
    Returns the number of members. Paths are relative to root, without a leading ./ ."""
    n = 0
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.GNU_FORMAT) as tar:
        for rel, path in walk_sorted(root):
            ti = _tarinfo(rel, path, owner)
            if ti.isreg():
                with open(path, "rb") as f:
                    tar.addfile(ti, f)
            else:
                tar.addfile(ti)
            n += 1
    return n


def zstd_compress(tar_writer, out_path: str | os.PathLike, level: int = 19, threads: int = 0) -> None:
    """Run tar_writer(stdin_of_zstd) and compress to out_path. The zstd frame is deterministic for a fixed
    level and input (the content checksum is on, the dictionary and --long are off, so any zstd >= 1.4 can
    decompress it with the default 128 MiB window limit)."""
    cmd = ["zstd", f"-{level}", f"-T{threads}", "--quiet", "--force", "-o", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        assert proc.stdin is not None
        tar_writer(proc.stdin)
        proc.stdin.close()
    except BaseException:
        proc.kill()
        raise
    if proc.wait() != 0:
        raise SeedError("zstd failed")


def iter_archive(path: str | os.PathLike) -> Iterator[tarfile.TarInfo]:
    """Yield the members of a .tar.zst without extracting it."""
    proc = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            yield from tar
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()


# --- archive policy ----------------------------------------------------------------------------------

def check_member(ti: tarfile.TarInfo) -> str | None:
    """Return a reason when a seed archive member is not acceptable, else None.

    The rules are the contract every consumer enforces (SPEC.md, "Extraction rules"): only files,
    directories and symlinks; relative, normalised names inside the known layout; symlinks that stay inside
    /data or point into the OS image (/usr, /etc, /lib); no hard links, devices, set-uid/-gid or sticky bits.
    """
    name = ti.name
    if name.startswith("/") or name.startswith("./") or name == "." or "\\" in name or "\0" in name:
        return "absolute or non-normalised path"
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "path contains an empty, '.' or '..' component"
    top_ok = (
        name in PARENTS
        or name in FILES
        or any(name == c or name.startswith(c + "/") for c in COMPONENTS)
    )
    if not top_ok:
        return "outside the seed layout"
    if not (ti.isreg() or ti.isdir() or ti.issym()):
        return "not a regular file, directory or symlink"
    if ti.mode & 0o7000:
        return "set-uid, set-gid or sticky bit"
    if ti.issym():
        target = ti.linkname
        if target.startswith("/"):
            if not (target.startswith("/data/") or target.startswith("/usr/") or target.startswith("/etc/")
                    or target.startswith("/lib/") or target.startswith("/run/")):
                return "symlink to an absolute path outside /data, /usr, /etc, /lib and /run"
        else:
            depth = len(parts) - 1
            for comp in target.split("/"):
                if comp == "..":
                    depth -= 1
                    if depth < 0:
                        return "symlink escapes the seed tree"
                elif comp not in ("", "."):
                    depth += 1
    if ti.isreg() and ti.size < 0:
        return "negative size"
    return None


def check_archive(path: str | os.PathLike) -> list[str]:
    """Names and reasons for every member the policy refuses (empty list = acceptable)."""
    bad: list[str] = []
    for ti in iter_archive(path):
        why = check_member(ti)
        if why:
            bad.append(f"{ti.name}: {why}")
    return bad


# --- manifest ----------------------------------------------------------------------------------------

def validate_manifest(m: dict, *, archive_name: str | None = None) -> list[str]:
    """Structural validation of data-seed-<version>-<flavor>.manifest.json. Returns a list of problems."""
    errs: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(msg)

    need(m.get("schema") == SEED_SCHEMA, f"schema must be {SEED_SCHEMA}")
    need(m.get("kind") == "eitri-data-seed", "kind must be 'eitri-data-seed'")
    need(m.get("layout") == LAYOUT_VERSION, f"layout must be {LAYOUT_VERSION}")
    ver = m.get("version", "")
    need(isinstance(ver, str) and bool(VERSION_RE.match(ver)), "version must match " + VERSION_RE.pattern)
    flavor = m.get("flavor")
    need(flavor in FLAVORS, "flavor must be klipper or kalico")
    arc = m.get("archive", {})
    need(isinstance(arc, dict), "archive must be an object")
    if isinstance(arc, dict):
        need(bool(ARCHIVE_RE.match(arc.get("name", ""))), "archive.name must be data-seed-<version>-<flavor>.tar.zst")
        mt = ARCHIVE_RE.match(arc.get("name", ""))
        if mt:
            need(mt["version"] == ver and mt["flavor"] == flavor, "archive.name disagrees with version/flavor")
        if archive_name is not None:
            need(arc.get("name") == archive_name, "archive.name does not match the archive file name")
        need(bool(SHA256_RE.match(arc.get("sha256", ""))), "archive.sha256 must be 64 lowercase hex digits")
        need(isinstance(arc.get("size"), int) and arc["size"] > 0, "archive.size must be a positive integer")
        need(isinstance(arc.get("uncompressed_size"), int) and arc["uncompressed_size"] > 0,
             "archive.uncompressed_size must be a positive integer")
        need(arc.get("compression") == "zstd", "archive.compression must be zstd")
        need(arc.get("tar_format") == "gnu", "archive.tar_format must be gnu")
    need(isinstance(m.get("created"), str) and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", m.get("created", "")) is not None,
         "created must be an UTC timestamp like 2026-10-21T12:00:00Z")
    need(isinstance(m.get("source_date_epoch"), int) and m["source_date_epoch"] > 0, "source_date_epoch must be a positive integer")
    tgt = m.get("target", {})
    need(tgt.get("arch") == "aarch64", "target.arch must be aarch64")
    need(isinstance(tgt.get("python"), str) and bool(re.match(r"^3\.\d+\.\d+$", tgt.get("python", ""))), "target.python must be x.y.z")
    need(tgt.get("python_abi") == "cp" + tgt.get("python", "0.0.0").split(".")[0] + tgt.get("python", "0.0.0").split(".")[1],
         "target.python_abi must be cp<major><minor> of target.python")
    need(tgt.get("interpreter") == "/usr/bin/python3", "target.interpreter must be /usr/bin/python3")
    need(re.match(r"^2\.\d+$", str(tgt.get("glibc_max", ""))) is not None, "target.glibc_max must look like 2.42")
    libs = tgt.get("image_libraries")
    need(isinstance(libs, list) and bool(libs) and all(isinstance(x, str) and (x.startswith("lib") or x.startswith("ld-")) for x in libs),
         "target.image_libraries must list the shared libraries expected from the image")
    need(re.match(r"^2\.\d+$", str(tgt.get("glibc_needed", ""))) is not None, "target.glibc_needed must look like 2.34")
    src = m.get("sources", {})
    for key in ("firmware", "moonraker"):
        s = src.get(key, {})
        need(bool(COMMIT_RE.match(s.get("commit", ""))), f"sources.{key}.commit must be a 40-hex commit")
        need(bool(s.get("describe")), f"sources.{key}.describe is required")
        need(bool(s.get("origin", "").startswith("https://")), f"sources.{key}.origin must be an https URL")
        need(bool(s.get("branch")), f"sources.{key}.branch is required")
    if isinstance(src.get("firmware"), dict):
        need(src["firmware"].get("name") == flavor, "sources.firmware.name must equal flavor")
    need(bool(src.get("klipper_layer", {}).get("tree_sha256", "")) and bool(SHA256_RE.match(src.get("klipper_layer", {}).get("tree_sha256", ""))),
         "sources.klipper_layer.tree_sha256 is required")
    need(bool(SHA256_RE.match(src.get("klarity", {}).get("wheel_sha256", ""))), "sources.klarity.wheel_sha256 is required")
    venvs = m.get("venvs", {})
    for name in ("klippy", "moonraker", "klarity"):
        v = venvs.get(name, {})
        need(bool(SHA256_RE.match(v.get("lock_sha256", ""))), f"venvs.{name}.lock_sha256 is required")
        need(isinstance(v.get("packages"), list) and len(v["packages"]) > 0, f"venvs.{name}.packages must be a non-empty list")
        for p in v.get("packages", []) if isinstance(v.get("packages"), list) else []:
            need(isinstance(p, dict) and bool(p.get("name")) and bool(p.get("version")), f"venvs.{name}.packages entries need name and version")
        for b in v.get("built_from_source", []) if isinstance(v.get("built_from_source"), list) else []:
            need(bool(SHA256_RE.match(b.get("sdist_sha256", ""))) and bool(SHA256_RE.match(b.get("wheel_sha256", ""))),
                 f"venvs.{name}.built_from_source entries need sdist_sha256 and wheel_sha256")
    ch = m.get("chelper", {})
    need(bool(SHA256_RE.match(ch.get("sha256", ""))), "chelper.sha256 is required")
    need(ch.get("path") == "firmware/klipper/klippy/chelper/c_helper.so", "chelper.path is wrong")
    tree = m.get("tree", {})
    need(bool(SHA256_RE.match(tree.get("digest", ""))), "tree.digest is required")
    need(isinstance(tree.get("entries"), int) and tree["entries"] > 0, "tree.entries must be a positive integer")
    need(tree.get("components") == list(COMPONENTS), "tree.components must list the seed components in contract order")
    return errs


def seed_names(version: str, flavor: str) -> tuple[str, str]:
    return (f"data-seed-{version}-{flavor}.tar.zst", f"data-seed-{version}-{flavor}.manifest.json")


def iso_utc(epoch: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def iter_lock_packages(lock_text: str) -> Iterable[tuple[str, str]]:
    """(name, version) pairs from a `uv pip compile --generate-hashes` file."""
    for line in lock_text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", line)
        if m:
            yield m.group(1).lower().replace("_", "-"), m.group(2)
