# Status

Last integration pass: 2026-10-21. Pre-alpha. **Nothing in this repository has run on a printer.** Every
test below is a unit, simulation or static test; "needs hardware" lists what none of them can prove.

`make test` runs every suite below (about 5 minutes, most of it the real klippy). `make test-fast`
skips the klippy integration tests (about 1 minute). `make lint` is the syntax gate.

Legend: **done** = complete as far as it can be without hardware, **partial** = works but has a known
gap, **stub** = deliberately refuses to run.

## Components

| Component | State | Tests (last run) | What is missing |
|---|---|---|---|
| Repository core (`README`, `docs/`, `release.lock`, licences) | done | `tools/check-repo.py`: 672 checks pass; `tools/lint.py` clean | Python and nginx source digests are resolved from Buildroot at build time, not pinned in `release.lock`. Every pin is `tested = false`. Klipper is pinned to an untested master commit. |
| `os/` (Buildroot image, RAUC bundle, U-Boot, kernel, DTS) | partial | 102 passed, 4 skipped (U-Boot sandbox build not present); `check-pins` and `check-defconfig` (163 options) pass; a full image build succeeded earlier | Never booted. No release signing CA or key is committed. |
| `klarity/` (screen daemon) | partial | 175 passed (warnings are errors), ruff clean, `--check-map` OK | Screen map is 66 pages / 511 widgets, **105 flagged `todo`**, none checked against a live panel. File browser, preview/print start, session state (jog distance, z-offset step), thumbnails and forced page changes are not expressible. No camera page exists in the stock UI, so the camera preview has nowhere to show. |
| `klipper-layer/` (qd_* extras, printer.cfg template, macros) | partial | 152 passed: import checks, overlay install on throw-away clones of Klipper and Kalico, unit tests, render tests, and 44 runs of the real klippy for both flavors | `qd_probe` and `qd_reverse_home` are **stubs** (they refuse to load). Macros, currents, probe, interlock and power-loss recovery are untested on a machine. |
| `installer/` (in-place install, `eitri-mcuflash`, `eitri-update`, boot manager) | partial | mcuflash 69, eitri-update 74, installer suite 77 (includes a real U-Boot 2021.07 sandbox run of `boot.scr`) | Never run against a board. No release signing key, placeholder download URLs, no data-bundle or stage-2 initramfs builder (see "Not built by anyone"). |
| `configurator/` (setup wizard, port 8099) | done | 62 passed, including the real `eitri-mcuflash` CLI against non-existent ports and the real klipper-layer renderer | Calibration regexes, nmcli parsing, polkit, sandbox untested on hardware. Wi-Fi password goes to `nmcli` on argv. Screenshots were regenerated after the options change. |
| `camera/` (ustreamer, go2rtc, udev) | done | 81 passed, 0 failed, 4 skipped (hardware only) | ustreamer v6.67 flags unchecked against the real binary. The 23.7 fps cause is unknown. RTSP and `/webcam/` have no authentication. |

## Integration changes made in this pass

* **printer.cfg now comes from the klipper-layer.** The configurator had its own fallback templates, which
  put the mainboard in `[mcu]` and the toolhead in `[mcu U_1]`: the reverse of the layer, and of the
  reference unit's config. They are gone. The configurator renders `klipper-layer/config/printer.cfg.tmpl`
  with the layer's own renderer, fills the MCU ports from detection, runs `overlay-install.sh` after writing,
  and seeds `moonraker.conf` and `eitri-update-manager.conf` once. Per-printer overrides go in
  `printer_data/config/eitri-vars.toml`.
* Options the layer cannot honour (probe type, filament sensor, adaptive mesh) were removed from the
  wizard instead of being shown and ignored. Power-loss recovery and the ADXL345 are template switches
  (`plr_enabled`, `adxl_enabled`) and are verified to load in real klippy with both off.
* **OS image ships the layer and the installer's on-device half.** New package `eitri-klipper-layer`
  (installs `/usr/lib/eitri/klipper-layer`); `post-build.sh` runs `installer/tools/install-into-rootfs.sh`
  (eitri-update, eitri-bootctl, units, keys). Without this the configurator could not find its templates
  and the update tooling never reached the image.
