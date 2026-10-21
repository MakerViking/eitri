# SPDX-License-Identifier: Apache-2.0
import copy
import json
import os
import struct
import sys
import zlib
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
MCU = TESTS.parent
REPO = MCU.parent
sys.path[:0] = [str(MCU), str(REPO / "installer" / "mcuflash")]

import mcutool  # noqa: E402

PARAMS = mcutool.load_params()
LIVE = mcutool.load_live()
DESCRIBE = "v0.13.0-813-ga08fc79fc"


def build_root():
    """<project>/build when this tree sits in the project layout (eitri/ next to build/)."""
    return REPO.parent / "build"


def pinned_tree(flavor):
    """A checkout of the pinned firmware tree, or None (tests that need it then skip)."""
    env = os.environ.get("EITRI_SOURCE_" + flavor.upper())
    cand = Path(env) if env else build_root() / "seed" / "src" / flavor
    return cand if (cand / "src" / "stm32" / "Kconfig").is_file() else None


def seed_manifest(flavor):
    out = Path(os.environ.get("EITRI_SEED_OUT") or build_root() / "seed" / "out")
    for p in sorted(out.glob("data-seed-*-%s.manifest.json" % flavor)):
        return json.loads(p.read_text())
    return None


def resolved_params():
    """params.json with the toolhead pretended to be settled: only to test code that is gated behind it."""
    doc = copy.deepcopy(PARAMS)
    t = doc["boards"]["toolhead"]
    t["status"], t["flash_offset"] = "resolved", t["flash_offset_if_katapult"]
    return doc


def synth_image(board, bp=None, live=None, version=DESCRIBE, offset=None, sp=None, size=4096, config_override=None, kconfig_offset=None):
    """Something shaped like klipper.bin for *board*: Thumb vector table, filler, the zlib data dictionary."""
    bp = bp or PARAMS["boards"][board]
    live = live or LIVE[board]
    off = offset if offset is not None else mcutool.effective_offset(bp)
    ram_top = 0x20000000 + mcutool.hexint(bp["ram_size"])
    handlers = [(off + 0x100 + 4 * i) | 1 for i in range(1, 15)]
    words = [sp if sp is not None else ram_top] + [(off + 0x101)] + handlers[:13]
    words += [0] * (16 - len(words))
    cfg = {k: v for k, v in live.items() if k not in ("klipper_name", "port")}
    cfg.update({"RECEIVE_WINDOW": 192, "ADC_MAX": 4095})
    cfg.update(config_override or {})
    k_off = kconfig_offset if kconfig_offset is not None else off
    doc = {"app": "Klipper", "version": version, "build_versions": "gcc: (test) binutils: (test)",
           "commands": {"identify offset=%u count=%c": 1}, "responses": {}, "enumerations": {}, "config": cfg,
           "kconfig": "CONFIG_MACH_STM32=y\nCONFIG_STM32_FLASH_START_%X=y\n" % (k_off - 0x08000000)}
    comp = zlib.compress(json.dumps(doc, sort_keys=True).encode(), 9)
    blob = struct.pack("<16I", *words) + bytes((i * 7) & 0xFF for i in range(200)) + comp
    return blob + b"\xA5" * max(0, size - len(blob))


def synth_elf(path, paddr):
    """A 32-bit ARM ELF with one PT_LOAD at *paddr* (just enough for elf_link_address)."""
    ehdr = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\0" * 8
    ehdr += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, off := paddr + 1, 52, 0, 0x05000200, 52, 32, 1, 40, 0, 0)
    phdr = struct.pack("<IIIIIIII", 1, 0x1000, paddr, paddr, 64, 64, 5, 4)
    Path(path).write_bytes(ehdr + phdr)
