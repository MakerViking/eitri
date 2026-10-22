# SPDX-License-Identifier: Apache-2.0
"""install.sh against a fake stock printer (PATH shims for the block-device tools, file:// bundle)."""
import gzip
import json
import os
import shutil
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
    assert before == after                                                         # not a single file appeared or changed
    assert not os.path.exists(printer.state)                                       # not even an empty state directory
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
    # the stock kernel's modules: /lib/modules/<version>/..., xz-compressed like the printer's (5.16.20-rockchip64)
    moddir = "/lib/modules/5.16.20-rockchip64/kernel/fs"
    for m in ("vfat", "fat", "nls_iso8859_1"):
        printer.write(moddir + "/" + m + ".ko.xz", "\x7fELF-" + m)
    r = real_run(printer, FAKE_MODDIR=moddir)
    assert r.returncode == 0, r.stdout
    boot = printer.p("/boot")
    # the stock entry is still there, renamed, and the original is saved
    assert not os.path.exists(boot + "/extlinux/extlinux.conf")
    assert open(boot + "/extlinux/extlinux.conf.stock").read() == open(boot + "/eitri/extlinux.conf.orig").read()
    # boot.scr is a valid script image equal to the repo's boot.cmd
    scr = subprocess.run(["python3", os.path.join(INSTALLER, "boot", "mkscr.py"), "--check", boot + "/boot.scr"], stdout=subprocess.PIPE, universal_newlines=True)
    assert scr.returncode == 0 and scr.stdout == open(os.path.join(INSTALLER, "boot", "boot.cmd")).read()
    env = dict(l.split("=", 1) for l in open(boot + "/eitri/boot.env").read().splitlines())
    # armed with a start counter, not a one-shot flag: a power cut mid-conversion re-enters the installer
    assert env == {"eitri_mode": "stage2", "eitri_slot": "A", "eitri_tries": "3", "eitri_state_A": "unknown", "eitri_state_B": "unknown",
                   "eitri_s2_tries": "2"}
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
    seed = printer.seeds["klipper"]
    assert penv["data_seed_file"] == "data-seed-2026.10.0-klipper.tar.zst" and penv["data_seed_sha256"] == seed["sha256"]
    assert penv["data_seed_manifest_file"] == "data-seed-2026.10.0-klipper.manifest.json" and penv["data_seed_version"] == "2026.10.0"
    assert os.path.isfile(payload + "/" + penv["data_seed_file"]) and os.path.isfile(payload + "/" + penv["data_seed_manifest_file"])
    # the RAM installer = CI base archive + our extra archive, containing config, scripts and the stock modules
    blob = open(boot + "/eitri/stage2/initramfs.cpio.gz", "rb").read()
    base = open(os.path.join(printer.bundle, "stage2-initramfs.cpio.gz"), "rb").read()
    assert blob.startswith(base) and len(blob) > len(base)
    # the kernel unpacks the two members one after the other; cpio -t reads a single archive, so list each member
    listing = "".join(subprocess.run("zcat | cpio -it 2>/dev/null", shell=True, input=part, stdout=subprocess.PIPE).stdout.decode()
                      for part in (blob[:len(base)], blob[len(base):]))
    for needed in ("init", "etc/eitri/stage2.conf", "usr/lib/eitri/stage2.sh", "usr/lib/eitri/layout.sh", "usr/lib/eitri/www/index.html", "etc/eitri/modules.list",
                   "usr/libexec/eitri/eitri-seed-install", "usr/lib/eitri/modules/vfat.ko.xz", "usr/lib/eitri/modules/nls_iso8859_1.ko.xz"):
        assert needed in listing, listing
    # The base is a merged-/usr tree (/lib -> usr/lib): a member named lib/... (or bin/, sbin/, lib64/) in the appended archive
    # would make the kernel replace that symlink with a directory and stage 2 would panic ("Failed to execute /init").
    extra = subprocess.run("zcat | cpio -it 2>/dev/null", shell=True, input=blob[len(base):], stdout=subprocess.PIPE).stdout.decode().split()
    assert not [m for m in extra if m.split("/")[0] in ("lib", "lib64", "bin", "sbin")], extra
    mods = open(os.path.join(printer.state, "modules.s2.list")).read().split()
    assert mods == ["/usr/lib/eitri/modules/fat.ko.xz", "/usr/lib/eitri/modules/vfat.ko.xz", "/usr/lib/eitri/modules/nls_iso8859_1.ko.xz"] or \
        sorted(mods) == sorted(["/usr/lib/eitri/modules/fat.ko.xz", "/usr/lib/eitri/modules/vfat.ko.xz", "/usr/lib/eitri/modules/nls_iso8859_1.ko.xz"]), mods
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
                        "--confirm", "CONVERT", "--accept-mcu-roundtrip", "--mcu", "auto", EITRI_MCUFLASH=str(stub))
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
                        "--stock-mcu-bin", "mainboard=/x/qd_mcu.bin", "--accept-no-mcu-backup", "--accept-mcu-roundtrip", "--mcu", "auto", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 0, r.stdout
    logged = [l for l in printer.calls() if l.startswith("backup")][0]
    assert "--flavor kalico" in logged and "--stock-bin mainboard=/x/qd_mcu.bin" in logged and "--accept-no-mcu-backup" in logged
    assert "flavor=kalico" in open(os.path.join(printer.state, "stage2.conf")).read()


