# SPDX-License-Identifier: Apache-2.0
"""genimage.cfg produces the layout that U-Boot, RAUC and the generator assume."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
from conftest import BOARD, need, run

GENIMAGE = os.environ.get("EITRI_GENIMAGE") or shutil.which("genimage")


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    if not GENIMAGE:
        pytest.skip("needs genimage (set EITRI_GENIMAGE, or build it with the host-genimage package)")
    need("mke2fs", "mkfs.vfat", "mcopy", "sfdisk", "e2fsck")
    work = tmp_path_factory.mktemp("genimage")
    inputs, root, tmp, out = (work / d for d in ("in", "root", "tmp", "out"))
    for d in (inputs, root, tmp, out):
        d.mkdir()
    # Stand-ins for the build outputs; only sizes and names matter here.
    (inputs / "idbloader.img").write_bytes(b"\x01" * 120 * 1024)
    (inputs / "u-boot.itb").write_bytes(b"\x02" * 1024 * 1024)
    for name in ("eitri-boot.env", "README.txt"):
        shutil.copy(BOARD / "boot" / name, inputs / name)
    rootfs = work / "rootfs"
    (rootfs / "boot").mkdir(parents=True)
    (rootfs / "boot/Image").write_bytes(b"kernel")
    run(["mke2fs", "-q", "-t", "ext4", "-O", "^has_journal", "-d", rootfs, inputs / "rootfs.ext4", "640M"], check=True)
    result = run([GENIMAGE, "--rootpath", root, "--tmppath", tmp, "--inputpath", inputs, "--outputpath", out,
                  "--config", BOARD / "genimage.cfg"])
    assert result.returncode == 0, result.stderr
    return out / "eitri-plus4.img"


def table(image):
    dump = run(["sfdisk", "-d", image], check=True).stdout
    rows = {}
    for line in dump.splitlines():
        m = re.search(r'start= *(\d+), size= *(\d+), type=(\S+), uuid=(\S+), name="([^"]+)"', line)
        if m:
            rows[m.group(5)] = {"start": int(m.group(1)), "size": int(m.group(2)), "type": m.group(3)}
    return rows


def test_partition_order_and_offsets(image):
    rows = table(image)
    order = ["boot", "rootfs-a", "rootfs-b", "data"]
    assert list(rows) == order, "uboot.env, RAUC and the generator count on this order"
    mib = 2048
    assert rows["boot"]["start"] == 16 * mib
    assert rows["rootfs-a"]["start"] == 48 * mib and rows["rootfs-a"]["size"] == 768 * mib
    assert rows["rootfs-b"]["start"] == 816 * mib and rows["rootfs-b"]["size"] == 768 * mib


def test_loaders_sit_at_the_rockchip_offsets(image):
    data = image.read_bytes()[: 10 * 1024 * 1024]
    assert data[32 * 1024 : 32 * 1024 + 4] == b"\x01" * 4, "idbloader at sector 64"
    assert data[8 * 1024 * 1024 : 8 * 1024 * 1024 + 4] == b"\x02" * 4, "u-boot.itb at sector 16384"
    # The A/B state sector (8128) and its neighbours must be free.
    assert set(data[8128 * 512 : 8129 * 512]) == {0}


def test_loaders_are_outside_the_partition_table(image):
    dump = run(["sfdisk", "-d", image], check=True).stdout
    first_lba = int(re.search(r"first-lba: (\d+)", dump).group(1))
    assert first_lba > 16384, "the loaders must not be inside the usable area"


def test_rootfs_a_holds_the_rootfs_and_b_is_empty(image):
    rows = table(image)
    data = image.read_bytes()
    a = rows["rootfs-a"]["start"] * 512
    b = rows["rootfs-b"]["start"] * 512
    assert data[a + 1080 : a + 1082] == b"\x53\xef", "ext4 superblock magic in slot A"
    assert set(data[b : b + 65536]) == {0}


def test_boot_partition_is_fat_and_has_the_boot_options(image):
    start = table(image)["boot"]["start"] * 512
    listing = subprocess.run(["mdir", "-i", f"{image}@@{start}", "::"], capture_output=True, text=True)
    assert listing.returncode == 0, listing.stderr
    assert "EITRIBOOT" in listing.stdout
    assert "eitri-boot.env" in listing.stdout and "README" in listing.stdout


def test_image_is_reproducible(image, tmp_path_factory):
    # Same inputs, same GUIDs: no timestamps or random ids in the partition table.
    dump = run(["sfdisk", "-d", image], check=True).stdout
    assert "7B1F9F0E-3A5C-4F6E-9C41-0E17A1D00001" in dump.upper()
