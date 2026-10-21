# SPDX-License-Identifier: Apache-2.0
"""stage2.sh with the block-device tools replaced by recording shims (no real disk is ever touched)."""
import os
import shutil
import stat
import subprocess

import pytest

from helpers import Signer
from printerfix import INSTALLER, STOCK_DUMP, Printer, sha256

SHIMS2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shims2")


class Stage2Rig:
    def __init__(self, tmp):
        self.tmp = str(tmp)
        self.fixture = Printer(tmp, Signer(tmp))               # gives us a real signed bundle
        self.root = os.path.join(self.tmp, "s2root")           # S2_ROOT: /mnt/*, /run, /www live under here
        self.devdir = os.path.join(self.tmp, "s2dev")
        os.makedirs(self.devdir)
        self.disk = os.path.join(self.devdir, "mmcblk1")
        for n in ("", "p1", "p2"):
            open(self.disk + n, "wb").close()
        self.log = os.path.join(self.tmp, "s2calls.log")
        open(self.log, "w").close()
        self.mounts = os.path.join(self.tmp, "mounts")
        open(self.mounts, "w").close()
        self.table = os.path.join(self.tmp, "table")
        with open(self.table, "w") as fh:
            fh.write(STOCK_DUMP.format(disk=self.disk))
        self.shim_dir = os.path.join(self.tmp, "s2bin")
        shutil.copytree(SHIMS2, self.shim_dir)
        self.payload = os.path.join(self.tmp, "payload")
        shutil.copytree(self.fixture.bundle, self.payload)
        subprocess.run(["python3", os.path.join(INSTALLER, "tools", "prepare_payload.py"), "finalize", "--install-json",
                        os.path.join(self.payload, "install.json"), "--flavor", "klipper", "--dir", self.payload], check=True)
        self._build_boot()
        self._build_stock()
        self.write_conf()

    def p(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def put(self, path, data, mode=None):
        full = self.p(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb" if isinstance(data, bytes) else "w") as fh:
            fh.write(data)
        if mode:
            os.chmod(full, mode)

    def _build_boot(self):
        # as U-Boot leaves it when it launches stage 2: still armed (a power cut must re-enter the installer), one start spent
        self.put("/mnt/boot/eitri/boot.env", "eitri_mode=stage2\neitri_slot=A\neitri_tries=3\neitri_state_A=unknown\neitri_state_B=unknown\neitri_s2_tries=1\n")
        self.put("/mnt/boot/wpa_supplicant-wlan0.conf", 'network={ssid="home"}\n')
        self.put("/mnt/boot/extlinux/extlinux.conf.stock", "label x\n")

    def _build_stock(self):
        self.put("/mnt/stock/home/mks/printer_data/config/printer.cfg", "[mcu]\nserial: /dev/ttyS2\n")
        self.put("/mnt/stock/home/mks/printer_data/config/config.mksini", "token = SECRET-QIDI-CLOUD-TOKEN\n")
        self.put("/mnt/stock/home/mks/printer_data/config/config.mksini.bak", "token = SECRET\n")
        self.put("/mnt/stock/home/mks/printer_data/config/saved_variables.cfg", "[Variables]\nz_offset = 0.1\n")
        self.put("/mnt/stock/root/.ssh/authorized_keys", "ssh-ed25519 AAAAkey1 me@pc\n")
        self.put("/mnt/stock/home/mks/.ssh/authorized_keys", "ssh-ed25519 AAAAkey1 me@pc\nssh-ed25519 AAAAkey2 other\n")
        os.makedirs(self.p("/mnt/stock/etc/systemd/system"), exist_ok=True)
        self.put("/mnt/stock/var/lib/eitri-install/backup.env", "verified=1\ndest=usb:ABCD-1234:/EITRI-BACKUP/x/mmcblk1.img.gz\ngz_sha256=" + "a" * 64 + "\n")
        self.put("/mnt/stock/var/lib/eitri-install/mcu/backup/20261021T100000Z-toolhead/meta.json", "{}")
        os.makedirs(self.p("/mnt/data"), exist_ok=True)

    def write_conf(self, **over):
        conf = {
            "operation": "convert", "disk": self.disk, "part_prefix": "p", "boot_part": "1", "stock_part": "2",
            "stock_partuuid": "6a783df4-02", "stock_fs_uuid": "5dcae443", "stock_soc": "1.7.1",
            "payload_loc": "dir:" + self.payload, "flavor": "klipper", "mcu_mode": "auto", "backup_sha256": "a" * 64,
            "kernel_cmdline": "console=tty1 loglevel=4 panic=10 net.ifnames=0",
            "s2_kernel": "/Image", "s2_fdt": "/dtb/rockchip/rk3328-roc-cc.dtb", "s2_initrd": "/eitri/stage2/initramfs.cpio.gz",
            "s2_args": "console=tty1 rdinit=/init eitri.op=convert",
        }
        conf.update(over)
        self.put("/etc/eitri/stage2.conf", "".join("%s=%s\n" % kv for kv in conf.items()))

    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "PATH": self.shim_dir + os.pathsep + os.environ["PATH"],
            "S2_LIB": os.path.join(INSTALLER, "lib"), "S2_ROOT": self.root, "S2_PART_FMT": self.disk + "p%s",
            "S2_NO_SYSMOUNT": "1", "S2_NO_NET": "1", "S2_NO_REBOOT": "1",
            "FAKE_LOG": self.log, "FAKE_TABLE": self.table, "FAKE_MOUNTS": self.mounts, "FAKE_PART_DIR": self.devdir,
            "NO_COLOR": "1", "S2_SEED_INSTALL": os.path.join(INSTALLER, "bin", "eitri-seed-install"),
        })
        env.update(extra)
        return env

    def run(self, **env):
        return subprocess.run(["bash", os.path.join(INSTALLER, "stage2", "stage2.sh")], env=self.env(**env),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)

    def calls(self):
        return open(self.log).read().splitlines()

    def boot_env(self):
        return dict(l.split("=", 1) for l in open(self.p("/mnt/boot/eitri/boot.env")).read().replace("\0", "").splitlines() if "=" in l)

    def status(self):
        import json
        return json.load(open(self.p("/www/status.json")))


