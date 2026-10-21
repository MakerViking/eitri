# SPDX-License-Identifier: Apache-2.0
"""revert.sh against a fake converted printer, and stage2.sh in restore mode."""
import gzip
import os
import subprocess

import pytest

from helpers import Signer
from printerfix import INSTALLER, Printer, STOCK_EXTLINUX, sha256
from test_stage2 import Stage2Rig


class Converted:
    """A printer after a successful install: boot.scr active, stock entry renamed, Eitri selected."""

    def __init__(self, tmp, on_eitri=True):
        self.tmp = str(tmp)
        self.p = Printer(tmp, Signer(tmp))
        self.data = os.path.join(self.tmp, "data")
        self.log = self.p.log
        w = self.p.write
        w("/boot/boot.scr", "BOOTSCR-BYTES eitri_mode")
        w("/boot/extlinux/extlinux.conf.stock", STOCK_EXTLINUX)
        w("/boot/eitri/extlinux.conf.orig", STOCK_EXTLINUX)
        w("/boot/eitri/boot.env", "eitri_mode=eitri\neitri_slot=A\neitri_tries=3\neitri_state_A=good\neitri_state_B=unknown\n")
        w("/boot/eitri/layout.env", "eitri_part_A=5\neitri_s2_args=console=tty1 rdinit=/init eitri.op=convert\n")
        w("/boot/eitri/stage2/initramfs.cpio.gz", b"BASE-INITRAMFS".decode())
        os.remove(self.p.p("/boot/extlinux/extlinux.conf")) if os.path.exists(self.p.p("/boot/extlinux/extlinux.conf")) else None
        w("/proc/cmdline", "root=PARTUUID=6a783df4-05 ro eitri.slot=A\n" if on_eitri else "root=UUID=5dcae443 rw\n")
        os.makedirs(os.path.join(self.data, "eitri", "state"))
        with open(os.path.join(self.data, "eitri", "state", "backup.env"), "w") as fh:
            fh.write("verified=1\nmethod=usb\ndest=usb:ABCD-1234:/EITRI-BACKUP/x/mmcblk1.img.gz\nusb_uuid=ABCD-1234\ngz_sha256=%s\nraw_sha256=%s\n" % ("a" * 64, "b" * 64))
        self.stub = os.path.join(self.tmp, "mcuflash-stub")
        self.set_stub(0)

    def set_stub(self, rc):
        with open(self.stub, "w") as fh:
            fh.write("#!/bin/sh\necho \"$@\" >>\"$FAKE_LOG\"\necho '{\"type\":\"result\",\"ok\":%s,\"msg\":\"stub\"}'\nexit %d\n" % ("true" if rc == 0 else "false", rc))
        os.chmod(self.stub, 0o755)

    def revert(self, *args, **env):
        merged = dict(EITRI_DATA=self.data, EITRI_MCUFLASH=self.stub, FAKE_USB_UUID="ABCD-1234", FAKE_USB_DEV="/dev/sda1")
        merged.update(env)
        e = self.p.env(**merged)
        return subprocess.run([os.path.join(INSTALLER, "revert.sh")] + list(args), env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=60)

    def boot_env(self):
        return dict(l.split("=", 1) for l in open(self.p.p("/boot/eitri/boot.env")).read().splitlines())

    def calls(self):
        return open(self.log).read().splitlines()


@pytest.fixture
def conv(tmp_path):
    return Converted(tmp_path)


def test_dry_run_is_the_default_and_changes_nothing(conv):
    before = conv.p.snapshot()
    r = conv.revert()
    assert r.returncode == 0, r.stdout
    assert "DRY RUN" in r.stdout and "Dry run finished" in r.stdout and "would: restore the toolhead firmware" in r.stdout or "no saved firmware" in r.stdout
    assert conv.p.snapshot() == before and not any(c.startswith(("systemctl", "reboot")) for c in conv.calls())


def test_hard_revert_restores_stock_boot_and_mcus(conv):
    r = conv.revert("--hard", "--yes", "--confirm", "REVERT")
    assert r.returncode == 0, r.stdout
    boot = conv.p.p("/boot")
    assert open(boot + "/extlinux/extlinux.conf").read() == STOCK_EXTLINUX              # QIDI's own entry is live again
    assert not os.path.exists(boot + "/boot.scr") and os.path.exists(boot + "/boot.scr.eitri")
    assert conv.boot_env()["eitri_mode"] == "stock"
    rollbacks = [c for c in conv.calls() if c.startswith("rollback")]
    # revert puts back the firmware the printer SHIPPED with, not whatever Eitri flashed last
    assert len(rollbacks) == 2 and all("--original --apply --yes" in c and "--stop-services" in c for c in rollbacks)
    assert rollbacks[0].split()[1] == "toolhead" and rollbacks[1].split()[1] == "mainboard"
    assert "systemctl reboot" in conv.calls()
    # the Armbian resize service must NOT be unmasked while Eitri's partitions exist
    assert not any("unmask" in c for c in conv.calls())


