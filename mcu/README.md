# Eitri MCU firmware

Tooling, configs and checks for the Klipper/Kalico firmware of the X-Plus 4's two STM32 boards. Layout of what ships and
who reads it: [`installer/docs/BUNDLE.md`](../installer/docs/BUNDLE.md) (`mcu-firmware.tar.zst`), `eitri-mcuflash`
([`installer/mcuflash`](../installer/mcuflash/README.md)) and `eitri-update`.

> **Status: no firmware bundle is built.** One board's flash layout is an open question (`go = false` in
> [`params.json`](params.json)), and a firmware linked for the wrong offset or crystal does not boot. So:
> `build-mcu.sh` stops (exit 3) before it fetches or compiles anything, the CI workflow builds nothing and fails a `v*`
> tag, and `eitri-mcuflash` refuses to write the unresolved board. The tooling, the configs and their checks exist and are
> tested; the bundles will appear when the question is answered.

## The two boards

Identified by the MCU name in their data dictionary, never by port. Constants are those of the live printer's `klippy.log`
(`tests/live_constants.json`).

| | Mainboard | Toolhead |
|---|---|---|
| Klipper section, port | `[mcu U_1]`, `/dev/ttyS0` | `[mcu]`, `/dev/ttyS2` |
| Chip, clock | `stm32f401xc`, 84 MHz | `stm32f103xe`, 72 MHz (may be a GD32F103 clone) |
| Crystal | 8 MHz external (`STM32_CLOCK_REF_8M`); PH0/PH1 reserved | 8 MHz external: HSE certain, 8 MHz likely |
| Serial | USART1 PA10/PA9, 500000 baud | USART1 remapped PB7/PB6, 500000 baud |
| Application offset | **`0x08008000`** (`STM32_FLASH_START_8000`, behind a 32 KiB bootloader) | **unresolved**: `0x08002000` if Katapult, unknown if stock |
| Bootloader | stock QIDI/Makerbase at `0x08000000`: a UART path (`hid-flash`, 115200 8N1) and an SD-card path. **Not Katapult** | unresolved: Katapult (community add-on, ST-Link install) or the unknown stock one |
| Flash method | application only: `flash-sdcard.sh -b 500000 /dev/ttyS0 qidi-plus4`, or `qd_mcu.bin` on the card | Katapult `flashtool.py`, only while Katapult answers (250000 or 500000 baud, compile-time) |
| Status | **resolved**, confidence *likely* | **unresolved**, confidence *unsure* |
| Config | [`configs/mainboard-stm32f401.config`](configs/mainboard-stm32f401.config) | [`configs/toolhead-stm32f103.config`](configs/toolhead-stm32f103.config) (must not be built) |

Never write `0x08000000`-`0x08007FFF` on the mainboard, never install Katapult on it (it would overwrite the bootloader), and
never erase or overwrite a bootloader on either board. No procedure in this repository does.

### How the values were derived (clean room)

Only derived parameters are kept here (`params.json`, `evidence`); no QIDI binary or disassembly is in the repository.

* The chip, clock, serial pins and baud rate are the constants the stock firmware prints in `klippy.log`.
* Mainboard offset and crystal: a community Plus 4 image (not QIDI-authored) has its reset handler at the position that is
  consistent only with a link address of `0x08008000`, its PLL setting is the one that makes 84 MHz out of 8 MHz, its data
  dictionary equals the live constants, and a Plus 4 owner's working mainline configuration selects `FLASH_START_8000`,
  `CLOCK_REF_8M`, USART1 and 500000. Klipper's `board_defs.py` maps `qidi-plus4` onto the `qidi-x7` SD-card definition.
* Toolhead: the same method shows a community Plus 4 toolhead image linked at `0x08002000`, which fits Katapult's 8 KiB layout
  and says nothing about the factory layout. The stock layout was never observed.
* A direct read of the stock mainboard's `SCB->VTOR` is still missing; the risk is low (the community builds boot there).

## Open questions (what settles the toolhead)

1. **Does this printer's toolhead run Katapult or the stock bootloader?** Read-only checks, needing permission to touch the
   printer: a Katapult handshake on `/dev/ttyS2` at 250000 **and** 500000 baud (no write), and/or reading `SCB->VTOR`
   (`0xE000ED08`) and the words at `0x08000000`/`04` through klippy's `debug_read` with klippy stopped.
2. **Read the offset.** VTOR `0x2000` = the Katapult-era layout; `0x8000` = a stock Makerbase layout; anything else needs a
   new analysis. For a stock bootloader nothing shows that `hid-flash` works on the toolhead.
