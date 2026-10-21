# SPDX-License-Identifier: Apache-2.0
"""The bench microSD image (os/bench/mk-sd-bench.sh) booted by a real U-Boot 2021.07 (sandbox, see ubootsim.py).

The stock eMMC U-Boot of the printer runs `distro_bootcmd` with boot_targets="mmc1 mmc0 ...": the microSD slot first.
The sandbox build has the same distro scripts with boot_targets="host1 host0", so `run distro_bootcmd` with the card
bound as host 1 and a stand-in for the stock eMMC as host 0 replays the printer's boot order: devnum=1 for the card,
partition chosen by `part list -bootable`, /boot.scr found by scan_dev_for_scripts, then the card's own layout.env,
boot.env and rootfs slot.  booti has no sandbox implementation; it is replaced by an echo of the kernel arguments,
`crc32` over the loaded kernel and an `fdt get` of the device tree the kernel would get: the card's boot.scr must have
disabled the eMMC controller (/mmc@ff520000) in it, so Linux on the card never sees the eMMC.

Two images:
  * a synthetic one built by mk-sd-bench.sh in the test (small fake rootfs), always run when the sandbox exists;
  * the real build output, EITRI_SD_BENCH_IMG (default build/bench/eitri-sd-bench.img), skipped when absent.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zlib

import pytest

import ubootsim

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BUILD = os.path.abspath(os.path.join(REPO, "..", "build"))
BUILDER = os.path.join(REPO, "os", "bench", "mk-sd-bench.sh")
OVERLAY = os.path.join(REPO, "os", "board", "eitri-plus4", "rootfs-overlay")
BOOT_CMD = os.path.join(ubootsim.BOOT_DIR, "boot.cmd")
REAL_IMG = os.environ.get("EITRI_SD_BENCH_IMG", os.path.join(BUILD, "bench", "eitri-sd-bench.img"))
REAL_ROOTFS = os.environ.get("EITRI_SD_BENCH_ROOTFS", os.path.join(BUILD, "br-out", "images", "rootfs.ext4"))

DISK_ID = "e17a5d01"
STOCK_LABEL_ID = "6a783df4"           # the printer's stock eMMC (from its backup)
FDTFILE = "rockchip/rk3328-roc-cc.dtb"  # the stock U-Boot environment's fdtfile
# The stock U-Boot's load addresses (from the eMMC backup's environment).
STOCK_ADDRS = {"kernel_addr_r": "0x02080000", "fdt_addr_r": "0x01f00000", "ramdisk_addr_r": "0x06000000",
               "pxefile_addr_r": "0x00600000", "scriptaddr": "0x00500000"}

TOOLS = ("sfdisk", "mkfs.vfat", "mke2fs", "mcopy", "mtype", "mmd", "mdir", "debugfs", "dumpe2fs", "blkid", "fakeroot", "dtc", "fdtget")
pytestmark = pytest.mark.skipif(not (ubootsim.available() and all(shutil.which(t) for t in TOOLS)),
                                reason="U-Boot sandbox binary (EITRI_UBOOT_SANDBOX) or image tools missing")

ENV = dict(os.environ, MTOOLS_SKIP_CHECK="1")


def sh(*argv, **kw):
    return subprocess.run([str(a) for a in argv], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV, **kw)


def partitions(img):
    """{number: (start, size, attrs)} and the label-id, from sfdisk -d."""
    dump = sh("sfdisk", "-d", img).stdout.decode()
    parts = {}
    for line in dump.splitlines():
        m = re.match(r"\S*?(\d+) : start= *(\d+), size= *(\d+)(.*)$", line)
        if m:
            parts[int(m.group(1))] = (int(m.group(2)), int(m.group(3)), m.group(4))
    label = re.search(r"^label-id: 0x([0-9a-f]+)$", dump, re.M).group(1)
    return parts, label


def fat_read(img, start, path):
    res = subprocess.run(["mtype", "-i", "%s@@%d" % (img, start * 512), "::" + path], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=ENV)
    return res.stdout if res.returncode == 0 else None


def kv(data):
    return dict(line.split("=", 1) for line in data.decode().replace("\0", "").splitlines() if "=" in line)


def fake_emmc(path):
    """A stand-in for the stock eMMC: DOS, p1 FAT16 at 16 MiB with the stock extlinux.conf, label-id 6a783df4."""
    with open(path, "wb") as fh:
        fh.truncate(48 << 20)
    sh("sfdisk", "--quiet", path, input=("label: dos\nlabel-id: 0x%s\nunit: sectors\n1 : start=32768, size=40960, type=e\n"
                                         "2 : start=73728, size=24576, type=83\n" % STOCK_LABEL_ID).encode())
    sh("mkfs.vfat", "-F", "16", "-n", "BOOT", "--offset", "32768", path, "20480")
    sh("mmd", "-i", "%s@@%d" % (path, 32768 * 512), "::/extlinux")
    conf = os.path.join(os.path.dirname(path), "extlinux.conf")
    with open(conf, "w") as fh:
        fh.write("label Armbian\n  kernel /Image\n  fdt /dtb/rockchip/rk3328-roc-cc.dtb\n  append root=UUID=5dcae443 rw\n")
    sh("mcopy", "-i", "%s@@%d" % (path, 32768 * 512), conf, "::/extlinux/extlinux.conf")


class CardBoot:
    """A writable copy of the card, its generated boot.scr checked and then shimmed for the sandbox."""

    def __init__(self, tmp, image, copy_sectors=None, kernel=b"", shim_sysboot=False):
        self.tmp = str(tmp)
        self.card = os.path.join(self.tmp, "card.img")
        if copy_sectors:
            # Only the part up to the end of rootfs slot A is needed (and copied, sparse): the rest is not read.
            sh("dd", "if=%s" % image, "of=%s" % self.card, "bs=1M", "count=%d" % ((copy_sectors + 2047) // 2048),
               "conv=sparse", "status=none")
        else:
            shutil.copyfile(image, self.card)
        self.parts, self.label_id = partitions(image)
        self.p1 = self.parts[1][0]
        self.emmc = os.path.join(self.tmp, "emmc.img")
        fake_emmc(self.emmc)
        with open(self.emmc, "rb") as fh:
            self.emmc_digest = hashlib.sha256(fh.read()).hexdigest()

        generated = fat_read(self.card, self.p1, "/boot.scr")
        assert generated is not None, "no /boot.scr on partition 1"
        sys.path.insert(0, ubootsim.BOOT_DIR)
        try:
            import mkscr
        finally:
            sys.path.pop(0)
        text = mkscr.check(generated).decode()
        with open(BOOT_CMD) as fh:
            assert text == fh.read(), "the card's boot.scr is not installer/boot/boot.cmd"
        # booti -> print the arguments, checksum exactly the kernel bytes that were loaded, and show what the device tree
        # handed to the kernel says about the eMMC and SD controllers (boot.scr may have edited it in place).
        text = re.sub(r"(?m)^(\s*)booti \$\{kernel_addr_r\} - \$\{fdt_addr_r\}$",
                      lambda m: "%secho BOOTI bootargs=[${bootargs}]; crc32 ${kernel_addr_r} %x; %s" % (m.group(1), len(kernel), ubootsim.FDT_PROBE),
                      text)
        if shim_sysboot:
            text = re.sub(r"(?m)^sysboot ", "echo SYSBOOT: ", text)
        src = os.path.join(self.tmp, "boot.cmd")
        with open(src, "w") as fh:
            fh.write(text)
        subprocess.run([sys.executable, os.path.join(ubootsim.BOOT_DIR, "mkscr.py"), src, os.path.join(self.tmp, "boot.scr")], check=True)
        sh("mcopy", "-o", "-i", "%s@@%d" % (self.card, self.p1 * 512), os.path.join(self.tmp, "boot.scr"), "::/boot.scr")

    def set_state(self, **values):
        env = kv(fat_read(self.card, self.p1, "/eitri/boot.env"))
        env.update(values)
        path = os.path.join(self.tmp, "boot.env")
        with open(path, "w") as fh:
            fh.write("".join("%s=%s\n" % kv_ for kv_ in env.items()))
        sh("mcopy", "-o", "-i", "%s@@%d" % (self.card, self.p1 * 512), path, "::/eitri/boot.env")

    def boot(self):
        """The printer's power-on: distro_bootcmd over host1 (the card) then host0 (the eMMC)."""
        setenv = "; ".join("setenv %s %s" % kv_ for kv_ in STOCK_ADDRS.items())
        cmd = "host bind 1 %s; host bind 0 %s; %s; setenv fdtfile %s; run distro_bootcmd" % (self.card, self.emmc, setenv, FDTFILE)
        res = subprocess.run([ubootsim.SANDBOX, "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = res.stdout.decode("utf-8", "replace")
        with open(self.emmc, "rb") as fh:
            assert hashlib.sha256(fh.read()).hexdigest() == self.emmc_digest, "the eMMC stand-in was modified:\n" + out
        return out, kv(fat_read(self.card, self.p1, "/eitri/boot.env") or b"")


def crc_line(addr, data):
    """What U-Boot's crc32 prints for DATA loaded at ADDR."""
    a = int(addr, 16)
    return "%08x ... %08x ==> %08x" % (a, a + len(data) - 1, zlib.crc32(data) & 0xFFFFFFFF)


def check_eitri_boot(out, after, partuuid_a, layout, kernel, model):
    # The stock scan picked the card first, partition 1 by its bootable flag, and ran its boot.scr.
    assert "Scanning host 1:1..." in out and "Found U-Boot script /boot.scr" in out, out
    assert out.index("Scanning host 1:1") < out.index("## Executing script")
    assert "Eitri: booting slot A (partition 5, 2 attempt(s) left after this one)" in out, out
    want = "BOOTI bootargs=[root=PARTUUID=%s ro rootwait rootfstype=ext4 eitri.slot=A %s]" % (partuuid_a, layout["eitri_cmdline"])
    assert want in out, out
    # The kernel and the device tree came from the card's rootfs slot A, through the stock fdtfile name; the device
    # tree that the kernel gets has the eMMC controller disabled (Linux never sees the eMMC) and the SD slot enabled.
    assert crc_line(STOCK_ADDRS["kernel_addr_r"], kernel) in out, out
    assert "Eitri: eMMC controller /mmc@ff520000 disabled for Linux" in out, out
    assert "FDT-PROBE emmc=[disabled] sd=[okay] model=[%s]" % model in out, out
    assert out.index("disabled for Linux") < out.index("BOOTI")
    # The attempt was spent on the card's own FAT partition before the kernel would have started.
    assert after["eitri_tries"] == "2" and after["eitri_slot"] == "A" and after["eitri_mode"] == "eitri"


def check_falls_back_to_the_emmc(out):
    """Nothing loadable on the card: its boot.scr fails and the scan moves on to the untouched stock eMMC."""
    assert "BOOTI" not in out
    assert "SCRIPT FAILED: continuing..." in out, out
    assert "Scanning host 0:1..." in out and "Found /extlinux/extlinux.conf" in out, out
    assert out.index("Scanning host 1:1") < out.index("Scanning host 0:1")


# --- synthetic image -----------------------------------------------------------------------------------------------------

KERNEL = b"ARM64-KERNEL-IMAGE " * 4000
MODEL = "eitri-bench-synthetic"


def build_synthetic(tmp, emmc=True):
    """mk-sd-bench.sh over a small fake rootfs; returns (exit status, output, image, rootfs)."""
    root = tmp / "root"
    files = {
        "usr/libexec/eitri/grow-data": OVERLAY + "/usr/libexec/eitri/grow-data",
        "usr/libexec/eitri/rauc-bootloader": OVERLAY + "/usr/libexec/eitri/rauc-bootloader",
        "usr/libexec/eitri/rauc-pre-install": OVERLAY + "/usr/libexec/eitri/rauc-pre-install",
        "usr/libexec/eitri/mark-good": OVERLAY + "/usr/libexec/eitri/mark-good",
        "usr/libexec/eitri/data-init": OVERLAY + "/usr/libexec/eitri/data-init",
        "usr/lib/systemd/system-generators/eitri-generator": OVERLAY + "/usr/lib/systemd/system-generators/eitri-generator",
        "etc/rauc/system.conf": OVERLAY + "/etc/rauc/system.conf",
        "usr/lib/python3.14/site-packages/eitri_update/bootenv.py": REPO + "/installer/eitri-update/eitri_update/bootenv.py",
        "usr/lib/python3.14/site-packages/eitri_update/client.py": REPO + "/installer/eitri-update/eitri_update/client.py",
        "usr/libexec/eitri/eitri-bootctl": REPO + "/installer/bin/eitri-bootctl",
    }
    for dest, src in files.items():
        os.makedirs(os.path.dirname(str(root / dest)), exist_ok=True)
        shutil.copyfile(src, str(root / dest))
    os.makedirs(str(root / "boot" / "dtb" / "rockchip"))
    (root / "boot" / "Image").write_bytes(KERNEL)
    (root / "boot" / "rk3328-eitri-plus4.dtb").write_bytes(ubootsim.make_dtb(tmp, model=MODEL, emmc=emmc))
    os.symlink("../../rk3328-eitri-plus4.dtb", str(root / "boot" / "dtb" / "rockchip" / "rk3328-roc-cc.dtb"))
    rootfs = tmp / "rootfs.ext4"
    sh("mke2fs", "-q", "-t", "ext4", "-L", "rootfs", "-d", root, rootfs, "12M")
    img = tmp / "bench.img"
    res = subprocess.run(["bash", BUILDER, "--rootfs", str(rootfs), "--no-seed", "--root-mib", "16", "--boot-mib", "16",
                          "--headroom-mib", "8", "--output", str(img)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         env=dict(ENV, TMPDIR=str(tmp)))
    return res.returncode, res.stdout.decode(), img, rootfs


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    code, out, img, rootfs = build_synthetic(tmp_path_factory.mktemp("sdbench"))
    assert code == 0, out
    return img, rootfs


def test_builder_refuses_a_rootfs_whose_device_tree_has_no_emmc_node_to_disable(tmp_path):
    code, out, img, _ = build_synthetic(tmp_path, emmc=False)
    assert code != 0 and "no eMMC controller /mmc@ff520000" in out, out
    assert not os.path.exists(str(img))


def test_builder_writes_the_in_place_layout_with_its_own_ids(synthetic):
    img, rootfs = synthetic
    parts, label = partitions(img)
    assert label == DISK_ID and label != STOCK_LABEL_ID
    assert sorted(parts) == [1, 3, 5, 6, 7]
    assert "bootable" in parts[1][2] and all("bootable" not in parts[n][2] for n in (3, 5, 6, 7))
    assert parts[1][0] == 32768                     # nothing before 16 MiB: no Rockchip loader on the card
    ext_end = parts[3][0] + parts[3][1]
    assert all(parts[3][0] < parts[n][0] and parts[n][0] + parts[n][1] <= ext_end for n in (5, 6, 7))
    assert parts[7][0] + parts[7][1] == ext_end     # data is last, so eitri-grow-data can enlarge it
    with open(str(img), "rb") as fh:
        assert fh.read(32768 * 512)[512:].count(0) == 32768 * 512 - 512
    with open(str(img) + ".sha256") as fh:
        digest, name = fh.read().split()
    assert name == os.path.basename(str(img))
    with open(str(img), "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == digest

    p1 = parts[1][0]
    layout = kv(fat_read(str(img), p1, "/eitri/layout.env"))
    assert layout["eitri_part_A"] == "5" and layout["eitri_part_B"] == "6"
    assert layout["eitri_partuuid_A"] == DISK_ID + "-05" and layout["eitri_partuuid_B"] == DISK_ID + "-06"
    words = layout["eitri_cmdline"].split()
    assert "eitri.data=PARTUUID=%s-07" % DISK_ID in words and "eitri.bootfs=PARTUUID=%s-01" % DISK_ID in words
    assert "eitri.growdata=1" in words and not any("mmcblk" in w or "console=ttyS" in w for w in words)
    assert layout["eitri_hide_emmc"] == "1"                 # boot.scr disables the eMMC controller for Linux
    assert kv(fat_read(str(img), p1, "/eitri/boot.env")) == {
        "eitri_mode": "eitri", "eitri_slot": "A", "eitri_tries": "3", "eitri_state_A": "unknown", "eitri_state_B": "unknown"}
    assert fat_read(str(img), p1, "/extlinux/extlinux.conf") is None
    with open(str(rootfs), "rb") as fh:
        want = fh.read()
    with open(str(img), "rb") as fh:
        fh.seek(parts[5][0] * 512)
        assert fh.read(len(want)) == want, "rootfs slot A must be the built rootfs byte for byte"


def test_data_partition_carries_the_rauc_override_for_this_card_only(synthetic, tmp_path):
    img, _ = synthetic
    parts, _ = partitions(img)
    data = tmp_path / "data.ext4"
    sh("dd", "if=%s" % img, "of=%s" % data, "bs=512", "skip=%d" % parts[7][0], "count=%d" % parts[7][1], "status=none")
    label = sh("blkid", "-p", "-s", "LABEL", "-o", "value", data).stdout.decode().strip()
    assert label == "eitri-sd-data"
    conf = subprocess.run(["debugfs", "-R", "cat /eitri/state/etc/upper/rauc/system.conf", str(data)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode()
    devices = re.findall(r"(?m)^device=(.*)$", conf)
    assert devices == ["/dev/disk/by-partuuid/%s-05" % DISK_ID, "/dev/disk/by-partuuid/%s-06" % DISK_ID]
    assert "bootloader-custom-backend=/usr/libexec/eitri/eitri-bootctl" in conf
    assert "pre-install=/usr/libexec/eitri/rauc-pre-install" in conf
    assert not any("mmcblk" in line for line in conf.splitlines() if not line.lstrip().startswith("#"))
    stat = subprocess.run(["debugfs", "-R", "stat /eitri/state/etc/upper/rauc/system.conf", str(data)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode()
    assert re.search(r"User:\s+0\s+Group:\s+0", stat), stat


def test_stock_uboot_boots_the_synthetic_card_from_the_sd_slot(synthetic, tmp_path):
    img, _ = synthetic
    boot = CardBoot(tmp_path, str(img), kernel=KERNEL, shim_sysboot=True)
    out, after = boot.boot()
    layout = kv(fat_read(boot.card, boot.p1, "/eitri/layout.env"))
    check_eitri_boot(out, after, DISK_ID + "-05", layout, KERNEL, MODEL)


def test_a_card_that_cannot_boot_hands_over_to_the_stock_emmc(synthetic, tmp_path):
    img, _ = synthetic
    boot = CardBoot(tmp_path, str(img), kernel=KERNEL)       # real sysboot: extlinux.conf.stock is not on the card
    boot.set_state(eitri_tries="0")                                    # slot A out of attempts, B never good
    out, after = boot.boot()
    assert "no bootable slot left" in out and after["eitri_state_A"] == "bad"
    check_falls_back_to_the_emmc(out)


def test_eitri_mode_stock_makes_the_card_step_aside(synthetic, tmp_path):
    img, _ = synthetic
    boot = CardBoot(tmp_path, str(img), kernel=KERNEL)
    boot.set_state(eitri_mode="stock")
    out, after = boot.boot()
    check_falls_back_to_the_emmc(out)
    assert after["eitri_tries"] == "3"


# --- the real build output -----------------------------------------------------------------------------------------------

def debugfs_dump(image, path, dest):
    subprocess.run(["debugfs", "-R", "dump %s %s" % (path, dest), image], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(dest, "rb") as fh:
        return fh.read()


@pytest.mark.skipif(not (os.path.isfile(REAL_IMG) and os.path.isfile(REAL_ROOTFS)),
                    reason="no bench image (os/bench/mk-sd-bench.sh) or rootfs.ext4 built")
def test_stock_uboot_boots_the_generated_bench_image_from_the_sd_slot(tmp_path):
    parts, label = partitions(REAL_IMG)
    assert label != STOCK_LABEL_ID and "bootable" in parts[1][2]
    # Slot A is the built rootfs byte for byte, so its kernel and device tree are the reference.
    size = os.path.getsize(REAL_ROOTFS)
    h_img, h_root = hashlib.sha256(), hashlib.sha256()
    with open(REAL_IMG, "rb") as a, open(REAL_ROOTFS, "rb") as b:
        a.seek(parts[5][0] * 512)
        left = size
        while left:
            n = min(left, 8 << 20)
            h_img.update(a.read(n))
            h_root.update(b.read(n))
            left -= n
    assert h_img.digest() == h_root.digest(), "slot A of the bench image is not rootfs.ext4"
    kernel = debugfs_dump(REAL_ROOTFS, "/boot/Image", str(tmp_path / "Image"))
    dtb = debugfs_dump(REAL_ROOTFS, "/boot/rk3328-eitri-plus4.dtb", str(tmp_path / "board.dtb"))
    assert kernel[56:60] == b"ARM\x64" and dtb[:4] == b"\xd0\x0d\xfe\xed"

    model = sh("fdtget", str(tmp_path / "board.dtb"), "/", "model").stdout.decode().strip()
    assert sh("fdtget", str(tmp_path / "board.dtb"), "/mmc@ff520000", "status").stdout.decode().strip() == "okay"

    boot = CardBoot(tmp_path, REAL_IMG, copy_sectors=parts[5][0] + parts[5][1], kernel=kernel, shim_sysboot=True)
    layout = kv(fat_read(boot.card, boot.p1, "/eitri/layout.env"))
    assert layout["eitri_partuuid_A"] == "%s-05" % label and layout["eitri_part_A"] == "5" and layout["eitri_hide_emmc"] == "1"
    out, after = boot.boot()
    check_eitri_boot(out, after, "%s-05" % label, layout, kernel, model)
