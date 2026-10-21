# Recovery ladder

From least to most drastic. Try them in this order.

| Situation | Do this |
|---|---|
| Eitri boots but you want the old system back | `sudo ./revert.sh --hard --yes` (or `--soft` to keep Eitri installed and switch back later with `--to-eitri`) |
| Eitri updates and the new system misbehaves | automatic: three failed boots, or a failed health check, return to the previous slot. Manually: `eitri-update rollback`, reboot |
| Eitri will not boot at all | nothing to do: after three attempts U-Boot boots the stock system (the stock partition is kept) |
| Stock partition damaged / want the factory disk back | `sudo ./revert.sh --full --yes` from Eitri or stock: reboots into the RAM installer, which verifies the backup image *before* writing and restores everything except the bootloader sectors. The partition table and the boot partition are written last, after the whole stream has matched its recorded checksum, so an interrupted restore re-enters the installer (three starts) instead of leaving a disk that cannot repeat it. A streamed `--from-host` restore needs the image's checksum (`--raw-sha256`) |
| The conversion was interrupted (power cut) | automatic: the RAM installer starts again (two starts) and resumes. If the starts are spent the stock system boots; `./install.sh --status` shows the stage-2 phase. Then `revert.sh --full` (stock still boots) or the maskrom row below |
| The printer does not reach U-Boot's boot step (no boot at all) | RK3328 maskrom: **opens the case.** Short the eMMC CLK pad to GND, plug USB-A to A into the OTG port (D+/D-/GND only; never VCC), then `rkdeveloptool db <rk3328 loader>; rkdeveloptool wl 0 <backup .img>` after `gunzip`. The backup is a full-disk image taken from sector 0 |
| `revert.sh --hard` stops with "cannot be put back automatically" (usually the mainboard: it is flashed through its SD-card bootloader, which needs `spi_flash.py` plus `fatfs.so` or a compiler on the printer) | Nothing was changed: both boards still run Eitri's firmware and the boot selection still says Eitri. Flash both boards by hand (Katapult toolhead: `eitri-mcuflash rollback toolhead --original --apply --yes`; mainboard: QIDI's `qd_mcu.bin` on a FAT32 microSD, case open, RESET), then run `sudo ./revert.sh --hard --yes --skip-mcu` |
| An MCU board does not answer after a flash | `eitri-mcuflash rollback toolhead --apply --yes` (Katapult boards: the saved image is written back); mainboard: copy QIDI's stock `qd_mcu.bin` onto a FAT32 microSD, insert it in the mainboard slot (case open), press RESET |
| Katapult itself is gone (toolhead; applies only if this toolhead ran Katapult before Eitri, which is not yet established for the real printer, see `mcu/README.md`) | ST-Link V2 on the board's SWD header (solder a 6-pin header), write a Katapult image at `0x08000000`. This is reached only by corrupting the bootloader region, which an application flash never touches |

## What is kept, where

| Item | Location |
|---|---|
| Full eMMC image | the USB stick / PC you chose; recorded in `/var/lib/eitri-install/backup.env`, copied to `/data/eitri/state/backup.env` |
| Original partition table | `/boot/eitri/ptable.stock.sfdisk` |
| Original boot entry | `/boot/eitri/extlinux.conf.orig` (and `extlinux.conf.stock` while Eitri is active) |
| MCU firmware before flashing | `/data/eitri/mcu/backup/<time>-<board>/app.bin` (Katapult boards, read back byte for byte); stock images you supplied in `.../stock/` |
| Everything stage 2 did | `/boot/eitri/stage2.log`, `stage2.state`, `LAST_ERROR` |
| Stock root filesystem | partition 2, shrunk but intact, fsck-clean |

Nothing is deleted by the installer. `revert.sh --reclaim` is the only operation that removes Eitri, and it asks
for the phrase `DELETE EITRI`.

## Things the installer does to the *stock* system (and how they are undone)
* renames `/boot/extlinux/extlinux.conf` → `extlinux.conf.stock`; `revert.sh --hard` renames it back;
* adds `/boot/boot.scr` and `/boot/eitri/`; `--hard` retires `boot.scr`, `--reclaim` removes the directory;
* masks `armbian-resize-filesystem.service` (otherwise the stock system would grow its root over Eitri's
  partitions on its next boot); unmasked by `--reclaim`;
* stops Klipper/Moonraker/the screen service while the disk is imaged (they are started again afterwards).
