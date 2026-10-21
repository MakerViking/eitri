#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Release metadata tooling: build, check and verify the signed side of an Eitri release.

    python3 -I tools/release/release.py metadata  --artifacts DIR --out DIR --version V --base-url URL [--generated T]
    python3 -I tools/release/release.py preflight --dir DIR --stage inputs|metadata [--tag vV]
    python3 -I tools/release/release.py verify-signed --dir DIR
    python3 -I tools/release/release.py verify-file FILE [--sig FILE]

This is the part of the release that decides *what* gets signed; signing itself is ``sign-release.sh`` (it needs the
keys, this file never sees one).  Nothing here touches the network or a printer.

* ``metadata`` builds the unsigned ``install.json`` (first conversion, ``installer/docs/BUNDLE.md`` section 1) and
  ``manifest.json`` (channel manifest, section 2) from the release artifacts and the ``[release]`` tables of
  ``release.lock``.  A document that cannot be complete (no stage-2 initramfs yet, no OS bundle) is not written; the
  plan says what is missing.
* ``preflight`` is the gate in front of the signature.  It re-derives everything it can from the files and
  ``release.lock`` and refuses on any disagreement.  It also refuses a directory that contains something that looks
  like a private key.
* ``verify-signed`` checks the signed result with the code the printer runs (``eitri_update.minisign`` and
  ``eitri_update.manifest``), so a signature that would not be accepted on a printer is a CI failure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FLAVORS = ("klipper", "kalico")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CALVER_RE = re.compile(r"^\d{4}\.\d{2}\.\d+(?:[-+][0-9A-Za-z.]+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
INPUTS_SUMS = "INPUTS.sha256"
FINAL_SUMS = "SHA256SUMS"
GENERATED_FILES = ("install.json", "manifest.json", "release-plan.json")
SIGNED_FILES = ("install.json", "manifest.json", FINAL_SUMS)
# Validity of a channel manifest: long enough to survive a release gap, short enough that a replayed one dies.
MIN_VALIDITY_DAYS, MAX_VALIDITY_DAYS = 14, 400
# Anything that smells like key material must never be in a release directory.
SECRET_MARKERS = (
    b"-----BEGIN PRIVATE KEY", b"-----BEGIN EC PRIVATE KEY", b"-----BEGIN RSA PRIVATE KEY",
    b"-----BEGIN ENCRYPTED PRIVATE KEY", b"-----BEGIN OPENSSH PRIVATE KEY", b"-----BEGIN PGP PRIVATE KEY",
    b"minisign encrypted secret key", b"rsign encrypted secret key",
)
SECRET_NAMES = re.compile(r"(\.key|\.sec|\.p12|\.pfx|\.pem|\.jks|secret|private)", re.I)
SCAN_LIMIT = 16 * 1024 * 1024
# The time between collecting the unsigned set and signing it (a person has to approve in between).
MAX_APPROVAL_AGE_DAYS = 30


class ReleaseError(Exception):
    pass


# --- small helpers -----------------------------------------------------------------------------------
def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_ts(text: str) -> dt.datetime:
    if not isinstance(text, str) or not TS_RE.match(text):
        raise ReleaseError(f"bad timestamp {text!r} (expected YYYY-MM-DDTHH:MM:SSZ)")
    try:
        return dt.datetime.strptime(text, TS_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise ReleaseError(f"bad timestamp {text!r}: {exc}") from exc


def fmt_ts(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime(TS_FORMAT)


def now_ts() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc) if epoch and epoch.isdigit() else dt.datetime.now(dt.timezone.utc)
    return fmt_ts(when.replace(microsecond=0))


def load_json(path: str | os.PathLike) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_lock(path: str | os.PathLike) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"cannot read release.lock {path}: {exc}") from exc


def module_from(path: Path, name: str):
    """Import a module by file path (the interpreter runs with -I, which keeps the script directory off sys.path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReleaseError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def client(root: Path):
    """The code a printer runs to judge these files: minisign verification, the channel manifest parser, and the
    installer's payload validator.  Returns (minisign, manifest, prepare_payload)."""
    for sub in ("installer/eitri-update", "installer/mcuflash", "installer/tools"):
        p = str(root / sub)
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        return (importlib.import_module("eitri_update.minisign"), importlib.import_module("eitri_update.manifest"),
                importlib.import_module("prepare_payload"))
    except ImportError as exc:
        raise ReleaseError(f"cannot import the installer's own verification code from {root / 'installer'}: {exc}") from exc


def seedlib(root: Path):
    return module_from(root / "seed" / "tools" / "seedlib.py", "eitri_release_seedlib")


# --- release.lock [release] --------------------------------------------------------------------------
def release_settings(lock: dict) -> dict:
    """The ``[release]`` and ``[release.install]`` tables, validated."""
    rel = lock.get("release")
    if not isinstance(rel, dict):
        raise ReleaseError("release.lock has no [release] table")
    out: dict[str, Any] = {}
    chan = rel.get("channel")
    if not isinstance(chan, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", chan):
        raise ReleaseError("[release] channel must be a short lowercase name")
    out["channel"] = chan
    for key in ("manifest_sequence", "min_manifest_sequence"):
        if not isinstance(rel.get(key), int) or isinstance(rel.get(key), bool) or rel[key] < 0:
            raise ReleaseError(f"[release] {key} must be a non-negative integer")
        out[key] = rel[key]
    if out["manifest_sequence"] < 1:
        raise ReleaseError("[release] manifest_sequence must be at least 1")
    if out["min_manifest_sequence"] > out["manifest_sequence"]:
        raise ReleaseError("[release] min_manifest_sequence is above manifest_sequence: the release would be rejected by its own image")
    parse_ts(rel.get("clock_floor", ""))
    out["clock_floor"] = rel["clock_floor"]
    days = rel.get("manifest_validity_days")
    if not isinstance(days, int) or not MIN_VALIDITY_DAYS <= days <= MAX_VALIDITY_DAYS:
        raise ReleaseError(f"[release] manifest_validity_days must be {MIN_VALIDITY_DAYS}..{MAX_VALIDITY_DAYS}")
    out["manifest_validity_days"] = days
    inst = rel.get("install")
    if not isinstance(inst, dict):
        raise ReleaseError("release.lock has no [release.install] table")
    for key in ("compatible", "kernel_cmdline"):
        if not isinstance(inst.get(key), str) or not inst[key]:
            raise ReleaseError(f"[release.install] {key} is required")
    for key in ("root_mib", "data_min_mib"):
        if not isinstance(inst.get(key), int) or inst[key] <= 0:
            raise ReleaseError(f"[release.install] {key} must be a positive integer")
    soc = inst.get("stock_soc")
    if not isinstance(soc, list) or not soc or not all(isinstance(s, str) and s for s in soc):
        raise ReleaseError("[release.install] stock_soc must be a non-empty list of strings")
    out["install"] = {k: inst[k] for k in ("compatible", "kernel_cmdline", "root_mib", "data_min_mib")}
    out["install"]["stock_soc"] = list(soc)
    return out


# --- artifacts ---------------------------------------------------------------------------------------
def artifact_names(version: str) -> dict[str, Any]:
    return {
        "os_bundle": f"eitri-plus4-{version}.raucb",
        "image": f"eitri-plus4-{version}.img.zst",
        "rootfs": f"rootfs-{version}.img.zst",
        "stage2": f"stage2-initramfs-{version}.cpio.gz",
        "seed": {f: (f"data-seed-{version}-{f}.tar.zst", f"data-seed-{version}-{f}.manifest.json") for f in FLAVORS},
        "mcu": {f: f"mcu-firmware-{version}-{f}.tar.zst" for f in FLAVORS},
    }


def scan(directory: Path, version: str) -> dict[str, Any]:
    """Which of the files of the artifact contract exist in *directory*.  Files that look like a contract file for a
    different version are an error (a leftover from another release must not be signed by accident)."""
    names = {p.name for p in directory.iterdir() if p.is_file()}
    want = artifact_names(version)
    expected = {want["os_bundle"], want["image"], want["rootfs"], want["stage2"]}
    for f in FLAVORS:
        expected.update(want["seed"][f])
        expected.add(want["mcu"][f])
    stray = re.compile(r"^(eitri-plus4-.*\.(raucb|img\.zst)|rootfs-.*\.img\.zst|stage2-initramfs-.*\.cpio\.gz|data-seed-.*|"
                       r"mcu-firmware-.*\.tar\.zst|data-.*\.raucb)$")
    wrong = sorted(n for n in names if stray.match(n) and n not in expected)
    if wrong:
        raise ReleaseError(f"artifacts for another version (or with unknown names) in the directory: {', '.join(wrong)}")
    have = lambda n: n if n in names else None  # noqa: E731
    return {
        "os_bundle": have(want["os_bundle"]),
        "image": have(want["image"]),
        "rootfs": have(want["rootfs"]),
        "stage2": have(want["stage2"]),
        "seed": {f: tuple(want["seed"][f]) for f in FLAVORS if all(n in names for n in want["seed"][f])},
        "partial_seed": [f for f in FLAVORS if sum(n in names for n in want["seed"][f]) == 1],
        "mcu": {f: want["mcu"][f] for f in FLAVORS if want["mcu"][f] in names},
    }


def zstd_raw(path: Path) -> tuple[int, str]:
    """Decompressed size and SHA-256 of a .zst file (install.json describes the raw image)."""
    zstd = shutil.which("zstd")
    if not zstd:
        raise ReleaseError("zstd is needed to describe rootfs.img.zst (raw_size, raw_sha256)")
    h, size = hashlib.sha256(), 0
    with subprocess.Popen([zstd, "-dc", "--", str(path)], stdout=subprocess.PIPE) as proc:
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    if proc.returncode:
        raise ReleaseError(f"{path.name} is not a valid zstd file")
    return size, h.hexdigest()


def file_entry(directory: Path, name: str) -> dict[str, Any]:
    p = directory / name
    return {"url": name, "sha256": sha256_file(p), "size": p.stat().st_size}


def seed_entry(directory: Path, names: tuple[str, str]) -> dict[str, Any]:
    """The seed archive as install.json / manifest.json announce it: the archive plus its own (small) manifest, both
    reachable through the signed document (installer/tools/prepare_payload.py, eitri_update.manifest)."""
    entry = file_entry(directory, names[0])
    entry["manifest"] = file_entry(directory, names[1])
    return entry


def read_seed(directory: Path, root: Path, version: str, flavor: str, names: tuple[str, str]) -> dict:
    """A seed manifest, validated with the seed lane's own validator and checked against its archive file."""
    sl = seedlib(root)
    m = load_json(directory / names[1])
    problems = sl.validate_manifest(m, archive_name=names[0])
    if problems:
        raise ReleaseError(f"{names[1]}: " + "; ".join(problems))
    if m["version"] != version or m["flavor"] != flavor:
        raise ReleaseError(f"{names[1]} is for {m['version']}/{m['flavor']}, expected {version}/{flavor}")
    arc = directory / names[0]
    if m["archive"]["size"] != arc.stat().st_size or m["archive"]["sha256"] != sha256_file(arc):
        raise ReleaseError(f"{names[0]} does not match its manifest (size or SHA-256)")
    return m


def seed_matches_lock(m: dict, lock: dict, flavor: str) -> list[str]:
    """The seed must have been built from exactly the commits release.lock pins."""
    out = []
    fw = lock.get("firmware", {}).get(flavor, {})
    if m["sources"]["firmware"]["commit"] != fw.get("commit"):
        out.append(f"{flavor} seed was built from {m['sources']['firmware']['commit']}, release.lock pins {fw.get('commit')}")
    mr = lock.get("moonraker", {})
    if m["sources"]["moonraker"]["commit"] != mr.get("commit"):
        out.append(f"{flavor} seed has Moonraker {m['sources']['moonraker']['commit']}, release.lock pins {mr.get('commit')}")
    py = lock.get("python", {}).get("version")
    if m["target"]["python"] != py:
        out.append(f"{flavor} seed targets Python {m['target']['python']}, release.lock pins {py}")
    return out


# --- metadata ----------------------------------------------------------------------------------------
def build_metadata(directory: Path, out: Path, *, root: Path, lock_path: Path, version: str, base_url: str,
                   generated: str | None = None, expires: str | None = None, sequence: int | None = None,
                   previous: Path | None = None) -> dict:
    """Write install.json, manifest.json and release-plan.json into *out*; returns the plan."""
    if not CALVER_RE.match(version):
        raise ReleaseError(f"version {version!r} is not YYYY.MM.N (the channel manifest parser rejects anything else)")
    if not base_url.startswith("https://") or not base_url.endswith("/"):
        raise ReleaseError("--base-url must be an https URL ending in /")
    lock = load_lock(lock_path)
    cfg = release_settings(lock)
    generated = generated or now_ts()
    gen_dt = parse_ts(generated)
    expires = expires or fmt_ts(gen_dt + dt.timedelta(days=cfg["manifest_validity_days"]))
    parse_ts(expires)
    seq = cfg["manifest_sequence"] if sequence is None else sequence
    art = scan(directory, version)
    for f in art["partial_seed"]:
        raise ReleaseError(f"the {f} seed needs both its archive and its manifest")

    seeds = {f: read_seed(directory, root, version, f, names) for f, names in art["seed"].items()}
    for f, m in seeds.items():
        bad = seed_matches_lock(m, lock, f)
        if bad:
            raise ReleaseError("; ".join(bad))
    plan: dict[str, Any] = {"schema": 1, "version": version, "generated": generated, "expires": expires, "sequence": seq,
                            "channel": cfg["channel"], "documents": {}, "missing": {}, "files": {}}

    # --- install.json
    missing: list[str] = []
    if not art["rootfs"]:
        missing.append(artifact_names(version)["rootfs"])
    if not art["stage2"]:
        missing.append(artifact_names(version)["stage2"])
    offered = [f for f in FLAVORS if f in seeds and f in art["mcu"]]
    for f in FLAVORS:
        if f not in seeds:
            missing.append(artifact_names(version)["seed"][f][0])
        elif f not in art["mcu"]:
            missing.append(artifact_names(version)["mcu"][f])
    install_doc = None
    if art["rootfs"] and art["stage2"] and offered:
        raw_size, raw_sha = zstd_raw(directory / art["rootfs"])
        rootfs = file_entry(directory, art["rootfs"])
        rootfs.update({"raw_size": raw_size, "raw_sha256": raw_sha})
        inst = cfg["install"]
        install_doc = {
            "schema": 1, "version": version, "channel": cfg["channel"],
            "target": {"compatible": inst["compatible"]},
            "stock": {"soc": inst["stock_soc"]},
            "layout": {"root_mib": inst["root_mib"], "data_min_mib": inst["data_min_mib"]},
            "kernel_cmdline": inst["kernel_cmdline"],
            "flavors": {f: {"describe": seeds[f]["sources"]["firmware"]["describe"], "commit": seeds[f]["sources"]["firmware"]["commit"]}
                        for f in offered},
            "files": {
                "rootfs": rootfs,
                "stage2_initramfs": file_entry(directory, art["stage2"]),
                "data_seed": {f: seed_entry(directory, art["seed"][f]) for f in offered},
                "mcu_firmware": {f: file_entry(directory, art["mcu"][f]) for f in offered},
            },
        }
    # The flavors that are not offered are named so that a release with only one flavor is a visible choice.
    plan["documents"]["install.json"] = bool(install_doc)
    plan["missing"]["install.json"] = [] if install_doc else sorted(set(missing))

    # --- manifest.json
    manifest_doc = None
    if art["os_bundle"]:
        # One OS bundle; per flavor that has both a data seed and matching MCU firmware, a seed and an mcu entry
        # (eitri_update.manifest refuses a seed without its MCU firmware: host and MCU are protocol-locked).
        bundles = [dict(kind="os", reboot=True, **file_entry(directory, art["os_bundle"]))]
        for f in offered:
            bundles.append(dict(kind="mcu", flavor=f, **file_entry(directory, art["mcu"][f])))
            bundles.append(dict(kind="seed", flavor=f, restart_services=["klipper", "moonraker", "klarity"],
                                **seed_entry(directory, art["seed"][f])))
        for b in bundles:
            b["url"] = base_url + b["url"]
            if "manifest" in b:
                b["manifest"]["url"] = base_url + b["manifest"]["url"]
        flavors = {f: {"describe": m["sources"]["firmware"]["describe"], "commit": m["sources"]["firmware"]["commit"]}
                   for f, m in sorted(seeds.items()) if f in offered}
        upstream: dict[str, dict] = {}
        for f, m in sorted(seeds.items()):
            fw = m["sources"]["firmware"]
            upstream[f] = {"origin": fw["origin"], "branch": fw["branch"]}
        if seeds:
            mr = next(iter(seeds.values()))["sources"]["moonraker"]
            upstream["moonraker"] = {"origin": mr["origin"], "branch": mr["branch"]}
        this_release = {"version": version, "released": generated[:10], "flavors": flavors, "bundles": bundles,
                        "notes_url": base_url.replace("/releases/download/", "/releases/tag/").rstrip("/")}
        releases = [this_release]
        if previous is not None:
            prev = load_json(previous)
            if not isinstance(prev, dict) or prev.get("schema") != 1 or not isinstance(prev.get("releases"), list):
                raise ReleaseError("--previous is not a channel manifest")
            if prev.get("channel", cfg["channel"]) != cfg["channel"]:
                raise ReleaseError("--previous belongs to another channel")
            if not isinstance(prev.get("sequence"), int) or seq <= prev["sequence"]:
                raise ReleaseError(f"sequence {seq} must be above the previous manifest's {prev.get('sequence')}")
            releases += [r for r in prev["releases"] if r.get("version") != version]
        manifest_doc = {"schema": 1, "channel": cfg["channel"], "sequence": seq, "generated": generated, "expires": expires,
                        "releases": releases, "addons": {}, "upstream": upstream}
    plan["documents"]["manifest.json"] = bool(manifest_doc)
    plan["missing"]["manifest.json"] = [] if manifest_doc else [artifact_names(version)["os_bundle"]]
    plan["files"] = {name: {"sha256": sha256_file(directory / name), "size": (directory / name).stat().st_size}
                     for name in sorted(p.name for p in directory.iterdir() if p.is_file()) if name not in GENERATED_FILES}

    out.mkdir(parents=True, exist_ok=True)
    for name, doc in (("install.json", install_doc), ("manifest.json", manifest_doc)):
        target = out / name
        if doc is None:
            if target.exists():
                target.unlink()
            continue
        target.write_text(dump_json(doc), encoding="utf-8")
    (out / "release-plan.json").write_text(dump_json(plan), encoding="utf-8")
    return plan


# --- preflight ---------------------------------------------------------------------------------------
def parse_sums(path: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = re.fullmatch(r"([0-9a-f]{64})  (\*?)([^/\\\0]+)", line)
        if not m:
            raise ReleaseError(f"{path.name} line {n} is not '<sha256>  <file>'")
        if m.group(3) in sums:
            raise ReleaseError(f"{path.name} lists {m.group(3)} twice")
        sums[m.group(3)] = m.group(1)
    return sums


def looks_secret(path: Path) -> str | None:
    if SECRET_NAMES.search(path.name):
        return f"{path.name}: the file name looks like key material"
    try:
        size = path.stat().st_size
        if size > SCAN_LIMIT:
            return None
        data = path.read_bytes()
    except OSError as exc:
        return f"{path.name}: unreadable ({exc})"
    for marker in SECRET_MARKERS:
        if marker in data:
            return f"{path.name}: contains {marker.decode()!r}"
    return None


def key_checks(root: Path, keys_dir: Path, ms) -> list[str]:
    errs = []
    pubs = sorted(keys_dir.glob("*.pub"))
    if not pubs:
        return [f"no trusted public key in {keys_dir}: a release signed by an unknown key cannot be verified by any printer"]
    try:
        keys = ms.load_keys([str(keys_dir)])
    except Exception as exc:  # SignatureError: nothing usable left after revocation
        return [f"{keys_dir}: {exc}"]
    ids = {k.key_id_hex for k in keys}
    if len(ids) != len(keys):
        errs.append(f"{keys_dir} holds the same key twice")
    return errs


def preflight(directory: Path, *, stage: str, root: Path, lock_path: Path, tag: str | None, keys_dir: Path | None = None,
              keyring: Path | None = None, sums: Path | None = None, now: dt.datetime | None = None) -> list[str]:
    """Returns the list of problems; empty means the set may go on to the next step."""
    errs: list[str] = []
    now = now or dt.datetime.now(dt.timezone.utc)
    ms, mf, pp = client(root)
    keys_dir = keys_dir or root / "installer" / "keys"
    keyring = keyring or root / "os" / "board" / "eitri-plus4" / "rauc" / "keyring.crt"
    try:
        lock = load_lock(lock_path)
        cfg = release_settings(lock)
    except ReleaseError as exc:
        return [str(exc)]

    # the directory
    files = sorted(p for p in directory.iterdir())
    for p in files:
        if not p.is_file() or p.is_symlink():
            errs.append(f"{p.name}: not a regular file")
    regular = [p for p in files if p.is_file() and not p.is_symlink()]
    for p in regular:
        bad = looks_secret(p)
        if bad:
            errs.append("SECRET? " + bad)
    sums_file = sums or directory / INPUTS_SUMS
    if not sums_file.is_file():
        return errs + [f"{INPUTS_SUMS} is missing: the build outputs must be pinned by digest before anything is signed"]
    try:
        sums = parse_sums(sums_file)
    except ReleaseError as exc:
        return errs + [str(exc)]
    ignore = set(GENERATED_FILES) | {INPUTS_SUMS}
    present = {p.name for p in regular} - ignore
    for name in sorted(present - set(sums)):
        errs.append(f"{name} is in the directory but not in {INPUTS_SUMS}")
    for name in sorted(set(sums) - present):
        errs.append(f"{name} is in {INPUTS_SUMS} but missing")
    for name in sorted(present & set(sums)):
        # RAUC bundles are re-signed between the two stages, which changes their bytes by design; the plan pins the new ones.
        if stage == "metadata" and name.endswith(".raucb"):
            continue
        if sha256_file(directory / name) != sums[name]:
            errs.append(f"{name} does not match {INPUTS_SUMS}")

    # version
    version = None
    for p in regular:
        m = re.fullmatch(r"eitri-plus4-(.+)\.(?:raucb|img\.zst)", p.name)
        if m:
            version = version or m.group(1)
            if m.group(1) != version:
                errs.append(f"{p.name}: another version than {version}")
    if version is None:
        for p in regular:
            m = re.fullmatch(r"data-seed-(.+)-(?:klipper|kalico)\.tar\.zst", p.name)
            if m:
                version = m.group(1)
                break
    if version is None:
        errs.append("no OS bundle, image or seed in the directory")
        return errs
    if not CALVER_RE.match(version):
        errs.append(f"version {version!r} is not YYYY.MM.N")
    if tag is not None and tag != "v" + version:
        errs.append(f"the tag is {tag} but the artifacts are version {version}")
    try:
        art = scan(directory, version)
    except ReleaseError as exc:
        return errs + [str(exc)]
    if not art["os_bundle"]:
        errs.append(f"{artifact_names(version)['os_bundle']} is missing: there is nothing to put in the channel manifest")
    if not art["image"]:
        errs.append(f"{artifact_names(version)['image']} is missing")
    if not art["seed"]:
        errs.append("no data seed in the directory")
    for f in art["partial_seed"]:
        errs.append(f"the {f} seed needs both its archive and its manifest")

    # seeds
    for f, names in art["seed"].items():
        try:
            m = read_seed(directory, root, version, f, names)
        except ReleaseError as exc:
            errs.append(str(exc))
            continue
        errs += seed_matches_lock(m, lock, f)

    # trust roots
    errs += key_checks(root, keys_dir, ms)
    if art["os_bundle"] and not keyring.is_file():
        errs.append(f"{keyring}: the RAUC CA certificate the image trusts is not committed; the OS bundle cannot be signed for any image")

    # release.lock vs the client
    if cfg["clock_floor"] != mf.CLOCK_FLOOR:
        errs.append(f"release.lock [release] clock_floor {cfg['clock_floor']} differs from CLOCK_FLOOR {mf.CLOCK_FLOOR} in "
                    "installer/eitri-update/eitri_update/manifest.py: bump them together")

    if stage == "inputs":
        return errs
    if stage != "metadata":
        return errs + [f"unknown stage {stage!r}"]

    # the unsigned metadata
    plan_path = directory / "release-plan.json"
    if not plan_path.is_file():
        return errs + ["release-plan.json is missing (run the metadata step first)"]
    try:
        plan = load_json(plan_path)
    except ReleaseError as exc:
        return errs + [str(exc)]
    if plan.get("version") != version:
        errs.append("release-plan.json is for another version")
    for name in GENERATED_FILES:
        if not (directory / name).is_file() and name != "install.json":
            errs.append(f"{name} is missing")
    gen = plan.get("generated", "")
    try:
        gen_dt, exp_dt = parse_ts(gen), parse_ts(plan.get("expires", ""))
    except ReleaseError as exc:
        return errs + [str(exc)]
    if gen_dt < parse_ts(mf.CLOCK_FLOOR):
        errs.append(f"generated {gen} is before CLOCK_FLOOR {mf.CLOCK_FLOOR}")
    if gen_dt > now + dt.timedelta(days=1):
        errs.append(f"generated {gen} is in the future")
    if now - gen_dt > dt.timedelta(days=MAX_APPROVAL_AGE_DAYS):
        errs.append(f"generated {gen} is more than {MAX_APPROVAL_AGE_DAYS} days old: the inputs were collected too long ago, collect them again")
    days = (exp_dt - gen_dt).days
    if not MIN_VALIDITY_DAYS <= days <= MAX_VALIDITY_DAYS:
        errs.append(f"the manifest is valid for {days} days (allowed {MIN_VALIDITY_DAYS}..{MAX_VALIDITY_DAYS})")
    if plan.get("sequence", 0) < cfg["min_manifest_sequence"]:
        errs.append(f"sequence {plan.get('sequence')} is below the floor {cfg['min_manifest_sequence']} baked into the image")
    if plan.get("sequence", 0) < cfg["manifest_sequence"]:
        errs.append(f"sequence {plan.get('sequence')} is below release.lock manifest_sequence {cfg['manifest_sequence']}")
    for name in (n for n in plan.get("files", {}) if n in present):
        if sha256_file(directory / name) != plan["files"][name]["sha256"]:
            errs.append(f"{name} changed after release-plan.json was written")
    errs += check_documents(directory, root, ms, mf, pp, version, cfg, lock, art, now)
    return errs


def url_file(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def consumer_seed_check(root: Path, directory: Path, lock: dict, version: str, flavor: str, archive: dict, release_flavor: dict | None) -> list[str]:
    """What a printer checks about a seed before it installs it (eitri_update.seed.check_manifest), against the values
    the signed document announces.  Skipped when the installed tree predates that module."""
    try:
        seedmod = importlib.import_module("eitri_update.seed")
    except ImportError:
        return []
    abi = "cp" + "".join(lock["python"]["version"].split(".")[:2])
    try:
        seedmod.check_manifest(load_json(directory / seedmod.manifest_name(version, flavor)), flavor=flavor, version=version, archive=archive,
                               release_flavor=release_flavor, expect_abi=abi)
    except Exception as exc:   # FlashError
        return [f"{flavor} seed: {exc}"]
    return []


def check_documents(directory: Path, root: Path, ms, mf, pp, version: str, cfg: dict, lock: dict, art: dict, now: dt.datetime) -> list[str]:
    errs: list[str] = []
    seeds = {f: load_json(directory / names[1]) for f, names in art["seed"].items()}

    def check_entry(label: str, e: dict, expect_name: str | None = None) -> None:
        name = url_file(e.get("url", ""))
        if expect_name and name != expect_name:
            errs.append(f"{label}: url names {name}, expected {expect_name}")
        p = directory / name
        if not p.is_file():
            errs.append(f"{label}: {name} is not in the release")
            return
        if e.get("size") != p.stat().st_size or e.get("sha256") != sha256_file(p):
            errs.append(f"{label}: size or SHA-256 does not match {name}")

    def check_seed(label: str, f: str, entry: dict, release_flavor: dict | None) -> None:
        check_entry(label, entry, art["seed"][f][0])
        check_entry(label + " manifest", entry.get("manifest") or {}, art["seed"][f][1])
        errs.extend(consumer_seed_check(root, directory, lock, version, f,
                                        {"name": art["seed"][f][0], "sha256": entry.get("sha256"), "size": entry.get("size")}, release_flavor))

    ij = directory / "install.json"
    if ij.is_file():
        doc = load_json(ij)
        if doc.get("version") != version:
            errs.append("install.json: version differs from the artifacts")
        if doc.get("channel") != cfg["channel"]:
            errs.append("install.json: channel differs from release.lock")
        for f in doc.get("flavors", {}):
            try:
                pp.validate(doc, f)
            except pp.PayloadError as exc:
                errs.append(f"install.json ({f}): {exc}")
            if f not in seeds:
                errs.append(f"install.json offers {f} without its seed")
                continue
            fw = seeds[f]["sources"]["firmware"]
            if doc["flavors"][f] != {"describe": fw["describe"], "commit": fw["commit"]}:
                errs.append(f"install.json: {f} describe/commit disagree with the seed manifest")
            if doc["flavors"][f]["commit"] != lock["firmware"][f]["commit"]:
                errs.append(f"install.json: {f} commit differs from release.lock")
            check_seed(f"install.json data_seed.{f}", f, doc["files"]["data_seed"][f], doc["flavors"][f])
            check_entry(f"install.json mcu_firmware.{f}", doc["files"]["mcu_firmware"][f], art["mcu"].get(f))
        check_entry("install.json rootfs", doc["files"]["rootfs"], art["rootfs"])
        check_entry("install.json stage2_initramfs", doc["files"]["stage2_initramfs"], art["stage2"])
    mj = directory / "manifest.json"
    if mj.is_file():
        raw = mj.read_bytes()
        try:
            man = mf.parse_manifest(raw)
        except Exception as exc:
            return errs + [f"manifest.json is rejected by the printer's parser: {exc}"]
        if man.sequence < cfg["min_manifest_sequence"]:
            errs.append("manifest.json: sequence is below the image floor")
        if man.expired(now):
            errs.append("manifest.json: already expired")
        newest = man.latest()
        if newest.version != version:
            errs.append(f"manifest.json: the newest release is {newest.version}, not {version}")
        for b in newest.bundles:   # older releases are published files; they are not in this directory
            label = f"manifest.json {newest.version} {b.kind} bundle"
            entry = {"url": b.url, "sha256": b.sha256, "size": b.size}
            if b.kind == "os":
                check_entry(label, entry, art["os_bundle"])
            elif b.kind == "mcu":
                check_entry(label + f" ({b.flavor})", entry, art["mcu"].get(b.flavor))
            elif b.kind == "seed" and b.flavor in seeds:
                m = b.manifest
                check_seed(label + f" ({b.flavor})", b.flavor, dict(entry, manifest={"url": m.url, "sha256": m.sha256, "size": m.size}),
                           newest.flavors.get(b.flavor))
                fl = newest.flavors.get(b.flavor)
                if fl is None or fl.get("commit") != lock["firmware"][b.flavor]["commit"]:
                    errs.append(f"manifest.json: {b.flavor} commit differs from release.lock")
            else:
                errs.append(f"{label}: unexpected bundle for this release's files ({b.kind}/{b.flavor})")
    return errs


# --- verification of the signed result ---------------------------------------------------------------
def verify_signed(directory: Path, *, root: Path, keys_dir: Path | None = None, lock_path: Path | None = None) -> list[str]:
    """Every signature in *directory* must verify with the keys a printer trusts, and the file set must match the
    signed SHA256SUMS."""
    ms, mf, pp = client(root)
    keys_dir = keys_dir or root / "installer" / "keys"
    errs: list[str] = []
    for name in SIGNED_FILES:
        f = directory / name
        if name == "install.json" and not f.is_file():
            continue
        if not f.is_file():
            errs.append(f"{name} is missing")
            continue
        try:
            ms.verify_file(str(f), str(f) + ".minisig", [str(keys_dir)])
        except Exception as exc:
            errs.append(f"{name}: signature check failed: {exc}")
    sums_path = directory / FINAL_SUMS
    if sums_path.is_file():
        try:
            sums = parse_sums(sums_path)
        except ReleaseError as exc:
            return errs + [str(exc)]
        listed = set(sums)
        actual = {p.name for p in directory.iterdir() if p.is_file()} - {FINAL_SUMS, FINAL_SUMS + ".minisig"}
        for name in sorted(actual - listed):
            errs.append(f"{name} is not covered by {FINAL_SUMS}")
        for name in sorted(listed - actual):
            errs.append(f"{FINAL_SUMS} lists {name}, which is missing")
        for name in sorted(listed & actual):
            if sha256_file(directory / name) != sums[name]:
                errs.append(f"{name} does not match {FINAL_SUMS}")
    for p in directory.iterdir():
        if p.is_file() and not p.name.endswith(".minisig"):
            bad = looks_secret(p)
            if bad:
                errs.append("SECRET? " + bad)
    mj = directory / "manifest.json"
    if mj.is_file():
        try:
            mf.parse_manifest(mj.read_bytes())
        except Exception as exc:
            errs.append(f"manifest.json is rejected by the printer's parser: {exc}")
    return errs


# --- command line ------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="release.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    ap.add_argument("--lock", type=Path, help="release.lock (default: <root>/release.lock)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("metadata", help="write the unsigned install.json, manifest.json and release-plan.json")
    m.add_argument("--artifacts", type=Path, required=True)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--version", required=True, help="release version without the leading v, e.g. 2026.10.0")
    m.add_argument("--base-url", required=True, help="https URL of the release's download directory, ending in /")
    m.add_argument("--generated", help="UTC timestamp (default: now / SOURCE_DATE_EPOCH)")
    m.add_argument("--expires", help="UTC timestamp (default: generated + manifest_validity_days)")
    m.add_argument("--sequence", type=int, help="override release.lock manifest_sequence (emergency manifests only)")
    m.add_argument("--previous", type=Path, help="the previous, already verified channel manifest to carry releases over from")
    m.add_argument("--require", default="", help="comma list of documents that must be complete: install,manifest")

    p = sub.add_parser("preflight", help="refuse to go on unless the directory is exactly what the build produced")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--stage", choices=("inputs", "metadata"), required=True)
    p.add_argument("--tag", help="the git tag (vYYYY.MM.N) the directory must belong to")
    p.add_argument("--keys-dir", type=Path)
    p.add_argument("--keyring", type=Path)
    p.add_argument("--sums", type=Path, help=f"the pinned digests of the build outputs (default: <dir>/{INPUTS_SUMS})")

    v = sub.add_parser("verify-signed", help="check every signature the way a printer does")
    v.add_argument("--dir", type=Path, required=True)
    v.add_argument("--keys-dir", type=Path)

    vf = sub.add_parser("verify-file", help="verify one minisign signature with the trusted keys")
    vf.add_argument("file", type=Path)
    vf.add_argument("--sig", type=Path)
    vf.add_argument("--keys-dir", type=Path)

    args = ap.parse_args(argv)
    root = args.root.resolve()
    lock_path = args.lock or root / "release.lock"
    try:
        if args.cmd == "metadata":
            plan = build_metadata(args.artifacts, args.out, root=root, lock_path=lock_path, version=args.version, base_url=args.base_url,
                                  generated=args.generated, expires=args.expires, sequence=args.sequence, previous=args.previous)
            for name in ("install.json", "manifest.json"):
                state = "written" if plan["documents"][name] else "NOT written, missing: " + ", ".join(plan["missing"][name])
                print(f"{name}: {state}")
            need = [x for x in args.require.split(",") if x]
            for x in need:
                if x not in ("install", "manifest"):
                    raise ReleaseError(f"--require: unknown document {x!r}")
                if not plan["documents"][x + ".json"]:
                    raise ReleaseError(f"{x}.json is required but incomplete")
            return 0
        if args.cmd == "preflight":
            errs = preflight(args.dir, stage=args.stage, root=root, lock_path=lock_path, tag=args.tag, keys_dir=args.keys_dir,
                             keyring=args.keyring, sums=args.sums)
            for e in errs:
                print("PREFLIGHT FAIL:", e, file=sys.stderr)
            if not errs:
                print(f"preflight ({args.stage}): ok")
            return 1 if errs else 0
        if args.cmd == "verify-signed":
            errs = verify_signed(args.dir, root=root, keys_dir=args.keys_dir)
            for e in errs:
                print("VERIFY FAIL:", e, file=sys.stderr)
            if not errs:
                print("signed set: ok")
            return 1 if errs else 0
        if args.cmd == "verify-file":
            ms, _, _ = client(root)
            sig = ms.verify_file(str(args.file), str(args.sig) if args.sig else None, [str(args.keys_dir or root / "installer" / "keys")])
            print("ok:", sig.trusted_comment)
            return 0
    except ReleaseError as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    except Exception as exc:  # signature errors from the client code
        print("error:", exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
