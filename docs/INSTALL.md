# Installing Eitri

> **Pre-alpha.** There is no released Eitri image or installer yet. This document describes the intended procedure and the decisions behind it. Each step must be validated on a bench printer before the first release. Do not follow it on a printer you depend on.

## Before you start

You need:

- A **QIDI X-Plus 4**. It is the only supported printer.
- **Wired Ethernet.** Wired is the supported network path. Wireless is not supported until it has been measured on this hardware.
- A computer on the same LAN with an SSH client.
- A **full eMMC backup** of the printer, taken and checked before the first install. The installer makes one itself (to a USB stick of 8 GB or more, or over SSH to your computer) and refuses to continue without it. [`RECOVERY.md`](RECOVERY.md#1-full-emmc-backup) describes how to take and check one by hand.
- Optionally, a **USB-A to USB-A cable** and the recovery procedure, if you are willing to open the printer. Entering maskrom requires opening it. Know how before you start, not after something fails.
- Enough time to finish in one sitting. Stop only at a safe point (described below).

Do not start an install while a print is running, or when the printer is not on a stable power supply.

You do **not** need internet access on the printer after the installer has downloaded its files. Everything the printer runs is in the release you download (see [The data seed](#the-data-seed-and-the-offline-first-boot)).

## Choosing the install path

| Path | Use it when | Reversible? | Needs the case open? |
|---|---|---|---|
| **1. In-place install over SSH** (the default) | You want to convert the printer you have. It keeps the stock U-Boot and the stock system in its own partition until you remove it | Yes: `revert.sh` returns to QIDI's boot setup and MCU firmware, `revert.sh --full` restores the whole backup | No. Opening the case once for the maskrom recovery proof is strongly recommended |
| **2. Flash-once image** (bench, microSD, CI) | You are building a test printer, a bench board or a CI check, and do not need the stock system | Yes, through the eMMC backup | Yes, for maskrom entry (the card slot for a microSD boot is inside, too) |

The in-place install is the supported way for a user. The flash-once image exists for the people who work on Eitri; it replaces the whole eMMC and uses a different partition layout (GPT, no stock U-Boot).

## Path 1: in-place install over SSH

Run the installer from your computer or from the printer's stock system (it is a shell script plus Python 3.7, the stock Debian 10 interpreter). The first run only looks; nothing is changed until you pass the flags that say so.

```sh
sudo ./install.sh --flavor klipper                                          # look, do not touch (default)
sudo ./install.sh --flavor klipper --i-have-a-backup --yes --backup-to-usb  # convert
```

1. **Preflight (read-only).** The installer checks the device tree, the stock layout, that it is running from the eMMC, that nothing is printing, and that there is room.
2. **Back up first.** The whole eMMC is written to a USB stick or over SSH, then read back and verified. If the backup cannot be written, the installer stops.
3. **Download and verify.** `install.json` is verified against the Eitri release key before anything else in it is read. Every payload file (root file system, stage-2 installer, data seed, MCU firmware) is then checked against the SHA-256 and size in that signed file.
4. **Prove MCU recovery.** Each board is reset into its bootloader, its firmware read back and written back, and the copy kept. If a board cannot be proven recoverable the installer stops here. This is the one write before the point of no return and has its own consent flag.
5. **Check the backup again, then type `CONVERT`.** Nothing irreversible has happened yet.
6. **Stage 2 (RAM installer).** The printer reboots into a one-shot installer that shrinks the stock root, creates the two Eitri root slots and `/data`, writes and verifies the Eitri root file system, and then **installs the data seed** into the new `/data` (next section). A power cut during this resumes by itself; `install.sh --status` says where things stand.
7. **First boot of Eitri.** Both MCUs are flashed to match the host firmware, health checks run, and only then is the system marked good. Three failed boots fall back to the stock system automatically.

The stock system stays in its own partition and keeps booting until you remove it. Stop at any step that fails. Do not continue past a failed backup, a failed write verification, or a failed MCU readback.

The details of every step, the flags, and `revert.sh` are in [`installer/README.md`](../installer/README.md); the boot logic is in [`installer/docs/BOOT.md`](../installer/docs/BOOT.md).

## The data seed and the offline first boot

Everything the printer runs on top of the OS lives on `/data`: the pinned **Klipper or Kalico** and **Moonraker** clones, their Python environments, the **Klarity** environment, and the **`qd_*` extras** and printer configuration layer. CI builds all of it for the printer's CPU and ships it per release as one archive per flavor, `data-seed-<version>-<flavor>.tar.zst`, with a manifest. The installer writes the seed you chose into `/data` in stage 2.

What this means for you:

* **First boot works offline.** No `git clone`, no `pip install`, no download on the printer. The seed contains the exact commits pinned in [`release.lock`](../release.lock).
* **The extras and the configuration layer live on `/data`** (`/data/eitri/extras`, `/data/eitri/klipper-layer`) and follow the Klipper or Kalico clone, not the OS image. An OS update does not change them and a seed update does. The OS image carries no copy of them; an image started without a seed simply has no Klipper layer until a seed is installed.
* **Klipper and Kalico stay pristine clones.** The extras are symlinked into `klippy/extras/` and hidden through `.git/info/exclude`; `git pull` on the clone never conflicts.
* **A seed is installed once and never overwrites your data.** `printer_data/` (configuration, G-code, logs, database) is not in the seed. Installing the same seed twice changes nothing.

The format and the install algorithm are specified in [`seed/SPEC.md`](../seed/SPEC.md).

## First boot: the setup wizard

After the first boot the printer's touchscreen shows a pairing code, and the setup wizard answers at `http://eitri.local:8099/`. Enter the code to pair your browser. The wizard walks through:

1. **Network.** Wired is expected; the wizard shows the address.
2. **Detect.** Identifies both MCU boards over their serial ports. Nothing is assumed about which port is which.
3. **Firmware flavor.** Klipper (default) or Kalico (see below).
4. **Options.** Chamber heater, power-loss recovery, accelerometer, and **web interface sign-in**:
   * *Trust my home network* (the default): anyone on your LAN can open the web interface, like the stock printer. This is the right choice on a home network you control.
   * *Require sign-in*: the web interface asks for a login. Choose this on a shared network.
5. **Remote access (optional).** Off unless you turn it on; see [Remote access](#remote-access-ssh).
6. **Review and apply**, then **flash the MCUs** (backup, flash, read back, verify) and **calibrate** (Z offset, shaper, mesh).

The wizard does not show the camera's RTSP password. It is created on first start and is shown on a **separate page** after setup, so it does not end up in a screenshot of the wizard.

**The wizard exits when setup is done.** It stops listening, which frees roughly 30 MiB of memory and leaves no open setup service on your network. To change something later, use the **Unlock setup** action on the printer's touchscreen: it restarts the wizard and shows a new pairing code. Over SSH the same thing is `sudo -n /usr/bin/eitri-configurator unlock`. An unlocked wizard needs a fresh pairing code for every change, and refuses changes while the printer is printing.

### Remote access (SSH)

Off by default: a new Eitri install has **no password login and no key** except the one(s) you add during setup. Turn it on in the wizard (step 5) if you want a shell:

* **Add an SSH public key** (recommended). Paste one OpenSSH public key; it is installed for the `eitri` user. SSH never accepts password login unless you also set a password.
* **Set a password** for the `eitri` user. This enables SSH password login. A key is the safer choice.
* **Both**, or neither.

To undo it, use the same wizard step ("disable remote access"): it removes the added keys and the password and goes back to keys from setup only. On a development bench image an `authorized_keys` file dropped on the boot partition is also picked up; the in-place install does not rely on that.

The `eitri` account may run only `eitri-update`, `eitri-bootctl` and the narrow helpers the wizard needs (`eitri-configurator unlock`, `eitri-access`) without a password.

### Which address, which port

Open the web interface and point slicers and apps at **port 80**: `http://eitri.local/`. Moonraker itself listens only on the loopback interface (`127.0.0.1:7125`); nginx on port 80 is the one way in, and it forwards the Moonraker API, the web interface and the webcam. The web server listens on **IPv4 only** for now.

## Path 2: flash-once image (bench, microSD, CI)

The release build also produces one raw image for the printer's eMMC or a microSD card, plus its SHA-256 digest and a signature on `SHA256SUMS`. It contains the OS with the data seed already unpacked or unpacked on first boot (offline either way).

1. Download the image and `SHA256SUMS` with its `.minisig`. Verify the signature against the public key in the repository (`installer/keys/`) and then the image against `SHA256SUMS`.
2. Enter maskrom mode by opening the printer and connecting the internal eMMC clock pin to ground, or by booting a blank eMMC. Connect the board's USB port to your computer with a USB-A to USB-A cable.
3. Write the image with `rkdeveloptool`, following [`RECOVERY.md`](RECOVERY.md#2-maskrom-and-rkdeveloptool). Record the flash information first, so the original capacity is known.
4. Boot the printer. The wizard and its pairing code appear as described above.

For a microSD boot (developers only) the card is written the same way and the eMMC is left alone. It requires opening the printer; remove the card to return to the stock system.

## Choosing Klipper or Kalico

The wizard asks you to choose the host firmware. The choice is recorded in `/data/eitri/flavor`, and the matching data seed is the one that was installed.

| | Klipper | Kalico |
|---|---|---|
| Upstream | `Klipper3d/klipper` | `KalicoCrew/kalico` |
| Default | **Yes** | No |
| Pinned | `master` at `a08fc79` (no release tag since v0.13.0) | `v2026.10.00` |
| Choose it when | You want the mainline project's behavior and release cadence | You want features that Kalico adds, and accept its different release cadence |

Both are pristine clones. Eitri's QIDI-specific behavior works on either. A few extras depend on internal APIs, and they have a per-base guard. Those guards are the only difference in Eitri's own code.

**Switching flavor after install is not a configuration change.** Host and MCU firmware are protocol-locked, so switching means installing the other flavor's data seed *and* rebuilding or fetching the MCU firmware for it and reflashing **both** boards. The wizard does this, but it takes time and it uses the same backup-and-verify steps as a first install.

## Update channels

| | Eitri release channel | Upstream mode |
|---|---|---|
| Default | **Yes** | No, opt-in |
| What it updates | Signed OS bundles, plus the matching data seed (Klipper or Kalico, Moonraker, Klarity, extras) and MCU firmware | `git pull` on Klipper, Moonraker, and Klarity, or any branch |
| MCU firmware | Built in CI, shipped with the release | Built on the device |
| Rollback | One click, to the previous slot; the seed update keeps the old trees until the new ones pass the health check | Manual, through git |
| Extra requirement | None | The `toolchain` add-on in `/data/eitri/toolchain`, about 300 MB (gcc and arm-none-eabi-gcc), and network access on the printer |

The release channel is signed twice (the channel manifest by the Eitri release key, the OS bundle by the Eitri RAUC certificate). Who holds those keys and how they are rotated is in [`RELEASING.md`](RELEASING.md).

Upstream mode is for people who build their own firmware, and for a project that has stopped being maintained upstream. Turning it on is a deliberate step. The system then reports that the installed versions no longer match `release.lock`, and the Python environments are no longer the hash-pinned ones from the seed: Moonraker's update manager installs requirements on the printer from the network.

Switching channels does not change the flavor.

## After install: verify

Work through this list before you print anything.

- [ ] Moonraker answers: `curl http://eitri.local/server/info` returns JSON.
- [ ] The web interface loads at `http://eitri.local/` on port 80.
- [ ] The services are active: `systemctl status klipper moonraker klarity eitri-camera`.
- [ ] `git -C /data/firmware/klipper status` is clean and `git -C /data/firmware/klipper log -1` shows the commit pinned for your flavor in `release.lock`.
- [ ] The camera responds: `curl -o /tmp/snap.jpg http://eitri.local:8080/snapshot`.
- [ ] Both MCUs are identified, and each reports the firmware version that matches your flavor and `release.lock`.
- [ ] The screen shows status, and pressing its controls does what the stock screen did. The screen offers **Unlock setup**.
- [ ] Heater runaway protection is active. Klipper's configuration must show `verify_heater` enabled for each heater.
- [ ] Homing completes without an error. A pre-triggered probe stops the run with an error. If you see one, stop and find out why before retrying.
- [ ] A test print of a small, known-good file completes.

## Troubleshooting

- **The installer stops at the backup step.** Fix the backup target. The installer will not continue without a verified backup.
- **An MCU readback does not match.** Do not retry in a loop. Stop, keep the log, and follow [`RECOVERY.md`](RECOVERY.md#3-mcu-recovery-over-serial).
- **`klipper` and `moonraker` are `inactive (dead)` after the first boot.** The data seed is missing or incomplete (the units start only when it is there). Check `/data/eitri/state/seed-installed.env` and `journalctl -u eitri-data-init`; reinstall the seed with `eitri-seed-install` ([`seed/SPEC.md`](../seed/SPEC.md#10-installing-a-seed)).
- **The setup page does not answer on port 8099.** That is normal after setup: the wizard has exited. Use **Unlock setup** on the touchscreen, or `sudo -n /usr/bin/eitri-configurator unlock` over SSH.
- **The screen stays blank.** Check that `klarity.service` is running and that `serial_port` in `klarity.cfg` points at the display's UART. The default is `/dev/ttyS1` at 115200; it is not confirmed on the mainline kernel.
- **You want to go back to stock.** Run `revert.sh` (the stock system is still in its own partition), or restore the eMMC backup you took before installing. [`RECOVERY.md`](RECOVERY.md) has the steps.
