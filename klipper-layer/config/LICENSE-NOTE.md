# Licensing of the files in config/

| file | SPDX identifier | why |
|---|---|---|
| `printer.cfg.tmpl` | `AGPL-3.0-or-later` | pin map, driver wiring, sensor types and calibrated values come from QIDI's Plus 4 configuration |
| `macros.cfg` | `AGPL-3.0-or-later` | machine coordinates, homing currents and the shape of the start/pause/cancel flow follow QIDI's `gcode_macro.cfg` |
| `eitri-variables.toml` | `AGPL-3.0-or-later` | its defaults are the calibrated values of the reference unit, taken from the same configuration |
| `flavors/klipper.cfg`, `flavors/kalico.cfg` | `Apache-2.0` | written for Eitri; they hold only host-specific switches |

The full AGPL-3.0 text is in `LICENSE` next to this note. `LICENSE` at the repository
root (Apache-2.0) covers the rest of the repository except `../extras/`
(`GPL-3.0-or-later`, its own `LICENSE`).

## Why AGPL

The configuration repository these files were adapted from, `QIDITECH/QIDI_PLUS4`
(tag `Plus4_v1.7.1`), contains an AGPL-3.0 `LICENSE` file while its README badge
says GPL-3.0. Eitri takes the stricter reading: every file here that adapts material
from it is distributed under `AGPL-3.0-or-later`, with the attribution line in its
header. Only hardware facts, calibrated numbers and the outline of the macro flow were
taken; the macro text is written for Eitri (see `README.md`). `AGPL-3.0-or-later` is
compatible with the GPL-3.0-or-later extras that these files load next to.

## What this means in practice

* Keep the SPDX line and the attribution comment when you copy or modify one of the
  AGPL files, and distribute your modified version under the same licence.
* AGPL section 13 applies to a modified version that users interact with over a
  network. Eitri ships these files unmodified; the `printer.cfg` that the setup wizard
  renders from `printer.cfg.tmpl` is a derivative and carries the same header. It stays on the printer
  and is yours to edit, and nothing here changes how you may use your own printer.
* A new file added to this directory that adapts QIDI material needs the same header. A
  file written from scratch uses the repository licence, `Apache-2.0`.
* This is a statement of how Eitri treats the material, not legal advice.
