# Status

Last integration pass: 2026-10-21 (overnight hardening run). Pre-alpha. **Nothing in this repository has run on a printer.** Every
test below is a unit, simulation or static test; "needs hardware" lists what none of them can prove.

`make test` runs every suite below (about 5 minutes, most of it the real klippy). `make test-fast`
skips the klippy integration tests (about 1 minute). `make lint` is the syntax gate.

Legend: **done** = complete as far as it can be without hardware, **partial** = works but has a known
gap, **stub** = deliberately refuses to run.

## Components

Test counts are from the final `make test` of the hardening run (all suites passed, `make lint` clean).

| Component | State | Tests (last run) | What is missing |
|---|---|---|---|
| Repository core (`README`, `docs/`, `release.lock`, licences, CI) | done | `tools/check-repo.py`: 744 checks pass; `tools/lint.py` clean | Python and nginx source digests are resolved from Buildroot at build time, not pinned in `release.lock`. Every pin is `tested = false`. Klipper is pinned to an untested master commit. CI actions are pinned to commit SHAs; the release key is still written to the runner during the Buildroot build (see Open). |
| `os/` (Buildroot image, RAUC bundle, U-Boot, kernel, DTS) | partial | 117 passed, 4 skipped (U-Boot sandbox build not present); `check-pins` and `check-defconfig` (162 options) pass; the image built earlier; QEMU smoke test passed 15/15 before the hardening changes | Never booted on the board. No release signing CA or key is committed. The image was **not rebuilt** after the hardening changes, so the QEMU run must be repeated. |
| `klarity/` (screen daemon) | partial | 207 passed (warnings are errors), ruff clean | Screen map is 66 pages / 511 widgets, **105 flagged `todo`**, none checked against a live panel. File browser, preview/print start, session state, thumbnails and forced page changes are not expressible. No camera page exists in the stock UI. Home e-stop widget is still `todo` (inert). |
| `klipper-layer/` (qd_* extras, printer.cfg template, macros) | partial | 208 passed: unit tests, overlay install on throw-away clones of Klipper and Kalico, render tests, real klippy batch tests for both flavors | `qd_probe` and `qd_reverse_home` are **stubs** (they refuse to load). Macros, currents, probe, interlock and power-loss recovery are untested on a machine. |
| `installer/` (in-place install, `eitri-mcuflash`, `eitri-update`, boot manager) | partial | mcuflash 72, eitri-update 118, installer suite 115 (includes a real U-Boot 2021.07 sandbox run of `boot.scr`) | Never run against a board. No release signing key, placeholder download URLs, no data-bundle or stage-2 initramfs builder (see "Not built by anyone"). |
| `configurator/` (setup wizard, port 8099) | done | 92 passed, including the real `eitri-mcuflash` CLI against non-existent ports and the real klipper-layer renderer | Pairing, lock-after-setup, nmcli handling, polkit and the sandbox are untested on hardware. |
| `camera/` (ustreamer, go2rtc, udev) | done | 125 passed, 0 failed, 4 skipped (hardware only), including real go2rtc 1.9.14 auth enforcement | ustreamer v6.67 flags unchecked against the real binary. The 23.7 fps cause is unknown. `/webcam/` has no authentication (RTSP does). |

## Measurements (QEMU virt, no printer)

**Re-measured 2026-10-22 on the hardened code** (after fixing the stale local-package build, see below): boot 1 (blank /data) = 0.49 s kernel + 17.6 s userspace to `multi-user.target`; boot 2 = 0.49 s + **13.0 s** (was 29.9 s — the camera unit no longer blocks boot); idle ≈ **143 MiB** used of 982 MiB; 16/16 smoke checks pass. `eitri-prepare`/`eitri-verify` now fail only because QEMU boots without a bootloader (no `eitri.slot=`), which is correct behaviour; the `/data/eitri/logs` bug is fixed.