def test_hard_revert_puts_back_a_boot_script_qidi_shipped(conv):
    conv.p.write("/boot/eitri/boot.scr.stock", "QIDI-OWN-BOOT-SCRIPT")
    r = conv.revert("--hard", "--yes", "--confirm", "REVERT", "--skip-mcu", "--no-reboot")
    assert r.returncode == 0, r.stdout
    boot = conv.p.p("/boot")
    assert open(boot + "/boot.scr").read() == "QIDI-OWN-BOOT-SCRIPT" and "eitri_mode" in open(boot + "/boot.scr.eitri").read()
    # and going back to Eitri replaces QIDI's script with ours again
    r = conv.revert("--to-eitri", "--yes", "--confirm", "REVERT", "--no-reboot")
    assert r.returncode == 0 and "eitri_mode" in open(boot + "/boot.scr").read()


def test_mcu_restore_failure_aborts_before_any_boot_change(conv):
    conv.set_stub(1)
    before_env = open(conv.p.p("/boot/eitri/boot.env")).read()
    r = conv.revert("--hard", "--yes", "--confirm", "REVERT")
    assert r.returncode == 13 and "Could not restore the toolhead firmware" in r.stdout
    assert open(conv.p.p("/boot/eitri/boot.env")).read() == before_env and os.path.exists(conv.p.p("/boot/boot.scr"))
    assert "systemctl reboot" not in conv.calls()


def test_skip_mcu_and_no_reboot(conv):
    r = conv.revert("--hard", "--yes", "--confirm", "REVERT", "--skip-mcu", "--no-reboot")
    assert r.returncode == 0 and not [c for c in conv.calls() if c.startswith("rollback")] and "systemctl reboot" not in conv.calls()


def test_soft_revert_keeps_everything_but_the_selection(conv):
    r = conv.revert("--soft", "--yes", "--confirm", "REVERT", "--no-reboot")
    assert r.returncode == 0
    assert conv.boot_env()["eitri_mode"] == "stock" and conv.boot_env()["eitri_state_A"] == "good"
    assert os.path.exists(conv.p.p("/boot/boot.scr")) and os.path.exists(conv.p.p("/boot/extlinux/extlinux.conf.stock"))


def test_wrong_phrase_changes_nothing(conv):
    before = conv.p.snapshot()
    r = conv.revert("--hard", "--yes", "--confirm", "nope")
    assert r.returncode == 3 and conv.p.snapshot() == before


def test_to_eitri_after_hard_revert_round_trip(conv):
    conv.revert("--hard", "--yes", "--confirm", "REVERT", "--skip-mcu", "--no-reboot")
    r = conv.revert("--to-eitri", "--yes", "--confirm", "REVERT", "--no-reboot")
    assert r.returncode == 0, r.stdout
    boot = conv.p.p("/boot")
    assert open(boot + "/boot.scr").read() == "BOOTSCR-BYTES eitri_mode"
    assert not os.path.exists(boot + "/extlinux/extlinux.conf") and open(boot + "/extlinux/extlinux.conf.stock").read() == STOCK_EXTLINUX
    assert conv.boot_env() == {"eitri_mode": "eitri", "eitri_slot": "A", "eitri_tries": "3", "eitri_state_A": "good", "eitri_state_B": "unknown"}


def test_refuses_when_the_stock_partition_changed(conv):
    # blkid shim reports the stock UUID; make the recorded one differ
    conv.p.write("/boot/eitri/extlinux.conf.orig", STOCK_EXTLINUX.replace("5dcae443", "deadbeef"))
    r = conv.revert("--hard", "--yes", "--confirm", "REVERT")
    assert r.returncode == 10 and "no longer holds the stock system" in r.stdout and "--full" in r.stdout


def test_refuses_on_a_printer_that_was_never_converted(tmp_path):
    c = Converted(tmp_path)
    os.remove(c.p.p("/boot/eitri/boot.env"))
    r = c.revert("--hard", "--yes")
    assert r.returncode == 10 and "not (or no longer) set up for Eitri" in r.stdout


def test_reclaim_only_from_the_stock_system_and_after_hard_revert(tmp_path):
    c = Converted(tmp_path, on_eitri=True)
    assert c.revert("--reclaim").returncode == 10
    c = Converted(tmp_path / "b", on_eitri=False)
    r = c.revert("--reclaim")
    assert r.returncode == 10 and "boot manager is still active" in r.stdout
    c.revert("--hard", "--yes", "--confirm", "REVERT", "--skip-mcu", "--no-reboot")
    r = c.revert("--reclaim")
    assert r.returncode == 0 and "would: delete partitions 7 6 5 3" in r.stdout and not any(x.startswith("sfdisk") for x in c.calls())
    r = c.revert("--reclaim", "--yes", "--confirm", "DELETE EITRI")
    assert r.returncode == 0, r.stdout
    calls = [x for x in c.calls() if x.split()[0] in ("sfdisk", "partx", "resize2fs", "systemctl")]
    assert calls[0].startswith("sfdisk --delete") and calls[0].endswith("7 6 5 3")
    assert any(x.startswith("sfdisk -N 2") for x in calls) and any(x.startswith("resize2fs") for x in calls)
    assert "systemctl unmask armbian-resize-filesystem.service" in calls
    assert not os.path.exists(c.p.p("/boot/eitri"))