@pytest.fixture
def rig(tmp_path):
    return Stage2Rig(tmp_path)


DESTRUCTIVE = ("sfdisk-apply", "blockdev-rereadpt", "mke2fs")


def destructive_calls(rig):
    out = [c for c in rig.calls() if c in DESTRUCTIVE or (c.startswith("resize2fs ") and not c.startswith("resize2fs -P"))]
    return out


def test_convert_success_end_to_end(rig):
    r = rig.run()
    assert r.returncode == 0, r.stdout
    calls = rig.calls()
    # order: check -> shrink -> verify shrink -> partition -> reread -> make data
    seq = [c.split()[0] for c in calls if c.split()[0] in ("e2fsck", "resize2fs", "sfdisk-apply", "blockdev-rereadpt", "mke2fs")]
    assert seq == ["e2fsck", "resize2fs", "e2fsck", "sfdisk-apply", "blockdev-rereadpt", "mke2fs"], calls
    assert calls[[c.split()[0] for c in calls].index("e2fsck")] == "e2fsck -fp %sp2" % rig.disk
    shrink = [c for c in calls if c.startswith("resize2fs ")][0]
    assert shrink == "resize2fs %sp2 %ss" % (rig.disk, "21657600") or shrink.endswith("s"), shrink
    assert [c for c in calls if c.startswith("e2fsck -fn")]
    # the new partition table keeps p1/p2 starts, label id and adds p5-p7
    table = open(rig.table).read()
    assert "label-id: 0x6a783df4" in table and "p5 :" in table and "p6 :" in table and "p7 :" in table
    assert "start=32768" in table.replace(" ", "") and "p2:start=557056," in table.replace(" ", "")      # p1/p2 start sectors unchanged
    # slot A holds exactly the image; slot B was cleared; data has the seed + migrated settings
    with open(rig.disk + "p5", "rb") as fh:
        assert fh.read(len(rig.fixture.rootfs_raw)) == rig.fixture.rootfs_raw
    assert open(rig.disk + "p6", "rb").read(4 * 1024 * 1024) == b"\0" * (4 * 1024 * 1024)
    data = rig.p("/mnt/data")
    # the data seed: every component in place, recorded, flavor shipped by the seed itself
    assert open(data + "/firmware/klipper/klippy/klippy.py").read().startswith("# klipper 2026.10.0")
    assert os.path.isfile(data + "/venv/moonraker/bin/python") and os.path.isfile(data + "/eitri/klarity/venv/bin/klarity")
    assert os.path.isfile(data + "/eitri/extras/qd_dummy.py") and os.path.isfile(data + "/eitri/klipper-layer/config/printer.cfg.tmpl")
    rec = dict(l.split("=", 1) for l in open(data + "/eitri/state/seed-installed.env").read().splitlines() if "=" in l and not l.startswith("#"))
    assert rec["SEED_VERSION"] == "2026.10.0" and rec["SEED_FLAVOR"] == "klipper" and rec["SEED_MODE"] == "install"
    assert rec["SEED_ARCHIVE_SHA256"] == rig.fixture.seeds["klipper"]["sha256"] and rec["SEED_KEPT"] == ""
    assert not os.path.exists(data + "/.eitri-seed-staging") and not os.path.exists(data + "/.eitri-seed.lock")
    assert open(data + "/eitri/flavor").read().strip() == "klipper"
    assert open(data + "/eitri/release").read().strip() == "2026.10.0"
    assert open(data + "/eitri/migrate/stock-config/printer.cfg").read().startswith("[mcu]")
    assert os.path.exists(data + "/eitri/migrate/stock-config/saved_variables.cfg")
    assert not [f for f in os.listdir(data + "/eitri/migrate/stock-config") if f.startswith("config.mksini")]     # QIDI tokens never leave the old system
    keys = open(data + "/eitri/migrate/authorized_keys").read().splitlines()
    assert keys == ["ssh-ed25519 AAAAkey1 me@pc", "ssh-ed25519 AAAAkey2 other"]
    wifi = data + "/eitri/migrate/network/wpa_supplicant-wlan0.conf"
    assert stat.S_IMODE(os.stat(wifi).st_mode) == 0o600
    assert os.path.isfile(data + "/eitri/mcu/firmware/klipper/manifest.json")
    assert os.path.isdir(data + "/eitri/mcu/backup/20261021T100000Z-toolhead")
    assert os.path.exists(data + "/eitri/state/backup.env")
    inst = dict(l.split("=", 1) for l in open(data + "/eitri/state/install.env").read().splitlines())
    assert inst["flavor"] == "klipper" and inst["stock_soc"] == "1.7.1" and inst["mcu_flash"] == "auto" and inst["bundle_version"] == "2026.10.0"
    # the stock system is protected from the Armbian resize service
    link = rig.p("/mnt/stock/etc/systemd/system/armbian-resize-filesystem.service")
    assert os.path.islink(link) and os.readlink(link) == "/dev/null"
    # boot selection: layout first, the switch last
    boot = rig.p("/mnt/boot/eitri")
    assert open(boot + "/ptable.stock.sfdisk").read().startswith("label: dos")             # undo information saved
    assert rig.boot_env() == {"eitri_mode": "eitri", "eitri_slot": "A", "eitri_tries": "3", "eitri_state_A": "unknown", "eitri_state_B": "unknown"}
    layout = dict(l.split("=", 1) for l in open(boot + "/layout.env").read().splitlines())
    assert layout["eitri_partuuid_A"] == "6a783df4-05" and layout["eitri_part_B"] == "6"
    assert "phase=committed" in open(boot + "/stage2.state").read() and not os.path.exists(boot + "/LAST_ERROR")
    assert rig.status()["state"] == "done" and rig.status()["pct"] == 100


