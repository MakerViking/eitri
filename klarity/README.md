# Klarity

Klarity is the open screen daemon of [Eitri](../README.md), the open firmware for the QIDI X-Plus 4.
It replaces QIDI's closed screen host program. The panel is a TJC (Nextion-compatible) serial
display that renders its own UI from the stock `.tft`; Klarity talks to it over the UART and to
Moonraker over a websocket. **The panel is never reflashed.**

```
 TJC/Nextion panel  <--UART-->  Klarity  <--JSON-RPC websocket-->  Moonraker  <-->  Klipper
  (stock .tft)                    |
                                  +--HTTP GET /snapshot--> ustreamer (camera preview)
```

## Status

| Piece | State |
|---|---|
| Nextion framing (parse + build), resync after garbage | done, unit-tested |
| Moonraker client (subscribe, G-code, print control, reconnect/backoff) | done, tested against a fake server |
| State -> screen push loop (diffing, coalescing, rate limit, page repaint) | done, tested |
| YAML screen map, learn mode, action runner with guards | done, tested |
| Camera snapshot fetch + resize | done; **pushing it to the panel is unverified** (off by default) |
| Stock page / widget / component ids | **partial.** `maps/default.yaml` has 66 pages and 511 widget entries observed from the stock host's behaviour; 104 entries are flagged `todo` (unconfirmed or not expressible) and ignored at runtime. **None of it has been checked against a live panel.** |
| Typed numbers (stock `0x71 page widget lo hi` frame), `bind.offset`, `derived.speed_mms` | done, tested |
| Link state for the configurator (`/run/klarity/status.json`) | done |
| "Unlock setup" from the panel (Settings > Network > accounts button) | done, tested with a fake serial link and fake `sudo`/`systemctl`; the button, `t7` and the sudo/polkit path are **unverified on hardware** |
| UART device and baud of the panel | shipped default `/dev/ttyS1` @ 115200 (what the stock host opens); **unconfirmed on mainline**, the 8250 numbering depends on the device tree |

Not yet expressible in the map schema (these stay `todo`): the file browser, preview and print
start, session state such as the move distance or z-offset step, thumbnails, and the automatic page
changes the stock host forces on printer events. The stock UI has no camera page, so the camera
preview has no place to show until a page for it exists.

Before using the map on a real printer run `klarity --learn --dry-run`, tap through the pages and
compare the learn log with the map (in particular the press-or-release edge, see `meta`). If
nothing in the map is active Klarity runs in *observe-only* mode and just logs.

## Install and run

Eitri's data seed (`seed/SPEC.md`) installs Klarity as a wheel into the venv `/data/eitri/klarity/venv`
(console script `/data/eitri/klarity/venv/bin/klarity`, the path `klarity.service` starts and
`ConditionPathExists=` checks), with the unit in
`systemd/klarity.service` and the config at `/data/printer_data/config/klarity.cfg`
(template: `config/klarity.cfg`). Manually:

```sh
uv venv --python 3.14 /data/eitri/klarity/venv
uv pip install --python /data/eitri/klarity/venv/bin/python /path/to/eitri/klarity   # add [camera] for Pillow
cp config/klarity.cfg /data/printer_data/config/klarity.cfg     # then set serial_port
sudo cp systemd/klarity.service /etc/systemd/system/ && sudo systemctl enable --now klarity
```

The service runs as `eitri` (needs `dialout` for the UART). Exit code 78 (bad config, unset
serial port) is not restarted.

Useful flags: `--check-map`, `--learn`, `--simulate` (in-memory panel, no hardware),
`--dry-run` (never send G-code or print control), `--serial/--baud`, `--log-level DEBUG`
(shows every instruction sent), `--allow-todo` (see Safety).

## Unlock setup (reopening the configurator from the panel)

The configurator exits after setup and stays locked (Decision 8). To change settings later the owner
opens it from the printer: **Settings > Network > the stock "accounts" button** (page `0x3e`, widget 9).
The `.tft` cannot get a new button, so Klarity repurposes the one stock widget on the network page that
the stock host left inert (it only logged an account list; Eitri has no accounts). It keeps its stock
label; if it is not the right button on your panel, move the widget id in `maps/default.yaml`
(`network.widgets.unlock_setup`). On a tap Klarity

