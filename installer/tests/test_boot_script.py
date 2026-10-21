# SPDX-License-Identifier: Apache-2.0
"""boot.cmd executed by a real U-Boot hush interpreter (see ubootsim.py)."""
import os
import re
import shutil

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


# --- what the two files may set (env import whitelist) --------------------------------------------------------------------

def test_a_hand_edited_boot_env_cannot_redirect_writes_or_run_commands(tmp_path):
    """boot.env is imported with ${eitri_keys} only: eitri_dev (where fatwrite writes), eitri_save (what `run` executes)
    and eitri_hide_emmc in it are ignored."""
    sim = BootSim(tmp_path, env(), extra_boot_env="eitri_dev=host 7:1\neitri_save=echo INJECTED-SAVE\neitri_hide_emmc=1\n"
                                                  "eitri_keys=eitri_mode\nbootcmd=echo INJECTED-BOOTCMD\n",
                  dtb=ubootsim.make_dtb(tmp_path), probe_fdt=True)
    out, after = sim.boot()
    assert "INJECTED" not in out
    assert after["eitri_tries"] == "2" and after["eitri_state_A"] == "unknown"   # written to the real boot partition, all keys
    assert "BOOTI" in out and "FDT-PROBE emmc=[okay]" in out and "eMMC controller" not in out


def test_a_hand_edited_layout_env_cannot_redirect_writes_either(tmp_path):
    out, after = BootSim(tmp_path, env(), extra_layout_env="eitri_dev=host 7:1\neitri_save=echo INJECTED-SAVE\n").boot()
    assert "INJECTED" not in out and after["eitri_tries"] == "2" and "BOOTI" in out


def _imports(text):
    return [line.split()[5:] for line in text.splitlines() if line.strip().startswith("env import")]


def test_boot_scr_imports_only_the_documented_variables():
    from eitri_update.bootenv import BOOT_SCR_STATE_KEYS, LAYOUT_KEYS
    text = open(os.path.join(ubootsim.BOOT_DIR, "boot.cmd")).read()
    imports = _imports(text)
    assert len(imports) == 3 and all(imports), "every env import names its variables"
    assert all(len(line.split()) <= 16 for line in text.splitlines() if line.strip().startswith("env "))  # CONFIG_SYS_MAXARGS
    assert set(imports[0] + imports[1]) == set(LAYOUT_KEYS) and len(imports[0] + imports[1]) == len(LAYOUT_KEYS)
    assert imports[2] == ["${eitri_keys}"]
    keys = re.search(r'^setenv eitri_keys "([^"]*)"$', text, re.M).group(1).split()
    assert tuple(keys) == BOOT_SCR_STATE_KEYS


def _written_keys(path, target):
    """The variables a shell script writes into eitri/<target> (kv_write / kv_set / printf ... >layout.env)."""
    text = open(path).read().replace("\\\n", " ")
    keys = set()
    for line in text.splitlines():
        if target not in line or line.lstrip().startswith("#"):
            continue
        if re.search(r"\bkv_set\b", line):
            m = re.search(r"kv_set\s+\S*%s\S*\s+(\w+)" % re.escape(target), line)
            if m:
                keys.add(m.group(1))
        if re.search(r"\bkv_write\b|printf ", line):
            keys |= set(re.findall(r'"(eitri_\w+)=', line))
    return keys


def test_every_writer_of_the_two_files_stays_within_the_imported_lists():
    from eitri_update.bootenv import BOOT_SCR_STATE_KEYS, LAYOUT_KEYS, STATE_KEYS
    repo = os.path.abspath(os.path.join(ubootsim.BOOT_DIR, "..", ".."))
    writers = [os.path.join(repo, p) for p in ("installer/install.sh", "installer/stage2/stage2.sh", "installer/revert.sh",
                                               "os/bench/mk-sd-bench.sh")]
    layout, state = set(), set()
    for path in writers:
        layout |= _written_keys(path, "layout.env")
        state |= _written_keys(path, "boot.env")
    assert layout and layout <= set(LAYOUT_KEYS), layout - set(LAYOUT_KEYS)
    assert state and state <= set(BOOT_SCR_STATE_KEYS), state - set(BOOT_SCR_STATE_KEYS)
    assert set(STATE_KEYS) <= set(BOOT_SCR_STATE_KEYS)
    assert "eitri_hide_emmc" in _written_keys(writers[3], "layout.env")             # the bench card, and only the bench card
    assert not any("eitri_hide_emmc" in _written_keys(p, "layout.env") for p in writers[:3])


# --- the bench card's eMMC hiding (eitri_hide_emmc=1 in layout.env) -------------------------------------------------------

needs_dtc = pytest.mark.skipif(not shutil.which("dtc"), reason="dtc not installed")


@needs_dtc
def test_in_place_install_hands_linux_the_device_tree_unchanged(tmp_path):
    """On a converted eMMC the eMMC IS the boot disk: its controller must stay enabled."""
    out, after = BootSim(tmp_path, env(), dtb=ubootsim.make_dtb(tmp_path), probe_fdt=True).boot()
    assert "BOOTI" in out and "FDT-PROBE emmc=[okay] sd=[okay] model=[eitri-test-board]" in out, out
    assert "eMMC controller" not in out and "fdt" not in out.lower().replace("fdt-probe", "")


@needs_dtc
def test_bench_card_disables_the_emmc_controller_before_booti(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_hide_emmc="1"), dtb=ubootsim.make_dtb(tmp_path), probe_fdt=True).boot()
    assert "Eitri: eMMC controller /mmc@ff520000 disabled for Linux" in out, out
    assert "FDT-PROBE emmc=[disabled] sd=[okay] model=[eitri-test-board]" in out, out
    assert out.index("disabled for Linux") < out.index("BOOTI") and after["eitri_tries"] == "2"


@needs_dtc
def test_bench_card_does_not_boot_a_device_tree_it_cannot_hide_the_emmc_in(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_hide_emmc="1"), dtb=ubootsim.make_dtb(tmp_path, emmc=False), probe_fdt=True).boot()
    assert "cannot hide the eMMC from Linux, not booting Eitri" in out and "BOOTI" not in out and "SYSBOOT" in out


def test_bench_card_does_not_boot_a_blob_that_is_no_device_tree(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_hide_emmc="1")).boot()           # the slot's "dtb" is text
    assert "cannot hide the eMMC from Linux" in out and "BOOTI" not in out and "SYSBOOT" in out


def test_bench_card_never_starts_the_installer_environment(tmp_path):
    out, after = BootSim(tmp_path, env(eitri_mode="stage2", eitri_s2_tries="3", eitri_hide_emmc="1")).boot()
    assert "not started from a card that hides the eMMC" in out and "BOOTI" not in out and "SYSBOOT" in out
    assert after["eitri_s2_tries"] == "3"                 # nothing spent, nothing written


def test_hide_flag_in_boot_env_does_not_count(tmp_path):
    out, _ = BootSim(tmp_path, env(eitri_mode="stage2", eitri_s2_tries="3"), extra_boot_env="eitri_hide_emmc=1\n",
                     booti_returns=False).boot()
    assert "BOOTI bootargs=[console=tty1 rdinit=/init eitri.op=convert]" in out