def test_corrupt_payload_stops_before_anything_destructive(rig):
    with open(os.path.join(rig.payload, "rootfs.img.zst"), "ab") as fh:
        fh.write(b"bitrot")
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "staged payload does not match its checksums" in r.stdout
    assert destructive_calls(rig) == []
    assert rig.boot_env()["eitri_mode"] == "stock"
    assert "nothing was changed" in open(rig.p("/mnt/boot/eitri/LAST_ERROR")).read()
    assert rig.status()["state"] == "failed"


def test_stock_filesystem_errors_stop_everything(rig):
    r = rig.run(FAKE_E2FSCK_RC="4", S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "nothing was changed" in open(rig.p("/mnt/boot/eitri/LAST_ERROR")).read()
    assert destructive_calls(rig) == []


def test_layout_that_does_not_fit_changes_nothing(rig):
    r = rig.run(FAKE_MIN_BLOCKS="6500000", S2_FAIL_DELAY="0")           # ~25 GB of stock data on a 28 GB disk
    assert r.returncode == 1 and destructive_calls(rig) == []
    assert "nothing was changed" in open(rig.p("/mnt/boot/eitri/LAST_ERROR")).read()


def test_resize_failure_leaves_the_partition_table_alone(rig):
    r = rig.run(FAKE_RESIZE_RC="1", S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "sfdisk-apply" not in rig.calls()
    assert rig.boot_env()["eitri_mode"] == "stock" and open(rig.table).read().count("p5") == 0


def test_sfdisk_failure_keeps_stock_bootable(rig):
    r = rig.run(FAKE_FAIL_SFDISK="1", S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "mke2fs" not in " ".join(rig.calls())
    assert rig.boot_env()["eitri_mode"] == "stock" and "sfdisk failed" in open(rig.p("/mnt/boot/eitri/LAST_ERROR")).read()
    assert rig.status()["state"] == "failed"


def test_handled_failure_disarms_the_installer(rig):
    rig.run(FAKE_FAIL_SFDISK="1", S2_FAIL_DELAY="0")
    env = rig.boot_env()
    assert env["eitri_mode"] == "stock" and env["eitri_s2_tries"] == "0"


def test_power_cut_mid_conversion_leaves_the_installer_armed_and_the_rerun_finishes(rig):
    """An unhandled stop must not look like a handled failure: boot.env keeps eitri_mode=stage2 (boot.scr re-enters
    stage 2 until its start counter is spent) and the second run resumes from the converted partition table."""
    r = rig.run(FAKE_KILL_PARENT="1")
    assert r.returncode != 0 and rig.status()["state"] == "running"
    env = rig.boot_env()
    assert env["eitri_mode"] == "stage2" and env["eitri_s2_tries"] == "1"            # nobody touched the selection
    assert "phase=rootfs_written" in open(rig.p("/mnt/boot/eitri/stage2.state")).read()
    n_before = len([c for c in rig.calls() if c == "sfdisk-apply"])
    shutil.rmtree(rig.p("/mnt/data"))
    os.makedirs(rig.p("/mnt/data"))
    assert rig.run().returncode == 0
    assert len([c for c in rig.calls() if c == "sfdisk-apply"]) == n_before          # resumed: no second repartitioning
    assert rig.boot_env()["eitri_mode"] == "eitri" and "eitri_s2_tries" not in rig.boot_env()


def _usb_backup(rig, content=b"x" * 1000, bytes_recorded=None, present=True):
    by_uuid = os.path.join(rig.tmp, "by-uuid")
    os.makedirs(by_uuid, exist_ok=True)
    if present:
        open(os.path.join(by_uuid, "ABCD-1234"), "w").close()
    rig.put("/mnt/usb/EITRI-BACKUP/x/mmcblk1.img.gz", content)
    rig.write_conf(backup_method="usb", backup_usb_uuid="ABCD-1234", backup_path="/EITRI-BACKUP/x/mmcblk1.img.gz",
                   backup_bytes=str(len(content) if bytes_recorded is None else bytes_recorded))
    return dict(S2_BY_UUID_DIR=by_uuid, S2_DEVICE_WAIT="1", S2_FAIL_DELAY="0")


def test_the_backup_is_looked_at_again_before_anything_is_shrunk(rig):
    env = _usb_backup(rig)
    r = rig.run(**env)
    assert r.returncode == 0, r.stdout
    assert "backup re-checked on the USB stick (1000 bytes)" in r.stdout


@pytest.mark.parametrize("kind", ["stick-missing", "file-missing", "wrong-size"])
def test_a_vanished_or_changed_backup_stops_before_the_shrink(rig, kind):
    env = _usb_backup(rig, bytes_recorded=999 if kind == "wrong-size" else None, present=kind != "stick-missing")
    if kind == "file-missing":
        os.unlink(rig.p("/mnt/usb/EITRI-BACKUP/x/mmcblk1.img.gz"))
    r = rig.run(**env)
    assert r.returncode == 1 and destructive_calls(rig) == []
    msg = open(rig.p("/mnt/boot/eitri/LAST_ERROR")).read()
    assert "nothing was changed" in msg.lower() or "Nothing was changed" in msg
    assert rig.boot_env()["eitri_mode"] == "stock"


def test_split_fat32_backup_is_summed_over_its_parts(rig):
    env = _usb_backup(rig)
    os.unlink(rig.p("/mnt/usb/EITRI-BACKUP/x/mmcblk1.img.gz"))
    rig.put("/mnt/usb/EITRI-BACKUP/x/mmcblk1.img.gz.part000", b"a" * 600)
    rig.put("/mnt/usb/EITRI-BACKUP/x/mmcblk1.img.gz.part001", b"b" * 400)
    assert rig.run(**env).returncode == 0


def test_bad_write_readback_never_switches_to_eitri(rig):
    # compressed checksums still match, but the decompressed image is not what payload.env promises
    penv = os.path.join(rig.payload, "payload.env")
    text = open(penv).read().replace("rootfs_raw_sha256=" + sha256(rig.fixture.rootfs_raw), "rootfs_raw_sha256=" + "0" * 64)
    open(penv, "w").write(text)
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "read-back" in open(rig.p("/mnt/boot/eitri/LAST_ERROR")).read()
    assert rig.boot_env()["eitri_mode"] == "stock"                                   # the one switch was never thrown
    assert "mke2fs" not in " ".join(rig.calls())


def test_resume_after_an_interrupted_run_reuses_the_partitions(rig):
    # a run that fails (a payload file went missing) must leave the disk exactly as it was ...
    os.rename(os.path.join(rig.payload, "data-seed-2026.10.0-klipper.tar.zst"), os.path.join(rig.payload, "seed.moved"))
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1                                                         # payload.sha256 no longer matches -> stops early
    os.rename(os.path.join(rig.payload, "seed.moved"), os.path.join(rig.payload, "data-seed-2026.10.0-klipper.tar.zst"))
    assert rig.run().returncode == 0
    n_before = len([c for c in rig.calls() if c == "sfdisk-apply"])
    # a second full run on the already converted table must not repartition or shrink again
    shutil.rmtree(rig.p("/mnt/data"))
    os.makedirs(rig.p("/mnt/data"))
    rig.put("/mnt/boot/eitri/boot.env", "eitri_mode=stock\neitri_slot=A\neitri_tries=3\neitri_state_A=unknown\neitri_state_B=unknown\n")
    before = len([c for c in rig.calls() if c.startswith("resize2fs ")])
    assert rig.run().returncode == 0
    assert len([c for c in rig.calls() if c == "sfdisk-apply"]) == n_before
    assert len([c for c in rig.calls() if c.startswith("resize2fs ")]) == before


def test_missing_tool_is_reported_before_the_disk_is_touched(rig):
    r = rig.run(S2_FAIL_DELAY="0", S2_REQUIRED_TOOLS="sfdisk definitely-not-installed-tool")
    assert r.returncode == 1 and "lacks: definitely-not-installed-tool" in r.stdout
    assert destructive_calls(rig) == []