1. refuses if a print is running or paused (map guard, and again in the action runner),
2. runs `sudo -n /usr/bin/eitri-configurator unlock` (no shell; one narrow sudoers rule, see below),
3. runs `systemctl --no-block start eitri-configurator.service` (polkit; brings back a retired service),
4. waits up to `wait` seconds for `/data/eitri/configurator/pairing.json` (`{"state": "pairing", "code": "1234-5678"}`;
   note: the real path is `/data/eitri/...`, not `/run`) and
5. writes `Code 1234-5678 at 192.168.0.5:8099` into the page's text field `t7` (where the stock host showed the
   IP). Open that address in a browser and enter the code. On failure the field says `Unlock failed - see the log`
   and the panel beeps. Nothing is sent to Klipper.

The address is the printer's IPv4 on its default route (a UDP `connect`, no traffic), else `<hostname>.local`;
`[unlock] host` overrides it. Only the pairing code format `dddd-dddd` is accepted from the file. A QR image is
not pushed (the file-transfer path is unverified on hardware, see `[camera] push_enabled`).

Map verb: `unlock: setup` (it **requires** a `guard`, like the print-control verbs) sets `{code}`, `{host}`,
`{url}` and `{message}` for the `set` actions after it in the same list. These values are Klarity's own and bypass
the panel-text filter, which still applies to everything the panel sends. `--dry-run` never unlocks; it only
shows a code that is already open. Disable with `[unlock] enabled: false`.

The OS image provides the `/usr/bin/eitri-configurator` wrapper and a sudoers rule for exactly
`eitri ALL=(root) NOPASSWD: /usr/bin/eitri-configurator unlock`. Because that runs as root, the wrapper must not
execute code from the user-writable `/data/eitri/configurator/venv`. `klarity.service` therefore drops
`NoNewPrivileges` (it would block the setuid `sudo`), uses none of the seccomp-based sandbox options (systemd sets `NoNewPrivs` anyway when
they are present, measured on the QEMU image) and makes `/data/eitri/configurator` writable.

## Configuration

`klarity.cfg` is Klipper-style INI; unknown sections/options are errors. Every option and its
default is in [`config/klarity.cfg`](config/klarity.cfg). The ones that need hardware
measurements are marked `TBD`: `[screen] serial_port`, `baud`, `quote_style`, and
`[camera] push_enabled`.

## The screen map

All knowledge about the stock `.tft` lives in one YAML file, so it can be completed without
touching code. The annotated schema is at the top of
[`src/klarity/maps/default.yaml`](src/klarity/maps/default.yaml). In short:

* **Input**: `pages.<key>.widgets.<key>` with the numeric `id` the panel reports in
  `0x65 page widget type` touch events, and `on_press` / `on_release` action lists.
* **Output**: `obj` (component name, e.g. `t_nozzle`) plus `bind` entries that turn Moonraker
  state (`extruder.temperature`, `heater_generic chamber.target`, `derived.progress`, ...) into a
  Nextion attribute (`txt`, `val`, `vis`, `bco`, `pic`, ...), with `format`, `scale`, `test`
  and value `map`s. Only changed values are sent, only for the page being shown.
* **Actions**: `gcode`, `print` (pause/resume/cancel/start/...), `page`, `beep`, `set`, `log`,
  each with optional `guard` (state predicate), `cooldown` and `read` (ask the panel for a number
  or text first, e.g. a keypad value, and use it as `{value}` in G-code).
* **Markers**: the one-byte `printh` page markers (`0xF9`, `0x91`, ...).

## Mapping the stock screen (learn mode)

With the printer idle and the panel connected:

```sh
klarity -c klarity.cfg --learn          # or set [learn] enabled: true
```

Tap through every page and button. Every event is written to
`/data/printer_data/logs/klarity-learn.jsonl` (and unmapped ones to the journal) with page, widget,
press/release and whether the map knows it. Two seconds after each release Klarity also logs what
changed on the printer side (non-noisy Klipper state fields and G-code responses), which shows what
a button does when something else (e.g. the stock host, a macro) is driving. On shutdown, or on
`SIGUSR1`, it prints a YAML skeleton of every widget seen but not yet mapped; paste it into the
map, add `obj` names and actions, drop the `todo` flags, then check with `klarity --check-map`.

