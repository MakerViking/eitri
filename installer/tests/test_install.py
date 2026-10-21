# SPDX-License-Identifier: Apache-2.0
"""install.sh against a fake stock printer (PATH shims for the block-device tools, file:// bundle)."""
import gzip
import json
import os
import subprocess

import pytest

from helpers import Signer
from printerfix import INSTALLER, Printer, sha256


@pytest.fixture
def printer(tmp_path):
    return Printer(tmp_path, Signer(tmp_path))


def test_dry_run_is_the_default_and_changes_nothing(printer):
    before = printer.snapshot()
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 0, r.stdout
    assert "DRY RUN" in r.stdout and "Dry run finished: nothing was changed" in r.stdout
    after = printer.snapshot()
    # only our own state directory (checksummed download manifest, layout plan) may appear
    new = {k for k in after if k not in before}
    assert all(k.startswith("var/lib/eitri-install/") for k in new), new
    assert before.items() <= after.items()
    assert not any(c.startswith(("systemctl stop", "systemctl mask", "systemctl reboot", "reboot")) for c in printer.calls())
    assert os.path.exists(printer.p("/boot/extlinux/extlinux.conf")) and not os.path.exists(printer.p("/boot/eitri"))
    assert not os.path.exists(os.path.join(printer.state, "payload"))             # nothing downloaded
    assert "p5" in r.stdout and "eitri-data" in r.stdout and "stock-root" in r.stdout


def test_one_flag_alone_is_still_a_dry_run(printer):
    for flag in ("--i-have-a-backup", "--yes"):
        r = printer.install("--flavor", "klipper", flag)
        assert r.returncode == 0 and "Both --i-have-a-backup and --yes" in r.stdout and "DRY RUN" in r.stdout
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--dry-run")
    assert "DRY RUN" in r.stdout and not os.path.exists(printer.p("/boot/eitri"))


def test_json_events_are_one_valid_object_per_line(printer):
    r = printer.install("--flavor", "klipper", "--json")
    events = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
    assert events and all(e["schema"] == 1 and e["component"] == "install" for e in events)
    steps = [(e["step"], e["status"]) for e in events if e["type"] == "step"]
    assert ("preflight", "ok") in steps and ("layout", "ok") in steps
    assert events[-1]["type"] == "result" and events[-1]["ok"] is True


def test_refuses_other_hardware(printer):
    printer.write("/proc/device-tree/compatible", "raspberrypi,4-model-b\0brcm,bcm2711\0")
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 10 and "not a Firefly ROC-RK3328-CC" in r.stdout


def test_refuses_when_not_booted_from_the_emmc(printer):
    r = printer.install("--flavor", "klipper", FAKE_ROOT_SRC="/dev/mmcblk0p1")
    assert r.returncode == 10 and "Booted from an SD card" in r.stdout


def test_refuses_a_non_stock_partition_table(printer):
    with open(printer.dump, "a") as fh:
        fh.write("%sp3 : start=60000000, size=1000, type=83\n" % printer.disk)
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 10 and "unexpected partition table" in r.stdout


def test_refuses_during_a_print(printer):
    r = printer.install("--flavor", "klipper", FAKE_PRINT_STATE="printing")
    assert r.returncode == 10 and "a print is printing" in r.stdout


def test_unknown_print_state_is_only_a_warning(printer):
    r = printer.install("--flavor", "klipper", FAKE_PRINT_STATE="none")
    assert r.returncode == 0 and "could not ask Moonraker" in r.stdout


def test_refuses_an_already_converted_printer(printer):
    printer.write("/boot/eitri/boot.env", "eitri_mode=eitri\n")
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 10 and "already runs (or has run) Eitri" in r.stdout


def test_wrong_architecture(printer):
    r = printer.install("--flavor", "klipper", FAKE_ARCH="x86_64")
    assert r.returncode == 10 and "expected aarch64" in r.stdout


def test_unsigned_or_tampered_bundle_is_refused(printer):
    with open(os.path.join(printer.bundle, "install.json"), "a") as fh:
        fh.write(" ")
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 12 and "not signed by a trusted Eitri key" in r.stdout


