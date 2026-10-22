# SPDX-License-Identifier: Apache-2.0
"""The rootfs written to slot A must be readable by the printer's stock U-Boot 2021.07 (boot.scr ext4loads the kernel
and device tree from it).  The OS image's mke2fs options are the single place that decides the features; here they are
applied to a small tree and the result is read back through a real U-Boot 2021.07 (sandbox build), byte for byte."""
import os
import re
import shutil
import subprocess
import zlib

import pytest

import ubootsim

HERE = os.path.dirname(os.path.abspath(__file__))
DEFCONFIG = os.path.join(HERE, "..", "..", "os", "configs", "eitri_plus4_defconfig")
# Features U-Boot 2021.07's ext4 driver cannot read (inline_data), or that it predates and that the stock boot path must
# never meet (the list mk-dev-bundle.sh refuses as well).
DENY = ("64bit", "metadata_csum_seed", "orphan_file", "huge_file", "inline_data", "encrypt", "casefold", "verity")


def mkfs_options():
    text = open(DEFCONFIG).read()
    m = re.search(r'^BR2_TARGET_ROOTFS_EXT2_MKFS_OPTIONS="([^"]*)"', text, re.M)
    assert m, "BR2_TARGET_ROOTFS_EXT2_MKFS_OPTIONS missing from the defconfig"
    return m.group(1).split()


def make_fs(tmp_path, opts, name="fs.img"):
    tree = tmp_path / "tree"
    if not tree.exists():
        (tree / "boot" / "dtb" / "rockchip").mkdir(parents=True)
        (tree / "boot" / "Image").write_bytes(os.urandom(3 * 1024 * 1024 + 17))
        (tree / "boot" / "rk3328-eitri-plus4.dtb").write_bytes(os.urandom(38307))
        os.symlink("../../rk3328-eitri-plus4.dtb", str(tree / "boot" / "dtb" / "rockchip" / "rk3328-roc-cc.dtb"))
    img = str(tmp_path / name)
    subprocess.run(["mke2fs", "-q", "-F", "-t", "ext4", "-L", "rootfs"] + opts + ["-d", str(tree), img, "64M"], check=True)
    return img, tree


def features(img):
    out = subprocess.run(["dumpe2fs", "-h", img], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout
    return re.search(r"^Filesystem features:\s*(.*)$", out, re.M).group(1).split()


def uboot_crcs(img):
    out = subprocess.run([ubootsim.SANDBOX, "-c", "host bind 0 %s; ext4load host 0 0x2080000 /boot/Image && crc32 0x2080000 ${filesize}; "
                          "ext4load host 0 0x1f00000 /boot/dtb/rockchip/rk3328-roc-cc.dtb && crc32 0x1f00000 ${filesize}" % img],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60).stdout.decode("utf-8", "replace")
    return re.findall(r"crc32 for \S+ \.\.\. \S+ ==> ([0-9a-f]{8})", out.replace("\r", ""))


def test_the_image_options_leave_out_every_feature_the_stock_uboot_must_not_meet(tmp_path):
    if not shutil.which("mke2fs"):
        pytest.skip("e2fsprogs not installed")
    opts = mkfs_options()
    for f in ("64bit", "metadata_csum_seed", "orphan_file", "huge_file", "inline_data"):
        assert "^" + f in ",".join(opts), "the defconfig's mke2fs options must switch off %s" % f
    img, _ = make_fs(tmp_path, opts)
    assert not [f for f in features(img) if f in DENY], features(img)


@pytest.mark.skipif(not os.path.isfile(ubootsim.SANDBOX), reason="no U-Boot 2021.07 sandbox build (EITRI_UBOOT_SANDBOX)")
def test_uboot_2021_07_reads_kernel_and_dtb_from_a_rootfs_made_with_the_image_options(tmp_path):
    img, tree = make_fs(tmp_path, mkfs_options())
    want = ["%08x" % zlib.crc32((tree / "boot" / "Image").read_bytes()), "%08x" % zlib.crc32((tree / "boot" / "rk3328-eitri-plus4.dtb").read_bytes())]
    assert uboot_crcs(img) == want                                 # the device tree is reached through its symlink, too


@pytest.mark.skipif(not os.path.isfile(ubootsim.SANDBOX), reason="no U-Boot 2021.07 sandbox build (EITRI_UBOOT_SANDBOX)")
def test_the_sandbox_does_notice_an_unreadable_feature(tmp_path):
    # control: with inline_data (small files stored in the inode) U-Boot 2021.07 cannot load the device tree link
    img, _ = make_fs(tmp_path, ["-O", "inline_data"], "inline.img")
    assert len(uboot_crcs(img)) < 2
