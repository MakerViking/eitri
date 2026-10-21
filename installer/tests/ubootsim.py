# SPDX-License-Identifier: Apache-2.0
"""Run boot.cmd inside a real U-Boot (sandbox build of 2021.07, the version on the printer).

The sandbox binary is not part of the repo; point EITRI_UBOOT_SANDBOX at a
`make sandbox_defconfig` build (needs CONFIG_LEGACY_IMAGE_FORMAT, CMD_SYSBOOT,
CMD_EXT4, FAT_WRITE).  booti has no sandbox implementation, so it is replaced by
an echo of its arguments; everything else (hush, env import/export, load,
ext4load, fatwrite) runs for real against a disk image.
"""
import os
import re
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
BOOT_DIR = os.path.join(HERE, "..", "boot")
DEFAULT_SANDBOX = "/home/thomash/Koding/projects/Plus4/build/uboot-sandbox/u-boot-2021.07/u-boot"
SANDBOX = os.environ.get("EITRI_UBOOT_SANDBOX", DEFAULT_SANDBOX)
FAT_OFFSET = 2048 * 512


# What the kernel would have been handed: the status of the eMMC (mmc1, /mmc@ff520000) and SD (mmc0, /mmc@ff500000)
# controllers in the device tree at ${fdt_addr_r}, and its model.
FDT_PROBE = ("setenv probe_emmc; setenv probe_sd; setenv probe_model; fdt addr ${fdt_addr_r}; "
             "fdt get value probe_emmc /mmc@ff520000 status; fdt get value probe_sd /mmc@ff500000 status; "
             "fdt get value probe_model / model; echo FDT-PROBE emmc=[${probe_emmc}] sd=[${probe_sd}] model=[${probe_model}]")

TEST_DTS = """/dts-v1/;
/ {
	model = "%(model)s";
	compatible = "firefly,roc-rk3328-cc", "rockchip,rk3328";
	#address-cells = <2>;
	#size-cells = <2>;
	aliases { mmc0 = "/mmc@ff500000"; mmc1 = "/mmc@ff520000"; };
	mmc@ff500000 { compatible = "rockchip,rk3328-dw-mshc"; reg = <0x0 0xff500000 0x0 0x4000>; status = "okay"; };
%(emmc)s};
"""
EMMC_NODE = """\tmmc@ff520000 { compatible = "rockchip,rk3328-dw-mshc"; reg = <0x0 0xff520000 0x0 0x4000>; non-removable; status = "okay"; };\n"""


def make_dtb(tmpdir, model="eitri-test-board", emmc=True):
    """A small real device tree with the RK3328's SD and eMMC controller nodes (needs dtc)."""
    src = os.path.join(str(tmpdir), "test-%s.dts" % model)
    out = src[:-4] + ".dtb"
    with open(src, "w") as fh:
        fh.write(TEST_DTS % {"model": model, "emmc": EMMC_NODE if emmc else ""})
    subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", out, src], check=True)
    with open(out, "rb") as fh:
        return fh.read()


def available():
    return os.path.isfile(SANDBOX) and all(shutil.which(t) for t in ("sfdisk", "mkfs.vfat", "mke2fs", "mcopy", "mtype"))