def test_bundle_for_wrong_board_is_refused(printer):
    doc = printer.install_doc
    doc["target"]["compatible"] = "rockchip,rk3399"
    data = json.dumps(doc).encode()
    open(os.path.join(printer.bundle, "install.json"), "wb").write(data)
    open(os.path.join(printer.bundle, "install.json.minisig"), "w").write(printer.signer.sign(data))
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 12 and "not for the Firefly" in r.stdout


def test_bundle_with_a_serial_console_is_refused(printer):
    doc = printer.install_doc
    doc["kernel_cmdline"] = "console=ttyS2,1500000n8 loglevel=4"
    data = json.dumps(doc).encode()
    open(os.path.join(printer.bundle, "install.json"), "wb").write(data)
    open(os.path.join(printer.bundle, "install.json.minisig"), "w").write(printer.signer.sign(data))
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 12 and "MCU UARTs" in r.stdout


def test_layout_that_does_not_fit_is_refused(printer):
    r = printer.install("--flavor", "klipper", FAKE_DISK_SECTORS="30000000")   # ~14 GiB eMMC
    assert r.returncode == 10 and "not enough room" in r.stdout or "cannot shrink" in r.stdout


def test_real_run_without_backup_stops_before_touching_anything(printer):
    before = printer.snapshot()
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--confirm", "CONVERT")
    assert r.returncode == 11 and "no verified backup recorded" in r.stdout
    after = printer.snapshot()
    assert before.items() <= after.items() and not os.path.exists(printer.p("/boot/eitri"))
    assert not os.path.exists(os.path.join(printer.state, "payload"))               # not even downloaded


# ---- backups --------------------------------------------------------------------------------
def make_backup_file(printer, signature_ok=True, tamper=False):
    path = os.path.join(printer.tmp, "mmcblk1.img.gz")
    raw = open(printer.disk, "rb").read()
    if not signature_ok:
        raw = raw[:440] + b"\x00\x00\x00\x00" + raw[444:]
    with gzip.open(path, "wb") as fh:
        fh.write(raw)
    digest = sha256(open(path, "rb").read())
    if tamper:
        digest = "0" * 64
    return path, digest


def test_existing_backup_file_is_verified_against_this_disk(printer):
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path,
                        "--backup-sha256", digest, "--confirm", "CONVERT")
    assert r.returncode == 0, r.stdout
    rec = open(os.path.join(printer.state, "backup.env")).read()
    assert "verified=1" in rec and "label_id=0x6a783df4" in rec and "method=file" in rec


def test_backup_with_wrong_checksum_or_wrong_printer_is_rejected(printer):
    path, digest = make_backup_file(printer, tamper=True)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest)
    assert r.returncode == 11 and "checksum of" in r.stdout
    path, digest = make_backup_file(printer, signature_ok=False)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest)
    assert r.returncode == 11 and "different eMMC" in r.stdout
    assert not os.path.exists(printer.p("/boot/eitri"))


def test_stale_backup_record_is_not_accepted(printer):
    path, digest = make_backup_file(printer)
    printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest,
                    "--stage-only")
    rec = os.path.join(printer.state, "backup.env")
    import re
    text = re.sub(r"created_epoch=\d+", "created_epoch=1000", open(rec).read())      # make it ancient
    open(rec, "w").write(text)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--stage-only")
    assert r.returncode == 11 and "no verified backup recorded" in r.stdout


def test_backup_to_usb_splits_fat32_and_verifies(printer, tmp_path):
    usb = tmp_path / "usbstick"
    usb.mkdir()
    # a stick with FAT32 semantics: the installer must split the stream
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-to-usb", "/dev/sda1",
                        "--stage-only", FAKE_USB_MNT=str(usb), FAKE_USB_FS="vfat", FAKE_USB_UUID="ABCD-1234")
    assert r.returncode == 0, r.stdout
    folders = os.listdir(usb / "EITRI-BACKUP")
    assert len(folders) == 1
    parts = sorted(os.listdir(usb / "EITRI-BACKUP" / folders[0]))
    assert parts == ["mmcblk1.img.gz.part000"]                               # small test image: one part
    data = b"".join(open(str(usb / "EITRI-BACKUP" / folders[0] / p), "rb").read() for p in parts)
    assert gzip.decompress(data) == open(printer.disk, "rb").read()          # the backup IS the disk
    rec = open(os.path.join(printer.state, "backup.env")).read()
    assert "usb_uuid=ABCD-1234" in rec and "split=1" in rec and "dest=usb:ABCD-1234:/EITRI-BACKUP/" in rec


