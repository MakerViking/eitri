# SPDX-License-Identifier: Apache-2.0
"""stage2.sh: the RAUC slot override for the in-place layout (real ext4 image + debugfs)."""
import os
import shutil
import subprocess

import pytest

from printerfix import INSTALLER

pytestmark = pytest.mark.skipif(not (shutil.which("mke2fs") and (shutil.which("debugfs") or os.path.exists("/sbin/debugfs"))),
                                reason="e2fsprogs not installed")

FLASH_ONCE_CONF = """[system]
compatible=eitri-plus4
bootloader=custom

[handlers]
bootloader-custom-backend=/usr/libexec/eitri/rauc-bootloader

[slot.rootfs.0]
device=/dev/mmcblk1p2
type=ext4
bootname=A

[slot.rootfs.1]
type=ext4
bootname=B
device=/dev/mmcblk1p3
"""


def run_override(tmp, conf):
    tmp = str(tmp)
    tree = os.path.join(tmp, "tree", "etc", "rauc")
    os.makedirs(tree)
    if conf is not None:
        with open(os.path.join(tree, "system.conf"), "w") as fh:
            fh.write(conf)
    img = os.path.join(tmp, "rootfs.img")
    env = dict(os.environ, PATH=os.environ["PATH"] + ":/sbin:/usr/sbin")
    subprocess.run(["mke2fs", "-q", "-t", "ext4", "-d", os.path.join(tmp, "tree"), img, "4m"], check=True, env=env)
    root = os.path.join(tmp, "s2root")
    data = os.path.join(tmp, "data")
    os.makedirs(os.path.join(root, "run", "eitri-stage2"))
    os.makedirs(data)
    script = ('source "%s/stage2/stage2.sh"; DISK=/dev/mmcblk1; PART_PREFIX=p; ROOT_A_DEV="%s"; M_DATA="%s"; WORK="%s"; s2_rauc_override'
              % (INSTALLER, img, data, os.path.join(root, "run", "eitri-stage2")))
    r = subprocess.run(["bash", "-c", script], env=dict(env, EITRI_STAGE2_NO_MAIN="1", S2_LIB=os.path.join(INSTALLER, "lib"), S2_ROOT=root),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
    return r, os.path.join(data, "eitri", "state", "etc")


def test_slots_follow_the_inplace_layout_and_handler_is_ours(tmp_path):
    r, etc = run_override(tmp_path, FLASH_ONCE_CONF)
    assert r.returncode == 0, r.stdout
    out = open(os.path.join(etc, "upper", "rauc", "system.conf")).read()
    assert "device=/dev/mmcblk1p5\ntype=ext4\nbootname=A" in out
    assert "type=ext4\nbootname=B\ndevice=/dev/mmcblk1p6" in out          # order of keys in the section does not matter
    assert "bootloader-custom-backend=/usr/libexec/eitri/eitri-bootctl" in out
    assert "p2" not in out and "p3" not in out and "rauc-bootloader" not in out
    assert "compatible=eitri-plus4" in out and os.path.isdir(os.path.join(etc, "work"))      # everything else untouched


def test_image_without_a_rauc_config_leaves_the_overlay_alone(tmp_path):
    r, etc = run_override(tmp_path, None)
    assert r.returncode == 0, r.stdout
    assert not os.path.exists(os.path.join(etc, "upper", "rauc", "system.conf"))