**Build-system fix (2026-10-22):** Buildroot only rsyncs `SITE_METHOD = local` packages (eitri-camera, eitri-configurator, eitri-klipper-layer, eitri-mcuflash) on their first build, so earlier images/QEMU runs shipped stale copies after code edits. `make -C os image` and `os/qemu/build.sh` now run `<pkg>-rebuild` for every local package first.

`os/qemu/smoke-test.sh` boots the real image (same Buildroot tree, overlay, packages and units; no TF-A or
U-Boot) on a blank `/data`, powers off, boots again on the same `/data` and writes a report. QEMU virt is
4 x Cortex-A53 under TCG (software emulation) with 1024 MiB: **times compare one run with the next, not with
the printer; memory sizes do not depend on the CPU.** These numbers were taken **before** the hardening
changes; repeat the run on the rebuilt image.

| | first boot (blank /data) | second boot |
|---|---|---|
| kernel | 0.51 s | 0.49 s |
| userspace to `multi-user.target` | 31.3 s | 29.7 s |
| last non-camera service active (since kernel start) | 13.8 s (sshd) | 12.2 s (nginx) |
| QEMU start to login prompt (wall clock) | 18.5 s | 16.9 s |
| poweroff | 2.3 s | 2.1 s |

`multi-user.target` came about 17 s after the real services were up because of the camera unit waiting 20 s for
a device that does not exist; without it userspace is roughly 14 s. The camera unit is now skipped with
`ConditionPathExists=/dev/eitri-cam` and waits 5 s at most, so this should be gone (unverified). First-boot
critical path: `dev-vda.device` 5.3 s, `systemd-udev-trigger` 4.0 s, `eitri-data-init` 1.9 s (0.2 s on the second
boot), `eitri-prepare` and `eitri-verify` 2.4 s and 1.8 s (spent on a traceback caused by the missing
`/data/eitri/logs`, fixed).

Idle RAM (`MemTotal` 982 MiB): `free -m` used 92 to 95 MiB, buff/cache 108 MiB, available 820 MiB; about
140 MiB in use at idle (total minus available, minus the 19 MiB measuring tool); slab 20 MiB; zram swap 491 MiB
unused. Per unit, RSS / PSS in kB:

| unit | RSS | PSS |
|---|---:|---:|
| eitri-configurator | 38,984 | 32,433 |
| NetworkManager | 16,224 | 9,883 |
| systemd (init) | 11,676 | 6,447 |
| udevd | 10,296 | 5,181 |
| polkit | 6,300 | 2,986 |
| journald | 7,664 | 2,956 |
| sshd | 7,148 | 2,897 |
| wpa_supplicant | 5,536 | 2,626 |
| nginx | 5,732 | 2,399 |
| logind | 7,032 | 2,372 |
| chrony | 4,892 | 2,071 |
| dbus | 3,884 | 1,693 |
| avahi | 4,724 | 1,260 |
| **total** | **138,112** | **78,047** |

Klipper, Moonraker, Klarity and the camera streamer never ran (no data seed, no camera), so they have no
numbers; the real budget is unknown until they do. The configurator is the largest idle process; the
`--retire-when-locked` option (implemented, not enabled) would exit it after setup.

First-boot `/data` layout is created correctly (`eitri/{extras,klarity,toolchain,configurator/venv,home,state}`,
`printer_data/{config,logs,gcodes,database,comms}`, `venv/*`, `state/first-boot`, `layout-version=1`, the `ui`
symlink, the `/etc` overlay). The configurator answers on 8099 and nginx on 80. `klipper`, `moonraker`, `klarity`
show `inactive (dead)` with "start condition unmet" until the data seed exists: no error is logged, so the
missing seed is invisible in `systemctl --failed`. `os/qemu/known-failures.txt` lists the units expected to
fail in QEMU; its `eitri-prepare` and `eitri-verify` lines are stale if the rebuilt image passes.

## Legal-info and SBOM

