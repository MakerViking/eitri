# SPDX-License-Identifier: Apache-2.0
"""Resolve the MCU firmware to flash: fetch a prebuilt one, or build on-device.

Default ("eitri" channel): the release bundle ships CI-built images together
with a manifest, so host and MCU are version-locked by construction.  In the
opt-in "upstream" mode the toolchain add-on is installed and the image is built
here from the (pristine) Klipper/Kalico clone using an out-of-tree build
directory, so the clone never becomes dirty.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .errors import FlashError
from .fsutil import sha256_file
from .proc import Runner, run

FLAVORS = ("klipper", "kalico")
DEFAULT_FIRMWARE_ROOT = "/data/eitri/mcu/firmware"
DEFAULT_KLIPPER_DIR = "/data/firmware/klipper"
DEFAULT_TOOLCHAIN_DIR = "/data/eitri/toolchain"   # the optional "follow upstream" add-on

_DESCRIBE = re.compile(r"^(?P<tag>.+?)-(?P<n>\d+)-g(?P<sha>[0-9a-f]{7,40})(?P<dirty>-dirty)?")


@dataclass
class FirmwareArtifact:
    board: str
    flavor: str
    path: str
    sha256: str
    size: int
    version: str
    mcu: str
    app_address: Optional[int]
    source_commit: str = ""
    origin: str = "bundle"  # "bundle" | "built"

    def read(self) -> bytes:
        with open(self.path, "rb") as fh:
            return fh.read()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "board": self.board,
            "flavor": self.flavor,
            "file": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "version": self.version,
            "mcu": self.mcu,
            "app_address": ("0x%08x" % self.app_address) if self.app_address is not None else None,
            "source_commit": self.source_commit,
            "origin": self.origin,
        }


def extract_embedded_dictionary(blob: bytes) -> Optional[Dict[str, Any]]:
    """Find the zlib-compressed data dictionary Klipper embeds in klipper.bin."""
    for idx in range(len(blob) - 2):
        b0, b1 = blob[idx], blob[idx + 1]
        if b0 != 0x78 or ((b0 << 8) | b1) % 31:
            continue
        try:
            raw = zlib.decompressobj().decompress(blob[idx : idx + (1 << 18)])
            doc = json.loads(raw)
        except (zlib.error, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("app") == "Klipper":
            return doc
    return None


def parse_describe(version: str) -> Optional[Dict[str, Any]]:
    m = _DESCRIBE.match(version.strip())
    if not m:
        return None
    return {"tag": m.group("tag"), "ahead": int(m.group("n")), "sha": m.group("sha"), "dirty": bool(m.group("dirty"))}


def versions_match(host_version: str, mcu_version: str) -> bool:
    """True if both come from the same commit (the -dirty/date/host suffix is ignored)."""
    a, b = parse_describe(host_version), parse_describe(mcu_version)
    if not a or not b:
        return host_version.strip() == mcu_version.strip()
    n = min(len(a["sha"]), len(b["sha"]))
    return a["sha"][:n] == b["sha"][:n] and a["tag"] == b["tag"]


def host_version(klipper_dir: str = DEFAULT_KLIPPER_DIR, runner: Runner = run) -> str:
    res = runner(["git", "-C", klipper_dir, "describe", "--always", "--tags", "--long", "--dirty"])
    if res.returncode != 0:
        raise FlashError("cannot determine the host Klipper version in %s: %s" % (klipper_dir, res.output.strip()), code="host_version")
    return res.output.strip().splitlines()[-1]


def load_artifact(root: str, flavor: str, board: str, *, expect_mcu: Optional[str] = None) -> FirmwareArtifact:
    if flavor not in FLAVORS:
        raise FlashError("unknown firmware flavor %r (expected klipper or kalico)" % flavor, code="flavor")
    flavor_dir = os.path.join(root, flavor)
    manifest_path = os.path.join(flavor_dir, "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise FlashError(
            "no MCU firmware manifest for flavor %s at %s (%s)" % (flavor, manifest_path, exc),
            code="no_firmware",
            board=board,
            recovery="Install the release bundle that matches the chosen flavor, or build with --build in upstream mode.",
        ) from exc
    if manifest.get("flavor") != flavor:
        raise FlashError("firmware manifest %s is for flavor %r" % (manifest_path, manifest.get("flavor")), code="flavor", board=board)
    entry = (manifest.get("boards") or {}).get(board)
    if not entry:
        raise FlashError("manifest %s has no firmware for the %s board" % (manifest_path, board), code="no_firmware", board=board)
    path = os.path.realpath(os.path.join(flavor_dir, entry["file"]))
    if not path.startswith(os.path.realpath(flavor_dir) + os.sep):
        raise FlashError("manifest path escapes the firmware directory: %s" % entry["file"], code="manifest", board=board)
    if not os.path.isfile(path):
        raise FlashError("firmware file missing: %s" % path, code="no_firmware", board=board)
    digest = sha256_file(path)
    if digest != entry["sha256"].lower():
        raise FlashError(
            "firmware checksum mismatch for %s (manifest %s, file %s)" % (path, entry["sha256"], digest),
            code="checksum",
            board=board,
            recovery="Re-download the release bundle; do not flash this file.",
        )
    blob = open(path, "rb").read()
    embedded = extract_embedded_dictionary(blob) or {}
    version = str(embedded.get("version") or entry.get("version") or "")
    mcu = str((embedded.get("config") or {}).get("MCU") or entry.get("mcu") or "")
    if embedded and entry.get("version") and embedded.get("version") != entry["version"]:
        raise FlashError(
            "manifest version %s does not match the version %s embedded in %s"
            % (entry["version"], embedded.get("version"), path),
            code="manifest",
            board=board,
        )
    if not mcu or not version:
        raise FlashError("cannot determine MCU/version of %s" % path, code="manifest", board=board)
    if expect_mcu and mcu.lower() != expect_mcu.lower():
        raise FlashError(
            "firmware %s is built for %s but the board reports %s" % (path, mcu, expect_mcu),
            code="mcu_mismatch",
            board=board,
            recovery="Rebuild with the board's MCU selected; flashing it would not boot.",
        )
    app = entry.get("app_address")
    return FirmwareArtifact(
        board=board,
        flavor=flavor,
        path=path,
        sha256=digest,
        size=len(blob),
        version=version,
        mcu=mcu,
        app_address=int(app, 0) if isinstance(app, str) else app,
        source_commit=str((manifest.get("source") or {}).get("commit", "")),
    )


def build_artifact(
    board: str,
    flavor: str,
    *,
    klipper_dir: str,
    config_file: str,
    out_dir: str,
    runner: Runner = run,
    jobs: int = 4,
    on_line=None,
    check_toolchain: bool = True,
    toolchain_dir: str = DEFAULT_TOOLCHAIN_DIR,
) -> FirmwareArtifact:
    """Build klipper.bin out-of-tree from the pristine clone (upstream mode)."""
    tool_bin = os.path.join(toolchain_dir, "bin")
    search_path = tool_bin + os.pathsep + os.environ.get("PATH", "")
    if check_toolchain and shutil.which("arm-none-eabi-gcc", path=search_path) is None:
        raise FlashError(
            "arm-none-eabi-gcc not found; install the toolchain add-on first (eitri-update upstream enable)",
            code="no_toolchain",
            board=board,
        )
    if not os.path.isfile(config_file):
        raise FlashError("no firmware config for %s at %s" % (board, config_file), code="no_config", board=board)
    # Fresh output directory every time; the .config lives next to it because
    # `make clean` wipes OUT.  Both are passed on the command line since the
    # Klipper Makefile assigns KCONFIG_CONFIG itself (the environment loses).
    out_dir = out_dir.rstrip("/")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    kconfig = out_dir + ".config"
    shutil.copyfile(config_file, kconfig)
    base = ["make", "-C", klipper_dir, "OUT=%s/" % out_dir, "KCONFIG_CONFIG=%s" % kconfig]
    for target in (["olddefconfig"], ["-j%d" % jobs]):
        res = runner(base + target, on_line=on_line, env={"PATH": search_path})
        if res.returncode != 0:
            tail = "\n".join(res.output.strip().splitlines()[-12:])
            raise FlashError(
                "firmware build failed (make %s):\n%s" % (" ".join(target), tail), code="build_failed", board=board
            )
    built = os.path.join(out_dir, "klipper.bin")
    if not os.path.isfile(built):
        raise FlashError("build finished but %s is missing" % built, code="build_failed", board=board)
    blob = open(built, "rb").read()
    embedded = extract_embedded_dictionary(blob) or {}
    mcu = str((embedded.get("config") or {}).get("MCU", ""))
    version = str(embedded.get("version", ""))
    if not mcu or not version:
        raise FlashError("built image has no embedded data dictionary", code="build_failed", board=board)
    kc = embedded.get("kconfig") or {}
    app = kc.get("CONFIG_FLASH_APPLICATION_ADDRESS")
    return FirmwareArtifact(
        board=board,
        flavor=flavor,
        path=built,
        sha256=hashlib.sha256(blob).hexdigest(),
        size=len(blob),
        version=version,
        mcu=mcu,
        app_address=int(app, 0) if isinstance(app, str) else app,
        source_commit=parse_describe(version)["sha"] if parse_describe(version) else "",
        origin="built",
    )