* **Config path clash fixed.** `eitri-update` wrote its Moonraker include to
  `printer_data/config/eitri/update_manager.conf`, but `config/eitri` is a symlink to the read-only layer.
  It is now `printer_data/config/eitri-update-manager.conf`.
* **Klarity** gained what the extracted screen map needs: the stock `71 page widget lo hi` frame, per-widget
  `on_number` actions with `clamp`/`scale`/`zero_ok`, `bind.offset`, `derived.speed_mms`, and a link-state
  file for the configurator. Default panel port is `/dev/ttyS1` at 115200 (what the stock software opens;
  unconfirmed on mainline) and text is UTF-8 (the stock software sends UTF-8, so the panel fonts are).
  The stock cancel button only opens the confirmation page; a test enforces that every live print-control
  action has a state guard.
* Unit files: Klarity and the configurator units now wait for their venvs and for `eitri-data-init`.
* Removed a real printer address from an installer test shim. `release.lock` lists the new components.
* New: `Makefile`, `tools/lint.py`, `tools/check-repo.py`, `.github/workflows/ci.yml`.

## Needs hardware (none of this is proven)

* **Boot chain:** open TPL DRAM init on this board (fallback: Rockchip DDR blob), BootROM order between
  microSD and eMMC, mmcblk numbering, A/B state sector, TF-A v2.15 BL31, Ethernet (gmac2phy), USB VBUS pin
  for the camera, regulators.
* **Which UART is which:** the layer, the installer and the reference config say ttyS2 = toolhead and
  ttyS0 = mainboard, the original brief said the opposite. Everything probes (`eitri-mcuflash plan`); confirm.
  Also each board's bootloader (Katapult at 250000? SD-card bootloader on the mainboard?) and chip IDs.
* **Screen:** that the panel is on `/dev/ttyS1` at 115200 under mainline, press vs release edge, string
  escaping, whether marker bytes carry terminators, the map against the live panel, 0x71 handling.
* **Klipper machine:** sensorless XY homing currents and thresholds, the 7 mm lift then probe-down Z homing,
  the PA10 probe identity and polarity (`probe_z_offset` ships as 0 and PRINT_START refuses to run until it
  is calibrated), the bed/chamber interlock against the real PSU, chamber PID, power-loss resume, the 0.1 s
  multi-MCU homing timeout.
* **Updates:** RAUC A/B with rollback, boot counting, `eitri-verify`, MCU rollback, in-place conversion
  (U-Boot `boot.scr` order, `fatwrite`, stage 2 on the stock 5.16 kernel, resize time).
* **Camera:** 1080p fps and whether auto-exposure throttles it, `--encoder=HW` passthrough, udev
  hot-plug, OBS over RTSP/MJPEG, hardware H.264 (expected absent).
* **Network:** rtl8xxxu throughput and AP mode, NetworkManager hotspot, mDNS name.

## Not built by anyone yet

