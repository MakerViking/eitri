# Testing, and what only hardware can tell us

```sh
./run-tests.sh            # bash -n on every script, py_compile + a Python 3.7 grammar check, then all pytest suites
```

| Suite | Covers | Fakes used |
|---|---|---|
| `mcuflash/tests` | Klipper wire protocol (checked against Klipper's own `msgproto.py`), Katapult protocol, probing, role binding, firmware manifests, preflight, backup, flash, verify, rollback, failure injection | simulated STM32 boards with a real flash array; a pty-backed board exercises the termios code |
| `eitri-update/tests` | minisign + pure-Python Ed25519 (RFC 8032 vectors, `cryptography` and the real `minisign` binary as oracles), manifest rules, resumable download, upgrade/post-boot/rollback, bootctl, upstream mode | file:// channel, fake `rauc`/`systemctl` |
| `tests/test_boot_script.py` | `boot/boot.cmd` executed by **U-Boot 2021.07 sandbox** (hush, `env import/export`, `fatwrite`, `ext4load`) on a real FAT+ext4 image: counters, fallbacks, one-shot stage 2 | `booti`/`sysboot` echo their arguments (no sandbox implementation) |
| `tests/test_install.py` | `install.sh` end to end: refusals, dry run changes nothing, backups, signatures, arming | PATH shims for `sfdisk blockdev findmnt lsblk blkid systemctl …`, fake root (`EITRI_ROOT`) |
| `eitri-update/tests/test_seed_delivery.py`, `test_switch_flavor.py`, `test_seed_unit.py` | a release that carries a data seed and MCU firmware: manifest and archive checks before the OS is written, install after the reboot, commit, rollback of the whole data layer (incl. interrupted runs and leftovers), `switch-flavor` both ways with every failure restoring the old flavor, hostile MCU archives | synthetic seeds in the real layout (`tests/seedpack.py`) unpacked by the **real** `seed/bin/eitri-seed-install`; fake `rauc`/`systemctl`/`eitri-mcuflash` |
| `tests/test_seed_install.py` | the seed in the in-place conversion: stage 1 checks (digest, manifest vs the signed release, pinned commit, ABI, size, unpack rehearsal, hostile members), offline use (`--bundle-dir`, `--seed-file`), stage 2 running the real seed installer and failing back to a bootable stock system | as `test_install.py` / `test_stage2.py` |
| `tests/test_stage2.py`, `test_revert_restore.py` | `stage2.sh` convert/restore incl. failure injection and resume; `revert.sh` every mode | recording shims for `sfdisk resize2fs e2fsck mke2fs mount …`; devices are plain files |

The U-Boot sandbox is not in the repo. Build it once (`make sandbox_defconfig`, enable `LEGACY_IMAGE_FORMAT`,
`CMD_SYSBOOT`, `CMD_EXT4`, `FAT_WRITE`, disable `EFI_LOADER`; `make NO_SDL=1 HOSTCFLAGS=-fcommon u-boot`) and point
`EITRI_UBOOT_SANDBOX` at the binary; those tests skip otherwise.

## Needs real hardware (not verifiable here)

0. **The data seed on a Cortex-A53.** Everything about the seed here is proven with synthetic seeds and (in `seed/`) with
   qemu-user. Unmeasured: how long stage 2 takes to unpack 55-94 MiB of zstd into 380-470 MiB on the eMMC (a few minutes
   expected), the first slow `git status` of the stat-free clone, the klippy + MCU handshake with the shipped `c_helper.so`,
   and a real `eitri-update switch-flavor` (stop services, swap trees, flash both MCUs, restart) on the machine.

1. **Which UART is which board.** The stock `printer.cfg` puts steppers/bed/chamber on `[mcu U_1]` = `/dev/ttyS0`
   and the extruder on `[mcu]` = `/dev/ttyS2`; the code binds boards from that evidence (and from an explicit
   `--assign`), and refuses to flash on the default-wiring hint alone. Confirm on the printer with
   `eitri-mcuflash plan`.
1b. **`scripts/spi_flash/fatfs.so` on Eitri.** The mainboard method needs it prebuilt or a host compiler; the base image has neither
   (nothing in `os/` or `klipper-layer/` builds it yet). Without it the mainboard can only be flashed with `sdcard-file`.
2. **The bootloader on each board.** Expected: Katapult (250000 baud) on the toolhead; the QIDI SD-card bootloader
   on the mainboard (`spi_flash.py … qidi-plus4`, board definition exists in mainline Klipper). If the toolhead
   shows no Katapult, flashing is refused and the answer is an ST-Link, not a workaround.
3. Katapult `REQUEST_BLOCK` reading the *whole* application region (the backup) and the app offset (`app_start`)
   the real bootloader reports. Also that `Request Serial Bootloader!!` at 500000 baud enters it on the stock
   firmware.
4. `spi_flash.py` with the real mainboard: SD card present/formatted, how long the SD bootloader takes (timeout is
   150 s), and whether the stock firmware's dictionary exposes `config_spi`.
5. U-Boot on the printer: `env export -t` sets `filesize` and `fatwrite` replaces `eitri/boot.env` (true in 2021.07
   sandbox and the strings of the shipped binary), `CONFIG_SYS_MAXARGS` ≥ 16 (default), `boot.scr` found at `/`
   (prefix list `/ /boot/`).
6. The RAM installer: `stage2-initramfs` with the tool list in BUNDLE.md; stock kernel modules `vfat`/`usb_storage`
   built or loadable; `eth0` for the progress page; timing of `resize2fs` shrinking ~7 GB on this eMMC.
7. The first Eitri boot: `/boot/Image` loaded from ext4 by this U-Boot, `root=PARTUUID=…-05` resolving on a dos
   table, mainline 6.18 bringing up the eMMC controller.
8. `revert.sh --reclaim` (delete partitions, grow the mounted root online) and `--full` from a USB stick.
9. Release keys: `keys/` ships no key; CI must add the real `eitri-release.pub`.
