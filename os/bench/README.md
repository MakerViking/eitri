# Bench microSD image

`mk-sd-bench.sh` builds `../build/bench/eitri-sd-bench.img` (next to the repository) and its `.sha256`: a microSD
card that the QIDI X-Plus 4's **stock** eMMC U-Boot boots, so Eitri can run on a printer while its eMMC keeps the
stock system, untouched. Pull the card and the printer is stock again. Developers only: the slot is inside the case.

```sh
make seed && make -C os image                 # the rootfs and the data seed it is built from
os/bench/mk-sd-bench.sh                       # -> ../build/bench/eitri-sd-bench.img
os/bench/mk-sd-bench.sh --stock-backup ../downloads/emmc-backup/mmcblk1.img.gz   # check against the ids of your own eMMC
dd if=../build/bench/eitri-sd-bench.img of=/dev/sdX bs=4M conv=fsync status=progress   # you, after checking sdX
```

The script only writes the image file. It never opens a block device; which device is the card is your call.

## How the stock U-Boot gets to it

The stock U-Boot (2021.07-armbian, July 2022, read from the printer's eMMC backup) runs `distro_bootcmd` with
`boot_targets=mmc1 mmc0 usb0 pxe dhcp`. On the RK3328 `mmc1` is the microSD slot, so the card is scanned first:

1. `part list -bootable` picks partition 1 (the only one with the bootable flag).
2. With no `extlinux/extlinux.conf` there, `scan_dev_for_scripts` finds `/boot.scr` and sources it. That is the
   in-place boot manager, `installer/boot/boot.cmd`, wrapped by `installer/boot/mkscr.py`, unchanged.
3. `boot.scr` reads `eitri/layout.env` and `eitri/boot.env` from the card, spends one boot attempt (written back to
   the card with `fatwrite`), and loads `/boot/Image` and `/boot/dtb/${fdtfile}` from rootfs slot A on the card.
   The stock environment has `fdtfile=rockchip/rk3328-roc-cc.dtb`; the rootfs ships that name as a link to
   `rk3328-eitri-plus4.dtb`, so nothing is set or overridden. Load addresses are the stock ones
   (`kernel_addr_r=0x02080000`, `fdt_addr_r=0x01f00000`).
   The card's `eitri/layout.env` has `eitri_hide_emmc=1`, so before `booti` the script runs
   `fdt addr ${fdt_addr_r}; fdt resize; fdt set /mmc@ff520000 status disabled` on the loaded device tree: the eMMC
   controller (Linux's `mmc1`) is switched off and **Linux on the card never sees the eMMC**, no `mmcblk` device,
   nothing a bug could write to. If any of the three commands fails, Eitri is not booted (the attempt is already
   spent; the scan moves on to the stock eMMC). The stock U-Boot has the `fdt` command (`fdt resize`, `fdt set` are in
   its help text in the eMMC backup). The in-place eMMC install never sets `eitri_hide_emmc`.
4. Kernel arguments: `root=PARTUUID=<id>-05 ro rootwait rootfstype=ext4 eitri.slot=A console=tty1 loglevel=4 panic=10
   net.ifnames=0 eitri.data=PARTUUID=<id>-07 eitri.bootfs=PARTUUID=<id>-01 eitri.growdata=1`.

If slot A runs out of attempts (three boots that never reached `eitri-verify`), or `eitri/boot.env` says
`eitri_mode=stock`, `boot.scr` ends in a `sysboot` of `extlinux.conf.stock`, which the card does not have: the script
fails, `distro_bootcmd` moves on to `mmc0`, and the stock system on the eMMC boots. To retry Eitri, edit
`eitri/boot.env` on the card (`eitri_tries=3`, `eitri_state_A=unknown`, `eitri_mode=eitri`).

Nothing is written below sector 32768, so the card has no Rockchip loader: whichever order the boot ROM uses, the
eMMC's own loader starts.

## Layout

DOS, the partition numbers of the in-place conversion (`installer/lib/layout.sh`), label-id `0xe17a5d01`
(`--disk-id`):

| | start | size | content |
|---|---|---|---|
| p1 | 16 MiB | 64 MiB | vfat `EITRISDBOOT`, bootable: `boot.scr`, `eitri/layout.env`, `eitri/boot.env`, `README.txt` |
| p2 | | | unused (on a converted eMMC this is the shrunk stock root) |
| p3 | | | extended container |
| p5 | | 768 MiB | rootfs slot A: `rootfs.ext4` byte for byte |
| p6 | | 768 MiB | rootfs slot B: empty until the first OS update |
| p7 | | seed + 128 MiB | `/data`, ext4 `eitri-sd-data`, built from the data seed by `os/board/eitri-plus4/mkdata.sh` |

`/data` also carries `eitri/state/etc/upper/rauc/system.conf`, the persistent `/etc` layer's RAUC configuration: the
image's slots (`/dev/mmcblk1p2`, `p3`, the eMMC of the flash-once layout) are replaced with
`/dev/disk/by-partuuid/e17a5d01-05` and `-06`, and the boot-state backend with `eitri-bootctl` (the card's
`eitri/boot.env`), exactly as the in-place conversion does. On the first boot `eitri-grow-data` enlarges p3 and p7 to
the end of the card and `eitri-data-init` grows the file system.

The build refuses an image whose label-id, file-system ids or labels match the stock eMMC's (label-id `0x6a783df4`,
`BOOT` `C085-07AC`, root `5dcae443-...`, or whatever `--stock-backup` reads), and a `rootfs.ext4` whose
device-handling files (`grow-data`, `rauc-bootloader`, `rauc-pre-install`, `mark-good`, `data-init`,
`eitri-generator`, `system.conf`, `eitri_update`) differ from this tree.

## What protects the eMMC

First, the running system has no eMMC: `boot.scr` disables its controller in the device tree (see above), and the
build refuses a rootfs whose device tree has no `/mmc@ff520000` dw-mshc node marked `non-removable` with alias
`mmc1`. `boot.scr` imports only a fixed list of variables from `eitri/layout.env` and `eitri/boot.env`, so editing
those files cannot redirect its `fatwrite` (`eitri_dev`) or what it runs (`eitri_save`), and a card that hides the
eMMC never starts the stage-2 installer.

Beyond that, every part of the running system that writes a block device finds its target from the card, never from a device
name: `/data` and `/boot/firmware` are mounted by the card's PARTUUIDs, `eitri-grow-data` only repartitions the disk
holding `/` and only with a matching label-id, RAUC's slots are the card's PARTUUIDs and `rauc-pre-install` refuses
any slot on another disk, `rauc-bootloader` (the flash-once layout's raw eMMC state sector) refuses to write without
`eitri.disk=` naming its disk, and the boot state lives in the card's `eitri/boot.env`. No `install.env` is put on the
card, so `eitri-update` never flashes the MCUs on its own.

**The MCUs are a different matter.** The printer's MCUs run QIDI's firmware, which the stock system needs. Do not
flash them from the configurator (or `eitri-mcuflash`) on a bench card unless you mean to: the stock system will not
talk to Eitri's MCU firmware.

## Tests

`installer/tests/test_sd_bench.py` builds a small card with this script and boots it, and the real build output when
it exists, in a U-Boot 2021.07 sandbox (the printer's U-Boot version): `run distro_bootcmd` with the card as host 1
and a stand-in stock eMMC as host 0, checking the kernel arguments, the CRC of the loaded kernel, that the device tree
handed to the kernel has `/mmc@ff520000` `disabled` and `/mmc@ff500000` `okay`, the spent attempt, the hand-over to
the eMMC, and that the eMMC stand-in is never written. `installer/tests/test_boot_script.py` checks the other side: the
in-place eMMC install hands the device tree over unchanged, a device tree without the node is not booted, and an
`eitri_dev`/`eitri_save`/`eitri_hide_emmc` injected into `boot.env` is ignored.
