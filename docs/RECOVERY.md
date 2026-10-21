# Recovery

This document explains how to get a printer back to a working state when something goes wrong. Read it before you install anything. The procedures are ordered from the least to the most drastic. Use the first one that solves your problem.

> **Pre-alpha.** These procedures describe the intended recovery design. Each one must be exercised on a bench printer before it is relied on. Mark a step as verified only after you have run it yourself on your own hardware.

## The recovery ladder

| Tier | Problem | Method | Needs the case open? | Section |
|---|---|---|---|---|
| 0 | A bad OS update | A/B rollback to the previous slot (automatic or one-click) | No | [§6](#6-software-rollback) |
| 1 | Broken app layer, or a bad config | Re-run the installer, or restore `/data` from a backup | No | [§6](#6-software-rollback) |
| 2 | Printer does not boot, but maskrom is reachable | `rkdeveloptool` over USB, restore the eMMC backup | **Yes** | [§2](#2-maskrom-and-rkdeveloptool) |
| 3 | An MCU has a damaged application | Re-flash over serial, using the bootloader | No | [§3](#3-mcu-recovery-over-serial) |
| 4 | An MCU bootloader is erased | ST-Link V2 over SWD, the worst case | **Yes** | [§4](#4-st-link-worst-case-mcu-recovery) |

The printer's internal microSD slot and the board's USB OTG port are **inside the printer**. Any tier that needs them requires opening the case. The in-place install and the software tiers do not.

## 1. Full eMMC backup

Take this backup before the first install, and keep it. It is the basis of every recovery that restores a whole system.

**What you need:** a copy of the eMMC image, stored off the printer, with its SHA-256 digest and the size of the device.

Method A, over SSH while the stock system still boots. Run it as root on the printer and write the stream to your computer:

```sh
ssh root@<printer-host> 'dd if=/dev/mmcblk1 bs=4M' > emmc-backup.img
sha256sum emmc-backup.img
```

The stock filesystem is mounted while `dd` reads it, so this image can be internally inconsistent. Stop the stock services first, or use Method B for a consistent copy. Check the backup before you rely on it:

- Its size equals the device size. Read the device size on the printer with `blockdev --getsize64 /dev/mmcblk1` and compare.
- Its partition table is readable. `sfdisk -d emmc-backup.img` prints the layout and matches the stock layout.
- Store the digest with the file, and keep two copies on separate media.

Method B, over maskrom, with `rkdeveloptool rl` (see [§2](#2-maskrom-and-rkdeveloptool)). Use it when the system will not boot.

Keep backups **out of the repository**. `.gitignore` blocks the common image extensions, but backups can still contain secrets and device identifiers.

## 2. Maskrom and rkdeveloptool

Maskrom is the RK3328's built-in USB recovery mode. The boot ROM serves a USB device on the board's OTG port. Maskrom is reached whenever the bootloader is missing or blank, or when a test point on the board is shorted to ground during power-up. **On this printer, the OTG port and the test point are inside the case, so this path requires opening the printer.**

### What you need

- A Linux computer with `rkdeveloptool` built from the upstream project (`rockchip-linux/rkdeveloptool`), and a udev rule that gives you access to the Rockchip USB device (vendor ID `2207`).
- A USB-A to USB-A cable. **Connect only D+, D−, and GND. Never connect VCC.** Connecting VCC between two hosts can damage both.
- The RK3328 loader that matches the board. Eitri does not ship Rockchip's loader in this repository. Obtain it from the matching Eitri release bundle or from Rockchip's public loader repository, and confirm its redistribution terms before you copy it anywhere.

### Procedure

1. Power the printer off and disconnect the mains.
2. Open the case and locate the eMMC clock line and a ground pad on the board. Short them together with a tweezer or a jumper while you apply power. The location of the test point or clock line on the printer's board has not been verified on this unit; identify it from the board before shorting anything. Hold the short until the host sees the device, then release it.
3. On the host, confirm the device:

   ```sh
   lsusb | grep -i 2207
   rkdeveloptool ld
   ```

4. Load the loader into the SoC's SRAM:

   ```sh
   rkdeveloptool db <rk3328-loader.bin>
   ```

5. Read the flash information, and record it. It gives the capacity you need for the backup and restore steps:

   ```sh
   rkdeveloptool rfi
   ```

6. Back up the whole eMMC. Use the sector count from `rfi`:

   ```sh
   rkdeveloptool rl 0 <total-sectors> emmc-backup.img
   sha256sum emmc-backup.img
   ```

7. Restore a known-good image. This writes the bootloader area as well, so only use an image you have verified:

   ```sh
   rkdeveloptool wl 0 emmc-backup.img
   rkdeveloptool rd
   ```

The command names above are from the upstream tool. Check `rkdeveloptool help` for the exact syntax of your version before you run a write.

Confirm maskrom entry and a successful `rfi` on your unit **before** the first destructive step of any install. That is the gate for every irreversible action in this project.

## 3. MCU recovery over serial

The two MCUs are reached through the SoC's UARTs at 500000 baud. Neither is on CAN or USB. A normal firmware update never erases the bootloader, so this tier should always be available.

1. **Identify the boards.** Do not assume `/dev/ttyS0` or `/dev/ttyS2` is a particular board. The configurator probes each port and reads the identity from the bootloader or the application.
2. **Back up the current firmware** for the board you are about to touch, and read it back. Keep the copy with the release you were running.
3. **Enter the bootloader.** The mainboard (answers as `stm32f401xc`) has the stock QIDI/Makerbase bootloader: it is entered by the SD-card path, never through Katapult. A toolhead (answers as `stm32f103xe`) is entered through Katapult (`flashtool.py`; the baud rate is compile-time, 250000 or 500000, so try both) **only if Katapult actually answers**. If it does not, stop: the toolhead's stock bootloader and application offset are unresolved (`mcu/README.md`), `eitri-mcuflash` refuses to write it, and first-time Katapult installation needs an ST-Link.
4. **Flash the application** that matches your flavor and `release.lock`, linked for the board's own bootloader offset (mainboard `0x08008000`; toolhead whatever Katapult reports). Use `flash-sdcard.sh`-style flashing at 500000 baud for the mainboard, and `flashtool.py` for a Katapult toolhead. Never write below the application offset.
5. **Read back and verify** the digest of what you wrote. A verification failure is an error. Do not ignore it, and do not retry in a loop.

If the bootloader does not answer, stop. Go to [§4](#4-st-link-worst-case-mcu-recovery) only if you have confirmed that the bootloader is actually gone.

## 4. ST-Link (worst case MCU recovery)

An ST-Link V2 is the last resort for a board whose bootloader has been erased or corrupted. It is not part of a normal install. It requires soldering or clipping to the board's SWD pads, which is inside the printer.

Before you connect anything, do these:

1. Confirm the ST-Link is detected and talks to the target. Use `st-info --probe` from `stlink-tools`, or STM32CubeProgrammer. Do not erase anything until this works.
2. Read the **whole flash** from the target and save it, with its digest. This is your only record of the board's firmware.
3. Confirm the read-out protection level. If read-out protection is set to level 1, a mass erase destroys the flash contents. Plan around that, and do not change the level without a backup.
4. Connect only the SWD signals the board documents (SWDIO, SWCLK, GND, and reset if the board uses it). Do not connect the 3.3 V supply unless the board is unpowered and you know it needs it.

Then write the bootloader back, and verify it by reading it back. Only after the bootloader is working should you flash the application over serial, as in [§3](#3-mcu-recovery-over-serial).

Erasing a bootloader without a verified backup of the flash is the one thing that can make a board unrecoverable without a new board. Do not do it.

## 5. microSD boot (developers only)

The printer's internal microSD slot lets you boot an Eitri image from a card while the eMMC keeps the stock system. Pulling the card returns the printer to stock. The stock eMMC U-Boot (2021.07-armbian, read from an eMMC backup) scans the microSD slot (`mmc1`) before the eMMC (`mmc0`) and runs a `boot.scr` it finds on the card's bootable partition, so a card built with `os/bench/mk-sd-bench.sh` boots Eitri without touching the eMMC (see `os/bench/README.md`). If Eitri on the card cannot boot, the card's boot script gives up and the stock system on the eMMC starts. This has been replayed in a U-Boot 2021.07 sandbox, not yet on a printer. The flash-once GPT image (`eitri-plus4.img`) is different: it carries its own loader, and whether that loader on a card wins over the eMMC's is still unverified.

This is a development tool. It needs the case open, and it is not a user-facing install path.

## 6. Software rollback

- **A/B rollback (tier 0).** Each OS update writes to the inactive root filesystem slot. If the new slot does not come up healthy, the system boots the previous slot. You can also choose the previous slot manually, through the update interface or RAUC.
- **Reinstall the app layer (tier 1).** Re-run the installer. It re-creates the symlinks and the venvs at the versions in `release.lock`. It never writes the same file twice.
- **Restore `/data` (tier 1).** Restore `/data` from a backup taken before the change. Stop the services before you restore, and start them again afterward.
- **Return to stock.** Restore the eMMC backup taken before the install (over SSH if Eitri still boots, otherwise through maskrom).

## Before you start any install

Record this, and keep the record with the backup:

- [ ] The full eMMC backup exists, has a digest, and has been checked.
- [ ] Maskrom entry has been reached and `rfi` read successfully on this unit.
- [ ] The MCU firmware backups exist, with digests, for both boards.
- [ ] You know which release, flavor, and `release.lock` the backup corresponds to.
- [ ] You have a USB-A to USB-A cable, and you know how to enter maskrom.

## Things never to do

- Never erase an MCU bootloader without a verified backup of the flash.
- Never flash an image or firmware whose digest you have not checked.
- Never flash toolhead firmware built for a different flavor or protocol version.
- Never connect VCC on a USB-A to USB-A cable.
- Never ignore a failed readback or verification.
- Never disable heater runaway protection to get a printer to boot or flash.
- Never store a backup, a dump, or a stock firmware image in this repository.
