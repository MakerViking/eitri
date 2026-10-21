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
* **Boards are identified, not assumed.** Port → board binding uses (in order) `--assign`, a previously verified
  map, the wiring in a working `printer.cfg` (which MCU owns the steppers / the extruder), and a chip name that
  fits only one board. The built-in `/dev/ttyS0`/`ttyS2` hints alone are **not enough** to flash.
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

## Methods
| Method | Boards | Notes |
|---|---|---|
| `katapult` | toolhead (and any board found in Katapult) | built-in client; `--katapult-tool …/flashtool.py` uses Katapult's own tool when pyserial is available |
| `sdcard-serial` | mainboard | Klipper's `scripts/spi_flash/spi_flash.py … qidi-plus4`, then the board's SD bootloader flashes itself. `spi_flash.py` compiles a FatFS helper (`fatfs.so`) with the host `gcc` on first use: the preflight blocks unless that file exists or a compiler does (the base Eitri image has none) |
| `sdcard-file` | mainboard (`--sd-mount DIR`) | copy `qd_mcu.bin` to a microSD you move by hand (case open) |

Firmware comes from `--firmware-root` (`<flavor>/manifest.json`, CI-built, checksummed) or is built on the device
with `--build` (upstream mode; out-of-tree, the Klipper clone stays clean).

## Development
`python3 -m pytest tests` - boards are simulated end to end ([../docs/TESTING.md](../docs/TESTING.md)).
