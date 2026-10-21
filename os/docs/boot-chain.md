# Boot chain

How the QIDI X-Plus 4 gets from power-on to Linux in Eitri OS, what was
verified without the printer, and what is still open. Nothing here has been
booted on the printer yet.

## The chain

```
RK3328 BootROM
  tries microSD first, then eMMC; loads the idbloader from sector 64
    idbloader.img        U-Boot TPL (DRAM init) + SPL        32 KiB offset
      u-boot.itb         U-Boot proper + TF-A BL31           8 MiB offset  (sector 16384)
        BL31             Trusted Firmware-A v2.15.0, rk3328, stays resident (PSCI)
        U-Boot           v2026.10, roc-cc-rk3328_defconfig + board/eitri-plus4/uboot.fragment
          uboot.env      the compiled-in boot script (never stored)
            loads /boot/Image and /boot/rk3328-eitri-plus4.dtb from the ext4 slot
              Linux 6.18.57, root=/dev/mmcblk1p{2,3}
                systemd
```

Everything is built by Buildroot from the pins in `release.lock`. The single
file `u-boot-rockchip.bin` that U-Boot also produces is not used; `genimage.cfg`
places `idbloader.img` and `u-boot.itb` separately, which is the layout the
stock eMMC and Rockchip's tools expect.

### Raw eMMC map

| Sector | Offset | Contents | Owner |
|---:|---|---|---|
| 0-33 | 0 | protective MBR, GPT header and entries | genimage, `grow-data` |
| 64 | 32 KiB | `idbloader.img` (about 120 KiB) | image, bootloader updates |
| 8128 | 3.97 MiB | one sector of A/B boot state | U-Boot, `rauc-bootloader` |
| 16384 | 8 MiB | `u-boot.itb` (about 1 MiB) | image, bootloader updates |
| 32768 | 16 MiB | partition 1 `boot` (FAT, 32 MiB) | image |
| 98304 | 48 MiB | partition 2 `rootfs-a` (ext4, 768 MiB) | RAUC |
| 1671168 | 816 MiB | partition 3 `rootfs-b` (ext4, 768 MiB) | RAUC |
| 3244032 | 1584 MiB | partition 4 `data` (ext4, grown to the end of the disk on first boot) | the printer |

In the stock image's eMMC backup (read, not modified) the area before the
first partition holds nonzero data only in sectors 0-7, 64-271 (Rockchip
idbloader) and 16384-29000 (the stock `LOADER`, `uboot.img` and `trust.img`
areas); the stock partitions start at sector 32768. Nothing lies between sector
272 and sector 16384, so the boot state sector at 8128 collides with nothing in
the stock layout.

### A/B state

The U-Boot environment is **not** stored (`CONFIG_ENV_IS_NOWHERE`). A saved
environment would shadow the boot script after every U-Boot update. The three
values that have to persist (`BOOT_ORDER`, `BOOT_A_LEFT`, `BOOT_B_LEFT`, plus
the marker `EITRI_STATE=1`) live in the text sector at 8128. U-Boot reads and
writes it with `mmc read/write` and `env import/export -t`;
`/usr/libexec/eitri/rauc-bootloader` is RAUC's custom bootloader backend for the
same sector. The format is what `env export -t` writes, so there is no second
parser to keep in step. A sector that is blank, garbled or without the marker
means the defaults (order `A B`, three attempts each).

Booting from a microSD card (bench mode) always runs slot A of the card and
never writes the state; `rauc-bootloader` refuses to write unless `eitri.disk=`
names the disk that holds the sector (so also when another boot manager started
the system and passed no `eitri.disk=` at all), and `mark-good` does nothing
when `eitri.disk=` names another disk.

## What was verified without the printer

| Item | How | Result |
|---|---|---|
| TF-A v2.15.0, `PLAT=rk3328 bl31` | built with an aarch64 gcc 14.3 cross toolchain | builds; no 32-bit toolchain needed |
| U-Boot v2026.10 with `uboot.fragment` and `uboot.env` | built with the same toolchain and that BL31 | produces `idbloader.img` (120 KiB) and `u-boot.itb` (1 MiB) |
| U-Boot with the rkbin DRAM blob (`uboot-ddrbin.fragment`) | built with `rk3328_ddr_333MHz_v1.21.bin` | produces both images |
| `uboot.env` slot selection | run in the U-Boot sandbox | correct for fresh state, exhausted slot, B-first order, all slots exhausted, forced slot |
| `env export -t` / `env import -t` round trip | U-Boot sandbox | the 512-byte sector reads back exactly |
| Kernel fragment | merged into the arm64 defconfig of 6.18.57 | all 215 settings stick (`scripts/check-kconfig-fragment.sh`) |
| `rk3328-eitri-plus4.dts` | compiled with the kernel's dtc, `W=1` | no warnings |
| Partition layout | `genimage` 19 on stand-in inputs | offsets and order as above |
| Partition growth | `grow-data` against a file-backed GPT disk | partition 4 reaches the last usable sector, backup GPT moves |

## The board is a Makerbase MKS-PI, not a ROC-RK3328-CC

The stock system's device tree (decoded from the stock image's `/boot`) names
the board "Makerbase mks-pi" and keeps `firefly,roc-rk3328-cc` as its
compatible. The mainline `rk3328-roc-cc.dts` is therefore the base, with these
overrides in `board/eitri-plus4/dts/rk3328-eitri-plus4.dts`, each taken from the
stock tree:

