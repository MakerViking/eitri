# Installing Eitri

> **Pre-alpha.** There is no released Eitri image or installer yet. This document describes the intended procedure and the decisions behind it. Each step must be validated on a bench printer before the first release. Do not follow it on a printer you depend on.

## Before you start

You need:

- A **QIDI X-Plus 4**. It is the only supported printer.
- **Wired Ethernet.** Wired is the supported network path. Wireless is not supported until it has been measured on this hardware.
- A computer on the same LAN with an SSH client.
- A **full eMMC backup** of the printer, taken and checked before the first install. [`RECOVERY.md`](RECOVERY.md#1-full-emmc-backup) describes how.
- A **USB-A to USB-A cable** and the recovery procedure ready. Entering maskrom requires opening the printer. Know how before you start, not after something fails.
- Enough time to finish in one sitting. Stop only at a safe point (described below).

Do not start an install while a print is running, or when the printer is not on a stable power supply.

## Choosing the install path

| Path | Use it when | Reversible? | Needs the case open? |
|---|---|---|---|
| **1. In-place install over SSH** (primary) | You want to convert the printer you already have, and keep your current eMMC until the new system is verified | Yes, through the eMMC backup. The stock system is replaced, so going back means restoring that backup | Not required. Strongly recommended once, for the maskrom recovery proof in step 2 |
| **2. Flash-once image** (advanced) | You want a clean, reproducible image, or you are rebuilding a printer from scratch | Yes, through the eMMC backup | Yes, for maskrom entry |
| **3. microSD boot** (developers only) | You are testing Eitri from a card with the eMMC left untouched | Yes. Remove the card to return to the stock system. | Yes, the card slot is inside the printer |

The primary path does not need the case open, but a failed write to the eMMC bootloader area can only be repaired through maskrom, which does need it. Opening the case once beforehand to prove that recovery works is strongly recommended, and the installer asks you to acknowledge the risk explicitly if you skip it. Every other step runs over SSH and the network.

## Path 1: in-place install over SSH

1. **Back up first.** Take a full eMMC backup and check it. If the backup cannot be written, stop. The installer refuses to continue without one.
2. **Prove recovery (recommended).** Confirm that the maskrom recovery procedure can read the eMMC over USB. Do this before anything irreversible. If you skip it, you accept that a failed bootloader write may need the case opened later.
3. **Record the stock state.** The installer saves the stock `printer_data` and configuration, the state of the stock display and camera services, and a readback of each MCU's firmware.
4. **Write the Eitri OS to the eMMC.** The installer stages the boot area, `boot`, `rootfs.A`, `rootfs.B`, and `data` from the release image, writes each with a stage-and-verify step (write, read back, compare digest), and asks for an explicit confirmation before the one step that replaces the bootloader area. This replaces the stock system, so it is the point of no return; from here, going back means restoring the backup. This step is a design target and has not been exercised on hardware.
5. **Install the application layer on `/data`.** This installs the chosen Klipper or Kalico clone, Moonraker, the `uv` virtual environments, Klarity, the `qd_*` extras, and the camera stack. Each piece is installed at the version in `release.lock`.
6. **Link the extras.** The installer symlinks each `qd_*` module into `klippy/extras/` and hides the links through `.git/info/exclude`. It symlinks the config files into `printer_data/config`. It never copies upstream files and never edits them.
7. **Keep the stock state on record.** The stock configuration and service definitions saved in step 3 stay in the backup set, so you can inspect or restore them. They are not carried into the running system.
8. **Hand off to the configurator.** Open `http://eitri.local:8099/`. The configurator confirms the board and toolhead, probes both MCU serial ports, and flashes the MCU firmware that matches the chosen flavor. Then it walks you through calibration.

The app-layer steps (5 and 6) are **idempotent**: running them again re-creates the symlinks and does not write anything twice. The OS write in step 4 is not repeated once it has been verified.

Stop at any step that fails. Do not continue past a failed backup, a failed recovery check (if you ran one), a failed write verification, or a failed MCU readback.

## Path 2: flash-once image (advanced)

The release build produces one image for the printer's eMMC, plus its SHA-256 digest and a signature.

1. Download the image and verify its digest and signature against the release notes.
2. Enter maskrom mode by opening the printer and connecting the internal eMMC clock pin to ground, or by booting a blank eMMC. Connect the board's USB port to your computer with a USB-A to USB-A cable.
3. Write the image with `rkdeveloptool`, following [`RECOVERY.md`](RECOVERY.md#2-maskrom-and-rkdeveloptool). Record the flash information first, so the original capacity is known.
4. Boot the printer. The configurator starts at `http://eitri.local:8099/`.

The image path gives the cleanest result. It also means the eMMC is written directly, so a bad write is recovered over USB, not by re-seating a card.

## Path 3: microSD boot (developers only)

This path runs Eitri from a microSD card with the eMMC left unchanged. It requires opening the printer. It is for development and bench testing, not for users. Remove the card to return to the stock system.

## Choosing Klipper or Kalico

The configurator asks you to choose the host firmware. The choice is recorded in `/data/eitri/flavor`.

| | Klipper | Kalico |
|---|---|---|
| Upstream | `Klipper3d/klipper` | `KalicoCrew/kalico` |
| Default | **Yes** | No |
| Pinned | `master` at `a08fc79` (no release tag since v0.13.0) | `v2026.10.00` |
| Choose it when | You want the mainline project's behavior and release cadence | You want features that Kalico adds, and accept its different release cadence |

Both are pristine clones. Eitri's QIDI-specific behavior works on either. A few extras depend on internal APIs, and they have a per-base guard. Those guards are the only difference in Eitri's own code.

**Switching flavor after install is not a configuration change.** Host and MCU firmware are protocol-locked, so switching means rebuilding the MCU firmware for the new flavor and reflashing **both** boards. The configurator does this, but it takes time and it uses the same backup-and-verify steps as a first install.

## Update channels

| | Eitri release channel | Upstream mode |
|---|---|---|
| Default | **Yes** | No, opt-in |
| What it updates | Signed OS and app-layer bundles, with matching MCU firmware | `git pull` on Klipper, Moonraker, and Klarity, or any branch |
| MCU firmware | Built in CI, shipped in the bundle | Built on the device |
| Rollback | One click, to the previous slot or bundle | Manual, through git |
| Extra requirement | None | The `toolchain` add-on in `/data/eitri/toolchain`, about 300 MB (gcc and arm-none-eabi-gcc) |

Upstream mode is for people who build their own firmware, and for a project that has stopped being maintained upstream. Turning it on is a deliberate step. The system then reports that the installed versions no longer match `release.lock`.

Switching channels does not change the flavor.

## After install: verify

Work through this list before you print anything.

- [ ] Moonraker answers: `curl http://eitri.local:7125/server/info` returns JSON.
- [ ] The web interface loads at `http://eitri.local/` on port 80.
- [ ] The services are active: `systemctl status klipper moonraker klarity eitri-camera`.
- [ ] The camera responds: `curl -o /tmp/snap.jpg http://eitri.local:8080/snapshot`.
- [ ] Both MCUs are identified, and each reports the firmware version that matches your flavor and `release.lock`.
- [ ] The screen shows status, and pressing its controls does what the stock screen did.
- [ ] Heater runaway protection is active. Klipper's configuration must show `verify_heater` enabled for each heater.
- [ ] Homing completes without an error. A pre-triggered probe stops the run with an error. If you see one, stop and find out why before retrying.
- [ ] A test print of a small, known-good file completes.

## Troubleshooting

- **The installer stops at the backup step.** Fix the backup target. The installer will not continue without a verified backup.
- **An MCU readback does not match.** Do not retry in a loop. Stop, keep the log, and follow [`RECOVERY.md`](RECOVERY.md#3-mcu-recovery-over-serial).
- **The screen stays blank.** Check that `klarity.service` is running and that `serial_port` in `klarity.cfg` points at the display's UART. The default is not set; it is chosen by probing.
- **You want to go back to stock.** Restore the eMMC backup you took before installing. [`RECOVERY.md`](RECOVERY.md) has the steps.