def test_dry_run_probe_mcu_only_probes(printer, tmp_path):
    stub = tmp_path / "mcuflash-stub"
    stub.write_text("#!/bin/sh\necho \"$@\" >>\"$FAKE_LOG\"\necho '{\"type\":\"result\",\"ok\":true}'\n")
    stub.chmod(0o755)
    r = printer.install("--flavor", "klipper", "--probe-mcu", "--mcu", "auto", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 0, r.stdout
    verbs = [l.split()[0] for l in printer.calls() if l.split()[:1] and l.split()[0] in ("probe", "plan", "backup", "flash")]
    assert verbs == ["probe"]                      # a dry run never resets or flashes a board


def test_dry_run_without_probe_leaves_klipper_alone(printer):
    r = printer.install("--flavor", "klipper", "--mcu", "auto")
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
    for exe in ("usr/bin/eitri-update", "usr/libexec/eitri/eitri-bootctl", "usr/libexec/eitri/eitri-seed-install", "usr/libexec/eitri/switch-flavor"):
        assert os.access(dest / exe, os.X_OK)
    # the seed installer is the reference implementation itself, byte for byte
    assert (dest / "usr/libexec/eitri/eitri-seed-install").read_bytes() == open(os.path.join(INSTALLER, "..", "seed", "bin", "eitri-seed-install"), "rb").read()
    # the wrappers are runnable against the copied package (the bootctl one answers for a missing boot dir with a clean error)
    out = subprocess.run(["python3", "-I", "-c", "import sys; sys.path[:0]=[sys.argv[1], sys.argv[2]]; from eitri_update.cli import main; sys.exit(main(['--version']))",
                          str(site), os.path.join(INSTALLER, "mcuflash")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    assert "eitri-update" in out.stdout
    for unit in ("eitri-prepare.service", "eitri-verify.service", "eitri-update-check.timer"):
        assert (dest / "usr/lib/systemd/system" / unit).is_file()
    assert (dest / "usr/lib/systemd/system/multi-user.target.wants/eitri-verify.service").is_symlink()


# ---- consent for the device write that precedes the CONVERT prompt -----------------------------------------------
def _ok_stub(tmp_path):
    stub = tmp_path / "mcuflash-stub"
    stub.write_text("#!/bin/sh\necho \"$@\" >>\"$FAKE_LOG\"\necho '{\"type\":\"result\",\"ok\":true}'\nexit 0\n")
    stub.chmod(0o755)
    return stub


def test_the_mcu_roundtrip_needs_explicit_consent_before_any_board_is_written(printer, tmp_path):
    stub = _ok_stub(tmp_path)
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--backup-file", path, "--backup-sha256", digest,
                        "--confirm", "CONVERT", "--mcu", "auto", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 3 and "--accept-mcu-roundtrip" in r.stdout and "ERASES the application" in r.stdout
    assert not [l for l in printer.calls() if l.startswith("backup")]                       # no board was touched
    assert not os.path.exists(os.path.join(printer.state, "payload"))                       # and nothing big was downloaded either
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--backup-file", path, "--backup-sha256", digest,
                        "--confirm", "CONVERT", "--accept-mcu-roundtrip", "--mcu", "auto", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 0, r.stdout
    assert [l for l in printer.calls() if l.startswith("backup")]


def test_mcu_skip_needs_no_roundtrip_consent(printer):
    r = real_run(printer)                                    # --mcu skip
    assert r.returncode == 0, r.stdout


def test_the_help_names_the_toolhead_write():
    out = subprocess.run([os.path.join(INSTALLER, "install.sh"), "--help"], stdout=subprocess.PIPE, universal_newlines=True).stdout
    assert "--accept-mcu-roundtrip" in out and "WRITES to the toolhead" in out


# ---- the backup must still exist when the point of no return is reached -------------------------------------------
def test_a_backup_file_that_disappeared_stops_the_conversion(printer):
    path, digest = make_backup_file(printer)
    assert printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest,
                           "--stage-only").returncode == 0
    os.unlink(path)                                          # the stick was pulled / the file deleted during the download
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--confirm", "CONVERT")
    assert r.returncode == 11 and "is gone" in r.stdout
    assert not os.path.exists(printer.p("/boot/eitri")) and "systemctl reboot" not in printer.calls()


def test_a_backup_file_changed_after_verification_stops_the_conversion(printer):
    path, digest = make_backup_file(printer)
    printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest, "--stage-only")
    with open(path, "ab") as fh:
        fh.write(b"x")
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--confirm", "CONVERT")
    assert r.returncode == 11 and "has changed since it was verified" in r.stdout


def test_a_usb_backup_with_the_stick_pulled_stops_the_conversion(printer, tmp_path):
    usb = tmp_path / "usbstick"
    usb.mkdir()
    env = dict(FAKE_USB_MNT=str(usb), FAKE_USB_FS="ext4", FAKE_USB_UUID="ABCD-1234", FAKE_USB_DEV="/dev/sda1")
    assert printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-to-usb", "/dev/sda1", "--stage-only", **env).returncode == 0
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--confirm", "CONVERT", **env)
    assert r.returncode == 0, r.stdout                       # stick still there: fine
    assert "Making sure the backup is still there" in r.stdout
    shutil.rmtree(printer.p("/boot/eitri"))                  # (undo the arming to run the check again)
    shutil.move(printer.p("/boot/extlinux/extlinux.conf.stock"), printer.p("/boot/extlinux/extlinux.conf"))
    for f in (usb / "EITRI-BACKUP").rglob("mmcblk1.img.gz*"):
        f.unlink()                                           # the file is gone from the stick
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--confirm", "CONVERT", **env)
    assert r.returncode == 11 and "no longer on the USB stick" in r.stdout
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--confirm", "CONVERT", FAKE_USB_DEV="", FAKE_USB_UUID="ABCD-1234")
    assert r.returncode == 11 and "not plugged in" in r.stdout


def test_stage2_is_told_where_the_backup_is(printer, tmp_path):
    usb = tmp_path / "usbstick"
    usb.mkdir()
    env = dict(FAKE_USB_MNT=str(usb), FAKE_USB_FS="vfat", FAKE_USB_UUID="ABCD-1234", FAKE_USB_DEV="/dev/sda1")
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-to-usb", "/dev/sda1", "--confirm", "CONVERT", **env)
    assert r.returncode == 0, r.stdout
    conf = open(os.path.join(printer.state, "stage2.conf")).read()
    assert "backup_method=usb" in conf and "backup_usb_uuid=ABCD-1234" in conf and "backup_path=/EITRI-BACKUP/" in conf and "backup_bytes=" in conf


def test_a_backup_image_stored_on_the_disk_being_converted_is_refused(printer):
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest,
                        FAKE_USB_DEV=printer.disk + "p2")
    assert r.returncode == 11 and "not a backup" in r.stdout