## Safety

* **Unconfirmed entries cannot fire.** Anything flagged `todo` in the map is ignored unless
  `allow_todo` / `--allow-todo` is set. The stock cancel button only opens the confirmation page;
  the cancel itself needs the confirm button and a printing or paused state.
* Actions can carry **guards**, so e.g. jogging and homing are refused while printing and
  cancel/pause only apply in the right print state. A blocked action can beep (`else_beep`).
* Text read from the panel is only substituted into G-code if it matches `[\w .+\-/]*`;
  numbers are always safe. Anything else aborts the action with an error beep.
* Klarity does not touch heater, probe or runaway settings; it only sends what the map says.
* Moonraker or Klipper being down is normal: Klarity shows `Offline`/`Klipper off` (via
  `derived.state`) and reconnects with exponential backoff. A pulled screen cable is retried too.

## Protocol notes

Implementation notes (the behavioural spec lives in the private project notes until it has passed
the clean-room review):

* Frames end with `FF FF FF`. Fixed-length events (`65` touch, `66` page, `67/68` coordinates,
  `71` number) are parsed by length, because a number such as `-1` contains `FF FF FF` itself.
* `70` strings and unknown ids are terminator-delimited. One-byte markers and Nextion status
  codes are emitted immediately; whether the stock `.tft` terminates them is unknown, so trailing
  `FF`s are swallowed either way. A bare `0xFF` marker (an app sync flag in the spec) is
  indistinguishable from terminator residue and is not reported.
* An unknown id is assumed to be terminator-delimited. If learn mode shows one that is really a
  bare byte, add it to `markers:` in the map and it is framed as one byte.
* Half-received frames are dropped after `frame_timeout` and the parser resynchronises on the
  next terminator.
* `get` replies carry no tag; `Screen.get` serialises requests so replies cannot cross.
* String quoting is configurable (`quote_style`). Text is encoded as UTF-8 by default: the stock
  host sends UTF-8 (even the degree sign), so the panel fonts are UTF-8. `encoding: gb18030` is
  available if non-ASCII text turns out garbled.
* The stock `.tft` reports a number typed on a keypad as `71 <page> <widget> <lo> <hi>`, not the
  standard 4-byte value. Replies to `get` use the standard form; Klarity decodes unsolicited `71`
  frames the stock way and dispatches them to `widget.on_number`.

## Development

```sh
uv venv --python 3.14 && uv pip install -e '.[camera]' --group dev   # or: uv sync --extra camera
python -m pytest tests          # 170+ tests, ~4 s, no hardware or network needed
ruff check src tests && ruff format src tests
```

Tests cover frame parsing (split, garbage, resync), command escaping, map loading and validation,
config, the Moonraker client against a fake websocket server (incl. reconnect), the serial path
over a pty, and end-to-end runs of the whole daemon between an in-memory panel and the fake
Moonraker (initial paint, incremental updates, pause/resume/cancel, guards, read-back, outage
recovery, learn mode, inert placeholder map).

## Needs real hardware

* Which UART and baud the panel uses on mainline (default `/dev/ttyS1` @ 115200 is from the stock host).
* The page/widget/component tables against a live panel, and the press-or-release edge.
* Whether the TJC firmware honours `\"` / `\\` / `\r` escapes in strings (`quote_style`).
* The camera preview transport: that `twfile` is accepted, which path (`ram/` or SD) works,
  and that setting the picture component's `path` shows the file.
* Throughput and latency of the push loop at the panel's real baud (tune `[push]`).

## License

Apache-2.0, like the rest of Eitri. Clean-room: written from a behavioural description of the display protocol and
public Nextion documentation; no QIDI binaries, `.tft`/`.HMI` assets or decompiled code are
included. The map holds interoperability facts only (ids, component names, what a tap does); under
[docs/CLEAN-ROOM.md](../docs/CLEAN-ROOM.md) it still needs the second-person review before release.
