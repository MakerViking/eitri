# Eitri installer

Everything that gets a QIDI X-Plus 4 onto Eitri, keeps it up to date, and gets it back to stock.

| Part | What it is | Runs on |
|---|---|---|
| `install.sh` | In-place conversion of a **stock** printer over SSH (stage 1) | stock Debian 10, Python 3.7 |
| `stage2/` | The RAM-resident half of the conversion (shrinks stock, writes Eitri) | initramfs, stock kernel |
| `boot/` | U-Boot boot manager (`boot.cmd`), `mkscr.py` (writes `boot.scr` without `mkimage`) | U-Boot 2021.07 |
| `mcuflash/` | `eitri-mcuflash`: identify, back up, flash, verify the two STM32 boards | stock + Eitri |
| `eitri-update/` | `eitri-update` release-channel client, RAUC glue, `eitri-bootctl`, upstream mode | Eitri |
| `revert.sh` | Back to stock: soft, hard, reclaim, or full image restore | stock or Eitri |
| `tools/` | `prepare_payload.py`, `mkcpio.py` (stdlib only); `install-into-rootfs.sh` puts `eitri-update`, `eitri-bootctl`, their units and the release keys into an OS image tree | stock + CI |
| `docs/` | [BUNDLE](docs/BUNDLE.md) (contract with CI / OS image), [BOOT](docs/BOOT.md), [EVENTS](docs/EVENTS.md), [recovery](docs/recovery.md), [TESTING](docs/TESTING.md) | |

## Using it

```sh
# 1. look, don't touch (the default). Safe to run on a printer in use.
sudo ./install.sh --flavor klipper

# 2. convert. Both flags are required for real changes; a backup method is required too.
sudo ./install.sh --flavor klipper --i-have-a-backup --yes --backup-to-usb          # USB stick, 8 GB+
sudo ./install.sh --flavor kalico  --i-have-a-backup --yes --backup-to-host me@pc:/backups
```

What a real run does, and where you can still turn back:

1. **Preflight** (read-only): Firefly ROC-RK3328-CC device tree, stock 2-partition eMMC layout, booted *from* the
   eMMC, not printing, enough room.
2. **Backup** (required): the whole eMMC as a gzip stream to a USB stick (split into 1.9 GB parts on FAT32) or over
   ssh to your PC, then **read back and verified**. A recorded backup is accepted for 72 h. QIDI ships its own
   updates on a USB stick with a `QD_Update/` folder; the same stick works, the backup goes into `EITRI-BACKUP/`
   and leaves `QD_Update/` alone (keep it: reapplying QIDI's own package is one more way back to stock software).
3. **Stage**: the signed `install.json` is verified (minisign / Ed25519) before anything else is read; every
   payload file is checked against the signed SHA-256s.
4. **MCU recovery**: each board is reset into its bootloader over the UART, its firmware read back and written
   back (round trip), and the copy kept. If a board cannot be proven recoverable the installer stops here.
5. **You type `CONVERT`.** Nothing irreversible has happened yet.
6. **Arm stage 2**: boot manager installed, stock boot entry renamed (not deleted), reboot into a one-shot RAM
   installer that shrinks the stock root, creates rootfs A/B and `/data`, writes and verifies Eitri.
7. **First boot of Eitri** flashes both MCUs to match the host, health-checks, and only then marks the system good.
   Three failed boots fall back to the stock system automatically.

The stock system stays in its own partition and keeps booting until you remove it (`revert.sh --reclaim`).

```sh
sudo ./revert.sh                       # dry run
sudo ./revert.sh --hard --yes          # back to QIDI's boot setup + stock MCU firmware
sudo ./revert.sh --to-eitri --yes      # and forth again
sudo ./revert.sh --full --yes          # last resort: restore the whole backup image
```

## Repository layout

```
installer/
  install.sh  revert.sh
  lib/        eitri-common.sh (logging, JSON events, typed confirmation, atomic key=value files)
              layout.sh (partition planning), stage2-build.sh (RAM installer assembly)
  boot/       boot.cmd  mkscr.py
  stage2/     stage2.sh  www/index.html (progress page on :8099)
  tools/      prepare_payload.py  mkcpio.py
  bin/        eitri-mcuflash  eitri-update  eitri-bootctl   (run straight from a checkout, python -I)
  mcuflash/   Python package (+ tests)      eitri-update/   Python package, systemd units (+ tests)
  keys/       trusted minisign public keys
  tests/      shell + integration tests
```

## Status and honesty

Unit and integration tests run against fakes: simulated STM32 boards speaking both wire protocols (also through a
pty), a real U-Boot 2021.07 sandbox executing `boot.cmd` against a FAT+ext4 disk image, and shimmed block-device
tools for `install.sh` / `stage2.sh` / `revert.sh`. **Nothing has been run on a real printer.** The list of things
that need hardware is in [docs/TESTING.md](docs/TESTING.md); read it before trusting any of this with an eMMC.

License: Apache-2.0. `eitri-update`/`mcuflash` talk to Klipper only over its public wire protocol; the Katapult
protocol is re-implemented from its published behaviour (Katapult itself is GPL-3.0 and is only *run*, never copied).
