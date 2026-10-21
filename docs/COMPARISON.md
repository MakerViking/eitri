# Comparison with other open projects

This is the extended comparison behind the table in [`README.md`](../README.md#how-eitri-compares). It is written from each project's public documentation and license text as reviewed on **2026-10-21**. Projects change, and this page will go out of date. If you find an error, open an issue and cite the source you checked.

We have tried to describe each project fairly. Several of them do things that Eitri does not do yet, and some have a large lead in field time. Where a claim is not verified, we say so.

## The printers

The comparison covers projects that target the same printer family, and the closest projects for other printers that have set a standard for install and recovery.

| Project | Printer | Summary |
|---|---|---|
| **Eitri** | QIDI X-Plus 4 | Pre-alpha. Open, pristine-upstream Klipper or Kalico with separate QIDI modules, an open screen daemon that keeps the stock display, and USB recovery. |
| **FreeDi** | QIDI X3, Q1 Pro, X-Plus 4 | Released. Armbian base with upstream Klipper, Moonraker, and Mainsail. Ships its own screen firmware. Licensed under a custom source-available license. |
| **OpenNept4une** | Elegoo Neptune 4 family | Released. Converts Neptune 4 printers to Armbian with upstream Klipper and a Neptune display daemon. Included as the architecture reference for a TJC display daemon. |
| **Simple AF** | Creality K1 series | Released. Pre-rooted images, removal of vendor components, and install and recovery tooling. Included as the install and recovery reference. It does not support the QIDI X-Plus 4. |

## Comparison by topic

### Licensing

- **Eitri:** Apache-2.0 for the repository, and GPL-3.0-or-later for the Klipper extras, matching Klipper. Upstream components keep their own licenses. See [`NOTICE`](../NOTICE).
- **FreeDi:** Its `LICENSE` file is a custom "All rights reserved" source-available license, version 1.0. Its public text forbids commercial use, forbids derivative works and modifications without written permission, forbids relicensing, and contains a no-reverse-engineering clause. It is not an OSI-approved license, and GitHub reports its license as `NOASSERTION`. The restrictions matter for anyone who wants to fork or adapt the project.
- **OpenNept4une:** GPL-3.0 (per the repository metadata). The `display_connector` daemon is GPL-3.0 as well.
- **Simple AF:** GitHub detects no license for `pellcorp/creality`. We did not look further, and nothing from it is copied.

### Klipper base

- **Eitri:** A pristine `git clone` of Klipper or Kalico. QIDI behavior is added through `qd_*` modules and config symlinks, and the upstream tree is never edited. The design goal is that `git pull` on the upstream tree never conflicts.
- **FreeDi:** Upstream Klipper, installed through the project's scripts. It drops QIDI behavior such as the Box MMU, the dual-probe `get_zoffset`, reverse homing, and the chamber/bed interlock, rather than re-implementing it as separate modules.
- **OpenNept4une:** Upstream Klipper on Armbian, with a virtual Raspberry Pi MCU service for host GPIO.
- **Simple AF:** Klipper, with the vendor's additions removed. Its documentation describes the result as a genuine Klipper experience.

### Touchscreen

This is the biggest technical difference, so the details matter.

- **Eitri:** The open daemon, Klarity, speaks the TJC/Nextion serial protocol to the display's **existing** `.tft`. The display is never reflashed. The protocol is documented under the clean-room policy in [`CLEAN-ROOM.md`](CLEAN-ROOM.md). This is a design target, not a released feature.
- **FreeDi:** Ships its own screen firmware, a compiled TJC `.tft`, which is flashed to the display. The screen is replaced, not left in place.
- **OpenNept4une:** `display_connector` is a GPL-3.0 Python daemon for the TJC/Nextion display family. It is tuned to specific Neptune display firmware versions, and it expects the vendor-supplied `.tft` that the project ships. It is not a drop-in for the QIDI display.
- **Simple AF:** Uses a different display path. It has no equivalent TJC daemon.

### Closed code

- **Eitri:** No closed code is planned for the runtime. The screen daemon, camera configuration, and extras are open. Eitri does not ship any QIDI binary, display asset, or Cython module.
- **FreeDi, OpenNept4une, Simple AF:** Not reviewed in detail for this page. Check each project's current state before you rely on this point.

### Install model

- **Eitri:** A flash-once CI image with a web configurator for board and toolhead selection, MCU flashing, and calibration. The same flow can also be run in place over SSH, on the printer you already have, with a full eMMC backup taken first.
- **FreeDi:** An installer script on an Armbian base, with a setup wizard. Its documented flow includes flashing the Armbian image to the eMMC, then running the installer.
- **OpenNept4une:** A pre-built image for each Neptune 4 model and PCB revision, plus an installer script for converting an existing Armbian base.
- **Simple AF:** Pre-rooted images, Wi-Fi configuration over USB, and a USB emergency factory reset.

### Recovery

- **Eitri:** Software rollback through A/B root filesystems, eMMC backup and restore over RK3328 maskrom with `rkdeveloptool` (no vendor writer), serial MCU recovery, and ST-Link as the last resort. The procedures are documented in [`RECOVERY.md`](RECOVERY.md). The maskrom path is planned and has not been exercised yet.
- **FreeDi:** Its wiki documents a restore-to-stock procedure for the Plus 4. The project's own rollback path is the fallback, and it depends on the stock image being available.
- **OpenNept4une:** Recovery is by re-flashing the image. We have not checked the details of its recovery documentation.
- **Simple AF:** USB emergency factory reset, and pre-rooted images for recovery.

### OS base

- **Eitri:** Buildroot, glibc, mainline Linux 6.18 LTS, systemd, a read-only A/B root filesystem with RAUC, and a writable `/data`.
- **FreeDi and OpenNept4une:** Armbian.
- **Simple AF:** The vendor's Creality base, cleaned.

### Known issues reported by others

These are reported by their projects, and we cite them only as examples of the problems each project has been working on. We have not verified them.

- FreeDi reports that its setup wizard reappears on every boot on the Plus 4 ([issue #418](https://github.com/Phil1988/FreeDi/issues/418)), and a reported mix of bugs with FreeDi and Qidi Studio ([issue #472](https://github.com/Phil1988/FreeDi/issues/472)).
- The stock QIDI screen service is reported to use a lot of CPU ([QIDI_PLUS4 issue #126](https://github.com/QIDITECH/QIDI_PLUS4/issues/126)).

## Where the others are ahead

- **Field time.** FreeDi, OpenNept4une, and Simple AF have many users and years of fixes. Eitri has none.
- **Working software.** Those projects install and run today. Eitri does not.
- **Support.** FreeDi, OpenNept4une, and Simple AF have active communities that can help. Eitri does not yet have a support channel.

## Sources

- FreeDi: <https://github.com/Phil1988/FreeDi>, the FreeDi wiki at <https://freedi-wiki.github.io/>, and its `LICENSE` file on the `main` branch.
- OpenNept4une: <https://github.com/OpenNeptune3D/OpenNept4une> and <https://github.com/OpenNeptune3D/display_connector>.
- Simple AF: the Simple AF documentation at <https://pellcorp.github.io/creality-wiki/prerooted_firmware/>.
- Eitri's own pins and their sources: [`release.lock`](../release.lock).
