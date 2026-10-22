#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tooling behind mcu/build-mcu.sh: the parameter gate, Kconfig checks, image verification and bundle packaging.

    mcutool.py gate [--board B]...            exit 3 unless every selected board's parameters are resolved
    mcutool.py check-config --tree DIR        expand each board's config in a Klipper/Kalico tree (needs `make` and
                                              python only, no toolchain) and compare it with mcu/params.json
    mcutool.py package ...                    verify built images, write the bundle + sidecar manifest (gate first)
    mcutool.py verify ARCHIVE                 re-verify a built bundle against the live constants
    mcutool.py lock SECTION KEY               read one value from release.lock

Standard library only (Python >= 3.11 for tomllib); run it with `python3 -I`.  Nothing here touches a device.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MCU_DIR = Path(__file__).resolve().parent
REPO = MCU_DIR.parent
PARAMS_PATH = MCU_DIR / "params.json"
LIVE_PATH = MCU_DIR / "tests" / "live_constants.json"
LOCK_PATH = REPO / "release.lock"

# One parser for the data dictionary: the flasher's own, so the build checks exactly what the flasher will read.
sys.path.insert(0, str(REPO / "installer" / "mcuflash"))
from eitri_mcuflash import firmware as _fw  # noqa: E402

BOARDS = ("mainboard", "toolhead")
FLAVORS = ("klipper", "kalico")
EX_OK, EX_FAIL, EX_USAGE, EX_UNRESOLVED = 0, 1, 2, 3
RAM_BASE = 0x20000000


