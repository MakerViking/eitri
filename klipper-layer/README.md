# klipper-layer

The clean-tree overlay that turns a pristine Klipper or Kalico clone into an
Eitri-ready host stack for the QIDI X-Plus 4. Nothing here edits a tracked
Klipper/Kalico file: the clone stays a literal `git clone` that `git pull`
updates without conflicts.

```
klipper-layer/
  extras/                 Klipper extras, GPL-3.0-or-later (own LICENSE)
  config/                 printer.cfg template, macros, flavor files
  scripts/
    overlay-install.sh    link extras + config into a clone / printer_data
    render-config.py      printer.cfg.tmpl + variables -> printer.cfg
  tests/                  pytest suite (see "Tests")
```

## What the overlay replaces

| QIDI fork change | Eitri |
|---|---|
| `gcode_shell_command` | `extras/gcode_shell_command.py` (clean implementation, mainline only; Kalico already ships one) |
| `chamber_fan` | `extras/qd_chamber_fan.py` |
| chamber/bed power interlock in `heaters.py` | `extras/qd_power_interlock.py` (no Klipper file touched, `verify_heater` untouched) |
| `SAVE/LOAD_ZOFFSET_*` in `gcode_move.py` | `extras/qd_zoffset_var.py` |
| power-loss resume hard-wired into `virtual_sdcard.py` | `extras/qd_plr.py` |
| `TRSYNC_TIMEOUT` edit in `mcu.py` | `extras/qd_runtime_patches.py` (mainline) / `[danger_options]` (Kalico) |
| dual probe monkey patch (`qdprobe.py`) | stub with design notes: `extras/qd_probe.py`; single stock `[probe]` meanwhile |
| `REVERSE_HOMING` in `stepper.py` | stub with design notes: `extras/qd_reverse_home.py` |
| Box/AMS, `buttons_irq`, `gcode.py` break flag, error-to-info probe downgrade, `MAX_HEAT_TIME`, `verify_heater` position_z, endless probe retries, per-print `SAVE_CONFIG_QD` | dropped on purpose |

Stubs refuse to load (`[qd_probe]` / `[qd_reverse_home]` raise "design stub"),
so a config can never silently run without them.

## Install

```sh
scripts/overlay-install.sh                 # /data/firmware/klipper, /data/printer_data
scripts/overlay-install.sh --klipper-dir ~/klipper --printer-data ~/printer_data --init-config
scripts/overlay-install.sh --uninstall
```

* Flavor (Klipper or Kalico) is detected from the clone; `--flavor` forces it
  and `/data/eitri/flavor` is cross-checked (warning on mismatch, never
  written).
* Each `extras/*.py` is symlinked into `klippy/extras/` and listed in the
  clone's `.git/info/exclude` (managed block, rewritten on every run). A name
  that is **tracked in the clone is skipped** (that is how Kalico keeps its own
  `gcode_shell_command`); an untracked regular file with one of our names is
  left alone and the run exits 3.
* `config/` is linked as `printer_data/config/eitri`, and
  `printer_data/config/eitri-flavor.cfg` points at `eitri/flavors/<flavor>.cfg`.
  `printer.cfg` just does `[include eitri-flavor.cfg]` and
  `[include eitri/macros.cfg]`.
