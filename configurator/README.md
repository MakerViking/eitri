# Eitri configurator

The first-run web wizard for Eitri, the open firmware for the QIDI X-Plus 4. It
runs on the printer at port 8099 (nginx can also reverse-proxy it), walks a
user from a freshly flashed image to a calibrated printer, and needs no
internet: fonts, icons and scripts are all bundled.

```
Welcome > Network > Detect > Firmware flavor > Options > Review > Flash > Calibrate > Done
```

* `eitri_configurator/` - Python backend (aiohttp). Python 3.13+.
* `web/` - frontend, plain ES modules and CSS, no build step, no CDN.
* `tests/` - pytest, runs the whole backend in mock mode over real HTTP.
* `tools/screenshots.py` - drives headless Chromium through the entire wizard.
* `screenshots/` - generated images of every step (desktop, phone, dark, light).
* `systemd/eitri-configurator.service` - the unit.

## Develop on a PC

```sh
cd configurator
python3 -m venv ../../build/configurator/venv && ../../build/configurator/venv/bin/pip install -e '.[test]'
python3 -m eitri_configurator --mock          # http://127.0.0.1:8099
python3 -m pytest
python3 tools/screenshots.py                  # needs chromium
```

`--mock` swaps every piece of hardware for a stand-in, while the real code
paths still run:

| Real thing | Mock | Used how |
|---|---|---|
| `eitri-mcuflash` | `eitri_configurator/mock/mcuflash.py` | launched as a subprocess, JSON events parsed |
| Moonraker + Klipper | `mock/moonraker.py`, an aiohttp app on a loopback port | the real HTTP + websocket client talks to it |
| `/data` | a temp dir seeded with a stock-style `printer.cfg` | real file writes, diffs and backups |
| systemd / NetworkManager | recorded calls, scripted Wi-Fi list | |

Knobs: `--mock-speed N` (0 = instant), `--mock-fail toolhead:flash` (make a
phase fail), `--mock-freeze flash:62` (stall at 62 %), `--mock-unassigned` (probing cannot tell which UART is which).

## What each step does

| Step | Backend |
|---|---|
| Network | `GET /api/network`, `GET/POST /api/network/wifi` (NetworkManager via `nmcli`, argv only), `POST /api/network/hostname` |
| Detect | `POST /api/detect` starts a job: host facts, `mcuflash detect`, camera (`/dev/eitri-cam`, ustreamer `/state`), Klarity. Boards are bound to roles by what answered. |
| Firmware | flavors and pins from `release.lock`; `PUT /api/state {flavor}` |
| Options | schema in `options.py` is served to the UI and validates input |
| Review | `POST /api/preview` renders templates and diffs them against `/data/printer_data/config`; `POST /api/apply` backs up and writes |
| Flash | `POST /api/flash` runs `mcuflash flash` per board and streams events; stops Klipper first and always restarts it |
| Calibrate | Z-offset (`PROBE_CALIBRATE`/`TESTZ`/`ACCEPT`), input shaper (`MEASURE_AXES_NOISE`, `SHAPER_CALIBRATE`), bed mesh (`BED_MESH_CALIBRATE`) through Moonraker; `SAVE_CONFIG` on demand |

Long operations are **jobs** (`jobs.py`). Each buffers its events, so
`GET /api/jobs/{id}/events` (Server-Sent Events, resumable with `Last-Event-ID`)
replays history and then follows live. A reload, or opening the wizard on a
phone mid-flash, picks up where it was. Wizard progress is kept in
`/data/eitri/configurator/state.json`.

## Safety properties

