# SPDX-License-Identifier: Apache-2.0
"""rauc-pre-install: RAUC may only write slots on the disk that holds the running root file system."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import LIBEXEC, OVERLAY, run

HOOK = LIBEXEC / "rauc-pre-install"


@pytest.fixture
def sysfs(tmp_path: Path) -> Path:
    """A sysfs with a microSD card (mmcblk0, p1 p5 p6 p7) and an eMMC (mmcblk1, p1 p2)."""
    root = tmp_path / "sys"
    for disk, parts in (("mmcblk0", (1, 5, 6, 7)), ("mmcblk1", (1, 2))):
        for nr in parts:
            part = root / "devices" / "platform" / disk / f"{disk}p{nr}"
            part.mkdir(parents=True)
            (part / "partition").write_text(f"{nr}\n")
            link = root / "class" / "block" / f"{disk}p{nr}"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(part)
        (root / "class" / "block" / disk).symlink_to(root / "devices" / "platform" / disk)
    return root


def hook(sysfs: Path, root_disk: str | None, **slots: str):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RAUC_", "EITRI_"))}
    env["EITRI_SYSFS"] = str(sysfs)
    if root_disk is not None:
        env["EITRI_ROOT_DISK"] = root_disk
    env["RAUC_TARGET_SLOTS"] = " ".join(slots)
    for n, dev in slots.items():
        env[f"RAUC_SLOT_DEVICE_{n}"] = dev
    return run([HOOK], env=env)


def test_slot_next_to_the_running_root_is_allowed(sysfs):
    assert hook(sysfs, "/dev/mmcblk0", **{"2": "/dev/mmcblk0p6"}).returncode == 0
    assert hook(sysfs, "/dev/mmcblk1", **{"1": "/dev/mmcblk1p2"}).returncode == 0


@pytest.mark.parametrize("dev", ["/dev/mmcblk1p2", "/dev/mmcblk1p3", "/dev/mmcblk1", "/dev/nonexistent9", ""])
def test_a_slot_on_another_disk_is_refused_when_running_from_the_card(sysfs, dev):
    """The image's own system.conf (mmcblk1p2/p3) seen from a microSD boot: the stock eMMC must stay untouched."""
    result = hook(sysfs, "/dev/mmcblk0", **{"1": dev})
    assert result.returncode != 0 and "rauc-pre-install" in result.stderr


def test_every_target_slot_is_checked(sysfs):
    result = hook(sysfs, "/dev/mmcblk0", **{"1": "/dev/mmcblk0p6", "2": "/dev/mmcblk1p2"})
    assert result.returncode != 0 and "mmcblk1p2" in result.stderr


def test_unknown_root_disk_refuses(sysfs, tmp_path):
    env_sysfs = tmp_path / "empty-sys"
    env_sysfs.mkdir()
    result = hook(env_sysfs, None, **{"1": "/dev/mmcblk0p6"})
    assert result.returncode != 0


def test_system_conf_installs_the_hook():
    conf = (OVERLAY / "etc/rauc/system.conf").read_text()
    assert "pre-install=/usr/libexec/eitri/rauc-pre-install" in conf