def test_backup_stream_mode_emits_a_valid_gzip_of_the_disk(printer):
    r = subprocess.run([os.path.join(INSTALLER, "install.sh"), "--backup-stream"], env=printer.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert r.returncode == 0, r.stderr.decode()
    assert gzip.decompress(r.stdout) == open(printer.disk, "rb").read()
    assert sha256(r.stdout) in r.stderr.decode()                              # the user is told what to compare against


def test_no_usb_stick_gives_actionable_guidance(printer):
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-to-usb")
    assert r.returncode == 11 and "no USB stick found" in r.stdout and "QIDI firmware updates" in r.stdout


# ---- the real run ----------------------------------------------------------------------------
def real_run(printer, *extra, **env):
    path, digest = make_backup_file(printer)
    return printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path,
                           "--backup-sha256", digest, "--confirm", "CONVERT", *extra, **env)


def test_existing_boot_script_is_kept_for_revert(printer):
    printer.write("/boot/boot.scr", "QIDI-OWN-BOOT-SCRIPT")
    r = real_run(printer)
    assert r.returncode == 0, r.stdout
    boot = printer.p("/boot")
    assert open(boot + "/eitri/boot.scr.stock").read() == "QIDI-OWN-BOOT-SCRIPT"
    assert b"QIDI-OWN" not in open(boot + "/boot.scr", "rb").read()


def test_real_run_arms_stage_two_and_reboots(printer, tmp_path):
    moddir = tmp_path / "mods"
    moddir.mkdir()
    for m in ("vfat", "fat"):
        (moddir / (m + ".ko")).write_bytes(b"\x7fELF-" + m.encode())
    r = real_run(printer, FAKE_MODDIR=str(moddir))
    assert r.returncode == 0, r.stdout
    boot = printer.p("/boot")
    # the stock entry is still there, renamed, and the original is saved
    assert not os.path.exists(boot + "/extlinux/extlinux.conf")
    assert open(boot + "/extlinux/extlinux.conf.stock").read() == open(boot + "/eitri/extlinux.conf.orig").read()
    # boot.scr is a valid script image equal to the repo's boot.cmd
    scr = subprocess.run(["python3", os.path.join(INSTALLER, "boot", "mkscr.py"), "--check", boot + "/boot.scr"], stdout=subprocess.PIPE, universal_newlines=True)
    assert scr.returncode == 0 and scr.stdout == open(os.path.join(INSTALLER, "boot", "boot.cmd")).read()
    env = dict(l.split("=", 1) for l in open(boot + "/eitri/boot.env").read().splitlines())
    assert env == {"eitri_mode": "stage2", "eitri_slot": "A", "eitri_tries": "3", "eitri_state_A": "unknown", "eitri_state_B": "unknown"}
    layout = dict(l.split("=", 1) for l in open(boot + "/eitri/layout.env").read().splitlines())
    assert layout["eitri_partuuid_A"] == "6a783df4-05" and layout["eitri_partuuid_B"] == "6a783df4-06"
    assert layout["eitri_s2_kernel"] == "/Image" and layout["eitri_s2_fdt"] == "/dtb/rockchip/rk3328-roc-cc.dtb"
    # the OS mounts /data and the boot partition from kernel arguments: this disk has no GPT names to derive them from
    assert "eitri.data=LABEL=eitri-data" in layout["eitri_cmdline"] and "eitri.bootfs=PARTUUID=6a783df4-01" in layout["eitri_cmdline"]
    assert "console=tty1" in layout["eitri_cmdline"] and "ttyS2" not in layout["eitri_cmdline"] + layout["eitri_s2_args"]
    # staged payload verified, and what stage 2 reads is complete
    payload = os.path.join(printer.state, "payload")
    assert subprocess.run(["sha256sum", "-c", "payload.sha256", "--quiet"], cwd=payload).returncode == 0
    penv = dict(l.split("=", 1) for l in open(payload + "/payload.env").read().splitlines())
    assert penv["rootfs_raw_sha256"] == sha256(printer.rootfs_raw) and penv["mcu_firmware_file"] == "mcu-firmware.tar.zst"
    # the RAM installer = CI base archive + our extra archive, containing config, scripts and the stock modules
    blob = open(boot + "/eitri/stage2/initramfs.cpio.gz", "rb").read()
    base = open(os.path.join(printer.bundle, "stage2-initramfs.cpio.gz"), "rb").read()
    assert blob.startswith(base) and len(blob) > len(base)
    # the kernel unpacks the two members one after the other; cpio -t reads a single archive, so list each member
    listing = "".join(subprocess.run("zcat | cpio -it 2>/dev/null", shell=True, input=part, stdout=subprocess.PIPE).stdout.decode()
                      for part in (blob[:len(base)], blob[len(base):]))
    for needed in ("init", "etc/eitri/stage2.conf", "usr/lib/eitri/stage2.sh", "usr/lib/eitri/layout.sh", "usr/lib/eitri/www/index.html", "etc/eitri/modules.list"):
        assert needed in listing, listing
    s2 = open(os.path.join(printer.state, "stage2.conf")).read()
    assert "operation=convert" in s2 and "payload_loc=stock:/var/lib/eitri-install/payload" in s2 and "stock_fs_uuid=5dcae443-0796-4029-ae6c-bf5bd2a37067" in s2
    # services and reboot
    calls = printer.calls()
    assert "systemctl mask armbian-resize-filesystem.service" in calls and "systemctl reboot" in calls
    assert calls.index("systemctl mask armbian-resize-filesystem.service") < calls.index("systemctl reboot")