3. Do the same `VTOR` read on the mainboard once, to confirm `0x08008000`.
4. Chip genuineness (GD32 clone) and the `STM32F103GD_DISABLE_SWD` option must be settled before the toolhead config is frozen.
5. Optional: a QIDI OTA image would settle both offsets (needs permission to contact QIDI's servers).

Once answered, in one change: set the toolhead's `status`, `flash_offset` and `kconfig_flash_start` in `params.json`, the
matching `parameters` in `installer/mcuflash/eitri_mcuflash/boards.json` (`tests/test_params.py` fails if the two drift),
the `FLASH_START` line in the toolhead config, flip `go`, and pin the toolchain in `release.lock`. If the toolhead has no
Katapult, no serial route exists for it and Eitri must say so (first-time Katapult needs an ST-Link).

Until then `eitri-mcuflash` writes the toolhead only while Katapult answers *at that moment*, and then only an image linked
for the application start Katapult reports (`runner._check_parameters`).

## Building

```sh
# What CI runs while a board is unresolved: the configs expanded by the pinned trees' own Kconfig (out of tree, no
# toolchain), compared with params.json; the pinned commit's `git describe` compared with the host's.
mcu/build-mcu.sh --check-only --flavor both --work /tmp/mcu-work \
    --source klipper=<clone> --source kalico=<clone> --seed-manifest <data-seed-...-klipper.manifest.json> ...

# The real build (refused, exit 3, while any board is unresolved):
mcu/fetch-toolchain.sh build/toolchains/arm-none-eabi          # release.lock [mcu.toolchain], SHA-256 checked first
mcu/build-mcu.sh --flavor both --version 2026.10.0 --out build/mcu/out --work build/mcu/work \
    --toolchain build/toolchains/arm-none-eabi --seed-manifest <klipper manifest> --seed-manifest <kalico manifest>
```

* Sources: a clean checkout of the exact `release.lock` commit (`--source FLAVOR=DIR`, a clone that already has it, or a fresh
  clone) with `core.abbrev=9`, so `git describe` equals the seed manifest's `sources.firmware.describe` and the MCU
  reports the host's version. The build refuses a mismatch and a dirty tree. The pristine trees are never written to
  (out-of-tree `OUT=` and `KCONFIG_CONFIG=`).
* Output per flavor in `--out`: `mcu-firmware-<version>-<flavor>.tar.zst` (`<flavor>/manifest.json`,
  `<flavor>/<board>/klipper.bin`) and `mcu-firmware-<version>-<flavor>.manifest.json` (per board: file, sha256, size, mcu,
  flash offset, link address, clock, serial, baud, bootloader, method, firmware describe, toolchain).
* Reproducible: `SOURCE_DATE_EPOCH` (the firmware commit time unless set), a sorted tar with fixed owner/time/mode, zstd `-19 -T1`.
  The version string is clean only with a complete toolchain (Klipper appends date and host otherwise): that is why the
  toolchain is pinned.
* Both flavors use the same two config files; `tests/test_configs.py` proves they expand to the same chip, clock and offset
  in both trees (Kalico's dictionary names itself `Kalico`, which the flasher accepts).

### Toolchain

Arm GNU Toolchain, `arm-none-eabi`, x86_64 Linux host, into `build/toolchains/`; the SHA-256 is the one Arm publishes next to the
download. **Not pinned yet** (nothing is built while a board is unresolved). The entry to add to `release.lock` then (resolve
every value against Arm's page; a field that is not resolved is left out, never filled):

```toml
[mcu.toolchain]
version = "..."          # latest stable at that time (15.2.rel1 was the newest seen, unverified)
source = "https://developer.arm.com/-/media/Files/downloads/gnu/<version>/binrel/arm-gnu-toolchain-<version>-x86_64-arm-none-eabi.tar.xz"
sha256 = "..."           # from the .sha256asc Arm publishes beside the download
verified = "YYYY-MM-DD"
tested = false
```

## Verification

`python3 -m pytest mcu/tests` (also run in CI; no toolchain, no device):

| Test | Proves |
|---|---|
| `test_params.py` | `params.json` equals the live constants, `boards.json` (the flasher's registry) and the `go` flag |
| `test_configs.py` | both configs expand, in the pinned Klipper and Kalico trees, to the right chip, 84/72 MHz, 8 MHz crystal, serial, 500000 baud, application address, without writing to the tree |
| `test_mcutool.py` | the image checks on synthetic images (live constants, vector table at the offset, stack at the top of RAM, fit above the bootloader, version == host describe), packaging refuses unresolved boards, bundles are byte-reproducible and load in `eitri-mcuflash` |
| `test_build_script.py` | `build-mcu.sh` refuses a real build with exit 3 and fetches nothing; `--check-only` matches the seed's version |
| `test_artifacts.py` | for every built bundle in `$EITRI_MCU_OUT` / `build/mcu/out`: the embedded dictionary's `MCU`, `CLOCK_FREQ`, `RESERVE_PINS_serial`, `RESERVE_PINS_crystal`, `SERIAL_BAUD` equal the live ones, the vector table is at `0x08000000 + offset`, the size fits above it, the version is the host's. **Skips when nothing is built** and fails if a bundle exists while a board is unresolved |

Not proven by any of this: that a built image boots on a printer. Nothing has run on hardware.
