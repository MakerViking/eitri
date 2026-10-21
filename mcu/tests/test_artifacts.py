# SPDX-License-Identifier: Apache-2.0
"""Verification of BUILT firmware bundles. Runs without rebuilding: with no artifacts it skips.

Looks in $EITRI_MCU_OUT, else <project>/build/mcu/out. For each mcu-firmware-*-<flavor>.tar.zst it decompresses every
image's embedded data dictionary and checks MCU, CLOCK_FREQ, RESERVE_PINS_serial, RESERVE_PINS_crystal (mainboard) and
SERIAL_BAUD against tests/live_constants.json, the vector table against the flash offset, the size against the flash
above it, and the version against the host's `git describe` (the data seed's manifest, when present)."""
import os
from pathlib import Path

import pytest

from conftest import LIVE, PARAMS, build_root, mcutool, seed_manifest


def bundles():
    out = Path(os.environ.get("EITRI_MCU_OUT") or build_root() / "mcu" / "out")
    return sorted(out.glob("mcu-firmware-*-*.tar.zst")) if out.is_dir() else []


def flavor_of(path):
    return path.name[: -len(".tar.zst")].rsplit("-", 1)[1]


def test_no_release_bundle_exists_while_a_board_is_unresolved():
    unresolved = mcutool.gate(PARAMS)
    assert not (unresolved and bundles()), "release bundles exist although %s" % "; ".join(unresolved)


@pytest.mark.parametrize("archive", bundles() or [None], ids=lambda p: p.name if p else "no-artifacts")
def test_built_bundle_matches_the_live_printer(archive):
    if archive is None:
        pytest.skip("no built MCU firmware bundle (EITRI_MCU_OUT or build/mcu/out); nothing to verify")
    flavor = flavor_of(archive)
    seed = seed_manifest(flavor)
    describe = seed["sources"]["firmware"]["describe"] if seed else None
    problems, sidecar = mcutool.verify_bundle(archive, describe=describe)
    assert problems == []
    assert sidecar["flavor"] == flavor
    for board in ("mainboard", "toolhead"):
        entry = sidecar["boards"][board]
        assert entry["mcu"] == LIVE[board]["MCU"] and entry["baud"] == LIVE[board]["SERIAL_BAUD"]
        if describe:
            assert entry["version"] == describe