def test_wrong_confirmation_phrase_changes_nothing(printer):
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest,
                        "--confirm", "yes please")
    assert r.returncode == 3 and "is not 'CONVERT'" in r.stdout
    assert os.path.exists(printer.p("/boot/extlinux/extlinux.conf")) and not os.path.exists(printer.p("/boot/eitri"))
    assert "systemctl reboot" not in printer.calls()


def test_corrupt_payload_download_is_never_armed(printer):
    # replace the rootfs in the bundle without updating the signed checksum
    with open(os.path.join(printer.bundle, "rootfs.img.zst"), "ab") as fh:
        fh.write(b"corruption")
    r = real_run(printer)
    assert r.returncode == 12 and "does not match the signed checksum" in r.stdout
    assert not os.path.exists(printer.p("/boot/eitri")) and os.path.exists(printer.p("/boot/extlinux/extlinux.conf"))


def test_mcu_recovery_failure_blocks_arming(printer, tmp_path):
    stub = tmp_path / "mcuflash-stub"
    stub.write_text("#!/bin/sh\necho \"$@\" >>\"$FAKE_LOG\"\necho '{\"type\":\"result\",\"ok\":false,\"msg\":\"toolhead: no Katapult bootloader answered\"}'\nexit 1\n")
    stub.chmod(0o755)
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--backup-file", path, "--backup-sha256", digest,
                        "--confirm", "CONVERT", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 13 and "could not be proven recoverable" in r.stdout
    assert not os.path.exists(printer.p("/boot/eitri")) and "systemctl reboot" not in printer.calls()
    logged = [l for l in printer.calls() if l.startswith("backup")][0]
    assert "--apply --yes" in logged and "--no-version-lock" in logged and "--flavor klipper" in logged


def test_mcu_recovery_success_passes_stock_binaries_through(printer, tmp_path):
    stub = tmp_path / "mcuflash-stub"
    stub.write_text("#!/bin/sh\necho \"$@\" >>\"$FAKE_LOG\"\necho '{\"type\":\"result\",\"ok\":true}'\nexit 0\n")
    stub.chmod(0o755)
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "kalico", "--i-have-a-backup", "--yes", "--backup-file", path, "--backup-sha256", digest, "--confirm", "CONVERT",
                        "--stock-mcu-bin", "mainboard=/x/qd_mcu.bin", "--accept-no-mcu-backup", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 0, r.stdout
    logged = [l for l in printer.calls() if l.startswith("backup")][0]
    assert "--flavor kalico" in logged and "--stock-bin mainboard=/x/qd_mcu.bin" in logged and "--accept-no-mcu-backup" in logged
    assert "flavor=kalico" in open(os.path.join(printer.state, "stage2.conf")).read()