# ---- backup-to-host must not let USER@HOST:DIR run anything ---------------------------------------------------------
def _fake_ssh(tmp_path):
    """An ssh that 'connects' to a sandbox directory: the remote command runs in a local sh, so a command injected through
    DIR or HOST would visibly happen."""
    root = tmp_path / "remote"
    root.mkdir()
    shim = tmp_path / "sshbin"
    shim.mkdir()
    (shim / "ssh").write_text("#!/bin/sh\necho \"ssh $*\" >>\"$FAKE_LOG\"\n"
                             "[ \"$1\" = -- ] || exit 97\nshift\nshift\ncd \"$FAKE_SSH_ROOT\" || exit 98\nexec sh -c \"$*\"\n")
    (shim / "ssh").chmod(0o755)
    return root, shim


def test_backup_to_host_quotes_the_directory_and_passes_the_host_after_double_dash(printer, tmp_path):
    root, shim = _fake_ssh(tmp_path)
    evil = "bk'; touch pwned; echo '"
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--stage-only",
                        "--backup-to-host", "me@pc:" + evil, PATH=str(shim) + os.pathsep + printer.env()["PATH"], FAKE_SSH_ROOT=str(root))
    assert r.returncode == 0, r.stdout
    assert not (root / "pwned").exists()
    stored = list((root / evil).glob("eitri-backup-*.img.gz"))
    assert len(stored) == 1 and gzip.decompress(stored[0].read_bytes()) == open(printer.disk, "rb").read()