1. **The data seed.** Nothing creates `/data/firmware/klipper` and `/data/firmware/moonraker` (pinned clones),
   `/data/venv/klippy` and `/data/venv/moonraker` (uv, Python 3.14, within Moonraker's dependency caps),
   `/data/eitri/klarity/venv` (needs pyserial-asyncio-fast, websockets, PyYAML), or the data bundle that
   `installer/docs/BUNDLE.md` describes (`data-seed.tar.zst`, `mcu-firmware.tar.zst`). `klipper.service` and
   `moonraker.service` do not start until it exists. This is the largest gap on the way to a first boot.
2. **MCU firmware builds** for both flavors (`installer/mcuflash` expects `<flavor>/manifest.json` plus
   `<board>/klipper.bin`), and the mainboard's recovery image (the stock one cannot be shipped).
3. **Release CI:** the minisign key (`installer/keys/eitri-release.pub`), the RAUC CA
   (`os/board/eitri-plus4/rauc/keyring.crt` and the three `EITRI_RAUC_*` secrets), `install.json` and
   `manifest.json` generation, the stage-2 initramfs and the conservative-feature rootfs for U-Boot 2021.07
   used by the in-place path. Download URLs in `install.sh` and `eitri-update` are guesses.
4. **Helpers the configurator calls if present:** `/usr/libexec/eitri/switch-flavor` and
   `/usr/libexec/eitri/toolchain`. Without them a flavor switch or the upstream add-on is only recorded.
5. MCU rebuild-and-flash after a git update in upstream mode is not hooked to Moonraker.
6. `spi_flash.py` needs `fatfs.so` or a host gcc on the device; the image has neither, so the mainboard can
   only be flashed through the `sdcard-file` method until one is provided.

## Known quirks and open decisions

* A Klipper clone with the overlay reports a version ending in `-dirty` (klippy marks any untracked or ignored
  file under `klippy/extras/`). `git status` and Moonraker stay clean. Compare commits, not version strings.
* Two OS update backends exist: the image's flash-once layout (GPT, `rauc-bootloader`) and the in-place
  layout (DOS logical partitions, `eitri-bootctl`, stage 2 rewrites RAUC's config). Both are untested; the OS's
  `eitri-mark-good` can mark a slot good before `eitri-verify` checks Klipper and Moonraker.
* Where the extras live: the contract says `/data/eitri/extras`; the image carries the layer at
  `/usr/lib/eitri/klipper-layer` and a copy in `/data/eitri/klipper-layer` takes precedence (so extras can
  follow the clone instead of the OS). Pick one before the data bundle is built.
* Licences to confirm before publishing: `QIDITECH/QIDI_PLUS4` is AGPL-3.0 by its LICENSE file but GPL-3.0 by
  its README; `klipper-layer/config/` carries no SPDX tag; the CRC16/VLQ code in
  `eitri_mcuflash/klipper_proto.py` is protocol-dictated but close to Klipper's GPL `msgproto.py`.
* **Clean-room review pending:** `klarity/src/klarity/maps/default.yaml` was derived from analysis of the
  stock host and is interoperability data (ids, component names, what a tap does). `docs/CLEAN-ROOM.md`
  requires a second-person review of such a specification before it is published.
* Chamber `verify_heater` keeps the stock limits (`max_error` 400, `check_gain_time` 600); tighten after PID
  tuning. The heaters' runaway protection is live in every configuration.
* `shellcheck` and `actionlint` are not installed here and were not run after the last shell edits.

## Ordered next steps to a first boot on the printer

Nothing below touches the printer until step 5.

1. **Decide and commit the trust roots:** generate the minisign key and the RAUC CA, commit the public halves,
   set the CI secrets. Confirm the AGPL question. Have a second person review the Klarity map.
2. **Build the data seed** (gap 1): pinned Klipper/Kalico and Moonraker clones, uv venvs, the Klarity venv and
   the layer; add it to `release.lock` and CI. Build the MCU firmware for both boards (gap 2).
3. **Build the OS image** (`make -C os image`, Linux host) and run `make test` plus `make -C os check`.
4. **Prepare recovery before any write:** read `docs/RECOVERY.md`, take the full eMMC backup, prove maskrom
   entry and `rkdeveloptool` on the board while the stock system still boots.
5. **Bench boot from microSD** (never the eMMC first): open TPL DRAM init, Ethernet, `/data` creation, services.
   Resolve the console question first: ttyS2 is wired to an MCU, so a console needs a deliberate bench setting.
6. **Probe, read-only:** `eitri-mcuflash plan` for UART roles and bootloaders; send `sendme` at 115200 to find
   the panel UART; `klarity --learn --dry-run` and walk the pages to confirm the map and the touch edge.
7. **Flash the MCUs** only after recovery for both boards is proven (`eitri-mcuflash` insists), then run the
   configurator: detect, apply, flash, calibrate.
8. **First motion** with a hand on the power switch: homing, probe polarity, `PROBE_CALIBRATE`, bed mesh,
   heaters, the interlock, then a short print. Re-pin Klipper to the commit that passed.
9. Only then write the eMMC (flash-once image or the in-place converter), exercise a RAUC update and a forced
   rollback, and mark pins `tested = true` one by one.