* One exclusive job at a time; flashing and calibration refuse to start while Moonraker reports a print.
* Flashing needs an explicit `confirm`, uses the **server-side** detection result (the client never supplies a port), and is blocked unless both boards were identified with a serial-reachable bootloader (ROM-only or unidentified bootloaders block it, because recovery is unproven).
* A failed flash stops the sequence, leaves the other board untouched, and restarts Klipper.
* Z-offset nudges are whitelisted and large downward steps are refused near the bed. The probe's pre-triggered check is never suppressed: Z is lifted first.
* Heater runaway protection is not an option anywhere. The chamber heater is off by default and needs an explicit acknowledgement.
* State-changing requests from another origin are refused; a CSP forbids inline script and external hosts; no subprocess is started through a shell.
* `printer.cfg` regeneration carries over Klipper's `SAVE_CONFIG` block, and every overwritten file is backed up under `config/.eitri-backups/<timestamp>/`.
* `klarity.cfg` belongs to Klarity: only `[screen] serial_port` and `[camera] enabled` are edited, in place.

## Contracts with other components

**mcuflash** (`installer/mcuflash`) - the installer family's shared event stream
(schema 1, JSON Lines) is parsed in `mcuflash.py`, which is the only file that knows
the CLI. Detection runs `eitri-mcuflash plan --json --flavor F --printer-cfg ...
[--assign BOARD=PORT]` (read-only: probes every UART, binds boards by evidence, runs
the preflight) and reads the plan from the final `result.data`. Flashing runs one
`flash --json --apply --yes --flavor F [--only BOARD] [--assign ...]` for all selected
boards, so the tool's own order applies (it proves recovery for every board before
erasing any), and the outcome is read from `result.data.{results,error,recovery}`.
When the tool cannot bind UARTs to boards (`roles` check blocks), the UI offers an
explicit "which UART is which" assignment, kept server-side and passed as `--assign`.
Default command is `eitri-mcuflash` on `PATH` (override with `--mcuflash-cmd`).
Klipper is stopped around detection and flashing by this app, not via
`--stop-services`, and always restarted.

**printer.cfg** - rendered from the Eitri klipper-layer (`klipper-layer/config/printer.cfg.tmpl`,
using the layer's own `scripts/render-config.py`). The layer is looked up in `--layer-dir`, then
`/data/eitri/klipper-layer` (a newer copy shipped by a data bundle), then `<repo>/klipper-layer`
(the source tree, and the image, where it is installed at `/usr/lib/eitri/klipper-layer`). The wizard supplies values only: the two MCU
serial ports from detection (`[mcu]` is the toolhead, `[mcu U_1]` the mainboard), `chamber_enabled`
(needs the acknowledgement), `plr_enabled` and `adxl_enabled`; everything else is the layer's default,
overridable per printer in `printer_data/config/eitri-vars.toml` (a `[vars]` table). After writing,
`overlay-install.sh` links the extras and macros into the Klipper/Kalico clone (skipped, with a
warning, while `/data/firmware/klipper` does not exist).

**Files written outside the config dir** - `/data/eitri/flavor` (contract),
`/data/eitri/channel`, `/data/eitri/ui`, `/data/eitri/hostname`. Optional helpers
are run when present: `/usr/libexec/eitri/switch-flavor <klipper|kalico>` and
`/usr/libexec/eitri/toolchain install`.

**Klarity** - status comes from `systemctl is-active klarity` and the daemon's
`/run/klarity/status.json` (`connected`, `display`, `port`, `baud`, `version`; written by Klarity
every few seconds, `[klarity] status_file`).

**Camera** - the UI links `/webcam/stream` (nginx, from `camera/moonraker`).

**Privileges** - the unit runs as `eitri`; it needs a polkit rule allowing that
user to start/stop/restart `klipper`, `moonraker`, `klarity`, `eitri-camera` and
`go2rtc`, and to call `hostnamectl set-hostname` and NetworkManager.

## Design

Dark "forge" theme with a "bone and iron" light theme (follows the OS, toggle
persists). Instrument Serif for display, Schibsted Grotesk for text, Martian Mono
for technical values (all OFL, bundled in `web/fonts`). Heat is the single recurring
idea: iron, cherry, ember, white-hot, used for progress, the active step and primary
actions. Each step has an Elder Futhark rune drawn as strokes (Kenaz, the torch, for
flashing). Layout is a step rail on desktop and a segmented heat meter with a sticky
action bar on phones. Motion respects `prefers-reduced-motion`.