@pytest.mark.parametrize("host", ["-oProxyCommand=touch /tmp/x", "me@pc -oX", "a b@c", "@pc", "me@"])
def test_backup_to_host_rejects_anything_but_a_plain_host(printer, tmp_path, host):
    root, shim = _fake_ssh(tmp_path)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--stage-only",
                        "--backup-to-host", host + ":/d", PATH=str(shim) + os.pathsep + printer.env()["PATH"], FAKE_SSH_ROOT=str(root))
    assert r.returncode == 2 and "plain USER@HOST" in r.stdout
    assert not [l for l in printer.calls() if l.startswith("ssh")]


def test_hostile_hostname_cannot_reach_a_file_name_or_remote_command(printer, tmp_path):
    root, shim = _fake_ssh(tmp_path)
    printer.write("/etc/hostname", "mk'$(touch pwned)spi\n")
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--stage-only", "--backup-to-host", "me@pc:d",
                        PATH=str(shim) + os.pathsep + printer.env()["PATH"], FAKE_SSH_ROOT=str(root))
    assert r.returncode == 0, r.stdout
    assert not (root / "pwned").exists() and [p.name for p in (root / "d").iterdir()][0].startswith("eitri-backup-mktouchpwnedspi-")


def test_status_explains_an_unfinished_conversion(printer):
    printer.write("/boot/eitri/boot.env", "eitri_mode=stage2\neitri_s2_tries=1\n")
    printer.write("/boot/eitri/stage2.state", "phase=partitioned\n")
    r = printer.install("--status")
    assert "has not finished" in r.stdout and "revert.sh --full" in r.stdout and "maskrom" in r.stdout
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 10 and "earlier conversion did not finish" in r.stdout