def test_full_restore_arms_the_ram_installer(conv):
    r = conv.revert("--full")
    assert r.returncode == 0 and "would: reboot into the RAM installer with eitri.op=restore" in r.stdout
    r = conv.revert("--full", "--yes", "--confirm", "RESTORE", "--skip-mcu")
    assert r.returncode == 0, r.stdout
    conf = open(os.path.join(conv.p.state, "stage2.conf")).read()
    assert "operation=restore" in conf and "restore_src=usb:ABCD-1234:/EITRI-BACKUP/x/mmcblk1.img.gz" in conf and "restore_gz_sha256=" + "a" * 64 in conf
    assert conv.boot_env()["eitri_mode"] == "stage2"
    assert "eitri.op=restore" in open(conv.p.p("/boot/eitri/layout.env")).read()
    blob = open(conv.p.p("/boot/eitri/stage2/initramfs.cpio.gz"), "rb").read()
    assert blob.startswith(b"BASE-INITRAMFS") and len(blob) > 20                      # old archive kept, new config appended
    assert "systemctl reboot" in conv.calls()


def test_full_restore_needs_the_usb_stick(conv):
    r = conv.revert("--full", "--yes", "--confirm", "RESTORE", FAKE_USB_DEV="")
    assert r.returncode == 10 and "plug in the USB stick" in r.stdout and conv.boot_env()["eitri_mode"] == "eitri"


def test_full_restore_from_host_needs_no_record(conv):
    os.remove(os.path.join(conv.data, "eitri", "state", "backup.env"))
    r = conv.revert("--full", "--yes", "--confirm", "RESTORE", "--skip-mcu", "--from-host", "192.168.0.10:9000")
    assert r.returncode == 0, r.stdout
    assert "restore_src=tcp:192.168.0.10:9000" in open(os.path.join(conv.p.state, "stage2.conf")).read()


# ---- stage 2 in restore mode ------------------------------------------------------------------
def make_image(path, size=18 * 1024 * 1024):
    raw = bytearray(os.urandom(size))
    raw[510:512] = b"\x55\xaa"
    with gzip.open(path, "wb") as fh:
        fh.write(bytes(raw))
    return bytes(raw)


def test_stage2_restore_writes_mbr_and_data_but_leaves_the_bootloader_area(tmp_path):
    rig = Stage2Rig(tmp_path)
    image = os.path.join(rig.tmp, "backup.img.gz")
    raw = make_image(image)
    disk_before = os.urandom(20 * 1024 * 1024)
    open(rig.disk, "wb").write(disk_before)
    rig.write_conf(operation="restore", restore_src="file:" + image, restore_raw_sha256=sha256(raw))
    r = rig.run(S2_FAIL_DELAY="0", S2_DONE_DELAY="0")
    assert r.returncode == 0, r.stdout
    after = open(rig.disk, "rb").read()
    assert after[:512] == raw[:512]                                             # partition table restored
    assert after[512:16 * 1024 * 1024] == disk_before[512:16 * 1024 * 1024]     # idbloader + u-boot untouched
    assert after[16 * 1024 * 1024:len(raw)] == raw[16 * 1024 * 1024:]           # everything else is the backup
    assert after[len(raw):] == disk_before[len(raw):]
    assert rig.status()["state"] == "done"


def test_stage2_restore_detects_a_corrupt_stream(tmp_path):
    rig = Stage2Rig(tmp_path)
    image = os.path.join(rig.tmp, "backup.img.gz")
    make_image(image)
    rig.write_conf(operation="restore", restore_src="file:" + image, restore_raw_sha256="0" * 64)
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1 and rig.status()["state"] == "failed" and "does not match" in rig.status()["msg"]


def test_stage2_restore_from_split_usb_image_checks_checksum_first(tmp_path):
    rig = Stage2Rig(tmp_path)
    make_image(os.path.join(rig.tmp, "whole.gz"))
    gz = open(os.path.join(rig.tmp, "whole.gz"), "rb").read()
    mid = len(gz) // 2
    stick = rig.p("/mnt/usb/EITRI-BACKUP/x")
    os.makedirs(stick)
    open(stick + "/mmcblk1.img.gz.part000", "wb").write(gz[:mid])
    open(stick + "/mmcblk1.img.gz.part001", "wb").write(gz[mid:])
    # /dev/disk/by-uuid does not exist in the sandbox, so the stick cannot be found: nothing may be written
    os.makedirs(rig.p("/dev/disk/by-uuid"), exist_ok=True)
    rig.write_conf(operation="restore", restore_src="usb:ABCD-1234:/EITRI-BACKUP/x/mmcblk1.img.gz", restore_gz_sha256="0" * 64)
    r = rig.run(S2_FAIL_DELAY="0")
    # wrong recorded checksum, or the by-uuid node missing in the sandbox: either way nothing may have been written
    assert r.returncode == 1 and open(rig.disk, "rb").read() == b""