* `--init-config` renders `printer.cfg` once; an existing file is never
  overwritten (it is the user's).
* Run as root (the installer), pass `--owner eitri:eitri` or let it copy the
  owner of `printer_data` (else of the clone) onto everything it creates: Klipper
  must be able to write `printer_data/config` (SAVE_CONFIG). `git` is invoked
  with `safe.directory` for the clone so a root run over an `eitri`-owned clone
  works.
* The clone's `git status --porcelain` is compared before and after; any change
  aborts with exit 4. Exit codes: 0 ok, 1 usage/invalid tree, 3 conflict,
  4 clean-tree violation. Idempotent; `--dry-run` shows the plan.

Known cosmetic effect: Klipper deliberately marks any untracked (even ignored)
`.py` or symlink under `klippy/extras/` as `-dirty` and logs
"Untracked files: ...". The host version string therefore reads
`v0.13.0-...-dirty` with the overlay installed. Behaviour is unaffected.
`git pull` replaces an ignored symlink silently if upstream ever adds a tracked
file of the same name; re-run the installer afterwards.

## Config

`config/printer.cfg.tmpl` is rendered by `scripts/render-config.py` with the
variables in `config/eitri-variables.toml` (`@@name@@` substitution and
`#@if name / #@else / #@endif` lines; Klipper's own Jinja is never touched).
Calibrated values (rotation distances, currents, PID, pressure advance, probe
offsets, mesh and Z-tilt geometry) are variables; override them with
`--vars file.toml` or `--set name=value`. The MCU serial paths are variables
too: which UART is the toolhead is established by probing, the defaults are
only the reference unit's.

* `macros.cfg`: homing override (sensorless XY, probe Z, lift first), `PRINT_START`
  (bed heats while homing, heat soak, tilt + Z at temperature, mesh at
  temperature, purge; **no `SAVE_CONFIG`**), `PRINT_END`, `CANCEL_PRINT`,
  `PAUSE`/`RESUME`, a Marlin-style `M109`/`M190` that does not stall on
  overshoot, `M141`/`M191` through the interlock, `M106`/`M107` fan mapping,
  filament load/unload, tunables in `[gcode_macro _EITRI_CFG]`.
* KAMP is optional: `PRINT_START` calls `BED_MESH_CALIBRATE` and uses KAMP's
  macro if `_KAMP_Settings` exists, Klipper's built-in `ADAPTIVE=1` otherwise.
* `flavors/klipper.cfg` / `flavors/kalico.cfg`: the few knobs that differ
  (`[qd_runtime_patches]` vs `[danger_options]`, hall-sensor flow compensation).
* Input shaper is left for `SHAPER_CALIBRATE` + `SAVE_CONFIG`.
* The chamber heater is off by default (`chamber_enabled`).
* Heater runaway protection is Klipper's stock behaviour for the extruder and
  bed; the chamber has an active `[verify_heater chamber]` with QIDI's loose
  limits (`max_error 400`, `check_gain_time 600`, looser than Klipper's
  defaults because the 0.4-power air heater is slow; tighten after
  `PID_CALIBRATE`). Nothing in this layer lowers a check, bypasses it, or
  accepts a pre-triggered probe.

* Behaviour worth knowing:
  * `G28 X` / `G28 Y` on an unhomed machine lift the head from an assumed Z
    (so the move is legal) and then mark Z unhomed again; only a Z home (or
    `G28`) marks it homed. `QD_PURGE` and `M603`/`M604` therefore home first.
  * A pause keeps its homed state when the idle timeout (30 min) turns the
    heaters off, so `RESUME` reheats and continues. If the motors were released
    anyway, `RESUME` refuses *before* heating anything.
  * `M290` baby-steps are limited to the same +/-1 mm total offset that
    `qd_zoffset_var` allows when saving.
  * The saved baby-step is tied to the probe calibration it was made against:
    after `PROBE_CALIBRATE` + `SAVE_CONFIG` it is discarded (with a message) at
    the next `PRINT_START`, and the first layer should be checked again.
  * `M141`/`M191` raise a target below the interlock's minimum (45 C) to it and
    clamp above the maximum, with a message, instead of aborting.
  * If the bed target is lowered below the interlock window mid-print, or the
    bed sags, the chamber heater is paused after 30 s (`park_delay`) and
    resumes when the bed is back; it never reaches the `verify_heater` shutdown
    just because the interlock held it off. `SET_HEATER_TEMPERATURE
    HEATER=chamber` is gated too but bypasses the bed raise and range checks;
    use `M141`/`QD_SET_CHAMBER`.
  * Power-loss resume stores the active bed mesh in its record and rebuilds it
    as a session-only profile (`plr-resume`), because the default adaptive mesh
    is never saved. If Z is already homed when `QD_PLR_RESUME` runs, the live Z
    is used instead of the recorded one.
* `gcode_shell_command` (Klipper only): `PARAMS` from any G-code source is
  appended verbatim to the configured argv (no shell). Set `params_pattern` in
  a `[gcode_shell_command ...]` section to restrict it.

See `config/README.md` for attribution and the stock-vs-Eitri differences.

## Tests

```sh
cd klipper-layer
python -m pytest                  # all
python -m pytest -k "not klippy"  # fast: unit, install, render (a few seconds)
```

Layers: import-check every extra with `python -I` against mainline and Kalico
(`tests/import_check.py`); unit tests against fakes for the interlock, chamber
fan, Z offset, PLR scanner/state machine, renderer; `overlay-install.sh` against
throw-away clones (git status must stay clean); and `test_klippy_batch.py`,
which runs the **real klippy** in batch mode (`-i file`) over the rendered config
with the overlay installed, for both flavors, including a SIGKILL power-cut
round trip of `qd_plr`.

The batch tests need the trees and firmware dictionaries; defaults assume the
Eitri workspace layout and can be overridden:

| variable | default |
|---|---|
| `EITRI_KLIPPER_SRC` | `../src/upstream-klipper` |
| `EITRI_KALICO_SRC` | `../build/kalico` (pinned tag) |
| `EITRI_KLIPPY_DICTS` | `../build/klipper-layer/dicts` (`tests/build-dicts.sh OUT_DIR` builds them; needs `arm-none-eabi-gcc`) |
| `EITRI_KLIPPY_PYTHON` | the interpreter running pytest (needs cffi, greenlet, jinja2, pyserial) |

Tests skip with a reason when a tree or dictionary is missing. The source trees
are never modified.

Batch mode simulates heaters and probes (sensors read 0, endstops never
trigger), so those tests prove syntax, wiring and control flow, not physical
behaviour.

## Needs a real printer

Everything below is implemented as far as possible without hardware and is
tracked in the component report; none of it has run on a machine.

* Sensorless XY homing currents/thresholds and the Z-from-unknown-position
  sequence (`homing_override`): first runs with a hand on the power switch.
* `probe z_offset` calibration (`PROBE_CALIBRATE`); `PRINT_START` refuses the
  placeholder 0.
* `qd_power_interlock` against the real PSU: bed window (70 C / 2 C), chamber
  PID tuning (`PID_CALIBRATE HEATER=chamber`), `[verify_heater chamber]` limits.
* `qd_plr`: resuming after a genuine power cut (Z drift while unpowered,
  recorded-Z trust, lift distance), eMMC write rate of the state file.
* Multi-MCU homing sync timeout (`trsync_timeout` / `multi_mcu_trsync_timeout`
  0.1 s) over the 500 kbaud UARTs.
* Stubs: `qd_reverse_home` (StallGuard thresholds on the Z screws) and
  `qd_probe` (second sensor), see the design notes at the top of each file.