# ---- release builds must carry a trust root -------------------------------------------------------------------------
def _rootfs_install(tmp_path, keys_dir=None, **env):
    dest = tmp_path / "rootfs"
    (dest / "usr" / "lib" / "python3.14" / "site-packages").mkdir(parents=True, exist_ok=True)
    tool_root = tmp_path / "installer-copy"
    if not tool_root.exists():
        shutil.copytree(INSTALLER, tool_root, ignore=shutil.ignore_patterns("__pycache__", "tests", "keys"))
        (tool_root / "keys").mkdir()
    if keys_dir:
        for f in os.listdir(keys_dir):
            shutil.copy(os.path.join(keys_dir, f), tool_root / "keys" / f)
    e = dict(os.environ)
    e["EITRI_SEED_INSTALL_SRC"] = os.path.join(INSTALLER, "..", "seed", "bin", "eitri-seed-install")      # the copy has no seed/ beside it
    e.update(env)
    r = subprocess.run([str(tool_root / "tools" / "install-into-rootfs.sh"), str(dest)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, env=e)
    return dest, r


def test_a_build_that_requires_a_release_key_fails_without_one(tmp_path):
    dest, r = _rootfs_install(tmp_path, EITRI_REQUIRE_UPDATE_KEY="1")
    assert r.returncode == 1 and "EITRI_REQUIRE_UPDATE_KEY=1" in r.stdout
    dest, r = _rootfs_install(tmp_path / "dev")
    assert r.returncode == 0 and "warning: no keys/*.pub" in r.stdout            # development builds only warn


def test_a_release_build_ships_the_key_the_revocations_and_the_sequence_floor(tmp_path):
    keys = tmp_path / "k"
    keys.mkdir()
    signer = Signer(tmp_path)
    shutil.copy(signer.pub, keys / "eitri-release.pub")
    (keys / "revoked-keys.txt").write_text("# none yet\n")
    dest, r = _rootfs_install(tmp_path, str(keys), EITRI_REQUIRE_UPDATE_KEY="1", EITRI_MIN_MANIFEST_SEQUENCE="42")
    assert r.returncode == 0, r.stdout
    assert (dest / "usr/share/eitri/keys/eitri-release.pub").is_file() and (dest / "usr/share/eitri/keys/revoked-keys.txt").is_file()
    assert (dest / "usr/share/eitri/min-manifest-sequence").read_text() == "42\n"
    dest, r = _rootfs_install(tmp_path / "bad", str(keys), EITRI_MIN_MANIFEST_SEQUENCE="soon")
    assert r.returncode == 2


def test_a_rootfs_build_without_the_seed_installer_fails_loudly(tmp_path):
    dest, r = _rootfs_install(tmp_path, EITRI_SEED_INSTALL_SRC=str(tmp_path / "missing"))
    assert r.returncode == 1 and "eitri-seed-install not found" in r.stdout


def test_the_help_says_which_install_path_is_the_user_path():
    out = subprocess.run([os.path.join(INSTALLER, "install.sh"), "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True).stdout
    assert "This IS the way to put Eitri on a printer" in out
    assert "GPT disk image built by os/ is for bench" in out and "microSD cards and CI only" in out
    assert "--seed-file FILE --seed-manifest FILE" in out and "data seed" in out


def all_dirs(root):
    return sorted(os.path.relpath(d, root) for d, _subdirs, _files in os.walk(root))


def test_a_dry_run_creates_no_directory_either(printer, tmp_path):
    # The real printer's dry run (2026-10-22) left an empty /var/lib/eitri-install/bundle behind. A dry run must not
    # create anything on the printer, also not with a backup file, --probe-mcu or a failure half-way.
    path, digest = make_backup_file(printer)
    stub = _ok_stub(tmp_path)
    dirs = all_dirs(printer.root)
    for args in ((), ("--backup-file", path, "--backup-sha256", digest), ("--mcu", "auto", "--probe-mcu")):
        r = printer.install("--flavor", "klipper", *args, EITRI_MCUFLASH=str(stub), TMPDIR=str(tmp_path))
        assert r.returncode == 0, r.stdout
        assert all_dirs(printer.root) == dirs, (args, set(all_dirs(printer.root)) - set(dirs))
    r = printer.install("--flavor", "klipper", FAKE_DISK_SECTORS="30000000", TMPDIR=str(tmp_path))   # fails at the layout
    assert r.returncode == 10 and all_dirs(printer.root) == dirs
    assert not [d for d in os.listdir(str(tmp_path)) if d.startswith("eitri-dry-run.")]           # the scratch dir is gone


def test_the_dry_run_checks_a_backup_file_read_only(printer):
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--backup-file", path, "--backup-sha256", digest)
    assert r.returncode == 0 and "comes from this eMMC" in r.stdout, r.stdout
    r = printer.install("--flavor", "klipper", "--backup-file", path, "--backup-sha256", "0" * 64)
    assert r.returncode == 11 and "checksum of" in r.stdout


def test_the_dry_run_finds_a_damaged_local_bundle(printer):
    with open(os.path.join(printer.bundle, "rootfs.img.zst"), "ab") as fh:
        fh.write(b"x")
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 12 and "rootfs.img.zst has the wrong size" in r.stdout


def keep_stock_bundle(printer):
    """A release that carries no MCU firmware: both boards keep their stock firmware (the first in-place install)."""
    doc = printer.install_doc
    doc["mcu_policy"] = "keep-stock"
    del doc["files"]["mcu_firmware"]
    os.unlink(os.path.join(printer.bundle, "mcu-firmware.tar.zst"))
    data = json.dumps(doc).encode()
    open(os.path.join(printer.bundle, "install.json"), "wb").write(data)
    open(os.path.join(printer.bundle, "install.json.minisig"), "w").write(printer.signer.sign(data))


def test_mcu_skip_is_the_default_and_never_touches_a_board(printer, tmp_path):
    stub = _ok_stub(tmp_path)
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--backup-file", path, "--backup-sha256", digest,
                        "--confirm", "CONVERT", EITRI_MCUFLASH=str(stub))                    # no --mcu, no --accept-mcu-roundtrip
    assert r.returncode == 0, r.stdout
    assert not [l for l in printer.calls() if l.split()[:1] and l.split()[0] in ("probe", "plan", "backup", "flash")]
    assert "mcu_mode=skip" in open(os.path.join(printer.state, "stage2.conf")).read()
    assert not os.path.exists(os.path.join(printer.state, "mcu-fw"))                      # not even unpacked


def test_a_release_without_mcu_firmware_installs_with_both_boards_stock(printer, tmp_path):
    keep_stock_bundle(printer)
    stub = _ok_stub(tmp_path)
    r = printer.install("--flavor", "klipper", EITRI_MCUFLASH=str(stub))                    # dry run
    assert r.returncode == 0 and "would stage mcu-firmware" not in r.stdout, r.stdout
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--backup-file", path, "--backup-sha256", digest,
                        "--confirm", "CONVERT", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 0, r.stdout
    penv = dict(l.split("=", 1) for l in open(os.path.join(printer.state, "payload", "payload.env")).read().splitlines())
    assert penv["mcu_firmware_file"] == "" and penv["mcu_policy"] == "keep-stock"
    assert not [l for l in printer.calls() if l.split()[:1] and l.split()[0] in ("probe", "plan", "backup", "flash")]


def test_kalico_can_be_chosen_at_install_time(printer, tmp_path):
    # The bundle offers both flavors (printerfix builds a seed per flavor); picking kalico must stage the kalico seed.
    keep_stock_bundle(printer)
    stub = _ok_stub(tmp_path)
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "kalico", "--i-have-a-backup", "--yes", "--backup-file", path, "--backup-sha256", digest,
                        "--confirm", "CONVERT", EITRI_MCUFLASH=str(stub))
    assert r.returncode == 0, r.stdout
    penv = dict(l.split("=", 1) for l in open(os.path.join(printer.state, "payload", "payload.env")).read().splitlines())
    assert penv["data_seed_file"] == "data-seed-2026.10.0-kalico.tar.zst", penv
    assert os.path.isfile(os.path.join(printer.state, "payload", penv["data_seed_file"]))
    assert "flavor=kalico" in open(os.path.join(printer.state, "stage2.conf")).read()


def test_a_release_without_mcu_firmware_refuses_mcu_auto(printer):
    keep_stock_bundle(printer)
    for mode in ("auto", "manual"):
        r = printer.install("--flavor", "klipper", "--mcu", mode)
        assert r.returncode == 12 and "carries no MCU firmware" in r.stdout


def test_a_release_without_mcu_firmware_must_say_so(printer):
    doc = printer.install_doc
    del doc["files"]["mcu_firmware"]
    data = json.dumps(doc).encode()
    open(os.path.join(printer.bundle, "install.json"), "wb").write(data)
    open(os.path.join(printer.bundle, "install.json.minisig"), "w").write(printer.signer.sign(data))
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 12 and "keep-stock" in r.stdout


def test_a_dev_bundle_is_announced(printer):
    doc = printer.install_doc
    doc["channel"] = "dev"
    data = json.dumps(doc).encode()
    open(os.path.join(printer.bundle, "install.json"), "wb").write(data)
    open(os.path.join(printer.bundle, "install.json.minisig"), "w").write(printer.signer.sign(data))
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 0 and "DEVELOPMENT bundle" in r.stdout


def test_a_disk_name_without_a_trailing_digit_has_no_p_in_its_partitions(printer):
    vda = os.path.join(printer.tmp, "dev", "vda")
    shutil.copy(printer.disk, vda)
    from printerfix import STOCK_DUMP
    with open(printer.dump, "w") as fh:
        fh.write(STOCK_DUMP.format(disk=vda).replace(vda + "p", vda))
    r = printer.install("--flavor", "klipper", EITRI_DISK=vda, FAKE_ROOT_SRC=vda + "2", FAKE_BOOT_SRC=vda + "1")
    assert r.returncode == 0, r.stdout
    assert "running from the eMMC (%s2, %s1)" % (vda, vda) in r.stdout


def test_a_stock_system_without_zstd_uses_the_release_s_own(printer, tmp_path):
    # Debian 10 on the printer has no zstd, but the data seed is a .tar.zst that stage 1 checks before anything changes.
    # The release carries a static zstd (files.zstd, signed like everything else); here a copy of the host's.
    host_zstd = shutil.which("zstd")
    shutil.copy(host_zstd, os.path.join(printer.bundle, "zstd-aarch64"))
    doc = printer.install_doc
    data = open(os.path.join(printer.bundle, "zstd-aarch64"), "rb").read()
    doc["files"]["zstd"] = {"url": "zstd-aarch64", "sha256": sha256(data), "size": len(data)}
    blob = json.dumps(doc).encode()
    open(os.path.join(printer.bundle, "install.json"), "wb").write(blob)
    open(os.path.join(printer.bundle, "install.json.minisig"), "w").write(printer.signer.sign(blob))
    # a PATH that has everything the host offers except zstd
    nozstd = tmp_path / "nozstd-bin"
    nozstd.mkdir()
    seen = set()
    for d in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            src = os.path.join(d, name)
            if name in seen or name.startswith("zstd") or name in ("unzstd",) or not os.access(src, os.X_OK) or os.path.isdir(src):
                continue
            seen.add(name)
            os.symlink(src, str(nozstd / name))
    path = printer.shim_dir + os.pathsep + str(nozstd)
    r = printer.install("--flavor", "klipper", PATH=path, TMPDIR=str(tmp_path))                     # dry run
    assert r.returncode == 0 and "using the release's static zstd" in r.stdout, r.stdout
    assert not os.path.exists(printer.state)
    # a tampered zstd is refused before it is ever run
    with open(os.path.join(printer.bundle, "zstd-aarch64"), "ab") as fh:
        fh.write(b"x")
    r = printer.install("--flavor", "klipper", PATH=path, TMPDIR=str(tmp_path))
    assert r.returncode == 12 and "zstd does not match its signed checksum" in r.stdout
    open(os.path.join(printer.bundle, "zstd-aarch64"), "wb").write(data)
    bpath, digest = make_backup_file(printer)
    r = printer.install("--flavor", "klipper", "--i-have-a-backup", "--yes", "--backup-file", bpath, "--backup-sha256", digest,
                        "--confirm", "CONVERT", PATH=path)
    assert r.returncode == 0, r.stdout
    assert "unpack rules verified" in r.stdout and os.access(os.path.join(printer.state, "bin", "zstd"), os.X_OK)
