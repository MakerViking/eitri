# eitri-mcuflash

Finds out what is on the two UARTs of the X-Plus 4, keeps a way back, flashes the firmware of the chosen
flavor and proves it took. Python >= 3.7, no third-party packages (it also runs on the stock Debian 10 image).

```sh
eitri-mcuflash probe                    # who answers on /dev/ttyS0 and /dev/ttyS2 (read-only)
eitri-mcuflash plan                     # + roles, firmware, version lock, recovery: the preflight table (read-only)
eitri-mcuflash backup --apply --yes     # reset each board into its bootloader, read its firmware back, write it back, boot it
eitri-mcuflash flash                    # DRY RUN by default
eitri-mcuflash flash --apply --yes      # back up (all boards) → flash (safe boards first) → verify; rolls back on failure
eitri-mcuflash rollback toolhead --apply --yes            # the firmware saved before the last flash
eitri-mcuflash rollback toolhead --original --apply --yes # the firmware the printer SHIPPED with (what revert.sh uses)
eitri-mcuflash backups --json
```

`--json` makes every line a progress event ([docs/EVENTS.md](../docs/EVENTS.md)) ending with a `result` event;
exit codes 0 ok, 1 failed, 2 usage, 3 blocked by preflight (nothing touched).

## Design rules
* **Boards are identified, not assumed.** The mainboard is the board that answers as `stm32f401xc`, the toolhead the one that
  answers as `stm32f103xe` (the live printer's `klippy.log`); the name in the data dictionary decides, never the port.
  Port → board binding uses (in order) `--assign`, a previously verified map, the wiring in a working `printer.cfg`
  (which MCU owns the steppers / the extruder), and the chip name. A port is never bound to a board of another chip, not
  even by `--assign`. The built-in `/dev/ttyS0` (mainboard) / `/dev/ttyS2` (toolhead) hints alone are **not enough** to flash.
* **Unresolved boards are refused.** `boards.json` records per board what is settled about the flash layout
  (`parameters.status`). The mainboard is settled (application at `0x08008000` behind the 32 KiB stock bootloader,
  8 MHz crystal). The **toolhead is unresolved**: it may run Katapult (application `0x08002000`, baud 250000 or 500000) or the
  stock bootloader (offset unknown), and a wrong offset needs an ST-Link to undo. Preflight therefore blocks it with
  the open question named, unless Katapult is seen answering right now, in which case the image must be linked for the
  application start Katapult reports. Nothing guesses an offset. The registry is changed only after the question has been
  answered read-only (`mcu/README.md`).
* **The image must be for the board.** `mcu`, `CLOCK_FREQ`, `SERIAL_BAUD` and `RESERVE_PINS_*` in the image's data
  dictionary must equal what the board reports (`expect_config`); a settled board's image must be linked for the
  registry's offset and fit above it.
* **The image is checked, not trusted.** Besides manifest and checksum, the flasher reads the image's own Cortex-M vector
  table (stack pointer in SRAM, Thumb reset vector inside the window that starts at the board's application offset) and
  the operator-supplied stock recovery image gets the same test. The checks run again after an on-device `--build`
  (`build_mismatch`, nothing staged); `--build` finds `<board>.config` or the repository's `<board>-*.config`.
* **SD staging is forced to the card.** `sdcard-file` fsyncs the file and its directory and calls `sync` before the
  operator is told to pull the card; `sdcard-serial` hands `spi_flash.py` the dictionary byte for byte (it compares raw bytes).
* **No Katapult on the mainboard.** A mainboard that answers as Katapult is refused: that would mean its stock bootloader
  is gone.
* **Prove recovery for every board before erasing any.** Katapult boards: enter the bootloader, read the whole
  application region back, write it back and boot it (round trip), keep the copy. SD-bootloader boards cannot be
  read back, so a stock image (`--stock-bin mainboard=qd_mcu.bin`) or an explicit `--accept-no-mcu-backup` is
  required. A board with no serial bootloader is refused (ST-Link territory), never "tried".
* **The original firmware is remembered separately.** `backup --origin stock` (the installer runs it on the untouched
  printer) tags its images; every later flash saves another backup, so `rollback --original` picks the tagged one
  (else the oldest), never merely the latest. An operator-supplied `--stock-bin` is copied into the state directory
  at that point so rollbacks work later without the original file.
* **Version lock.** Image version == host `git describe` (and, with `--commit SHA`, built from exactly that pinned commit) or no flash; image MCU == the chip that answers;
  image link address == Katapult's application start; image fits above the bootloader.
* **No bootloader writes.** Only the application region is ever written.
* Dry run is the default; a dry run still *talks* to the MCUs (the `identify` handshake is read-only) but never
  resets, erases or writes.
* The ports must be free: `--stop-services` stops `klipper.service` (+ `--service UNIT`) and restarts exactly
  what it stopped.

## Known limits (not hardware-tested)
* The Katapult handshake is sent to **every** candidate port at 250000 and 500000 baud, mainboard port included, because the
  board behind a port is not known before the probe. It is read-only; the first real run will show whether the mainboard's
  running firmware ignores it quietly (expected).
* The mainboard's stock UART bootloader (`hid-flash`, 115200) is not implemented. A mainboard whose application does not
  start has no serial way back: open the case, put the stock `qd_mcu.bin` on a FAT32 card, press RESET.
* Automatic rollback of the mainboard needs the new firmware to answer (it goes through `spi_flash.py`); it cannot help when
  the new image does not boot. Preflight therefore insists on a stock image (`--stock-bin`) or an explicit acknowledgement.
* `--registry FILE` and `--mcu-arg` replace the board registry. That is an escape hatch for bench work: a registry that marks
  the toolhead resolved removes the toolhead refusal. Never use one to "try" an offset.

## Methods
| Method | Boards | Notes |
|---|---|---|
| `katapult` | toolhead, only while Katapult answers (tried at 250000 and 500000 baud; the rate is a compile-time choice) | built-in client; `--katapult-tool …/flashtool.py` uses Katapult's own tool when pyserial is available |
| `sdcard-serial` | mainboard | Klipper's `scripts/spi_flash/spi_flash.py … qidi-plus4`, then the board's stock SD bootloader flashes itself. `spi_flash.py` compiles a FatFS helper (`fatfs.so`) with the host `gcc` on first use: the preflight blocks unless that file exists or a compiler does (the base Eitri image has none). Kalico ships no `qidi-plus4` board definition, so on a Kalico tree the preflight blocks this method (and falls back to `sdcard-file` if `--sd-mount` is given) |
| `sdcard-file` | mainboard (`--sd-mount DIR`) | copy `qd_mcu.bin` to a microSD you move by hand (case open) |

The stock mainboard bootloader also has a UART path (`hid-flash`, 115200 8N1); it is not implemented here.

Firmware comes from `--firmware-root` (`<flavor>/manifest.json`, CI-built, checksummed) or is built on the device
with `--build` (upstream mode; out-of-tree, the Klipper clone stays clean).

## Development
`python3 -m pytest tests` - boards are simulated end to end ([../docs/TESTING.md](../docs/TESTING.md)).
