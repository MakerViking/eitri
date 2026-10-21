# Status

Last integration pass: 2026-10-22 (data seed and Thomas's 13 decisions). Pre-alpha. **Nothing in this repository has run on a printer.**
Every test below is a unit, simulation, emulation or static test; "Needs hardware" lists what none of them can prove.

`make test` runs every suite below (about 8 minutes, most of it the real klippy). `make test-fast` skips the klippy
integration tests. `make lint` is the syntax gate. `make seed` builds and verifies both data seeds (network, uv, zstd, the
Buildroot cross toolchain). `os/qemu/smoke-test.sh` boots the real image under QEMU and runs the first-run setup (about 10 minutes).

Legend: **done** = complete as far as it can be without hardware, **partial** = works but has a known gap, **stub** =
deliberately refuses to run.

## Where it stands

* The OS image (`make -C os image`, 2.14 GiB image, 119 MiB RAUC bundle) now ships with **a populated `/data`**: the klipper-flavor data seed is installed
  into `data.ext4` at image build time. Nothing is downloaded or compiled on a first boot.
* In QEMU the whole application layer runs for the first time: **Klipper (klippy), Moonraker, nginx, Klarity** are active, the
  setup wizard drives the real services (pair, detect, apply, optional SSH key, finish, retire, unlock) and every one of its 75
  checks passes, with the Klipper seed and with the Kalico seed. klippy loads the rendered `printer.cfg` and every `qd_*`
  extra and then, correctly, cannot find an MCU.
* What is still unproven is everything that needs a printer: see "Needs hardware" and "Next steps".

## Components

| Component | State | Tests (last `make test`) | What is missing |
|---|---|---|---|
| Repository core (`README`, `docs/`, `release.lock`, licences, CI, `tools/release`) | done | `tools/check-repo.py`: 1284 checks; release tooling 106 passed; `tools/lint.py` clean | Python and nginx source digests are resolved from Buildroot at build time, not pinned in `release.lock`. Every pin is `tested = false`. Klipper is pinned to an untested master commit. No release key is committed (the public halves do not exist yet). CI has never run on GitHub. |
| `seed/` (data seed: build, verify, reference installer) | done (never on a printer) | 159 passed (spec, manifest, archive policy, installer on synthetic and real seeds, built-seed checks, workflow shape); `make seed` verifies both seeds under qemu-aarch64 | The CI workflow (`data-seed.yml`, also called by `os-image.yml` and `qemu-smoke.yml`) has not run on GitHub; the Ubuntu gcc and the pinned python-build-standalone headers path was only emulated locally. First-boot timing of an unpack on the A53 and the first slow `git status` are unmeasured. |
| `os/` (Buildroot image, RAUC bundle, U-Boot, kernel, DTS, QEMU rig) | partial | 234 passed, 4 skipped (U-Boot sandbox); `check-pins` and `check-defconfig` (163 options) pass; QEMU smoke test 75/75 with the Klipper seed and 75/75 with the Kalico seed | Never booted on the board. No release signing CA or key is committed. U-Boot sandbox tests skip (no sandbox build here). |
| `klarity/` (screen daemon) | partial | 242 passed (warnings are errors), ruff clean | Screen map is 66 pages / 511 widgets, **104 flagged `todo`**, none checked against a live panel. The **Unlock setup** button reuses stock Settings widget 9 (page 0x3e) and the text field `t7`: both are assumptions about the panel. File browser, preview/print start, session state, thumbnails and forced page changes are not expressible. No camera page exists in the stock UI. Home e-stop widget is still `todo` (inert). |
| `klipper-layer/` (qd_* extras, printer.cfg template, macros) | partial | 259 passed: unit tests, overlay install on throw-away clones of Klipper and Kalico, render tests, licence tags, real klippy batch tests for both flavors | `qd_probe` and `qd_reverse_home` are **stubs** (they refuse to load). Macros, currents, probe, interlock, power-loss recovery and the new 85 °C chamber backstop are untested on a machine. |
| `installer/` (in-place install, `eitri-mcuflash`, `eitri-update`, boot manager, seed/MCU delivery, flavor switch) | partial | mcuflash 102, eitri-update 223, installer suite 162 passed (includes a real U-Boot 2021.07 sandbox run of `boot.scr`) | Never run against a board. No release signing key, placeholder download URLs, no stage-2 initramfs builder. The flasher refuses the toolhead (see "MCU firmware") and has no `hid-flash` path for the mainboard. |
| `mcu/` (MCU firmware parameters, configs, build and packaging tooling) | partial (**no firmware built**) | 48 passed, 1 skipped (artifact checks skip without a build) | Nothing compiled: the parameter gate stops the build while the toolhead is unresolved, and no toolchain is pinned in `release.lock`. The compile path has never run. See "MCU firmware". |
| `configurator/` (setup wizard, port 8099) | done | 170 passed, including the real `eitri-mcuflash` CLI against non-existent ports, the real klipper-layer renderer and the real `eitri-access` helper | Pairing, lock/retire, nmcli handling, polkit and the remote-access step are proven in QEMU, not on hardware. |
| `camera/` (ustreamer, go2rtc, udev) | done | 125 passed, 0 failed, 4 skipped (hardware only), including real go2rtc 1.9.14 auth enforcement | ustreamer v6.67 flags unchecked against the real binary. The 23.7 fps cause is unknown. `/webcam/` has no authentication (RTSP does). |

## The data seed (decision 3)

Built by `seed/build-seed.sh` (CI: `.github/workflows/data-seed.yml`), one archive per flavor, specified in `seed/SPEC.md`.
Reproducible: two builds from empty work directories give identical archives, also with uv 0.11.19 and the pinned 0.13.4.

| | archive | unpacked | pinned firmware | sha256 (this build) |
|---|---:|---:|---|---|
| Klipper | 55.2 MiB (57,869,239 B) | 383.0 MiB | Klipper `a08fc79` (`v0.13.0-813`), Moonraker `v0.11.0` | `7f5c893ed2ff...` |
| Kalico | 94.4 MiB (98,961,199 B) | 471.9 MiB | Kalico `v2026.10.00`, Moonraker `v0.11.0` | `b83ece442f31...` |

Contents: shallow git clones at the exact `release.lock` commits (clean `git status`), uv venvs for CPython 3.14 / aarch64 built
from hash-pinned locks (klippy, Moonraker, Klarity), a cross-compiled `c_helper.so` (klippy never calls gcc on the printer),
the `qd_*` extras, the klipper-layer config set, `eitri/flavor` and the seed record. The flash-once `data.ext4` is 609 MiB with the Klipper seed. The in-place installer's stage 2 and `eitri-update` consume the same archive with `eitri-seed-install`.

## Measurements (QEMU virt, no printer)

Taken on the final image, one run, with the data seed installed and **Klipper (klippy) + Moonraker + Klarity + nginx
running** and the configurator retired (second boot, after the setup). QEMU virt is 4 x Cortex-A53 under TCG (software emulation)
with 1024 MiB on a 24-core x86 host: **times compare one run with the next, not with the printer; memory sizes do not depend on
the CPU.** klippy has no MCU here (it only loaded its config and keeps retrying), so its numbers are a floor, not the printing figure.

| | first boot, before the setup (configurator running) | same boot, after the setup (configurator retired) | second boot (configurator retired) |
|---|---:|---:|---:|
| kernel | 0.49 s | 0.49 s | 0.49 s |
| userspace to `multi-user.target` | 15.6 s | | 14.9 s |
| last service active (since kernel start) | 14.1 s (sshd) | 73.6 s (klarity, started by Apply) | 12.8 s (klarity) |
| memory used (total - available - the measuring tool) | 142 MiB | 211 MiB | **215 MiB** |
| processes: RSS / PSS | 134,336 / 77,290 kB | 234,852 / 155,184 kB | 233,636 / 154,716 kB |
| poweroff | 3.3 s | | 3.1 s |

Per unit on the second boot, kB (RSS / PSS):

| unit | RSS | PSS |
|---|---:|---:|
| moonraker | 65,352 | 55,432 |
| klipper (klippy) | 38,536 | 30,040 |
| klarity | 34,276 | 25,406 |
| NetworkManager | 16,396 | 9,707 |
| systemd (init) | 11,744 | 6,552 |
| udevd | 10,284 | 5,134 |
| journald | 7,832 | 3,046 |
| sshd | 7,172 | 2,529 |
| logind | 7,164 | 2,419 |
| polkit | 6,848 | 3,357 |
| nginx | 5,692 | 2,344 |
| wpa_supplicant | 5,484 | 2,545 |
| chrony | 4,860 | 1,671 |
| dbus | 3,780 | 1,708 |
| avahi | 4,520 | 1,195 |
| **total** | **233,636** | **154,716** |

Kalico instead of Klipper (its own run, 75/75): klippy 53.6 MB RSS / 44.7 MB PSS (Klipper 38.5 / 30.0), memory used 221 MiB (Klipper 215 MiB). The configurator was the largest process before the setup (33 MiB PSS); retiring it
saves that much (the second-boot total would be roughly 33 MiB higher with it running). On the printer klippy and Moonraker will grow with a real MCU connection,
a bed mesh and a file list, so the real budget is still unknown.

`multi-user.target` is reached about 15 s into userspace in both boots (the camera unit is skipped with no camera; the
critical path is device probing, `eitri-data-init` and sshd). Details of the rig: `os/qemu/README.md`.

## What the first-run setup checks in QEMU

`os/qemu/smoke.py` (75 checks; the rootfs is the real image, `/data` is built from the seed by the same `mkdata.sh` as the
printer image, only the kernel's platform options and the missing hardware differ):

* the seed is on `/data` from the first boot, the Klipper clone has a clean `git status`, Klipper/Moonraker/Klarity wait quietly
  for their configuration;
* pairing from the code in `/data/eitri/configurator/pairing.json`, detect, **apply** (renders `printer.cfg`, `klarity.cfg`,
  `moonraker.conf`, `eitri-access.conf`, links the extras, restarts Klipper/Moonraker/Klarity);
* Klipper, Moonraker, Klarity active with no restarts; Moonraker answers on `127.0.0.1:7125` and through nginx on 80, and not on
  the guest's address; klippy loaded the config with all its `qd_*` extras and reports only the missing MCU; Klarity has the
  Unlock-setup feature and a valid map and waits for `/dev/ttyS1` with a back-off instead of crash-looping;
* the optional **root-access step**: add an SSH key through `sudo -n eitri-access`, log in over SSH with it from the host, remove it,
  login refused;
* finish: the configurator locks and **retires** by itself; Klarity's sandbox (a transient unit with `klarity.service`'s own mount,
  device and cgroup options) runs `sudo -n /usr/bin/eitri-configurator unlock` under the image's sudoers rule and the wizard
  comes back with a new code; lock again, retire again; on the next boot it stays retired.

## What the integration pass found and fixed

None of these was visible in a unit test; each came from running the real services together.

* **sudo was blocked in the configurator and Klarity units.** systemd sets `NoNewPrivs` on a service that uses any
  seccomp-based sandbox option (`SystemCallFilter`, `ProtectKernelTunables`, `RestrictNamespaces`, `LockPersonality`,
  `RestrictAddressFamilies`, `MemoryDenyWriteExecute`, ...) when the service user cannot install a filter itself, even with
  `NoNewPrivileges=false`. `sudo -n eitri-access` and Klarity's `sudo -n eitri-configurator unlock` would have failed on the
  printer. Both units now use only mount, device and cgroup sandboxing; tests forbid the seccomp options in them.
* **Apply failed on a root-owned `/data/eitri`** (the OS lane keeps it root's for sshd's StrictModes and the root helpers): the
  configurator could not create `flavor`/`channel`/`ui`/`hostname`. `flavor`/`channel` are now service-user files rewritten in
  place (and kept by `eitri-update`), the hostname memo lives in `eitri/configurator`, and the web UI choice is the symlink
  `eitri/ui/current` (which also fixes a latent bug: the old code replaced the `ui` symlink with a text file, which would have
  broken nginx). `data-init` converts the old layout.
* **Apply did not start Klipper and Moonraker** on a fresh install (they are skipped at boot until their configuration exists);
  Calibrate would have found Moonraker down. Apply now restarts Klipper, then Moonraker.
* **`eitri-access` reset its own request counter to 0** while waiting (it truncated the file before reading it), and its sshd reload
  deadlocked against its own `Before=sshd.service` ordering until a 30 s timeout. Both fixed (`reload --no-block`).
* **The seed shipped a stale Klarity**: uv's cache reused an older wheel with the same name and version (fixed build time stamps),
  so the seeded Klarity had no Unlock-setup feature. The build now refreshes locally built wheels, and a test compares the seed's
  Klarity and extras with the repository byte for byte.
* **CI did not build a seed for the image.** `os-image.yml` and `qemu-smoke.yml` now call `data-seed.yml` (klipper flavor, nothing
  attached to a release) and put the seed into the image / the QEMU `/data` disk; `release.yml` takes the release seeds from the
  data-seed run only.
* The shared interface said the pairing file is `/run/eitri/configurator/pairing.json`. The real path, used by the configurator and
  Klarity, is `/data/eitri/configurator/pairing.json`.
* Removed the duplicate `configurator/bin/eitri-configurator` (the OS overlay's `/usr/bin/eitri-configurator` is the only one).

## What each decision changed (Thomas, 2026-10-22)

| # | Decision | Done in |
|---|---|---|
| 1 | DOS in-place is the user default; the GPT image is for bench/microSD/CI | `installer/` docs, `install.sh --help`, `docs/BOOT.md`; QEMU/CI use the GPT layout |
| 2 | Extras and layer config live on `/data`, follow Klipper/Kalico | package `eitri-klipper-layer` and `/usr/lib/eitri/klipper-layer` removed; seed ships `eitri/extras` + `eitri/klipper-layer`; `overlay-install.sh` links to `/data/eitri/extras`; docs updated |
| 3 | Seed built by CI per release, first boot offline | `seed/` (this pass's biggest piece); `mkdata.sh` populates `data.ext4`; stage 2 and `eitri-update` consume it; `eitri-seed.service` is the first-boot fallback |
| 4 | Root access is an optional configurator step, off by default | `eitri-access` helper + sudoers + `eitri-access.path`; configurator "Remote access" step; proven in QEMU incl. a real SSH login |
| 5 | Release keys offline; CI signs only via the protected `release` environment | `release.yml`, `tools/release/`, `docs/RELEASING.md`, deferred signing in `post-bundle.sh` (no keys committed yet) |
| 6 | Web login asked in setup, default "Trust my home network"; RTSP password on a separate page | configurator option + `camera.html` |
| 7 | nginx IPv6 listener removed | `nginx.conf` listens on `0.0.0.0:80` only |
| 8 | Configurator exits after setup; Klarity "Unlock setup" | `--retire-when-locked` default; `eitri-configurator unlock` wrapper; Klarity `unlock: setup` action (stock widget 9 on page 0x3e); proven end to end in QEMU except the panel tap |
| 9 | Moonraker loopback only, slicers use :80 | Moonraker binds 127.0.0.1; proven in QEMU (not reachable on the guest address) |
| 10 | shell PARAMS denied by default; motors stay energised in a long pause; PLR uses live Z when homed; chamber backstop 85 °C | `gcode_shell_command` + `qd_shell_guard` (Kalico), idle/pause macros, `qd_plr`, `eitri-variables.toml`; real-klippy tests for both flavors |
| 11 | Klarity print-control widget without a guard stays a load error | unchanged, tests keep it |
| 12 | `QIDI_PLUS4` treated as AGPL-3.0 | SPDX `AGPL-3.0-or-later` on `klipper-layer/config/{printer.cfg.tmpl,macros.cfg,eitri-variables.toml}`, `config/LICENSE`, `LICENSE-NOTE.md`, README/NOTICE |
| 13 | Keep `.py` source in the image | unchanged (the seed's venvs also keep `.py` next to the byte-code) |

## MCU facts measured on the real printer (2026-10-22, read-only probe)

Measured with Klipper's built-in `debug_read` (klippy stopped, then restarted; nothing written). **These supersede the inferred/unresolved values in the MCU section below.**

| | mainboard (`/dev/ttyS0`) | toolhead (`/dev/ttyS2`) |
|---|---|---|
| chip | genuine STM32F401xC (IDCODE 0x16423), 256 KiB | IDCODE 0x21040414, flash-size register 128 KiB → almost certainly a GD32F103 clone (built as stm32f103xe) |
| SCB->VTOR / app offset | **0x08008000 (confirmed)** | **0x08000000 — no bootloader** |
| bootloader | stock QIDI/Makerbase, 32 KiB at 0x08000000 | **none** |
| crystal (from RCC registers) | **8 MHz confirmed** (PLLCFGR 0x07412A04: M4 N168 P4, HSE) | **8 MHz confirmed** (CFGR 0x001DE40A: HSE, x9) |

Consequences:
- **Mainboard is buildable and serial-flashable**: offset and crystal confirmed on the board; flash via the stock bootloader / SD path. Its gate can flip to go once a real build passes the dictionary checks.
- **Toolhead has no bootloader**, so it cannot be reflashed over serial at all — **but it does not need to be**: the toolhead can stay on its stock Klipper v0.12.0-65 firmware with the newest host. Verified 2026-10-22: all 84 klippy batch tests pass on BOTH mainline Klipper and Kalico with the stock toolhead dictionary (extracted from the dump) when the ADXL345 block is off. Only the accelerometer query formats changed since v0.12 (mainline keeps a backward-compat path for the old `query_analog_in`/`analog_in_state`, so thermistors work; Kalico's analog format is unchanged). **Plan:** default install keeps the toolhead stock and reflashes only the mainboard; add a new-named extra `qd_adxl345_compat` that speaks the v0.12 ADXL345 format so input-shaper calibration works without flashing; offer a one-time ST-Link/SWD Katapult install as an *optional* advanced step (future-proofing). Batch mode proves command compatibility, not real-time behaviour — the first real run is the final check.
- Full stock flash dumps of both boards exist as private recovery images, kept outside the repo per the clean-room policy.

## MCU firmware (`mcu/`, `installer/mcuflash`)

**State: tooling and checks are built and green; no firmware was built and no release bundle exists, on purpose.** The parameter
gate (`mcu/params.json`, `make mcu-build`) stops the build while any board is unresolved, and the toolhead is. No toolchain was
downloaded, so `release.lock` has no `[mcu.toolchain]` entry (the build report gave no verified value; add one with Arm's published
SHA-256 when the build goes ahead). Nothing touched the printer or any device. Derived parameters only; no Qidi binary or
disassembly is in the repository (how each value was derived: `mcu/params.json`, `mcu/README.md`).

Facts, source **the live `klippy.log` of the printer** (stock Klipper v0.12.0-65 on the board):

| | mainboard (`[mcu U_1]`) | toolhead (`[mcu]`) |
|---|---|---|
| host port | `/dev/ttyS0` (USART1 PA10/PA9) | `/dev/ttyS2` (USART1 remapped PB7/PB6) |
| chip, clock | `stm32f401xc`, 84 MHz, crystal reserved (PH0/PH1) | `stm32f103xe` (may be a GD32F103 clone), 72 MHz |
| baud | 500000 | 500000 |
| crystal | 8 MHz (inferred from community images that boot, PLL M=4) | HSE certain, 8 MHz likely |
| bootloader | stock QIDI/Makerbase at `0x08000000`, ~32 KiB; UART path (`hid-flash`, 115200) and SD path (`qd_mcu.bin`); **not Katapult** | **unknown**: Katapult only if someone installed it with an ST-Link, factory layout unknown |
| application offset | **`0x08008000`** (`STM32_FLASH_START_8000`) | **unsettled**: `0x08002000` if Katapult, factory value unknown (`0x08008000` is only inferred from the Q2 toolhead) |
| flash method | `flash-sdcard.sh -b 500000 /dev/ttyS0 qidi-plus4` (Klipper script tree) or `qd_mcu.bin` on a FAT32 card; never Katapult | Katapult `flashtool.py`, only if Katapult answers a read-only handshake (250000 and 500000) |
| confidence | **likely** (no `SCB->VTOR` read yet) | **unsure** |
| state in `params.json` / `boards.json` | resolved | **unresolved, never written** |

What the flasher now enforces: boards are matched by chip name (never by port alone); an image whose data dictionary disagrees with the
live constants is blocked; a settled board's image must have a vector table (SP in SRAM, Thumb reset vector) inside the window that
starts at the application offset, checked at plan time **and again after an on-device build**; the stock recovery image gets the same
test; a mainboard that answers as Katapult is blocked; the toolhead is blocked unless Katapult answers right now and the image is linked
for the `app_start` it reports; `sdcard-file` fsyncs the file and directory and calls `sync` before telling the operator to pull the
card; `sdcard-serial` passes `spi_flash.py` the dictionary byte for byte (a re-serialised copy made every good flash end in "Version
Mismatch" and a rollback to stock). Kalico has no `qidi-plus4` board definition, so on a Kalico tree only `sdcard-file` (`--sd-mount`) can
write the mainboard. The mainboard's `hid-flash` path is not implemented (safe: fewer ways in).

Bundle sizes: **none built** (nothing to measure). Verification so far is on synthetic images, including a bundle read back by the
flasher's own loader, plus Kconfig expansion (`make olddefconfig`, no write to the trees) of both configs under the pinned Klipper
`a08fc79` and Kalico `v2026.10.00`, and a parse (never execution) of the FreeDi community images: the vector tables, link
addresses and dictionaries agree with the numbers above. `make mcu` runs the check-only pass (`v0.13.0-813-ga08fc79fc`,
`v2026.10.00-0-g3526de3bf`).

Independent review (two reviewers, both read the code and re-ran the checks): **safe_to_flash = false for both boards.**

| board | verdict | why |
|---|---|---|
| toolhead | **not safe** | bootloader type and offset unknown; a wrong guess needs an ST-Link. Refused by the gate, the registry and the flasher by design. |
| mainboard | **not safe yet** | parameters rest on community images, not on a read of the board (`SCB->VTOR`, crystal); nothing was built, so the dictionary-versus-live-constants check has not run on a real artifact; recovery needs the case open, a FAT32 card and a stock `qd_mcu.bin`. |

Reviewer findings fixed in this pass (no hardware needed): `--build` skipped every image check (now re-run after the build, a mismatch
raises `build_mismatch` before anything is staged) and looked for the wrong config file name (`<board>.config` or the one
`<board>-*.config` the repository ships); `sdcard-file` had no fsync; `spi_flash.py` got a re-serialised dictionary; the flasher did not
look at the vector table; a stock recovery image of the wrong offset was accepted. Findings that stay open, by design or for lack of
hardware: no `hid-flash`; mainboard rollback needs a booting image, otherwise manual SD recovery; the Katapult handshake is also sent
to the mainboard port (read-only, untested); `--registry` can override the toolhead refusal (documented as bench-only); the installer's
MCU step dies with code 13 while the toolhead is blocked, `--mcu skip` leaves the stock MCU firmware (v0.12.0-65) under the new host, and
that skew is untested; GD32 clone and `STM32F103GD_DISABLE_SWD` undecided.

## Legal-info and SBOM

`make -C os legal-info` completes. 107 target packages (95 host build tools are not distributed): 44 strong copyleft, 15 weak
copyleft (LGPL, MPL), 48 permissive, 1 proprietary-but-redistributable (`linux-firmware`, Realtek rtlwifi blobs, unmodified
redistribution allowed with the notice). The source set is 1.29 GB; without the 557 MiB `linux-firmware` tarball it is about 709 MB
and fits a GitHub release asset. `os/legal/make-sbom.py` writes CycloneDX 1.5 and SPDX 2.3. `os/legal/package-legal.sh` and
`.github/workflows/legal.yml` pack sources, licences, the SBOMs and a `git archive` of the tag; **neither has run end to end**. The
SBOM was not regenerated for the data seed (Python wheels, Klipper, Kalico, Moonraker clones): the seed manifest lists every package
with versions and hashes (`venvs.*.packages`), but no licence report exists for the wheels yet. Details: `os/legal/README.md`.

## Earlier work (kept for context)

* **Before the hardening run:** `printer.cfg` comes from the klipper-layer (the configurator renders `printer.cfg.tmpl` with the
  layer's renderer and seeds `moonraker.conf`); options the layer cannot honour were removed from the wizard; the config path clash
  for `eitri-update-manager.conf` was fixed; Klarity gained the stock frame/`on_number` actions; `Makefile`, `tools/lint.py`,
  `tools/check-repo.py` and CI were added.
* **The hardening run (2026-10-21)** fixed, among others: slot commit only after health (`eitri-update` is the only committer), the
  fresh-`/data` boot failures, the open camera gateway (RTSP password, no WebRTC), the unauthenticated configurator (pairing code,
  CSRF, lock), Moonraker's wildcard CORS (loopback + nginx), blanket passwordless root, the bricking paths in the installer
  (`revert`, MCU round trip consent), the release supply chain (SHA-pinned actions, expiring manifests, revocation, sequence
  floor), and machine safety in the klipper-layer (`G28 X/Y`, resume after the idle timeout, PLR mesh, chamber interlock). It also
  removed all fire imagery and wording. `verify_heater` is untouched and live in every configuration; a pre-triggered probe is
  still an error; the MCU flasher still never erases a bootloader; installer writes are dry-run by default. No Qidi binary or asset
  entered the repository.
* Build-system fix: local Buildroot packages are rebuilt (`<pkg>-rebuild`) by `make -C os image` and `os/qemu/build.sh`.

## Needs hardware (none of this is proven)

* **Boot chain:** open TPL DRAM init on this board (fallback: Rockchip DDR blob), BootROM order between microSD and eMMC, mmcblk
  numbering, A/B state sector, TF-A v2.15 BL31, Ethernet (gmac2phy), USB VBUS pin for the camera, regulators.
* **MCUs** (see "MCU firmware"): the toolhead's bootloader and application offset (Katapult? stock?) and a read of `SCB->VTOR` on both
  boards, both read-only through klippy's `debug_read` or a Katapult handshake. The UART assignment itself is settled by the live
  `klippy.log` (`/dev/ttyS0` = mainboard, `/dev/ttyS2` = toolhead) and is still confirmed by `eitri-mcuflash plan`, which binds boards by chip.
* **Screen:** that the panel is on `/dev/ttyS1` at 115200 under mainline, press vs release edge, string escaping, whether marker bytes
  carry terminators, the map against the live panel, 0x71 handling, and that **widget 9 on page 0x3e is a button that reports taps
  and that `t7` is wide enough for "Code 1234-5678 at 192.168.0.5:8099"**.
* **Klipper machine:** sensorless XY homing currents and thresholds, the 7 mm lift then probe-down Z homing, the PA10 probe identity
  and polarity (`probe_z_offset` ships as 0 and PRINT_START refuses to run until it is calibrated), the bed/chamber interlock against
  the real PSU, chamber PID, the 85 °C backstop against where the sensor really sits, power-loss resume with live Z, pause with
  energised motors past the idle timeout, the 0.1 s multi-MCU homing timeout. klippy on the A53 with a real MCU link: memory, CPU,
  first-start time of the seeded venv (byte-code is pre-compiled), the first slow `git status` (stat-free index).
* **Updates:** RAUC A/B with rollback, boot counting, `eitri-verify`, MCU rollback, in-place conversion (U-Boot `boot.scr` order,
  `fatwrite`, stage 2 on the stock 5.16 kernel, resize time, the seed unpack on stage 2), seed update and flavor switch with real MCU flashing.
* **Camera:** 1080p fps and whether auto-exposure throttles it, `--encoder=HW` passthrough, udev hot-plug, OBS over RTSP/MJPEG,
  hardware H.264 (expected absent).
* **Network:** rtl8xxxu throughput and AP mode, NetworkManager hotspot, mDNS name.
* **Sandboxes:** `MemoryHigh` values against real RSS; Moonraker's `machine`/`update_manager` components under `NoNewPrivileges`.

## Known quirks and open issues

* A Klipper clone with the overlay reports a version ending in `-dirty` (klippy marks any untracked file under `klippy/extras/`; it
  shows in Moonraker too: `v0.13.0-813-ga08fc79fc-dirty`). `git status` stays clean. Compare commits, not version strings.
* **Release signing is not whole yet.** `release.yml` + `tools/release` exist (deferred signing under a protected `release`
  environment), but `installer/keys/*.pub` and `os/board/eitri-plus4/rauc/keyring.crt` are not committed, so a tag build refuses to run
  and the updater cannot verify anything. Nothing has run on GitHub.
* The flash-once U-Boot environment still counts down into the never-written `rootfs-b`; `eitri-update` ignores unconfirmed slots,
  but U-Boot itself does not (initialise B as bad in `os/board/eitri-plus4/uboot.env`).
* Already-flashed devices keep their seeded `go2rtc.yaml`; `eitri-go2rtc-auth check` makes go2rtc refuse to start with the old open
  one (migration is unwritten).
* `EITRI_DEV_PASSWORD_HASH` has no effect (Buildroot creates the `eitri` user after `post-build.sh`); the QEMU image sets the bench
  passwords in `os/qemu/post-fakeroot.sh`. The printer image has no password at all: the first way in is the optional remote-access
  step, or the touchscreen/web UI.
* The seccomp-based systemd sandboxing is not available to the configurator and Klarity units (above): a flaw in either runs with
  the user's file access and the narrow sudo rules, without a syscall filter. Klipper and Moonraker keep `NoNewPrivileges`.
* The image carries no licence texts or `SOURCE-OFFER` (`os/legal/README.md`, finding 2); `uboot` is named `custom` in the manifest.
* `RESUME` after a pause with released motors refuses and asks for `G28`, which probes at the bed centre (possibly onto the part). With
  decision 10 the motors stay energised in a long pause, so this should now only apply after a manual motor release or a Klipper restart (not re-tested).
* `/data/eitri/updates/os-*.raucb` is not deleted after a committed update.
* `shellcheck` and `actionlint` are not installed here and were not run on the shell scripts and workflows (`sh -n`/`bash -n` and a
  YAML parse only).
* **Clean-room review pending:** `klarity/src/klarity/maps/default.yaml` was derived from analysis of the stock host and is
  interoperability data. `docs/CLEAN-ROOM.md` requires a second-person review of such a specification before it is published (the
  lead does it, decision 12).
* The configurator's `/usr/libexec/eitri/toolchain` helper (upstream mode's compiler add-on) does not exist; choosing it is only recorded.

## Not built by anyone yet

1. **MCU firmware builds** (tooling exists in `mcu/`, nothing is built, see "MCU firmware") for both flavors: `mcu-firmware-<version>-<flavor>.tar.zst` with `<flavor>/manifest.json` plus
   `<board>/klipper.bin` (what `eitri-mcuflash` and stage 2 expect), and the mainboard's recovery image (the stock one cannot be
   shipped). The MCU build must use the seed's source tree (or `git -c core.abbrev=9 describe`) so the host and MCU version
   strings agree with `sources.firmware.describe`. **Without them the in-place installer and the wizard's Flash step cannot run.**
2. **Release trust roots and the first signed release:** minisign key, RAUC CA, the three `EITRI_RAUC_*` secrets / protected
   environment, the stage-2 initramfs and the conservative-feature rootfs for U-Boot 2021.07 used by the in-place path, final download
   URLs in `install.sh` and `eitri-update`.
3. Upstream mode: the compiler add-on helper, and MCU rebuild-and-flash after a git update (not hooked to Moonraker).
4. `spi_flash.py` needs `fatfs.so` or a host gcc on the device; the image has neither, so the mainboard can only be flashed through
   the `sdcard-file` method (and `revert --hard` cannot restore it automatically) until one is provided.
5. Licence report for the seed's wheels and clones in the SBOM.

## Next steps to a first boot on the real printer

Step 1 touches the printer read-only and needs Thomas's permission. Nothing on the printer is written before step 8 (step 6 boots a microSD card, which is not a write to the printer's storage). Steps 2 to 5 are at the desk; Thomas has to do 1, 3, 4 and 6 onwards himself.

1. **Settle the MCUs read-only (Thomas's permission needed; nothing is written).** With klippy stopped: a Katapult handshake on
   `/dev/ttyS2` at 250000 and 500000, and `debug_read` of `SCB->VTOR` (`0xE000ED08`) plus the words at `0x08000000/04` on **both** boards.
   Mainboard: confirm `0x08008000`. Toolhead: `0x2000` means Katapult (then also read its `app_start` and baud), `0x8000` means a stock
   layout (then find its flash protocol, nothing documents one), anything else needs a new analysis. If the toolhead shows no
   Katapult, the way forward is an ST-Link, not a workaround. Then change `mcu/params.json` and `boards.json` in the same commit
   (`test_params.py` fails if they drift). Optionally with permission: whether QIDI's OTA package contains the stock MCU images.
2. **Build the MCU firmware** once the gate passes: resolve the Arm toolchain version and SHA-256 against Arm's page, add
   `[mcu.toolchain]` to `release.lock`, run `make mcu-build MCU_VERSION=... MCU_TOOLCHAIN=...` (or the `mcu-firmware` workflow), and
   check the built dictionaries against `mcu/tests/live_constants.json` (`mcu/tests/test_artifacts.py` stops skipping). Decide GD32 /
   `STM32F103GD_DISABLE_SWD` before the toolhead config is frozen. Get a stock `qd_mcu.bin` for mainboard recovery.
3. **Generate the trust roots (Thomas, offline):** the minisign key pair and the RAUC CA; commit only the public halves
   (`installer/keys/eitri-release.pub`, `os/board/eitri-plus4/rauc/keyring.crt`); create the protected `release` GitHub environment with
   Thomas as required reviewer and store the private keys as its secrets. Push a tag to run CI once end to end (nothing in this
   repository has run on GitHub yet); fix what the first CI run finds (Ubuntu gcc path of the seed, actions, caches).
4. **Prepare recovery before any write:** read `docs/RECOVERY.md`; take the full eMMC backup of the stock system; prove maskrom entry
   and `rkdeveloptool` on the board while the stock system still boots; keep the stock `revert.sh` path ready.
5. **Choose the first image:** build `make seed && make -C os image` (or take the CI artifact), `dd` the image to a **microSD card**
   (never the eMMC first). The image already has Klipper, Moonraker, Klarity and the setup wizard on it.
6. **Bench boot from microSD (printer powered, USB-serial or Ethernet attached, MCU cables removed or the MCUs unpowered for the first
   boot):** watch the open TPL DRAM init, Ethernet, `/data` growth, that the services come up. Check `NoNewPrivileges` and the
   `MemoryHigh` limits against Moonraker, and tune them against measured RSS. Ttys2 is wired to an MCU, so a console needs a
   deliberate bench setting.
7. **Probe, read-only:** `eitri-mcuflash plan` for UART roles and bootloaders; send `sendme` at 115200 to find the panel UART;
   `klarity --learn --dry-run` and walk the pages to confirm the map, the touch edge, and widget 9 / `t7` for Unlock setup; boot once
   with the camera unplugged and once plugged in. Put the answers into the screen map and `STATUS.md`.
8. **Flash the MCUs** only after recovery for both boards is proven (`eitri-mcuflash` insists), then run the wizard from a phone or PC
   with the code shown on the screen: pair, detect, apply, flash, calibrate. Optionally add your SSH key in the Remote access step.
9. **First motion** with a hand on the power switch: homing (including partial `G28 X`/`Y`), probe polarity, `PROBE_CALIBRATE`, bed mesh,
   heaters, the interlock and the 85 °C chamber backstop, pause/resume past the idle timeout (motors stay energised), power-loss resume,
   a short print. Re-pin Klipper to the commit that passed (`release.lock`).
10. Only then write the eMMC: the in-place converter (the user path, decision 1) on a printer you can recover, or the flash-once image on
   a spare board. Exercise a RAUC update and a forced rollback, a seed update, a flavor switch, an interrupted install and an interrupted
   restore, and mark pins `tested = true` one by one.
