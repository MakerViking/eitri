# config/

| file | purpose |
|---|---|
| `printer.cfg.tmpl` | X-Plus 4 `printer.cfg`, rendered once by `../scripts/render-config.py` |
| `eitri-variables.toml` | default values for every `@@variable@@` in the template |
| `macros.cfg` | all G-code macros, included as `[include eitri/macros.cfg]` |
| `flavors/klipper.cfg`, `flavors/kalico.cfg` | settings that exist on only one of the two hosts |

`printer_data/config/eitri` is a symlink to this directory (created by
`overlay-install.sh`), so macro updates arrive with the repository while the
user's `printer.cfg` stays theirs.

## Attribution

Hardware facts (pin map, driver wiring, the reference unit's calibrated values)
and the overall shape of the start/pause/cancel flow come from QIDI's public
Plus 4 configuration, `QIDITECH/QIDI_PLUS4` at tag `Plus4_v1.7.1`, and the
reference printer's own `printer.cfg`. Macro text was rewritten for Eitri.
QIDI's Box/AMS sections and `T0`-`T15` tool changes, the dual-probe switching
(`QIDI_PROBE_PIN_*`), `REVERSE_HOMING`, the per-print `SAVE_CONFIG_QD`, the
`gcode_macro_break` webhook hack and every option that only exists in QIDI's
Klipper fork were left out.

Licence note for review: the upstream `QIDI_PLUS4` repository carries an AGPL-3.0
`LICENSE` file while its README badge says GPL-3.0 (recorded in `release.lock`).
This directory contains only hardware constants plus text written for Eitri, but
whoever publishes the repository should confirm that is enough before
distributing it under Apache-2.0.

## Differences from the stock configuration

* Mainline-valid options only: `minimum_cruise_ratio` instead of
  `max_accel_to_decel`, `samples_result: median` with finite
  `samples_tolerance_retries` instead of `submaxmin` and endless retries,
  Z homes on one `[probe]` (no `*_reverse` stepper options).
* `max_accel` default 10000 (stock 20000) until the input shaper is calibrated;
  `extruder smooth_time` is Klipper's default (stock 1 us fed thermocouple noise
  straight into the wait logic).
* `[verify_heater extruder]` / `[verify_heater heater_bed]` are not overridden:
  the stock file loosened them (`heating_gain: 1`, bed `max_error: 200`,
  `hysteresis: 10`); Klipper's defaults apply. The chamber keeps the stock
  tolerances (`max_error: 400`, `check_gain_time: 600`) as variables; its
  `position_z` runaway bypass is gone.
* Object names that displays may read are kept (`heater_generic chamber`,
  `fan_generic cooling_fan`, `auxiliary_cooling_fan`, `chamber_circulation_fan`,
  `output_pin caselight/beeper`, `filament_switch_sensor fila`,
  `temperature_sensor GD32`). The chamber fan is `qd_chamber_fan chamber_fan`.
* Z steppers stay energised after `M84` (the bed would sink), as stock.
* `hall_filament_width_sensor` is a presence sensor only; on Klipper its flow
  compensation is disabled in `flavors/klipper.cfg`. Kalico always applies the
  sensor's `M221` (kept at 100 % by `max_difference: 0`), which will reset a
  manual flow factor about once a second while the sensor is enabled.