class McuError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- parameters
def load_params(path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    with open(path or PARAMS_PATH, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("schema") != 1:
        raise McuError("unsupported params schema %r" % doc.get("schema"))
    for name in BOARDS:
        if name not in doc.get("boards", {}):
            raise McuError("params.json has no %s board" % name)
    return doc


def load_live(path: Optional[os.PathLike] = None) -> Dict[str, Dict[str, Any]]:
    with open(path or LIVE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)["boards"]


def hexint(value: Any) -> Optional[int]:
    return int(value, 0) if isinstance(value, str) else value


def effective_offset(bp: Dict[str, Any]) -> Optional[int]:
    """Absolute application address a build of this board is linked for (params.json calls it flash_offset).

    For an unresolved board this is only the offset of the one layout the config is written for (Katapult's)."""
    if bp.get("status") == "resolved":
        return hexint(bp.get("flash_offset"))
    return hexint(bp.get("flash_offset_if_katapult"))


def gate(params: Dict[str, Any], boards: Sequence[str] = BOARDS) -> List[str]:
    """Reasons a release must not be built for *boards* (empty list = go)."""
    problems = []
    for name in boards:
        bp = params["boards"][name]
        if bp.get("status") != "resolved":
            q = (bp.get("open_questions") or ["the flash layout is not settled"])[0]
            problems.append("%s: parameters unresolved (confidence %s). Open question: %s" % (name, bp.get("confidence", "?"), q))
        elif bp.get("flash_offset") is None:
            problems.append("%s: marked resolved but has no flash_offset" % name)
    return problems


# ---------------------------------------------------------------------------------------------- Kconfig
def parse_defconfig(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def expand_config(config: os.PathLike, tree: os.PathLike, work: os.PathLike, make: str = "make") -> Dict[str, str]:
    """`make olddefconfig` out of tree: the Klipper/Kalico clone is never written to."""
    work = Path(work)
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    cfg = work / "config"
    shutil.copyfile(config, cfg)
    res = subprocess.run([make, "-C", str(tree), "OUT=%s/" % out, "KCONFIG_CONFIG=%s" % cfg, "olddefconfig"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, check=False)
    if res.returncode != 0:
        raise McuError("make olddefconfig failed for %s:\n%s" % (config, res.stdout[-800:]))
    return parse_defconfig(cfg.read_text())


def check_expanded(board: str, bp: Dict[str, Any], cfg: Dict[str, str]) -> List[str]:
    """Compare an expanded Kconfig with params.json."""
    problems: List[str] = []

    def want(key: str, expected: Any) -> None:
        have = cfg.get(key)
        if have is None or str(have).lower() != str(expected).lower():
            problems.append("%s: %s is %s, params.json says %s" % (board, key, have, expected))

    want("CONFIG_MCU", bp["mcu"])
    want("CONFIG_CLOCK_FREQ", bp["clock_freq"])
    want("CONFIG_SERIAL_BAUD", bp["baud"])
    want("CONFIG_STM32_CLOCK_REF_8M", "y")
    want("CONFIG_CLOCK_REF_FREQ", 8000000)
    want("CONFIG_" + bp["kconfig_serial"], "y")
    want("CONFIG_RAM_SIZE", hex(hexint(bp["ram_size"])))
    off = effective_offset(bp)
    if off is None:
        problems.append("%s: no application offset to compare" % board)
    else:
        have = cfg.get("CONFIG_FLASH_APPLICATION_ADDRESS")
        if have is None or int(have, 0) != off:
            problems.append("%s: CONFIG_FLASH_APPLICATION_ADDRESS is %s, params.json says 0x%08x" % (board, have, off))
    if cfg.get("CONFIG_FLASH_BOOT_ADDRESS") and int(cfg["CONFIG_FLASH_BOOT_ADDRESS"], 0) != hexint(bp["flash_base"]):
        problems.append("%s: CONFIG_FLASH_BOOT_ADDRESS %s is not the flash base" % (board, cfg["CONFIG_FLASH_BOOT_ADDRESS"]))
    return problems


# ---------------------------------------------------------------------------------------------- images
def vector_table(blob: bytes) -> Dict[str, Any]:
    if len(blob) < 64:
        raise McuError("image is only %d bytes" % len(blob))
    words = struct.unpack("<16I", blob[:64])
    return {"sp": words[0], "reset": words[1], "handlers": [w for w in words[1:] if w]}


def elf_link_address(path: os.PathLike) -> int:
    """Load address (LMA) of the first loadable segment of a 32-bit little-endian ARM ELF."""
    with open(path, "rb") as fh:
        head = fh.read(52)
        if head[:4] != b"\x7fELF" or head[4] != 1 or head[5] != 1:
            raise McuError("%s is not a 32-bit little-endian ELF" % path)
        phoff, = struct.unpack_from("<I", head, 28)
        phentsize, phnum = struct.unpack_from("<HH", head, 42)
        fh.seek(phoff)
        addrs = []
        for _ in range(phnum):
            ph = fh.read(phentsize)
            ptype, _off, _vaddr, paddr, filesz = struct.unpack_from("<IIIII", ph, 0)
            if ptype == 1 and filesz:
                addrs.append(paddr)
    if not addrs:
        raise McuError("%s has no loadable segment" % path)
    return min(addrs)


def check_image(blob: bytes, board: str, bp: Dict[str, Any], live: Dict[str, Any], *, describe: Optional[str] = None,
                link_address: Optional[int] = None) -> List[str]:
    """Everything that must hold for one built klipper.bin.  An empty list means it passed."""
    problems: List[str] = []
    doc = _fw.extract_embedded_dictionary(blob)
    if doc is None:
        return ["%s: no data dictionary inside the image" % board]
    cfg = doc.get("config") or {}
    # the live printer's constants (klippy.log)
    for key in ("MCU", "CLOCK_FREQ", "SERIAL_BAUD", "RESERVE_PINS_serial", "RESERVE_PINS_crystal"):
        if key not in live:
            continue
        if str(cfg.get(key)).lower() != str(live[key]).lower():
            problems.append("%s: %s is %s, the live printer reports %s" % (board, key, cfg.get(key), live[key]))
    if str(cfg.get("MCU")).lower() != bp["mcu"].lower():
        problems.append("%s: MCU %s is not params.json's %s" % (board, cfg.get("MCU"), bp["mcu"]))
    # the version string must be the host's `git describe`
    if describe is not None and doc.get("version") != describe:
        problems.append("%s: version %r is not the host's %r" % (board, doc.get("version"), describe))
    # where the image is linked
    off = effective_offset(bp)
    base = hexint(bp["flash_base"])
    if off is None:
        problems.append("%s: no application offset known" % board)
        return problems
    linked = _fw.dictionary_app_address(doc)
    if linked != off:
        problems.append("%s: the image's own build configuration says 0x%s, expected 0x%08x"
                        % (board, ("%08x" % linked) if linked is not None else "?", off))
    if link_address is not None and link_address != off:
        problems.append("%s: the ELF is linked at 0x%08x, expected 0x%08x" % (board, link_address, off))
    # vector table: stack at the top of RAM, every handler a Thumb address inside the image
    try:
        vt = vector_table(blob)
    except McuError as exc:
        problems.append("%s: %s" % (board, exc))
        return problems
    ram_top = RAM_BASE + hexint(bp["ram_size"])
    if vt["sp"] != ram_top:
        problems.append("%s: initial stack pointer 0x%08x is not the top of RAM 0x%08x" % (board, vt["sp"], ram_top))
    lo, hi = off, off + len(blob)
    stray = [h for h in vt["handlers"] if not (h & 1) or not (lo <= (h & ~1) < hi)]
    if stray:
        problems.append("%s: vector table entries %s are not Thumb addresses inside 0x%08x-0x%08x: the image is not linked for 0x%08x"
                        % (board, ", ".join("0x%08x" % h for h in stray[:4]), lo, hi, off))
    # fits above the bootloader
    room = bp["chip_flash_kib"] * 1024 - (off - base)
    if len(blob) > room:
        problems.append("%s: image is %d bytes, only %d fit above the bootloader" % (board, len(blob), room))
    return problems


def lock_value(section: str, key: str, lock: Optional[os.PathLike] = None) -> Any:
    import tomllib
    with open(lock or LOCK_PATH, "rb") as fh:
        doc = tomllib.load(fh)
    node: Any = doc
    for part in section.split("."):
        node = node[part]
    return node[key]


# ---------------------------------------------------------------------------------------------- packaging
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar_bytes(files: Dict[str, bytes], epoch: int) -> bytes:
    """A tar that depends only on its contents: sorted, fixed owner, fixed times and modes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        dirs = sorted({os.path.dirname(n) for n in files if os.path.dirname(n)} | {os.path.dirname(os.path.dirname(n)) for n in files if os.path.dirname(os.path.dirname(n))})
        for d in dirs:
            ti = tarfile.TarInfo(d + "/")
            ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, epoch
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tar.addfile(ti)
        for name in sorted(files):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(files[name]), 0o644, epoch
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tar.addfile(ti, io.BytesIO(files[name]))
    return buf.getvalue()


def _zstd(data: bytes) -> bytes:
    res = subprocess.run(["zstd", "-q", "-19", "-T1", "-c"], input=data, stdout=subprocess.PIPE, check=False)
    if res.returncode != 0:
        raise McuError("zstd failed")
    return res.stdout


def _unzstd(path: os.PathLike) -> bytes:
    res = subprocess.run(["zstd", "-q", "-d", "-c", str(path)], stdout=subprocess.PIPE, check=False)
    if res.returncode != 0:
        raise McuError("cannot decompress %s" % path)
    return res.stdout


def package(*, flavor: str, version: str, describe: str, commit: str, epoch: int, out: os.PathLike,
            builds: Dict[str, os.PathLike], toolchain: Dict[str, Any], params: Optional[Dict[str, Any]] = None,
            live: Optional[Dict[str, Any]] = None, origin: str = "") -> Dict[str, Any]:
    """Verify every built board and write mcu-firmware-<version>-<flavor>.tar.zst plus its sidecar manifest.

    *builds* maps board -> directory holding klipper.bin (and klipper.elf).  Refuses unresolved boards."""
    params = params or load_params()
    live = live or load_live()
    if flavor not in FLAVORS:
        raise McuError("unknown flavor %r" % flavor)
    built = [name for name in BOARDS if name in builds]
    if not built or set(builds) - set(BOARDS):
        raise McuError("a bundle needs one or both of %s, got %s" % (", ".join(BOARDS), ", ".join(sorted(builds)) or "none"))
    problems = gate(params, built)
    if problems:
        raise McuError("refusing to build a release bundle:\n  " + "\n  ".join(problems))
    # A board left out is named with the reason, so a consumer never mistakes it for a lost file.
    omitted = {name: (params["boards"][name].get("open_questions") or ["not built"])[0]
               for name in BOARDS if name not in builds}
    files: Dict[str, bytes] = {}
    boards: Dict[str, Any] = {}
    for name in built:
        bp = params["boards"][name]
        bdir = Path(builds[name])
        blob = (bdir / "klipper.bin").read_bytes()
        elf = bdir / "klipper.elf"
        link = elf_link_address(elf) if elf.is_file() else None
        bad = check_image(blob, name, bp, live[name], describe=describe, link_address=link)
        if link is None:
            bad.append("%s: klipper.elf is missing, so the link address cannot be proven" % name)
        if bad:
            raise McuError("image verification failed:\n  " + "\n  ".join(bad))
        files["%s/%s/klipper.bin" % (flavor, name)] = blob
        off = effective_offset(bp)
        boards[name] = {
            "file": "%s/klipper.bin" % name, "sha256": _sha256(blob), "size": len(blob),
            "mcu": bp["mcu"], "version": describe, "app_address": "0x%08x" % off, "flash_offset": "0x%08x" % off,
            "link_address": "0x%08x" % link, "clock_ref": bp["clock_ref"], "clock_freq": bp["clock_freq"],
            "serial": bp["serial"], "baud": bp["baud"], "bootloader": bp["bootloader"], "bootloader_baud": bp["bootloader_baud"],
            "method": bp["method"], "parameters": {"status": bp["status"], "confidence": bp["confidence"]},
            "firmware_describe": describe, "toolchain": toolchain.get("version", ""),
        }
    inner = {"schema": 1, "flavor": flavor, "version": version, "source": {"commit": commit, "describe": describe, "origin": origin},
             "toolchain": toolchain, "boards": boards, "omitted": omitted}
    files["%s/manifest.json" % flavor] = (json.dumps(inner, indent=2, sort_keys=True) + "\n").encode()
    archive = _zstd(_tar_bytes(files, epoch))
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = "mcu-firmware-%s-%s" % (version, flavor)
    (outdir / (stem + ".tar.zst")).write_bytes(archive)
    sidecar = dict(inner)
    sidecar["kind"] = "mcu-firmware"
    sidecar["source_date_epoch"] = epoch
    sidecar["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    sidecar["archive"] = {"file": stem + ".tar.zst", "sha256": _sha256(archive), "size": len(archive)}
    (outdir / (stem + ".manifest.json")).write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return sidecar


def verify_bundle(archive: os.PathLike, *, params: Optional[Dict[str, Any]] = None, live: Optional[Dict[str, Any]] = None,
                  describe: Optional[str] = None) -> Tuple[List[str], Dict[str, Any]]:
    """Re-check a built bundle: sidecar digest, member digests, then every image."""
    params = params or load_params()
    live = live or load_live()
    archive = Path(archive)
    sidecar_path = archive.with_name(archive.name[: -len(".tar.zst")] + ".manifest.json")
    problems: List[str] = []
    raw = archive.read_bytes()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8")) if sidecar_path.is_file() else {}
    if not sidecar:
        problems.append("no sidecar manifest next to %s" % archive.name)
    elif sidecar.get("archive", {}).get("sha256") != _sha256(raw):
        problems.append("archive sha256 does not match its manifest")
    members: Dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(_unzstd(archive)), mode="r:") as tar:
        for ti in tar.getmembers():
            if ti.isfile():
                members[ti.name] = tar.extractfile(ti).read()  # type: ignore[union-attr]
    flavor = sidecar.get("flavor", "")
    inner = json.loads(members.get("%s/manifest.json" % flavor, b"{}"))
    for name in BOARDS:
        entry = (inner.get("boards") or {}).get(name)
        if not entry:
            if not (inner.get("omitted") or {}).get(name):
                problems.append("bundle has no %s board and does not say why" % name)
            continue
        blob = members.get("%s/%s" % (flavor, entry["file"]))
        if blob is None:
            problems.append("%s: %s missing from the archive" % (name, entry["file"]))
            continue
        if _sha256(blob) != entry["sha256"]:
            problems.append("%s: image sha256 does not match the manifest" % name)
        problems += check_image(blob, name, params["boards"][name], live[name], describe=describe or entry.get("version"))
        if int(entry["app_address"], 0) != effective_offset(params["boards"][name]):
            problems.append("%s: manifest app_address %s disagrees with params.json" % (name, entry["app_address"]))
    return problems, sidecar


# ---------------------------------------------------------------------------------------------- CLI
def _cmd_gate(args: argparse.Namespace) -> int:
    params = load_params(args.params)
    boards = args.board or list(BOARDS)
    problems = gate(params, boards)
    for p in problems:
        sys.stderr.write("unresolved: %s\n" % p)
    if problems:
        sys.stderr.write("No release bundle is built while a board is unresolved (mcu/README.md, \"Open questions\").\n")
        return EX_UNRESOLVED
    print("go: %s" % ", ".join(boards))
    return EX_OK


def _cmd_check_config(args: argparse.Namespace) -> int:
    params = load_params(args.params)
    bad: List[str] = []
    for name in args.board or list(BOARDS):
        bp = params["boards"][name]
        with tempfile.TemporaryDirectory(prefix="mcu-kconfig-") as work:
            cfg = expand_config(MCU_DIR / bp["config"], args.tree, work, make=args.make)
        probs = check_expanded(name, bp, cfg)
        bad += probs
        print("%s: %s (%s, application 0x%s)" % (name, "FAILED" if probs else "ok", cfg.get("CONFIG_MCU"), cfg.get("CONFIG_FLASH_APPLICATION_ADDRESS", "?")[2:]))
    for p in bad:
        sys.stderr.write(p + "\n")
    return EX_FAIL if bad else EX_OK


def _cmd_package(args: argparse.Namespace) -> int:
    builds = dict(item.split("=", 1) for item in args.build)
    toolchain = {"name": args.toolchain_name, "version": args.toolchain_version, "sha256": args.toolchain_sha256, "gcc": args.toolchain_gcc}
    try:
        sidecar = package(flavor=args.flavor, version=args.version, describe=args.describe, commit=args.commit, epoch=args.epoch,
                          out=args.out, builds=builds, toolchain=toolchain, params=load_params(args.params), origin=args.origin)
    except McuError as exc:
        sys.stderr.write("%s\n" % exc)
        return EX_UNRESOLVED if "refusing to build" in str(exc) else EX_FAIL
    print(json.dumps({"archive": sidecar["archive"], "boards": {k: v["sha256"] for k, v in sidecar["boards"].items()}}, indent=2))
    return EX_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    problems, sidecar = verify_bundle(args.archive, params=load_params(args.params), describe=args.describe)
    for p in problems:
        sys.stderr.write(p + "\n")
    print("%s: %s" % (args.archive, "FAILED" if problems else "ok"))
    return EX_FAIL if problems else EX_OK


def _cmd_lock(args: argparse.Namespace) -> int:
    print(lock_value(args.section, args.key, args.lock))
    return EX_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="mcutool", description=__doc__.splitlines()[0])
    ap.add_argument("--params", help="alternative params.json (tests)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--board", action="append", choices=BOARDS)
    g.set_defaults(fn=_cmd_gate)
    c = sub.add_parser("check-config")
    c.add_argument("--tree", required=True, help="Klipper or Kalico source tree (never written to)")
    c.add_argument("--board", action="append", choices=BOARDS)
    c.add_argument("--make", default="make")
    c.set_defaults(fn=_cmd_check_config)
    p = sub.add_parser("package")
    p.add_argument("--flavor", required=True, choices=FLAVORS)
    p.add_argument("--version", required=True)
    p.add_argument("--describe", required=True, help="the host's `git describe` (seed manifest sources.firmware.describe)")
    p.add_argument("--commit", required=True)
    p.add_argument("--epoch", required=True, type=int)
    p.add_argument("--out", required=True)
    p.add_argument("--origin", default="")
    p.add_argument("--build", action="append", required=True, metavar="BOARD=DIR")
    p.add_argument("--toolchain-name", default="")
    p.add_argument("--toolchain-version", default="")
    p.add_argument("--toolchain-sha256", default="")
    p.add_argument("--toolchain-gcc", default="")
    p.set_defaults(fn=_cmd_package)
    v = sub.add_parser("verify")
    v.add_argument("archive")
    v.add_argument("--describe")
    v.set_defaults(fn=_cmd_verify)
    lk = sub.add_parser("lock")
    lk.add_argument("section")
    lk.add_argument("key")
    lk.add_argument("--lock")
    lk.set_defaults(fn=_cmd_lock)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except McuError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return EX_FAIL


if __name__ == "__main__":
    sys.exit(main())