def parse_env(text):
    out = {}
    for line in text.replace("\0", "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


class BootSim:
    STATE_KEYS = ("eitri_mode", "eitri_slot", "eitri_tries", "eitri_state_A", "eitri_state_B", "eitri_s2_tries")

    def __init__(self, tmp, boot_env=None, shim_sysboot=True, slot_a=True, slot_b=True, stage2=True, script=None, booti_returns=True,
                 dtb=None, probe_fdt=False, extra_boot_env=None, extra_layout_env=None):
        """dtb: bytes of a real device tree for the slots (default: a text stand-in).  probe_fdt: after the shimmed booti,
        print the status of the eMMC and SD controller nodes in the device tree at ${fdt_addr_r} (FDT-PROBE ...).
        extra_boot_env / extra_layout_env: raw lines appended to boot.env / layout.env (injection tests)."""
        self.dir = str(tmp)
        self.img = os.path.join(self.dir, "disk.img")
        self.env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
        self.boot_env = boot_env
        self.dtb = dtb
        self.probe_fdt = probe_fdt
        self.extra_boot_env = extra_boot_env or ""
        self.extra_layout_env = extra_layout_env or ""
        self._build(shim_sysboot, slot_a, slot_b, stage2, script, booti_returns)

    def _run(self, *argv, **kw):
        return subprocess.run(argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self.env, **kw)

    def _mcopy(self, src, dest):
        self._run("mcopy", "-o", "-i", self.img + "@@%d" % FAT_OFFSET, src, "::" + dest)

    def _build(self, shim_sysboot, slot_a, slot_b, stage2, script, booti_returns=True):
        with open(self.img, "wb") as fh:
            fh.truncate(64 * 1024 * 1024)
        sfdisk = "label: dos\nunit: sectors\nstart=2048, size=40960, type=e\nstart=43008, size=40960, type=83\nstart=83968, size=40960, type=83\n"
        self._run("sfdisk", self.img, input=sfdisk.encode())
        self._run("mkfs.vfat", "-F", "16", "--offset", "2048", self.img, "20480")
        for name, enabled, off in (("A", slot_a, 43008), ("B", slot_b, 83968)):
            root = os.path.join(self.dir, "root" + name)
            os.makedirs(os.path.join(root, "boot", "dtb", "rockchip"))
            if enabled:
                with open(os.path.join(root, "boot", "Image"), "w") as fh:
                    fh.write("KERNEL-" + name)
                with open(os.path.join(root, "boot", "dtb", "rockchip", "rk3328-roc-cc.dtb"), "wb") as fh:
                    fh.write(self.dtb if self.dtb is not None else ("DTB-" + name).encode())
            self._run("mke2fs", "-q", "-t", "ext4", "-d", root, "-E", "offset=%d" % (off * 512), self.img, "20480k")
        for d in ("eitri", "eitri/stage2", "extlinux", "dtb", "dtb/rockchip"):
            self._run("mmd", "-i", self.img + "@@%d" % FAT_OFFSET, "::/" + d)
        files = {"extlinux/extlinux.conf.stock": "label stock\n  kernel /Image\n"}
        if stage2:
            files.update({"Image": "STOCK-KERNEL", "dtb/rockchip/rk3328-roc-cc.dtb": "STOCK-DTB",
                          "eitri/stage2/initramfs.cpio.gz": "x" * 1000})
        if self.boot_env is not None:
            state = {k: v for k, v in self.boot_env.items() if k in self.STATE_KEYS}
            layout = {k: v for k, v in self.boot_env.items() if k not in self.STATE_KEYS}
            files["eitri/boot.env"] = "".join("%s=%s\n" % kv for kv in state.items()) + self.extra_boot_env
            files["eitri/layout.env"] = "".join("%s=%s\n" % kv for kv in layout.items()) + self.extra_layout_env
        for dest, content in files.items():
            tmpf = os.path.join(self.dir, "payload")
            with open(tmpf, "w") as fh:
                fh.write(content)
            self._mcopy(tmpf, "/" + dest)
        text = open(script or os.path.join(BOOT_DIR, "boot.cmd")).read()
        # A booti that works never returns.  The default shim returns (a kernel that cannot start); with
        # booti_returns=False the sandbox resets right there (its `exit` does not end a sourced script), like a kernel
        # that took over and then hung.
        probe = "; " + FDT_PROBE if self.probe_fdt else ""
        if booti_returns:
            text = re.sub(r"(?m)^(\s*)booti (.*)$", lambda m: "%secho BOOTI bootargs=[${bootargs}] ARGS: %s%s" % (m.group(1), m.group(2), probe), text)
        else:
            text = re.sub(r"(?m)^(\s*)booti (.*)$", lambda m: "%secho BOOTI bootargs=[${bootargs}] ARGS: %s%s; reset" % (m.group(1), m.group(2), probe), text)
        if shim_sysboot:
            text = re.sub(r"(?m)^sysboot ", "echo SYSBOOT: ", text)
        scr_src = os.path.join(self.dir, "boot.cmd")
        with open(scr_src, "w") as fh:
            fh.write(text)
        subprocess.run(["python3", os.path.join(BOOT_DIR, "mkscr.py"), scr_src, os.path.join(self.dir, "boot.scr")], check=True)
        self._mcopy(os.path.join(self.dir, "boot.scr"), "/boot.scr")

    def boot(self, fdtfile="rockchip/rk3328-roc-cc.dtb"):
        """Run boot.scr once; returns (console output, boot.env afterwards as dict)."""
        cmd = (
            "host bind 0 %s; setenv devtype host; setenv devnum 0; setenv distro_bootpart 1; "
            "setenv pxefile_addr_r 0x600000; setenv scriptaddr 0x500000; setenv kernel_addr_r 0x2080000; "
            "setenv fdt_addr_r 0x1f00000; setenv ramdisk_addr_r 0x6000000; %s"
            "load host 0:1 ${scriptaddr} /boot.scr; source ${scriptaddr}" % (self.img, ("setenv fdtfile %s; " % fdtfile) if fdtfile else "")
        )
        res = subprocess.run([SANDBOX, "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        out = res.stdout.decode("utf-8", "replace")
        out = out[out.find("## Executing"):] if "## Executing" in out else out
        proc = subprocess.run(["mtype", "-i", self.img + "@@%d" % FAT_OFFSET, "::/eitri/boot.env"], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return out, parse_env(proc.stdout.decode("utf-8", "replace"))