`make -C os legal-info` now completes (it failed on `eitri-camera`, `eitri-configurator` and `eitri-mcuflash`
before; fixed in this pass by copying the repository `LICENSE` into each build directory). 107 target
packages (95 host build tools are not distributed): 44 strong copyleft, 15 weak copyleft (LGPL, MPL), 48
permissive, 1 proprietary-but-redistributable (`linux-firmware`, Realtek rtlwifi blobs, unmodified redistribution
allowed with the notice). The source set is 1.29 GB; without the 557 MiB `linux-firmware` tarball it is about
709 MB and fits a GitHub release asset. `os/legal/make-sbom.py` writes CycloneDX 1.5 and SPDX 2.3 (110
components with Klipper, Kalico, Moonraker; 12 are `NOASSERTION` where Buildroot gives prose licences).
`os/legal/package-legal.sh` and `.github/workflows/legal.yml` pack sources, licences, the SBOMs and a
`git archive` of the tag; **neither has run end to end** (the 1.3 GB packing and the CI run were not done).
Rockchip `rkbin` is not in the default image. Details and open findings: `os/legal/README.md`.

## Earlier integration changes (before the hardening run)

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
* New: `Makefile`, `tools/lint.py`, `tools/check-repo.py`, `.github/workflows/ci.yml` (the earlier integration pass).

## What the hardening run changed

Audit findings fixed overnight, by severity. Everything is covered by tests; none of it has met hardware.

**High**
* **Slot committed too early (os, installer).** `eitri-update` (`eitri-verify.service`) is now the only
  committer; the OS health checks run as its `ExecStartPre` (`mark-good --check`). A slot is marked good only after
  `eitri-prepare` finished and Klipper, Moonraker and the MCUs passed health, so the 3-boot countdown is no longer refilled
  by an early commit. The GPT flash-once layout works too (BootControl delegates to `rauc-bootloader`; an
  unconfirmed or never-written slot is never a rollback target).
* **`eitri-prepare` and `eitri-verify` died on a fresh `/data`** (missing `/data/eitri/logs`): data-init creates it,
  the units wait for data-init, and the event log survives an unwritable file.
* **Camera gateway open to the LAN (camera).** RTSP now needs a per-install random password, WebRTC is off, and
  `go2rtc.service` refuses to start with a non-loopback listener that lacks credentials.
* **Unauthenticated configurator that could change the printer (configurator).** Every state-changing call needs a
  paired session (8-digit one-time code, rotated after use, CSRF and strict Origin checks); pairing closes when
  setup completes and re-opens only with `eitri-configurator unlock`. Apply and service restarts are refused while
  printing.
* **Moonraker exposed with wildcard CORS and `trusted_clients` (configurator, os).** Moonraker binds `127.0.0.1`;
  nginx is the only way in, with a LAN allow-list, a 1 MB body limit (1 GB only on file uploads).
* **Blanket passwordless root (os).** `sudo` is passwordless only for `eitri-update` and `eitri-bootctl`; sshd is keys
  only; klipper and moonraker run with `NoNewPrivileges`.
* **Bricking paths in the installer.** `revert --full` buffers and verifies before it writes the MBR last; `revert --hard`
  proves both boards can be restored before touching either; a blocked or incomplete MCU plan no longer counts as
  "already flashed"; an interrupted conversion re-enters stage 2 (start counter) instead of falling to a half-converted
  disk; the backup is re-validated right before the point of no return; the toolhead erase/write round trip needs
  named consent (`--accept-mcu-roundtrip`).
* **Release supply chain.** GitHub Actions pinned to commit SHAs, tag builds start with empty caches, the draft release
  race is fixed. Updates: mandatory manifest `expires`, revocation list, sequence floor, default trust root only
  `/usr/share/eitri/keys`, hostile toolchain tarballs rejected before extraction.
* **Machine safety (klipper-layer).** `G28 X`/`Y` no longer leaves a faked homed Z; resume after the 30 min idle timeout
  stays possible and refuses (before heating) when not homed; power-loss resume rebuilds an adaptive mesh that never
  existed as a saved profile and no longer overwrites a measured Z; the saved Z offset is tied to the probe
  calibration; `M290` is bounded; the chamber interlock parks and restores its own target.

