# SPDX-License-Identifier: Apache-2.0
"""boot.cmd executed by a real U-Boot hush interpreter (see ubootsim.py)."""
import os

import pytest

import ubootsim
from ubootsim import BootSim

pytestmark = pytest.mark.skipif(not ubootsim.available(), reason="U-Boot sandbox binary not built (EITRI_UBOOT_SANDBOX)")

BASE = {
    "eitri_mode": "eitri", "eitri_slot": "A", "eitri_tries": "3", "eitri_state_A": "unknown", "eitri_state_B": "unknown",
    "eitri_part_A": "2", "eitri_part_B": "3", "eitri_partuuid_A": "aaaa-02", "eitri_partuuid_B": "aaaa-03",
    "eitri_cmdline": "net.ifnames=0 panic=10",
    "eitri_s2_kernel": "/Image", "eitri_s2_fdt": "/dtb/rockchip/rk3328-roc-cc.dtb",
    "eitri_s2_initrd": "/eitri/stage2/initramfs.cpio.gz", "eitri_s2_args": "console=tty1 rdinit=/init eitri.op=convert",
}


def env(**over):
    d = dict(BASE)
    d.update(over)
    return d


def test_first_boot_spends_an_attempt_before_starting_the_kernel(tmp_path):
    out, after = BootSim(tmp_path, env()).boot()
    assert "booting slot A (partition 2" in out
    assert "BOOTI bootargs=[root=PARTUUID=aaaa-02 ro rootwait rootfstype=ext4 eitri.slot=A net.ifnames=0 panic=10]" in out
    assert after["eitri_tries"] == "2" and after["eitri_slot"] == "A" and after["eitri_mode"] == "eitri"
    assert "booting the stock system" in out  # the shimmed booti returns, as a failed boot would


def test_unset_fdtfile_falls_back_to_this_boards_device_tree(tmp_path):
    out, after = BootSim(tmp_path, env()).boot(fdtfile="")
    assert "BOOTI bootargs=[root=PARTUUID=aaaa-02" in out and after["eitri_tries"] == "2"


def test_attempts_run_out_then_slot_is_marked_bad_and_stock_boots(tmp_path):
    sim = BootSim(tmp_path, env(eitri_tries="1"))
    out, after = sim.boot()
    assert after["eitri_tries"] == "0" and "BOOTI" in out
    out, after = sim.boot()                   # the kernel never confirmed: counter exhausted
    assert "BOOTI" not in out and "SYSBOOT: host 0:1 any 0x600000 /extlinux/extlinux.conf.stock" in out
    assert after["eitri_state_A"] == "bad" and after["eitri_tries"] == "0"
    out, after = sim.boot()                   # stays on stock until userspace or the operator intervenes
    assert "BOOTI" not in out and "SYSBOOT" in out


def test_falls_back_to_the_other_slot_when_it_is_known_good(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_tries="0", eitri_slot="B", eitri_state_A="good")).boot()
    assert "slot B failed, falling back to slot A" in out
    assert "root=PARTUUID=aaaa-02" in out and after["eitri_slot"] == "A" and after["eitri_tries"] == "2"
    assert after["eitri_state_B"] == "bad"


def test_slot_b_boots_its_own_kernel_partition(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_slot="B", eitri_tries="3")).boot()
    assert "partition 3" in out and "root=PARTUUID=aaaa-03" in out and "eitri.slot=B" in out


def test_missing_kernel_in_slot_falls_through_to_stock(tmp_path):
    out, after = BootSim(tmp_path, env(), slot_a=False).boot()
    assert "slot A could not be loaded" in out and "SYSBOOT" in out and "BOOTI" not in out
    assert after["eitri_tries"] == "2"       # the attempt was still spent: no infinite retry


def test_stage2_start_spends_a_counter_but_keeps_the_mode_until_stage2_ends_it(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_mode="stage2", eitri_s2_tries="2"), booti_returns=False).boot()
    assert "starting the installer environment (1 start(s) left after this one)" in out
    assert "BOOTI bootargs=[console=tty1 rdinit=/init eitri.op=convert]" in out
    assert "0x2080000 0x6000000:3e8 0x1f00000" in out        # initramfs size passed (1000 bytes = 0x3e8)
    assert after["eitri_mode"] == "stage2" and after["eitri_s2_tries"] == "1"


def test_interrupted_installer_is_started_again_until_the_counter_is_spent(tmp_path):
    """A power cut mid-shrink must not leave a half-converted disk booting stock with nothing to resume it."""
    state = env(eitri_mode="stage2", eitri_s2_tries="2")
    for n, (left, booti) in enumerate([("1", True), ("0", True), ("0", False)]):
        sub = tmp_path / ("boot%d" % n)
        sub.mkdir()
        out, after = BootSim(sub, state, booti_returns=False).boot()
        assert ("BOOTI" in out) is booti, out
        state = {**BASE, **after}
        assert state["eitri_s2_tries"] == left
    assert state["eitri_mode"] == "stock" and "out of starts" in out and "SYSBOOT" in out


def test_an_installer_kernel_that_will_not_start_is_not_retried(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_mode="stage2", eitri_s2_tries="3")).boot()      # booti returns = failed
    assert "could not be started" in out and "SYSBOOT" in out and after["eitri_mode"] == "stock"


def test_stage2_without_a_start_counter_boots_stock(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_mode="stage2")).boot()      # a boot.env from before the counter existed
    assert "BOOTI" not in out and "SYSBOOT" in out and after["eitri_mode"] == "stock"


def test_stage2_with_missing_initramfs_does_not_hang_or_retry(tmp_path):
    sim = BootSim(tmp_path, env(eitri_mode="stage2", eitri_s2_tries="3", eitri_s2_initrd="/nope.gz"))
    out, after = sim.boot()
    assert "installer environment could not be started" in out and "SYSBOOT" in out and "BOOTI" not in out
    assert after["eitri_mode"] == "stock"


def test_a_finished_stage2_leaves_eitri_mode_with_no_stray_counter(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_mode="eitri", eitri_s2_tries="0")).boot()
    assert "booting slot A" in out and after["eitri_mode"] == "eitri"


@pytest.mark.parametrize("mode", ["stock", "garbage", ""])
def test_stock_and_unknown_modes_boot_stock(tmp_path, mode):
    out, after = BootSim(tmp_path, env(eitri_mode=mode)).boot()
    assert "SYSBOOT" in out and "BOOTI" not in out


def test_no_boot_env_boots_stock(tmp_path):
    out, after = BootSim(tmp_path, None).boot()
    assert "SYSBOOT" in out and "BOOTI" not in out


def test_real_sysboot_reads_the_stock_extlinux_copy(tmp_path):
    out, _ = BootSim(tmp_path, env(eitri_mode="stock"), shim_sysboot=False).boot()
    assert "extlinux.conf.stock" in out


def test_script_text_has_no_unshimmed_surprises():
    text = open(os.path.join(ubootsim.BOOT_DIR, "boot.cmd")).read()
    # the only commands that leave U-Boot are these two, both last-resort paths
    assert text.count("booti ") == 2 and text.count("\nsysboot ") == 1
