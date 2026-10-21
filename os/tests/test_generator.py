# SPDX-License-Identifier: Apache-2.0
"""The systemd generator that mounts /data and /boot/firmware on the booted disk."""
from __future__ import annotations

import os

import pytest
from conftest import OVERLAY, need, run

GENERATOR = OVERLAY / "usr/lib/systemd/system-generators/eitri-generator"


def generate(tmp_path, cmdline_text: str, cmdline):
    out = tmp_path / "normal"
    out.mkdir()
    env = {**os.environ, "EITRI_CMDLINE": str(cmdline(cmdline_text))}
    result = run([GENERATOR, out], env=env)
    return out, result


def test_emmc_boot_mounts_partitions_of_mmcblk1(tmp_path, cmdline):
    out, result = generate(tmp_path, "root=/dev/mmcblk1p2 ro eitri.disk=/dev/mmcblk1 quiet", cmdline)
    assert result.returncode == 0, result.stderr
    data = (out / "data.mount").read_text()
    assert "What=/dev/mmcblk1p4" in data and "Where=/data" in data and "Type=ext4" in data
    assert "systemd-fsck@dev-mmcblk1p4.service" in data
    # A journal error on the eMMC must stop writes, not be carried on past.
    assert "Options=noatime,errors=remount-ro" in data
    boot = (out / "boot-firmware.mount").read_text()
    assert "What=/dev/mmcblk1p1" in boot and "Where=/boot/firmware" in boot and "ro," in boot


def test_sd_boot_follows_the_booted_disk(tmp_path, cmdline):
    out, _ = generate(tmp_path, "root=/dev/mmcblk0p2 ro eitri.disk=/dev/mmcblk0", cmdline)
    assert "What=/dev/mmcblk0p4" in (out / "data.mount").read_text()


def test_data_is_wanted_never_required(tmp_path, cmdline):
    out, _ = generate(tmp_path, "eitri.disk=/dev/mmcblk1", cmdline)
    wants = out / "local-fs.target.wants"
    assert (wants / "data.mount").is_symlink() and (wants / "boot-firmware.mount").is_symlink()
    assert not (out / "local-fs.target.requires").exists()


def test_fsck_waits_for_the_partition_to_grow(tmp_path, cmdline):
    out, _ = generate(tmp_path, "eitri.disk=/dev/mmcblk1", cmdline)
    dropin = out / "systemd-fsck@dev-mmcblk1p4.service.d" / "10-eitri-grow.conf"
    assert "After=eitri-grow-data.service" in dropin.read_text()


@pytest.mark.parametrize("text", ["root=/dev/mmcblk1p2 ro", "eitri.disk=/dev/nvme0n1", "eitri.disk=/dev/mmcblk1;rm"])
def test_nothing_is_generated_without_a_valid_disk(tmp_path, cmdline, text):
    out, result = generate(tmp_path, text, cmdline)
    assert result.returncode == 0
    assert not (out / "data.mount").exists()


def test_generated_units_pass_systemd_analyze(tmp_path, cmdline):
    need("systemd-analyze")
    out, _ = generate(tmp_path, "eitri.disk=/dev/mmcblk1", cmdline)
    result = run(["systemd-analyze", "verify", "--no-pager", out / "data.mount", out / "boot-firmware.mount"])
    # Unknown units on the build host (the fsck template, the grow service) are
    # expected; syntax errors are not.
    assert "Unknown key" not in result.stderr and "Invalid" not in result.stderr, result.stderr


def test_explicit_devices_override_the_derived_ones(tmp_path, cmdline):
    # How another boot manager (the in-place installer) describes its own partition map.
    text = ("root=PARTUUID=6a783df4-05 ro eitri.data=PARTUUID=6a783df4-07 "
            "eitri.bootfs=PARTUUID=6a783df4-01 eitri.bootmount=/boot")
    out, result = generate(tmp_path, text, cmdline)
    assert result.returncode == 0, result.stderr
    data = (out / "data.mount").read_text()
    assert "What=/dev/disk/by-partuuid/6a783df4-07" in data
    assert "systemd-fsck@dev-disk-by\\x2dpartuuid-6a783df4\\x2d07.service" in data
    boot = (out / "boot.mount").read_text()
    assert "What=/dev/disk/by-partuuid/6a783df4-01" in boot and "Where=/boot" in boot


def test_explicit_device_wins_over_the_disk(tmp_path, cmdline):
    out, _ = generate(tmp_path, "eitri.disk=/dev/mmcblk1 eitri.data=/dev/mmcblk1p7", cmdline)
    assert "What=/dev/mmcblk1p7" in (out / "data.mount").read_text()
    assert "What=/dev/mmcblk1p1" in (out / "boot-firmware.mount").read_text()


@pytest.mark.parametrize(
    "spec", ["/etc/passwd", "/dev/mmcblk1p4;reboot", "/dev/../etc/shadow", "relative", "LABEL=a$b"])
def test_hostile_device_specs_are_refused(tmp_path, cmdline, spec):
    out, result = generate(tmp_path, f"eitri.data={spec} eitri.bootfs={spec}", cmdline)
    assert result.returncode == 0
    assert not (out / "data.mount").exists() and not (out / "boot-firmware.mount").exists()


def test_hostile_mount_paths_are_refused(tmp_path, cmdline):
    out, _ = generate(tmp_path, "eitri.disk=/dev/mmcblk1 eitri.bootmount=/boot/../etc", cmdline)
    assert not list(out.glob("*.mount")) or (out / "data.mount").exists()
    assert not (out / "boot-firmware.mount").exists() and not (out / "etc.mount").exists()