**Medium / low**
* Klarity: each loop runs under a restarting supervisor; print-cancel, pause, resume and start must carry a state guard
  (load error otherwise), e-stop needs `unguarded: true`; repeated taps and gcode runs are limited; a pending `get` can
  no longer swallow a keypad frame; format strings are validated at load; chamber presets use 45 instead of 40.
* Configurator: Wi-Fi passphrase no longer on the command line; ports validated with full matches; backups are atomic
  and fsynced; **all fire imagery and wording removed** (new tempered-steel palette, hammer and nozzle icons,
  `test_ui_wording.py` enforces it, also the installer's stage-2 page).
* Services: `MemoryHigh` (never `MemoryMax`) on moonraker, camera, go2rtc and the configurator; Klipper is unlimited
  with `OOMScoreAdjust=-500`; `/data` mounts `noatime,errors=remount-ro`; pip and setuptools are gone from the image
  and so are jinja2 and the configurator's template override.
* Camera unit no longer blocks boot for 20 s or loops without a camera; fallback cameras start on hotplug.
* Simplify pass: one atomic-write and one hashing helper for the installer, no dead code (about -490 lines).
* `make legal-info` fixed (above); licence tooling, SBOM and a legal workflow added.

**Not changed**: heater runaway protection (`verify_heater`) is untouched and live in every configuration; a
pre-triggered probe is still an error; the MCU flasher still never erases a bootloader and refuses without proven
recovery; installer writes are still dry-run by default. No Qidi binary or asset entered the repository.

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

## Known quirks and open issues

* A Klipper clone with the overlay reports a version ending in `-dirty` (klippy marks any untracked or ignored
  file under `klippy/extras/`). `git status` and Moonraker stay clean. Compare commits, not version strings.
* **Release signing is not whole yet.** The bundle is signed inside `make -C os image`, so the RAUC key is on the runner
  during the Buildroot package builds (narrowed to the Build step, removed right after). A deferred-signing mode in
  `os/board/eitri-plus4/post-bundle.sh` plus a separate signing job under a protected GitHub Environment is needed;
  `installer/keys/eitri-release.pub` does not exist, so the updater cannot verify anything yet.
* The flash-once U-Boot environment still counts down into the never-written `rootfs-b`; `eitri-update` ignores unconfirmed
  slots, but U-Boot itself does not (initialise B as bad in `os/board/eitri-plus4/uboot.env`).
* Already-flashed devices keep their seeded `go2rtc.yaml`; `eitri-go2rtc-auth check` makes go2rtc refuse to start with
  the old open one (migration is unwritten).
* `EITRI_DEV_PASSWORD_HASH` has no effect (Buildroot creates the `eitri` user after `post-build.sh`); it only affects
  bench images and belongs in a post-fakeroot hook. The image has no root shell for users until the password decision
  below is made.
* nginx's LAN allow-list covers IPv4 only; IPv6 is open (see decisions).
* `NoNewPrivileges` and the `MemoryHigh` values are unmeasured; Moonraker's machine and update components may need them
  relaxed on the first bench boot.
* The image carries no licence texts or `SOURCE-OFFER` (`os/legal/README.md`, finding 2); `uboot` is named `custom` in the
  manifest.
* `RESUME` after a pause with released motors refuses and asks for `G28`, which probes at the bed centre (possibly onto the
  part). A part-safe recovery needs a design.
* `shellcheck` and `actionlint` are not installed here and were not run after the last shell edits.
* Licences to confirm before publishing: `QIDITECH/QIDI_PLUS4` is AGPL-3.0 by its LICENSE file but GPL-3.0 by its README;
  `klipper-layer/config/` carries no SPDX tag; the CRC16/VLQ code in `eitri_mcuflash/klipper_proto.py` is
  protocol-dictated but close to Klipper's GPL `msgproto.py`.
* **Clean-room review pending:** `klarity/src/klarity/maps/default.yaml` was derived from analysis of the stock host and is
  interoperability data (ids, component names, what a tap does). `docs/CLEAN-ROOM.md` requires a second-person review
  of such a specification before it is published.
* One klipper-layer batch test (`test_config_loads_and_homes[kalico-chamber]`) failed once in an early full run and not in
  three later ones; the cause was not found. Watch for it.

## Decisions for Thomas

1. **Boot backend: GPT flash-once or DOS in-place?** Both are implemented and tested in simulation. Recommendation: ship
   DOS in-place as the user default (keeps the stock U-Boot so a bootloader is never erased; `revert.sh` is the recovery
   path) and keep the GPT image for bench, microSD and CI/QEMU. The OS code serves both.
2. **Where do the extras live?** `/data/eitri/extras` (the contract) versus the image's `/usr/lib/eitri/klipper-layer`
   with a `/data/eitri/klipper-layer` override. Pick one before the data bundle is built.
3. **Data seed design.** Pinned clones plus uv venvs built in CI and shipped as `data-seed.tar.zst`, or built on the printer
   at first boot? CI-built is faster for users but needs aarch64 builds and bigger releases; on-device is lighter to
   host but slow and network dependent. Nothing builds either yet.
4. **Root access for users.** Passwordless root is gone. Pick: a configurator step that sets a password, a boot-partition
   recovery file read by data-init, or no general root shell (physical or RAM-installer only).
5. **Release trust.** Who holds the minisign and RAUC private keys; create the `release` GitHub Environment with required
   reviewers; add the deferred-signing mode; when `EITRI_RELEASE=1` should imply the hard "release key required" error;
   bump `CLOCK_FLOOR` and `EITRI_MIN_MANIFEST_SEQUENCE` each release.
6. **Authentication of the web UI.** Should the printer UI (nginx :80, including `/webcam/`) get a login? Today it trusts
   the LAN. Related: the default of the configurator's new Access option ("Trust my home network" or "Require sign-in"),
   and whether to show the generated RTSP password in the wizard.
7. **IPv6 on nginx:** allow all (today), pin a prefix at runtime, or remove the IPv6 listener.
8. **Configurator after setup:** exit entirely (`--retire-when-locked`, saves about 30 MiB), stay up locked, and whether it
   binds `0.0.0.0:8099` or sits behind nginx. Does Klarity show the pairing code (`pairing.json`) and offer an unlock
   button?
9. **Moonraker port 7125 is loopback only** now; slicers and scripts must use port 80. Accept?
10. **Klipper-layer policy:** `gcode_shell_command` PARAMS unrestricted by default or default-deny; keep steppers energised
    during a long pause (current) or release and cancel; PLR resume with homed Z uses live Z; the chamber backstop sensor
    (`chamber_backstop_max_temp`, default 140) must be lowered to 85-90 if it sits in chamber air.
11. **Klarity strictness:** maps whose print cancel/pause/resume/start lack a guard now fail to load. Keep as an error or
    make it a warning? Same for the silent drop of repeat taps on an in-flight gcode action.
12. **Licences:** the AGPL/GPL question for `QIDI_PLUS4`, SPDX tag for `klipper-layer/config/`, and who does the clean-room
    review of the Klarity map.
13. **Python source:** `.py` files stay in the image (readable tracebacks); `PYC_ONLY` would save about 20 MiB uncompressed.

## Not built by anyone yet

1. **The data seed.** Nothing creates `/data/firmware/klipper` and `/data/firmware/moonraker` (pinned clones),
   `/data/venv/klippy` and `/data/venv/moonraker` (uv, Python 3.14, within Moonraker's dependency caps),
   `/data/eitri/klarity/venv` (needs pyserial-asyncio-fast, websockets, PyYAML), or the data bundle that
   `installer/docs/BUNDLE.md` describes (`data-seed.tar.zst`, `mcu-firmware.tar.zst`). `klipper.service` and
   `moonraker.service` do not start until it exists. This is the largest gap on the way to a first boot.
2. **MCU firmware builds** for both flavors (`installer/mcuflash` expects `<flavor>/manifest.json` plus
   `<board>/klipper.bin`), and the mainboard's recovery image (the stock one cannot be shipped).
3. **Release CI:** the minisign key, the RAUC CA (`os/board/eitri-plus4/rauc/keyring.crt` and the three `EITRI_RAUC_*`
   secrets), signing and upload of `install.json` and `manifest.json`, the stage-2 initramfs and the
   conservative-feature rootfs for U-Boot 2021.07 used by the in-place path. Download URLs in `install.sh` and
   `eitri-update` are guesses.
4. **Helpers the configurator calls if present:** `/usr/libexec/eitri/switch-flavor` and `/usr/libexec/eitri/toolchain`.
   Without them a flavor switch or the upstream add-on is only recorded.
5. MCU rebuild-and-flash after a git update in upstream mode is not hooked to Moonraker.
6. `spi_flash.py` needs `fatfs.so` or a host gcc on the device; the image has neither, so the mainboard can only be flashed
   through the `sdcard-file` method (and `revert --hard` cannot restore it automatically) until one is provided.
7. `eitri-configurator` is not on the SSH user's PATH (`python -m eitri_configurator unlock` in its venv works).

## Ordered next steps to a first boot on the printer

Nothing below touches the printer until step 6.

1. **Decide** the items in "Decisions for Thomas" that block building: boot backend (1), extras location (2), data seed
   design (3), root access (4), release trust (5).
2. **Commit the trust roots:** generate the minisign key and the RAUC CA, commit the public halves, set the CI secrets in a
   protected `release` Environment, add deferred signing. Confirm the licence questions; get the Klarity map reviewed.
3. **Build the data seed** (gap 1) and the MCU firmware for both boards (gap 2); add them to `release.lock` and CI.
4. **Rebuild the OS image** (`make -C os image`), run `make test` and `make -C os check`, then **repeat the QEMU smoke test**
   on the new image (`os/qemu/smoke-test.sh`): confirm `eitri-prepare`, `eitri-verify` and the camera skip behave as
   described, trim `known-failures.txt`, re-measure boot time and RAM, and add Klipper, Moonraker and Klarity to the RAM
   table once the seed lets them run. Run `package-legal.sh` once end to end.
5. **Prepare recovery before any write:** read `docs/RECOVERY.md`, take the full eMMC backup, prove maskrom entry and
   `rkdeveloptool` on the board while the stock system still boots.
6. **Bench boot from microSD** (never the eMMC first): open TPL DRAM init, Ethernet, `/data` creation, services. Check
   that `NoNewPrivileges` and the `MemoryHigh` limits do not break Moonraker, and tune them against measured RSS.
   Resolve the console question first: ttyS2 is wired to an MCU, so a console needs a deliberate bench setting.
7. **Probe, read-only:** `eitri-mcuflash plan` for UART roles and bootloaders; send `sendme` at 115200 to find the panel
   UART; `klarity --learn --dry-run` and walk the pages to confirm the map and the touch edge; boot once with the camera
   unplugged and once plugged in.
8. **Flash the MCUs** only after recovery for both boards is proven (`eitri-mcuflash` insists), then run the configurator:
   pair, detect, apply, flash, calibrate.
9. **First motion** with a hand on the power switch: homing (including partial `G28 X`/`Y`), probe polarity,
   `PROBE_CALIBRATE`, bed mesh, heaters, the interlock, pause/resume past the idle timeout, a short print. Re-pin
   Klipper to the commit that passed.
10. Only then write the eMMC (flash-once image or the in-place converter), exercise a RAUC update and a forced rollback,
    an interrupted install and an interrupted restore, and mark pins `tested = true` one by one.