| Difference | ROC-CC (mainline) | MKS-PI (stock) |
|---|---|---|
| Ethernet | `gmac2io`, external RGMII PHY | `gmac2phy`, the SoC's internal 100 Mbit PHY |
| `uart0`, `uart1` | disabled | enabled (`uart0` without RTS) |
| Console | `stdout-path = serial2:1500000n8` | none |
| `vcc_sd` enable | GPIO0_PD6, active low | GPIO0_PD6, active high |
| `vcc_host1_5v` (USB VBUS) | GPIO1_PD2 | GPIO0_PA2 |
| SD I/O voltage select | `grf_gpio` 0, 1.8 V when high | GPIO2_PC4, 1.8 V when low |

The USB VBUS line matters most: with the ROC-CC pin the camera would not be
powered.

## Open questions

These need the printer. They are the same ones listed in the architecture
document, with the detail the OS side adds.

### 1. DRAM initialisation: open TPL or Rockchip's blob (Q3)

The default image uses U-Boot's in-tree TPL, which initialises DRAM from the
parameters in `rk3328-sdram-ddr4-1600.dtsi`: **DDR4 at 800 MHz (DDR4-1600)**.
That file belongs to the ROC-RK3328-CC. Whether the MKS-PI has the same DRAM
type and speed is not known. The stock bootloader carries Rockchip's generic
DRAM blob (`DDR version V1.16 20190528`, found in the backup's idbloader), which
detects the memory itself and so says nothing about the type.

What the fallback is: `make -C os ddrbin-config` builds with
`rk3328_ddr_333MHz_v1.21.bin` from Rockchip's `rkbin` repository (pinned by
Buildroot, never committed here). The blob is proprietary, which is why it is a
fallback and not the default. It builds and produces the same image layout.

How to decide, safely:

1. Build the default image and write it to a **microSD card**. The BootROM
   tries the card before the eMMC, and nothing on the card path touches the
   eMMC. A DRAM-init failure then costs nothing.
2. If the printer comes up on the network (the board answers on its
   Ethernet address), the in-tree TPL works. Run a memory test from Linux
   (`memtester`, or a long Klipper job on the bench) before trusting it.
3. If it does not come up, build with `ddrbin-config` and repeat. If that boots,
   the in-tree parameters do not match this DRAM.
4. Only after one of the two is proven on the card, write the eMMC. A wrong
   bootloader on the eMMC is recoverable through Rockchip maskrom, which needs
   the case opened; see `docs/RECOVERY.md`.

Seeing why a boot fails needs the console, which is on `uart2`, the line that
goes to an MCU. Connecting a USB-UART for that means opening the printer.

### 2. Does the microSD card win over the eMMC?

The bench workflow in section 1 assumes the RK3328 BootROM tries the card before
the eMMC (Firefly documents it that way for the ROC-RK3328-CC), so a card
carrying `idbloader.img` and `u-boot.itb` starts this image while the stock
eMMC stays untouched. It is not verified on this printer (`docs/RECOVERY.md`
section 5). If the BootROM prefers a valid eMMC loader, the stock U-Boot starts
first and looks for `extlinux/extlinux.conf` or `boot.scr` on the card, which
this image does not carry, so the card would not boot.

The printer's stock eMMC U-Boot (2021.07-armbian, read from the eMMC backup) has
`bootcmd=run distro_bootcmd` and `boot_targets=mmc1 mmc0 usb0 pxe dhcp`, and on
the RK3328 mmc1 is the microSD slot: it scans the card first and runs a
`boot.scr` it finds there. `os/bench/mk-sd-bench.sh` builds a card for exactly
that path (no Rockchip loader on the card, the in-place `boot.scr`, the card's
own partitions by PARTUUID), so the open question above does not block bench
work: see [`../bench/README.md`](../bench/README.md).

### 3. Boot messages on the MCU line

U-Boot's TPL and SPL print on `uart2` at 1.5 Mbaud (`CONFIG_DEBUG_UART`), and
the main U-Boot is silenced with `silent=1`. The kernel has no console. Whether
the TPL and SPL output disturbs the MCU wired to `uart2` is untested; Klipper's
and Katapult's framing should discard it. If it does cause trouble,
`CONFIG_DEBUG_UART`, `CONFIG_SPL_SERIAL` and `CONFIG_TPL_SERIAL` can be turned
off in `uboot.fragment` at the price of a blind bring-up.

### 4. Which MKS-PI pins the other boards use

The device tree copies the regulator and pin choices of the stock tree. Pins
that the stock tree leaves unconfigured keep the ROC-CC defaults. Anything on the
40-pin header that the QIDI mainboard uses (fans, chamber sensors) is not
described, because Klipper drives those through the MCUs.

### 5. U-Boot source

`ftp.denx.de/pub/u-boot/u-boot-2026.10.tar.bz2` did not exist on 2026-10-21
although the `v2026.10` tag did, so the defconfig fetches the GitHub tag
archive and pins its SHA-256 in `board/eitri-plus4/patches/uboot/uboot.hash`.
The tarball's embedded commit is `5508406582f6...`, the value in `release.lock`.
Switch back to the Denx tarball once it is published.

### 6. TF-A

v2.15.0 builds for `rk3328` with a plain aarch64 toolchain. It has not run on
hardware. If it misbehaves, `lts-v2.12.1` is the fallback named in
`release.lock`: set `BR2_TARGET_ARM_TRUSTED_FIRMWARE_CUSTOM_VERSION_VALUE` to
that tag. Buildroot's own RK3328 reference configuration (`nanopi_r2s`) builds
v2.12 and also turns on `BR2_TARGET_ARM_TRUSTED_FIRMWARE_NEEDS_ARM32_TOOLCHAIN`;
the v2.15.0 build here did not need it.

### 7. eMMC speed mode

The stock tree and mainline `rk3328-roc.dtsi` both enable `mmc-hs200-1_8v` and
`mmc-ddr-1_8v` for the eMMC, so Linux should reach HS200. Not measured.