def test_dry_run_probe_mcu_only_probes(printer, tmp_path):
    stub = tmp_path / "mcuflash-stub"
    stub.write_text("#!/bin/sh\necho \"$@\" >>\"$FAKE_LOG\"\necho '{\"type\":\"result\",\"ok\":true}'\n")
    stub.chmod(0o755)
    r = printer.install("--flavor", "klipper", "--probe-mcu", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 0, r.stdout
    verbs = [l.split()[0] for l in printer.calls() if l.split()[:1] and l.split()[0] in ("probe", "plan", "backup", "flash")]
    assert verbs == ["probe"]                      # a dry run never resets or flashes a board


def test_dry_run_without_probe_leaves_klipper_alone(printer):
    r = printer.install("--flavor", "klipper")
    assert "use --probe-mcu" in r.stdout and not any(c.startswith("probe") for c in printer.calls())


def test_status_reports_state(printer):
    real_run(printer)
    printer.write("/boot/eitri/LAST_ERROR", "2026-10-21T10:00:00Z\nresize failed\n")
    r = printer.install("--status")
    assert "stage2" in r.stdout and "LAST ERROR" in r.stdout and "resize failed" in r.stdout and "recorded backup" in r.stdout


def test_bash_syntax_of_every_shell_file():
    for rel in ("install.sh", "revert.sh", "stage2/stage2.sh", "lib/eitri-common.sh", "lib/layout.sh", "firstboot/eitri-firstboot.sh"):
        path = os.path.join(INSTALLER, rel)
        if os.path.exists(path):
            assert subprocess.run(["bash", "-n", path]).returncode == 0, rel


def test_payload_can_be_staged_on_a_usb_stick(printer, tmp_path):
    usb = tmp_path / "stick"
    usb.mkdir()
    r = real_run(printer, "--stage-usb", "/dev/sda1", FAKE_USB_MNT=str(usb), FAKE_USB_FS="ext4", FAKE_USB_UUID="ABCD-1234", FAKE_USB_DEV="/dev/sda1")
    assert r.returncode == 0, r.stdout
    assert os.path.isfile(usb / "EITRI-PAYLOAD" / "rootfs.img.zst") and not os.path.exists(os.path.join(printer.state, "payload"))
    conf = open(os.path.join(printer.state, "stage2.conf")).read()
    assert "payload_loc=usb:ABCD-1234:/EITRI-PAYLOAD" in conf


def test_a_converted_printer_is_not_converted_twice(printer):
    assert real_run(printer).returncode == 0
    again = printer.install("--flavor", "kalico")
    assert again.returncode == 10 and "already runs (or has run) Eitri" in again.stdout


def test_kalico_flavor_is_carried_into_stage_two(printer):
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "kalico", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path,
                        "--backup-sha256", digest, "--confirm", "CONVERT")
    assert r.returncode == 0, r.stdout
    assert "flavor=kalico" in open(os.path.join(printer.state, "stage2.conf")).read()
    penv = open(os.path.join(printer.state, "payload", "payload.env")).read()
    assert "flavor=kalico" in penv


def test_install_into_rootfs_places_the_on_device_tools(tmp_path):
    dest = tmp_path / "rootfs"
    (dest / "usr" / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
    r = subprocess.run([os.path.join(INSTALLER, "tools", "install-into-rootfs.sh"), str(dest)], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True)
    assert r.returncode == 0, r.stdout
    site = dest / "usr" / "lib" / "python3.14" / "site-packages"
    assert (site / "eitri_update" / "cli.py").is_file() and not list(site.rglob("__pycache__"))
    for exe in ("usr/bin/eitri-update", "usr/libexec/eitri/eitri-bootctl"):
        assert os.access(dest / exe, os.X_OK)
    # the wrappers are runnable against the copied package (the bootctl one answers for a missing boot dir with a clean error)
    out = subprocess.run(["python3", "-I", "-c", "import sys; sys.path[:0]=[sys.argv[1], sys.argv[2]]; from eitri_update.cli import main; sys.exit(main(['--version']))",
                          str(site), os.path.join(INSTALLER, "mcuflash")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    assert "eitri-update" in out.stdout
    for unit in ("eitri-prepare.service", "eitri-verify.service", "eitri-update-check.timer"):
        assert (dest / "usr/lib/systemd/system" / unit).is_file()
    assert (dest / "usr/lib/systemd/system/multi-user.target.wants/eitri-verify.service").is_symlink()
