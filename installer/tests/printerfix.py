# SPDX-License-Identifier: Apache-2.0
"""A fake stock printer for exercising install.sh / revert.sh / stage2.sh without touching a real disk."""
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import io

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.abspath(os.path.join(HERE, ".."))
SHIMS = os.path.join(HERE, "shims")

STOCK_EXTLINUX = """label Armbian
  kernel /Image
  initrd /uInitrd
  fdt /dtb/rockchip/rk3328-roc-cc.dtb
  append root=UUID=5dcae443-0796-4029-ae6c-bf5bd2a37067 rootflags=data=writeback rw no_console_suspend consoleblank=0 fsck.fix=yes fsck.repair=yes net.ifnames=0 bootsplash.bootfile=bootsplash.armbian
"""
STOCK_DUMP = """label: dos
label-id: 0x6a783df4
device: {disk}
unit: sectors

{disk}p1 : start=       32768, size=      524288, type=e
{disk}p2 : start=      557056, size=    59441152, type=83
"""
XINDI_VERSION = "[version]\nmcu             = V0.12.0\nui              = V171\nsoc             = V1.7.1\n"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def sh(*argv, **kw):
    return subprocess.run(argv, check=True, **kw)


class Printer:
    def __init__(self, tmp, signer):
        from helpers import Signer  # noqa: F401  (eitri-update test helper, already on sys.path)
        self.tmp = str(tmp)
        self.root = os.path.join(self.tmp, "root")
        self.signer = signer
        self.disk = os.path.join(self.tmp, "dev", "mmcblk1")
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()
        self.state = os.path.join(self.root, "var/lib/eitri-install")
        self._build_root()
        self._build_disk()
        self.bundle = self._build_bundle()
        self.shim_dir = os.path.join(self.tmp, "bin")
        shutil.copytree(SHIMS, self.shim_dir)

    # ---- the stock system ----------------------------------------------------------
    def p(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def write(self, path, data, mode="w"):
        full = self.p(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, mode) as fh:
            fh.write(data)

    def _build_root(self):
        self.write("/proc/device-tree/compatible", "firefly,roc-rk3328-cc\0rockchip,rk3328\0")
        self.write("/proc/device-tree/model", "Firefly ROC-RK3328-CC\0")
        self.write("/etc/hostname", "mkspi\n")
        self.write("/root/xindi/version", XINDI_VERSION)
        self.write("/boot/extlinux/extlinux.conf", STOCK_EXTLINUX)
        self.write("/boot/Image", "STOCK-KERNEL")
        self.write("/boot/dtb/rockchip/rk3328-roc-cc.dtb", "STOCK-DTB")
        self.write("/boot/wpa_supplicant-wlan0.conf", "network={ssid=\"x\"}\n")
        self.write("/var/lib/.keep", "")
        os.makedirs(self.p("/mnt"), exist_ok=True)

    def _build_disk(self):
        os.makedirs(os.path.dirname(self.disk), exist_ok=True)
        with open(self.disk, "wb") as fh:
            mbr = bytearray(512)
            mbr[440:444] = bytes.fromhex("f43d786a")       # disk signature 0x6a783df4 (little endian)
            mbr[510:512] = b"\x55\xaa"
            fh.write(bytes(mbr) + os.urandom(3 * 1024 * 1024))
        self.dump = os.path.join(self.tmp, "sfdisk.dump")
        with open(self.dump, "w") as fh:
            fh.write(STOCK_DUMP.format(disk=self.disk))

    # ---- the release bundle --------------------------------------------------------------
    def _build_bundle(self):
        b = os.path.join(self.tmp, "bundle")
        os.makedirs(b)
        rootfs_raw = os.urandom(300 * 1024)
        sh("zstd", "-q", "-o", os.path.join(b, "rootfs.img.zst"), "-", input=rootfs_raw)
        # base initramfs as CI would build it: just an /init here
        base = os.path.join(self.tmp, "base-initramfs")
        os.makedirs(base)
        with open(os.path.join(base, "init"), "w") as fh:
            fh.write("#!/bin/sh\nexec /usr/lib/eitri/stage2.sh\n")
        os.chmod(os.path.join(base, "init"), 0o755)
        sh("python3", os.path.join(INSTALLER, "tools", "mkcpio.py"), os.path.join(b, "stage2-initramfs.cpio.gz"),
           "--file", "init=%s:0755" % os.path.join(base, "init"))
        seed = io.BytesIO()
        with tarfile.open(fileobj=seed, mode="w") as tf:
            info = tarfile.TarInfo("eitri/VERSION")
            data = b"2026.10.0\n"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        sh("zstd", "-q", "-o", os.path.join(b, "data-seed.tar.zst"), "-", input=seed.getvalue())
        # MCU firmware tree in the format eitri-mcuflash reads
        mcu = os.path.join(self.tmp, "mcu-src")
        from helpers_mcu import write_release
        write_release(mcu, flavor="klipper")
        write_release(mcu, flavor="kalico")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            tf.add(mcu, arcname=".")
        sh("zstd", "-q", "-o", os.path.join(b, "mcu-firmware.tar.zst"), "-", input=buf.getvalue())

        def entry(name, **extra):
            data = open(os.path.join(b, name), "rb").read()
            e = {"url": name, "sha256": sha256(data), "size": len(data)}
            e.update(extra)
            return e

        doc = {
            "schema": 1, "version": "2026.10.0", "channel": "stable",
            "target": {"compatible": "firefly,roc-rk3328-cc"},
            "stock": {"soc": ["1.7.1"]},
            "layout": {"root_mib": 2048, "data_min_mib": 4096},
            "kernel_cmdline": "console=tty1 loglevel=4 panic=10 net.ifnames=0",
            "flavors": {"klipper": {"describe": "v0.13.0-813-ga08fc79fc", "commit": "a08fc79f"},
                        "kalico": {"describe": "v2026.10.00", "commit": "3526de3b"}},
            "files": {
                "rootfs": entry("rootfs.img.zst", raw_size=len(rootfs_raw), raw_sha256=sha256(rootfs_raw)),
                "stage2_initramfs": entry("stage2-initramfs.cpio.gz"),
                "data_seed": {"klipper": entry("data-seed.tar.zst"), "kalico": entry("data-seed.tar.zst")},
                "mcu_firmware": {"klipper": entry("mcu-firmware.tar.zst"), "kalico": entry("mcu-firmware.tar.zst")},
            },
        }
        self.rootfs_raw = rootfs_raw
        data = json.dumps(doc, indent=1).encode()
        with open(os.path.join(b, "install.json"), "wb") as fh:
            fh.write(data)
        with open(os.path.join(b, "install.json.minisig"), "w") as fh:
            fh.write(self.signer.sign(data))
        self.install_doc = doc
        return b

    # ---- running things ------------------------------------------------------------------
    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "PATH": self.shim_dir + os.pathsep + os.environ["PATH"],
            "EITRI_ROOT": self.root, "EITRI_DISK": self.disk, "EITRI_ALLOW_NONROOT": "1",
            "EITRI_REBOOT_DELAY": "0", "NO_COLOR": "1",
            "FAKE_SFDISK_DUMP": self.dump, "FAKE_LOG": self.log,
            "FAKE_ROOT_SRC": self.disk + "p2", "FAKE_BOOT_SRC": self.disk + "p1",
        })
        env.update(extra)
        return env

    def install(self, *args, **env):
        cmd = [os.path.join(INSTALLER, "install.sh"), "--bundle-dir", self.bundle, "--pubkey", self.signer.pub] + list(args)
        return subprocess.run(cmd, env=self.env(**env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)

    def calls(self):
        return open(self.log).read().splitlines()

    def snapshot(self):
        """Every file under the fake root with its content hash - to prove a dry run changed nothing."""
        out = {}
        for d, _dirs, files in os.walk(self.root):
            for f in files:
                path = os.path.join(d, f)
                out[os.path.relpath(path, self.root)] = sha256(open(path, "rb").read()) if not os.path.islink(path) else "link"
        return out
