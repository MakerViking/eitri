# Eitri

**Open, mainline-clean firmware for the QIDI X-Plus 4.**

Eitri forges the printer's software from upstream parts: pristine Klipper (or Kalico), Moonraker, a read-only A/B operating system, and a camera stack built on tools that are meant to be read. **Klarity**, Eitri's screen daemon, takes over the printer's touchscreen without touching its display firmware.

> **Status: pre-alpha.** Nothing here has been released or validated on a printer. There is no release image, installer, or update channel yet, and interfaces, file layouts, and version pins will change. Do not install this on a printer you depend on.

---

## Why Eitri

The stock QIDI X-Plus 4 software is an old Debian base with a vendor kernel, a patched Klipper fork, and closed components: the `xindi` screen service and Cython modules for the Box accessory. Eitri takes a different approach.

1. **Upstream Klipper stays untouched.** Klipper or Kalico is a plain `git clone` that is never edited. Every QIDI-specific behavior lives in separate `qd_*` extras and config files that are linked in from the outside. `git pull` on the upstream tree never conflicts.
2. **The screen is not reflashed.** Klarity speaks the TJC/Nextion serial protocol to the display's existing `.tft`. The display firmware is never rewritten, so there is no screen-flash brick vector.
3. **The code is open.** Apache-2.0 for the repository, GPL-3.0-or-later for the Klipper extras (matching Klipper's own license). You may read it, fork it, and change it.
4. **Recovery works over a USB cable.** The RK3328 Rockchip maskrom mode and `rkdeveloptool` restore the eMMC without any vendor writer. Documented in [`docs/RECOVERY.md`](docs/RECOVERY.md).
5. **Releases are pinned and reproducible.** One [`release.lock`](release.lock) pins every upstream input. CI builds the images and signed update bundles, and A/B rootfs slots give you a rollback.

## What Eitri is building

Everything below is **planned**. None of it is released yet.

- **Base OS:** a Buildroot appliance on glibc, mainline Linux 6.18 LTS, systemd, a read-only A/B root filesystem managed by RAUC, and a writable `/data` partition for the application layer.
- **Firmware flavor, chosen at install:** mainline Klipper (default) or Kalico, each as a pristine clone.
- **Klarity:** an Apache-2.0 Python daemon that drives the stock TJC `.tft` over UART and mirrors Moonraker state to the screen.
- **Qidi behavior as clean modules:** chamber fan control, single-offset probing, reverse homing via StallGuard, the chamber/bed thermal interlock, z-offset variables, and power-loss recovery, all as `qd_*` extras.
- **Camera:** `ustreamer` MJPEG passthrough at 1920×1080, served on stable `/stream` and `/snapshot` paths, with optional `go2rtc` RTSP.
- **Web UI:** Fluidd or Mainsail on nginx, plus a first-run configurator on port 8099.
- **Two update channels:** an **Eitri release channel** (default; signed bundles with matching MCU firmware and one-click rollback) and an opt-in **upstream mode** (on-device MCU builds, `git pull`, any branch).
- **MCU flashing that probes instead of guessing:** each board is identified over its serial port, backed up, flashed, read back, and verified.

## How it fits together

```
 L1  Klarity ........... screen daemon        pyserial <-> stock TJC .tft (no reflash)
 L2  qd_* extras ....... GPL-3.0-or-later     symlinked into klippy/extras/
     Klipper | Kalico .. upstream, pristine    never patched
 L3  ustreamer / go2rtc camera streams        UVC 1bcf:0b09 -> :8080 /stream /snapshot
 L0  Buildroot OS ...... glibc, 6.18 LTS      read-only A/B rootfs, writable /data
```

The full design, including ports, services, the `/data` layout, and the safety invariants, is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How Eitri compares

The table compares Eitri (as planned) with the other open projects for the same printer family, or with the closest projects for other printers. It is written from each project's public documentation and license text as reviewed on 2026-10-21. Corrections are welcome. Details and sources are in [`docs/COMPARISON.md`](docs/COMPARISON.md).

| | **Eitri** | [FreeDi](https://github.com/Phil1988/FreeDi) | [OpenNept4une](https://github.com/OpenNeptune3D/OpenNept4une) | [Simple AF](https://pellcorp.github.io/creality-wiki/prerooted_firmware/) |
|---|---|---|---|---|
| Printer | QIDI X-Plus 4 | QIDI X3, Q1 Pro, X-Plus 4 | Elegoo Neptune 4 family | Creality K1 series |
| Project license | Apache-2.0; GPL-3.0-or-later for Klipper extras | Custom "All rights reserved" source-available license: no derivatives, no reverse engineering | GPL-3.0 | No license detected by GitHub; not checked further |
| Klipper base | Pristine upstream clone (Klipper or Kalico); QIDI behavior is separate `qd_*` modules | Upstream Klipper; some QIDI behavior dropped or bolted on | Upstream Klipper | Klipper with vendor parts removed |
| Touchscreen | Open daemon drives the stock `.tft`; display is **not** reflashed | Ships its own screen firmware, which is flashed to the display | Daemon for Neptune displays, tuned to a vendor-supplied `.tft` | Different display path; no equivalent daemon |
| OS base | Buildroot, glibc, Linux 6.18 LTS, read-only A/B | Armbian | Armbian | Creality-specific pre-rooted image |
| Install model | Flash-once CI image with web configurator, or in-place conversion over SSH | Installer script on Armbian, with a setup wizard | Image per model/PCB revision, plus installer script | Pre-rooted image, Wi-Fi via USB, USB factory reset |
| Recovery | RK3328 maskrom over USB with `rkdeveloptool` (planned, documented) | Restore-to-stock procedure in its wiki (not verified by us) | Re-flash the image (details not checked) | USB emergency factory reset |
| Maturity | **Pre-alpha; nothing released** | Released, actively used | Released | Released, actively used |

Eitri has no users yet, and FreeDi, OpenNept4une, and Simple AF have years of field time. Eitri's bet is that openness, a clean upstream tree, and a screen path that never reflashes hardware are worth the effort to build.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): layers, partitions, services, ports, the `/data` layout, MCU flashing, and safety invariants
- [`docs/INSTALL.md`](docs/INSTALL.md): install paths, Klipper vs Kalico, and update channels
- [`docs/RECOVERY.md`](docs/RECOVERY.md): eMMC backup and restore, microSD boot, maskrom recovery, and ST-Link as the last resort
- [`docs/CLEAN-ROOM.md`](docs/CLEAN-ROOM.md): the reverse-engineering and clean-room policy
- [`docs/COMPARISON.md`](docs/COMPARISON.md): the extended comparison with sources
- [`STATUS.md`](STATUS.md): per component, what is done, partial or a stub, the test results, what needs hardware, and the ordered steps to a first boot
- [`release.lock`](release.lock): every pinned upstream version and digest

## Developing

`make help` lists the entry points: `make test` runs every component's tests, `make lint` is the syntax gate, `make os-defconfig` configures the Buildroot image, and `make configurator-dev` runs the setup wizard against fake hardware. Nothing in the Makefile touches a printer.

## Licensing

- The repository is licensed under **Apache-2.0**. See [`LICENSE`](LICENSE).
- The Klipper extras under `klipper-layer/extras/` are licensed under **GPL-3.0-or-later**, with their own `LICENSE` file in that directory.
- Third-party components keep their own licenses. [`NOTICE`](NOTICE) lists each one and where it is used.
- Eitri does not include, and must never include, QIDI binaries, `.tft` or `.HMI` assets, Cython modules, or decompiled code. See [`docs/CLEAN-ROOM.md`](docs/CLEAN-ROOM.md).

## Trademarks and non-affiliation

Eitri is an independent project. It is **not affiliated with, endorsed by, or sponsored by QIDI TECH** or any of its subsidiaries. "QIDI," "QIDI X-Plus 4," and related names and logos are trademarks of their respective owners and are used here only to identify the hardware this software is intended to run on. Klipper, Kalico, Moonraker, Fluidd, Mainsail, FreeDi, OpenNept4une, Simple AF, and the other project names mentioned here belong to their respective projects and are used for comparison and attribution only.

Running Eitri may affect your printer's warranty, support eligibility, and the behavior of QIDI's own software. You are responsible for the choices you make on your hardware.

## Contributing, security, and conduct

- [`CONTRIBUTING.md`](CONTRIBUTING.md): how to propose changes, the clean-tree and safety rules, and the sign-off requirement
- [`SECURITY.md`](SECURITY.md): how to report a vulnerability privately
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md): expected community behavior

Eitri is pre-alpha and does not yet offer a support channel. Please use the issue tracker for bugs and questions, and read [`docs/RECOVERY.md`](docs/RECOVERY.md) before you test anything on a printer.
