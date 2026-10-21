# Architecture

This document describes the target design of Eitri: how it is meant to be put together. Parts that are not built yet are labeled as such. Where a value is still undecided or unverified on hardware, it is marked **TBD** or **open question**, and you will find it again in [Open questions](#open-questions). Pinned versions live in [`release.lock`](../release.lock), not here.

## 1. Goals and invariants

Every design choice below serves these goals. A change that breaks one of them needs an architecture decision recorded here first.

| # | Invariant | What it means in practice |
|---|---|---|
| I1 | **Upstream stays clean** | Klipper and Kalico are unmodified git clones. Eitri never edits `klippy/*.py` or any tracked file in `klippy/extras/`. |
| I2 | **No screen reflash** | The display keeps its stock `.tft`. Klarity speaks the serial protocol to it. |
| I3 | **No closed code in the runtime** | No vendor binary, Cython module, or display asset ships in an Eitri image or bundle. |
| I4 | **Recoverable over USB** | RK3328 maskrom and `rkdeveloptool` can restore the eMMC without a vendor writer. See [`RECOVERY.md`](RECOVERY.md). |
| I5 | **Atomic, reversible OS updates** | The OS is an A/B pair of read-only root filesystems. A bad update rolls back. |
| I6 | **Reproducible, pinned inputs** | Every third-party input is pinned in `release.lock` by commit or digest. |
| I7 | **Safety first** | Heater runaway protection, probe-error semantics, and bounded retries are never weakened (see [§7](#7-safety-invariants)). |

## 2. Layer model

Eitri has four versioned layers. Each is updated independently and has a clear interface to the others.

```
 +--------------------------------------------------------------------------+
 | L1  Klarity (Apache-2.0)                                                 |
 |     screen daemon: pyserial <-> stock TJC .tft over UART                 |
 |     Moonraker websocket for state, polls L3 /snapshot for the preview    |
 +--------------------------------------------------------------------------+
 | L2  Klipper extras (GPL-3.0-or-later), config layer (AGPL-3.0-or-later)  |
 |     qd_* modules symlinked into klippy/extras/, KAMP-style config dir,   |
 |     both on /data (from the data seed) and updated with Klipper/Kalico  |
 |     ----------------------------------------------------------------     |
 |     Klipper OR Kalico — pristine upstream clone, never patched           |
 |     Moonraker — pristine upstream clone, plus Eitri config               |
 +--------------------------------------------------------------------------+
 | L3  Camera (Apache-2.0 config, upstream GPL-3.0 ustreamer)               |
 |     ustreamer MJPEG on :8080 (/stream, /snapshot)                        |
 |     go2rtc optional: :1984 web/API, :8554 RTSP                           |
 +--------------------------------------------------------------------------+
 | L0  Base OS image (Apache-2.0 integration, GPL-2.0 kernel and packages)  |
 |     Buildroot appliance: glibc, Linux 6.18 LTS, systemd, nginx, RAUC     |
 |     rootfs A / rootfs B (read-only), /boot, /data (ext4, writable)       |
 +--------------------------------------------------------------------------+
```

Two things cut across the layers rather than sitting in them:

- **Install and configurator tooling** (the first-run web configurator on port 8099 and the installer) writes the config and drives MCU flashing. It is delivered inside the L0 image and the L2 app layer, and it is versioned with them.
- **Release tooling** (`release.lock`, CI, signing, RAUC bundle creation) builds and verifies every layer together.

The L0 image holds the OS. Everything that Klipper, Moonraker, and Klarity need at runtime lives in the **app layer** on `/data`: the pinned clones, their Python environments, the `qd_*` extras and the configuration layer. CI builds that layer for the printer's CPU and ships it as the **data seed** (§4.2), so a first boot needs no network. The app layer can be updated without reflashing the OS, and the OS can be updated without touching `/data`. The extras follow the Klipper or Kalico clone they are linked into, not the OS image.

## 3. Base OS (L0)

### 3.1 Platform

| Item | Value |
|---|---|
| Board | Firefly ROC-RK3328-CC (Rockchip RK3328, 4× Cortex-A53, aarch64) |
| Memory | 976 MB |
| Storage | 28.9 GB eMMC (`mmcblk1` on the stock system) |
| Wired network | `eth0`, in-kernel `rk_gmac-dwmac` (stmmac), 100 Mbit. The supported network path. |
| Wireless (optional) | Realtek USB `0bda:b711` (RTL8188GU family). Mainline `rtl8xxxu` is enabled, but wireless is not a supported path until it has been measured on this hardware. |
| Camera | USB UVC, Sunplus `1bcf:0b09`, on the USB2 hub |
| MCUs | Two STM32-class boards on the SoC's hardware UARTs (`/dev/ttyS0`, `/dev/ttyS2`) at 500000 baud. No CAN, no USB to the boards. |

The stock system runs Debian 10 (buster) with a 5.16 vendor kernel. Eitri replaces all of it.

### 3.2 Toolchain and components

| Component | Pin | Notes |
|---|---|---|
| Buildroot | 2026.02.3 (LTS) | 2026.08 is the latest stable. The LTS was chosen for point-release stability. |
| Linux | 6.18.57 (longterm) | Mainline kernel with the RK3328 and ROC-RK3328-CC device tree (`rk3328-roc-cc.dtb`). |
| U-Boot | v2026.10 | `configs/roc-cc-rk3328_defconfig` |
| TF-A (BL31) | v2.15.0 | Fallback `lts-v2.12.1`, which Buildroot supports natively. Not yet build-tested on this board. |
| Python | 3.14.5 | Provided by Buildroot 2026.02.3. 3.15 is not used: Moonraker's pinned dependencies do not list it. |
| nginx | 1.30.2 | Serves Fluidd or Mainsail and the configurator on port 80. |
| RAUC | 1.15.2 | Manages the A/B slots and signed bundles. |

The source of every pin, with its digest or commit, is in `release.lock`.

### 3.3 Storage layout

There are two boot backends. **The user default is the DOS in-place layout**: the installer converts the stock eMMC without replacing the stock U-Boot (a `boot.scr` chooses between Eitri and the stock entry, the stock system stays in its own partition, `revert.sh` is the way back; see [`installer/docs/BOOT.md`](../installer/docs/BOOT.md)). The **GPT flash-once layout** below is for the bench, microSD boots, CI and the QEMU smoke test; it is not offered to users as the install path. Both mount `/data` by label and never refer to partition numbers, so the same root file system serves both.

The GPT disk image (`os/board/eitri-plus4/genimage.cfg`) builds and has been checked statically; it has not been booted on a printer. The data partition is grown to the end of the eMMC on first boot. Sizes may still change after the first bench image.

| Region | Device | Contents | Written by |
|---|---|---|---|
| Reserved sectors before partition 1 | raw, `mmcblk` | idbloader (SPL/TPL, Rockchip offset sector 64), U-Boot `u-boot.itb` (Rockchip offset sector 16384) | Bootloader updates only |
| `boot` | FAT32, 32 MiB (`EITRIBOOT`, mounted at `/boot/firmware`) | Boot options (`eitri-boot.env`) only; an `authorized_keys` file dropped here from a PC is picked up on boot | Image, user |
| `rootfs.A` | ext4, 768 MiB, read-only | OS root filesystem, slot A, including the kernel `Image` and `rk3328-eitri-plus4.dtb` in its `/boot` | RAUC bundle |
| `rootfs.B` | ext4, 768 MiB, read-only | OS root filesystem, slot B | RAUC bundle |
| `data` | ext4, writable | `/data`: app layer, printer config, logs, gcode, database, comms | Installer, Moonraker, Klarity |

The Rockchip offsets are the standard RK3328 values. They must be checked against the U-Boot that is actually built before the first image.

### 3.4 A/B update and rollback

- Each RAUC bundle carries the kernel, device tree, and rootfs for one slot. It is signed. RAUC refuses unsigned or mismatched bundles.
- An update writes to the inactive slot, then marks it for next boot. If the new slot fails to reach a healthy state, the boot-attempt counting (designed with RAUC and U-Boot) selects the previous slot.
- `/data` is shared between slots and is not touched by an OS update. Application-layer changes are handled separately (see [§9](#9-update-channels)).

### 3.5 Init and services

systemd is the init system. Moonraker's `machine` component integrates with systemd through D-Bus. The units that Eitri installs are:

| Unit | Role | Port |
|---|---|---|
| `klipper.service` | Klipper host process | — |
| `moonraker.service` | API server, **loopback only** (reach it through nginx on 80; slicers and apps use port 80) | 7125 |
| `klarity.service` | Screen daemon (L1) | — (serial) |
| `eitri-camera.service` | ustreamer (L3) | 8080 |
| `go2rtc.service` | Optional RTSP gateway (password protected, WebRTC off) | 1984, 8554 |
| `eitri-configurator.service` | First-run and maintenance web configurator; **exits when setup is complete** and is restarted by Klarity's "Unlock setup" action | 8099 |
| `nginx.service` | Fluidd or Mainsail, configurator reverse proxy. **IPv4 only**: the LAN allow-list is IPv4, so there is no IPv6 listener | 80 |

Port 8080 serves `/stream` (MJPEG) and `/snapshot` (JPEG). Those are the stable paths that Klarity and Fluidd use.

**Configurator lifecycle.** The wizard is started with `--retire-when-locked` (the default in the unit). While setup is incomplete it listens on 8099 and publishes a one-time pairing code to `/data/eitri/configurator/pairing.json (mode 0640, owner eitri)`; Klarity shows that code on the touchscreen. When setup completes the wizard locks and the service exits, which frees about 30 MiB. Klarity's **Unlock setup** action runs `sudo -n /usr/bin/eitri-configurator unlock`, which restarts the wizard (the `--retire-when-locked` drop-in belongs to the OS image) and publishes a new code. The wizard refuses changes while a print is running.

**Privileged helpers.** Services run unprivileged. The few operations that need root are narrow helpers behind exact `sudoers` rules, called with `sudo -n`: `eitri-update`, `eitri-bootctl`, `eitri-configurator unlock`, and `/usr/libexec/eitri/eitri-access` (`set-key`, `set-password`, `disable`, `status`) which the wizard's optional remote-access step uses. They validate their input strictly and never interpolate it into a shell.

### 3.6 Users, hostname, and network

- Default hostname: `eitri`.
- The OS user is `eitri` (uid 1000), in the groups `dialout`, `video`, `plugdev`, and `tty`. Services that need serial or camera access run as this user, not as root.
- **Shell access is opt-in.** A new install has no password login. The wizard's optional remote-access step (off by default) adds an SSH public key and/or sets a password for `eitri` through `eitri-access`; `disable` removes both again. sshd enables password authentication only when a password was chosen.
- The printer is reachable as `eitri.local` through mDNS on the LAN. The mDNS implementation is TBD.

## 4. The `/data` application layer

`/data` is an ext4 partition that survives OS updates. It holds everything that a user or an update can change.

```
/data
├── firmware/
│   ├── klipper/              pristine clone (always named "klipper", whichever flavor), qd_* symlinks,
│   │                         prebuilt klippy/chelper/c_helper.so
│   └── moonraker/            pristine clone
├── venv/
│   ├── klippy/               Python 3.14 environment of Klippy (flavor-specific)
│   └── moonraker/            Python 3.14 environment of Moonraker
├── printer_data/             user data: never part of a seed, never overwritten by one
│   ├── config/               printer.cfg, moonraker.conf, klarity.cfg, eitri-vars.toml (optional overrides),
│   │                         eitri-update-manager.conf, eitri -> klipper-layer config set, eitri-flavor.cfg
│   ├── logs/
│   ├── gcodes/
│   ├── database/
│   └── comms/
└── eitri/
    ├── flavor                "klipper" or "kalico"
    ├── extras/               qd_* Klipper extras (GPL-3.0-or-later) and gcode_shell_command.py: the source of truth
    ├── klipper-layer/        config/ (printer.cfg template, macros, flavor files), scripts/, extras -> ../extras
    ├── klarity/venv/         Klarity's environment (the daemon itself is installed into it as a wheel)
    ├── seed/                 seed-info.json, seed-info.env, locks/ (what exactly was installed)
    ├── toolchain/            optional add-on for upstream mode (gcc, arm-none-eabi-gcc)
    ├── mcu/                  MCU firmware, backups and stock images (written by the installer and eitri-update)
    └── state/                install and update bookkeeping (seed-installed.env, boot.env copies, ...)
```

The directory `firmware/klipper` is always called `klipper`, even when the flavor is Kalico. Code should look up the flavor in `/data/eitri/flavor`, never from the directory name.

### 4.1 Where the extras and the layer live

The `qd_*` extras and the configuration layer are **on `/data`**, not in the OS image, so they update together with Klipper or Kalico (a seed update, or upstream mode) instead of with the OS. The OS image carries no copy of the layer (the former `/usr/lib/eitri/klipper-layer` package was removed), so nothing in the image decides what the machine does with Klipper.

### 4.2 The data seed

CI builds, for aarch64 and per firmware flavor, one archive `data-seed-<version>-<flavor>.tar.zst` plus `data-seed-<version>-<flavor>.manifest.json` (SHA-256 of the archive, flavor, pinned commits, Python version, creation date). Unpacking it yields the tree above except `printer_data/` and the state, mcu and toolchain directories:

* shallow clones of Klipper (or Kalico) and Moonraker at the exact commits in `release.lock`, attached to their upstream branches so Moonraker's update manager and `git describe` work;
* `venv/klippy`, `venv/moonraker` and `eitri/klarity/venv`, built from hash-pinned lock files with binary wheels only (the one dependency without an aarch64 wheel is cross-compiled from its pinned sdist);
* a prebuilt `c_helper.so`, so Klippy does not need a C compiler on the printer;
* the extras and the layer, with the overlay already applied.

A seed is installed by the in-place installer's stage 2, by `data-init` on a flash-once image (first boot, offline), or by `eitri-update` for a release or a flavor switch, always through the one tool `eitri-seed-install`, which verifies the digest before it reads the archive, refuses unsafe members, installs component by component with atomic renames, and never overwrites a non-empty target in install mode. The archive format, layout, update and rollback algorithm, reproducibility rules and the image prerequisites are in [`../seed/SPEC.md`](../seed/SPEC.md); the workflow that builds it is `.github/workflows/data-seed.yml`.

## 5. Firmware flavor: Klipper or Kalico (L2 base)

The installer offers two host firmware bases. Both are pristine clones that Eitri never edits.

| | Klipper (default) | Kalico |
|---|---|---|
| Upstream | `Klipper3d/klipper` | `KalicoCrew/kalico` (fork, formerly Danger Klipper) |
| Pinned in `release.lock` | `master` at `a08fc79`, since no tag exists after v0.13.0 | `v2026.10.00` |
| Character | Mainline, conservative | Extra features; its own release cadence |

The choice is recorded in `/data/eitri/flavor`. Three consequences follow:

1. **Extras must work on both bases.** Extras that reach into internal APIs (for example, `qd_probe` and the probe-session refactor, or `qd_reverse_home` and stepper internals) must guard on the base and may need a per-base shim. Config-only features do not care about the flavor.
2. **MCU firmware is per-flavor.** Host and MCU firmware are protocol-locked. The installer builds and flashes the MCU firmware that matches the chosen flavor.
3. **Switching flavor later means rebuilding and reflashing both MCUs.** It is not a configuration change. See [`INSTALL.md`](INSTALL.md#choosing-klipper-or-kalico).

## 6. Layers 1–3 in detail

### 6.1 L1 — Klarity (screen daemon)

Klarity replaces the closed `xindi` service. It drives the printer's **stock** TJC/Nextion `.tft` over UART and never reflashes the display.

- It talks to the display over the UART that the display is connected to. **The serial device path is a config key** (`serial_port` in `klarity.cfg`). The shipped default is `/dev/ttyS1` at 115200 8N1, which is what the stock software opens; it is **not confirmed on the mainline kernel** and has to be checked on the bench (the 8250 numbering follows the device tree: ttyS0 and ttyS2 are the MCUs).
- Its screen map holds the page, widget and component ids of the stock `.tft` as observed from the stock software's behaviour. Entries it is unsure of are flagged `todo` and ignored at run time. The map is interoperability data only and still needs the clean-room review in [`CLEAN-ROOM.md`](CLEAN-ROOM.md).
- It publishes its link state in `/run/klarity/status.json`, which the configurator reads.
- It gets printer state from Moonraker over its websocket.
- For the live camera preview, it polls the L3 `/snapshot` endpoint. It never opens the camera itself, because UVC devices allow only one opener.
- It runs as the `eitri` user, in the `dialout` group.

The display protocol is documented in the clean-room notes and summarized in [`CLEAN-ROOM.md`](CLEAN-ROOM.md#what-we-publish). The implementation is Apache-2.0.

### 6.2 L2 — Klipper extras and config

Eitri's QIDI-specific behavior is a set of **new, uniquely named** modules. The rules are:

- Each module is a new file named `qd_<feature>.py`, kept in `/data/eitri/extras` (the layer's `extras/` points there) and symlinked into `klippy/extras/` by `klipper-layer/scripts/overlay-install.sh`. The data seed puts the layer on `/data` (§4.1), so the extras follow the Klipper or Kalico clone rather than the OS.
- The symlink is hidden from `git status` through `.git/info/exclude` in the Klipper clone.
- Config and macro files are symlinked into `printer_data/config/` (`eitri` -> the layer's config set, `eitri-flavor.cfg` -> the file for the chosen flavor). `printer.cfg` is rendered by the configurator from the layer's template.
- The version string of a clone with the overlay reads `...-dirty`: `klippy.py` marks any untracked or ignored file under `klippy/extras/` that way. `git status` and Moonraker's checks stay clean; compare versions by commit, not by string.
- Eitri never shadows a file that upstream tracks in `klippy/extras/`.

The planned extras are:

| Module | Purpose | Note |
|---|---|---|
| `qd_chamber_fan` | Chamber fan control | |
| `qd_probe` | Single-offset probe handling | Dual-probe monkey-patching is dropped. It does not survive upstream's probe refactor. |
| `qd_reverse_home` | Reverse homing as `force_move` plus StallGuard | Replaces a patch to core `stepper.py`. |
| `qd_power_interlock` | Chamber-to-bed thermal interlock | Subject to [§7](#7-safety-invariants). |
| `qd_zoffset_var` | Z-offset stored as a variable | |
| `qd_plr` | Power-loss recovery | |
| `qd_runtime_patches` | Only if needed, for example TRSYNC timeout | Should be avoided. |

Config-only features (most macros, KAMP-style meshing, pressure advance and retraction profiles) need no extras at all.

**Dropped on purpose:** the Box/AMS (MMU) code, `buttons_irq`, `dynamaic_config_loader`, and every change that weakens a safety check. Box support is a clean-room re-implementation, and it is not in the first release.

Prior art, which Eitri may read and adapt with attribution: QIDI's public GPL Klipper fork and configs (`release.lock`, `[attributed.*]`).

### 6.3 L3 — Camera

- **Hardware:** USB UVC Sunplus `1bcf:0b09`. It offers MJPEG and YUYV at 640×480, 1280×720, and 1920×1080.
- **Mode:** MJPEG passthrough. The RK3328 has no hardware H.264 encoder, so there is no transcoding. YUYV is not used for streaming.
- **Resolution:** full **1920×1080** is the default. Dropping the default to 720p is not an option.
- **Device:** a stable udev symlink `/dev/eitri-cam` is created from the device's USB identity. Nothing refers to `/dev/video*` directly.
- **Measured frame rate:** about 23.7 fps, nearly the same at 1080p and 720p. Because bandwidth does not change the rate, the likely cause is auto-exposure throttling the frame rate. This is a hypothesis to verify, and it is an open item (Q5).
- **Streams:** `ustreamer` serves `/stream` and `/snapshot` on port 8080. `go2rtc` is optional. It reads the same source for RTSP on 8554.

## 7. Safety invariants

These rules apply to every build and every extra. A change that violates any of them is a bug, however it was introduced.

1. **`verify_heater` stays live.** Klipper's heater runaway protection is never disabled, never relaxed, and never switched off above a Z height or after a time. The fork Eitri replaces disabled it above Z 270 and lengthened `MAX_HEAT_TIME` from 5 s to 10 s. Eitri does neither.
2. **A pre-triggered probe is an error.** If the probe is already triggered when homing starts, the run stops with an error. It is not downgraded to an info message.
3. **Retries are bounded.** No probe, homing, or flashing routine retries endlessly. After the retry limit, the failure is raised as an error.
4. **The chamber/bed interlock is live before the chamber heater can run.**
5. **Heater and motion limits in shipped configs are never raised to make a test pass.**

## 8. MCU flashing

Both boards are reached over serial. Neither is on CAN or USB.

- **Never hard-code which port is which.** The mapping of `/dev/ttyS0` and `/dev/ttyS2` to mainboard and toolhead is contested in our sources. The installer identifies each board by probing its UART and reading its identity. It does not assume.
- **Mainboard:** Klipper's `scripts/flash-sdcard.sh`-style flash at 500000 baud, from a `klipper.bin` built for the chosen flavor.
- **Toolhead:** Katapult `flashtool.py` at **250000** baud to enter the bootloader, then flash the application.
- **Every flash:** back up the existing firmware first, flash, read back, and verify the digest. A failed verify is an error, not a warning.
- **Version lock:** the installer checks MCU firmware against the host version after each Klipper update, and reflashes when they differ.
- **Recovery:** the bootloader is never erased by a normal flash. The worst case, an erased bootloader on the toolhead, needs a soldered ST-Link V2. [`RECOVERY.md`](RECOVERY.md#4-st-link-worst-case-mcu-recovery) covers it.

## 9. Update channels

Eitri has two channels. The install chooses one, and it can be changed later.

| | **Eitri release channel** (default) | **Upstream mode** (opt-in) |
|---|---|---|
| What updates | Signed RAUC OS bundles, plus the release's data seed (clones, environments, extras, layer) and the matching MCU firmware | `git pull` on Klipper, Moonraker, and Klarity, or any branch, through the Moonraker update manager |
| MCU firmware | Built in CI, shipped in the bundle, matches the release | Built on the device |
| Rollback | One click, to the previous A/B slot; a seed update keeps the previous trees until the health check passes (`eitri-seed-install --mode rollback`) | Manual, through git |
| Requires | Nothing extra | `toolchain` add-on in `/data/eitri/toolchain` (about 300 MB: gcc and arm-none-eabi-gcc) |
| Who it is for | Most users | People who build their own firmware, and anyone whose project upstream has gone quiet |

Both channels record the pinned versions they run. Upstream mode is a deliberate departure from `release.lock`, and the system shows it as such.

## 10. Release model

1. `release.lock` pins every input (§3.2, §5, and the rest), and its `[release]` tables hold the numbers that move with each release (manifest sequence, image floor, clock floor).
2. CI builds the Buildroot image and, for each flavor, the data seed from those pins. MCU firmware is built for each flavor with the same pins.
3. Every artifact gets a SHA-256 digest. **No build job holds a signing key.** A separate `release` workflow collects the unsigned artifacts, waits for the maintainer's approval in a protected GitHub environment, re-signs the RAUC bundle, writes and minisigns `install.json`, `manifest.json` and `SHA256SUMS`, and verifies the result with the printer's own code before it attaches anything to a draft release.
4. The release is tested on hardware before it is marked `tested = true` in `release.lock`.

Keys, custody, rotation and revocation: [`RELEASING.md`](RELEASING.md). Nothing in `release.lock` is marked tested yet. Every pin is currently `tested = false`.

## 11. Licensing boundaries

| Path | License |
|---|---|
| Repository root, configurator, camera config, Klarity, installer, docs, tooling, `klipper-layer/config/flavors/` | Apache-2.0 |
| `klipper-layer/config/` (`printer.cfg.tmpl`, `macros.cfg`, `eitri-variables.toml`) | AGPL-3.0-or-later: adapted from `QIDITECH/QIDI_PLUS4`, which Eitri treats as AGPL-3.0; see `klipper-layer/config/LICENSE-NOTE.md` |
| `klipper-layer/extras/` (the `qd_*` modules) | GPL-3.0-or-later (a file derived from GPLv3-only code is GPL-3.0-only), with its own `LICENSE` |
| Kernel and U-Boot patches | GPL-2.0 |
| Upstream components (Klipper, Kalico, Moonraker, ustreamer, Fluidd, Mainsail, RAUC, Katapult, uv, nginx, Python) | Their own licenses, listed in [`NOTICE`](../NOTICE) |

`tools/check-repo.py` checks the SPDX tags of the two klipper-layer directories and that copyleft tags appear only where this table allows them.

## 12. Directory layout of the repository

Paths marked *planned* are not yet in the repository.

```
eitri/
├── README.md, LICENSE, NOTICE, CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md
├── release.lock
├── docs/                    this directory
├── klarity/                 L1 screen daemon and its screen map (Apache-2.0)
├── klipper-layer/           L2: extras/ (GPL-3.0-or-later), config/ (AGPL-3.0-or-later templates and macros), scripts/
├── camera/                  L3 ustreamer and go2rtc config, udev, systemd
├── configurator/            first-run web configurator (port 8099)
├── installer/               in-place installer, eitri-mcuflash, eitri-update, boot manager
├── seed/                    data seed: build, verify, install tool, specification (SPEC.md)
├── os/                      Buildroot BR2_EXTERNAL tree: defconfig, board files, packages, tests
├── tools/                   repository checks (check-repo.py, lint.py) and release tooling (release/: metadata, preflight, signing)
├── Makefile                 test, lint, os-defconfig, configurator-dev
├── STATUS.md                what works, what is a stub, what needs hardware
└── .github/                 CI workflows
```

## Open questions

These need live hardware or a decision before they can be closed. None of them is hidden.

| # | Question | How it gets answered |
|---|---|---|
| Q1 | Which UART is the mainboard and which is the toolhead? | Probe both at boot; record the result in config. Never hard-code. |
| Q2 | The `klarity.cfg` `serial_port` default | Shipped as `/dev/ttyS1` @ 115200 (what the stock software opens); confirm on the bench with `sendme` / `klarity --learn`. |
| Q3 | Does mainline TPL do DRAM init reliably, or is the vendor `ddrbin` needed? | Bench boot test. Maskrom is the backstop. |
| Q4 | Does mainline `rtl8xxxu` give usable throughput on this dongle? | Measure on hardware. Wired stays the supported path until then. |
| Q5 | Does the UVC camera offer MJPEG at 1080p and 720p with a usable frame rate? | Measure. Confirm whether auto-exposure is throttling the rate. |
| Q6 | Which bootloader is on each UART MCU (Katapult, the vendor serial bootloader, or the STM32 ROM)? | Identify on hardware. This selects the flash tool. |
| Q7 | Is sensorless Z-max viable on mainline for this toolhead? | Test with the actual toolhead MCU and driver. |
| Q8 | Exact MCU chip IDs | Read from the boards. The mainboard is reported as STM32F402 by an existing public project; this is not yet confirmed here. |
| Q9 | Final partition sizes and the exact Rockchip offsets in the built U-Boot | Build the first bench image. |
