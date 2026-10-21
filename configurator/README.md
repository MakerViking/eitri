# Eitri configurator

The first-run web wizard for Eitri, the open firmware for the QIDI X-Plus 4. It
runs on the printer at port 8099 (nginx can also reverse-proxy it), walks a
user from a freshly flashed image to a calibrated printer, and needs no
internet: fonts, icons and scripts are all bundled.

```
Welcome > Network > Detect > Firmware flavor > Options > Review > Flash > Calibrate > Remote access (optional) > Done
```

* `eitri_configurator/` - Python backend (aiohttp). Python 3.13+.
* `web/` - frontend, plain ES modules and CSS, no build step, no CDN.
* `tests/` - pytest, runs the whole backend in mock mode over real HTTP.
* `tools/screenshots.py` - drives headless Chromium through the entire wizard.
* `screenshots/` - generated images of every step (desktop, phone, dark, light).
* `systemd/eitri-configurator.service` - the unit.
* The `/usr/bin/eitri-configurator` command (`pairing`, `unlock`, `lock`) lives in the OS overlay (`os/board/eitri-plus4/rootfs-overlay/usr/bin/eitri-configurator`); it runs image code only, see "Pairing and lock".

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

Mock mode pairs like the real thing: read the code from the log or from `<data-dir>/eitri/configurator/pairing.json`
and enter it in the browser (`tools/screenshots.py` does exactly that).

Knobs: `--mock-speed N` (0 = instant), `--mock-fail toolhead:flash` (make a
phase fail), `--mock-freeze flash:62` (stall at 62 %), `--mock-unassigned` (probing cannot tell which UART is which).

## What each step does

| Step | Backend |
|---|---|
| Pair | `GET /api/auth`, `POST /api/pair {code}`, `POST /api/logout`: see "Pairing and lock" below. Everything that changes state needs a paired browser. |
| Network | `GET /api/network`, `GET/POST /api/network/wifi` (NetworkManager via `nmcli`, argv only; the passphrase goes through a 0600 file, never argv), `POST /api/network/hostname` |
| Detect | `POST /api/detect` starts a job: host facts, `mcuflash detect`, camera (`/dev/eitri-cam`, ustreamer `/state`), Klarity. Boards are bound to roles by what answered. |
| Firmware | flavors and pins from `release.lock`; `PUT /api/state {flavor}` |
| Options | schema in `options.py` is served to the UI and validates input |
| Review | `POST /api/preview` renders templates and diffs them against `/data/printer_data/config`; `POST /api/apply` backs up (fsynced, then renamed into place) and writes. Refused while a print is running. |
| Flash | `POST /api/flash` runs `mcuflash flash` per board and streams events; stops Klipper first and always restarts it |
| Calibrate | Z-offset (`PROBE_CALIBRATE`/`TESTZ`/`ACCEPT`), input shaper (`MEASURE_AXES_NOISE`, `SHAPER_CALIBRATE`), bed mesh (`BED_MESH_CALIBRATE`) through Moonraker; `SAVE_CONFIG` on demand |
| Remote access | Optional, off by default: `GET /api/access`, `POST /api/access/key|password|disable` through the `eitri-access` helper (see "Remote access"). |
| Done | `POST /api/complete` locks the configurator. Links to the camera sign-in page; it does not show the RTSP password. |

Long operations are **jobs** (`jobs.py`). Each buffers its events, so
`GET /api/jobs/{id}/events` (Server-Sent Events, resumable with `Last-Event-ID`)
replays history and then follows live. A reload, or opening the wizard on a
phone mid-flash, picks up where it was. Wizard progress is kept in
`/data/eitri/configurator/state.json`.

## Pairing and lock

The configurator can flash the control boards, rewrite the printer configuration, join Wi-Fi and
restart services, so it only obeys a browser that is **paired** with the printer, and it **locks** when
setup is finished.

* **Pairing code.** On first start an 8-digit one-time code is generated (`/data/eitri/configurator/auth.json`,
  mode 0600) and published for the screen daemon in `/data/eitri/configurator/pairing.json`
  (`{"state": "pairing", "code": "1234-5678"}`; Klarity shows it on the touchscreen). It is also logged
  (`journalctl -u eitri-configurator | grep PAIRING`) and `eitri-configurator pairing` prints it over SSH.
  Presenting it to `POST /api/pair` creates a session, and the code is then replaced, so every extra
  browser needs a fresh look at the screen. Wrong guesses back off (5 in a row: 30 s, doubling) and 10
  in a row replace the code.
* **Sessions.** 256-bit random tokens, stored only as SHA-256 digests, valid 12 hours. A browser holds one
  in an `HttpOnly; SameSite=Strict` cookie and must echo the per-session CSRF token in `X-Eitri-CSRF`
  on every write. Scripts use `Authorization: Bearer <token>` (the `token` in the pairing response).
  GET routes stay open (the wizard has to load before it can pair).
* **Writes** also need `Content-Type: application/json`, a same-origin `Origin` (when sent) and a
  same-origin `Sec-Fetch-Site` (when sent); the `Host` check against DNS rebinding stays.
* **Lock.** `POST /api/complete` (the Done step) signs out every other browser, leaves the finishing
  one ten minutes of grace, and closes pairing: the pairing file then says `"state": "locked"`. To change
  settings later, choose **Unlock setup** on the printer's touchscreen (Klarity) or run `eitri-configurator unlock`
  over SSH; pairing with the on-screen code works again until the wizard is finished once more or
  `eitri-configurator lock` is run.
* **Retiring (the default).** Once setup is complete and the lock is on, the process exits cleanly: at
  start-up while locked, and again when the finishing browser's grace period has ended (nobody is paired,
  no job runs, maintenance is closed). That frees about 30 MiB; the unit's `Restart=on-failure` leaves it stopped.
  `--no-retire-when-locked` keeps it running (`--mock` does by default, so a development server survives a finished walk-through).
* **`eitri-configurator unlock`** creates the maintenance flag, **starts the service if it is not running** (a running
  wizard, possibly mid-flash, is never restarted: it notices the flag within five seconds) and waits (on a terminal) for it to
  publish the pairing code, which it prints. Exit status 3 means the flag is set but the service could not be started.
  `--no-start` only sets the flag. With a `--data-dir` other than `/data` it never touches systemd.
* **Where the code is published: `/data/eitri/configurator/pairing.json`** (mode 0640, owner eitri; not under
  `/run`). `{"state": "pairing", "code": "1234-5678", "updated": 1760000000}` while pairing is open;
  `{"state": "locked", "code": null, ...}` otherwise, including after the service retired (the file stays).
  Klarity reads this file: its "Unlock setup" runs `sudo -n /usr/bin/eitri-configurator unlock` (a narrow sudoers rule), starts the unit,
  and polls the file until `state` is `pairing`.
* **Non-interactive callers do not wait.** Without a terminal on stdout, `unlock` does not wait for the code (the caller, Klarity with
  its 10 s command timeout, polls `pairing.json` itself); `--wait N` overrides. On a terminal it waits up to 20 s and prints the code.
* **`/usr/bin/eitri-configurator` is safe to run as root.** The wrapper executes only code from the read-only image
  (`python3 -I`, `/usr/lib/eitri/configurator`), never anything under `/data`, and the command drops to the `eitri` user before it
  touches a file, so a flag symlinked by the eitri user cannot be turned into a root write. The admin commands import no web
  libraries.
* **Trust model.** Same as Moonraker's: the local network may read; changing anything needs someone at the
  printer. See `SECURITY.md`.

## Remote access (optional)

An optional step before Done, **off by default**: nothing is added unless the person at the printer does it. It can add an SSH
public key for the `eitri` user, set that user's password (with SSH password login as a separate, explicit switch that defaults to
off) and remove both again. The configurator never edits `authorized_keys`, `shadow` or `sshd_config`; it calls the image's privileged helper
through the narrow sudoers rules for exactly these command lines:

```
sudo -n /usr/libexec/eitri/eitri-access set-key | set-password | set-password --ssh-login | disable | status
```

`set-key` reads one OpenSSH public key line on stdin, `set-password` reads the password on stdin, `status` prints JSON. The helper is the
os lane's; the configurator's checks are the first of two strict gates (a test runs the configurator against the real helper in its scratch-root test mode).

* **What a password means:** the helper documents that a password gives the account general sudo and a console login; sshd keeps refusing
  passwords unless `--ssh-login` is requested. The UI says so (a warning, and the "Also let SSH accept this password" switch, off by default).
* **Keys:** one line, `ssh-ed25519`, ECDSA (P-256/384/521), FIDO (`sk-...`) or RSA of 2048 bits and more. The base64 body must decode, its
  embedded type must match, key lengths must be exact, options such as `command="..."` and `ssh-dss` are refused, a private key gets an
  explicit warning, and the comment is reduced to harmless characters. The answer carries only the `ssh-keygen -l` fingerprint.
* **Passwords:** at least 10 characters, at most 128 bytes, no control characters or edge spaces, entered twice, with a short deny-list
  of obvious choices. The UI keeps it in the form only (no storage, state or URL) and clears it after sending.
* **Secrets never travel in argv or the environment** (the helper gets a minimal environment), are never logged and never echoed; helper
  error text that contains the secret is dropped. Exit 3 from the helper (input rejected) becomes a 422 with the helper's own message; exit 4
  (saved, applied by systemd a moment later) is a success with `pending: true`, which the UI follows until it clears.
* **Auth:** `GET /api/access` (it reveals how the printer can be reached) and all writes need a paired browser, writes also the CSRF header.
  A locked configurator refuses all of it.
* **`status` JSON the UI understands** (anything else is ignored): `keys` (list of `{fingerprint, ...}`), `password` (`{set, ssh_login}`),
  `pending`. A missing helper, a refusing sudo or unreadable output means "not available": the step then says so and can be skipped.
* **Sandbox:** the unit sets `NoNewPrivileges=false`, because sudo is setuid and `true` would block the helper (the OS image's drop-in
  `20-eitri-os.conf` repeats that). The unit uses none of the seccomp-based sandbox options (`SystemCallFilter`, `ProtectKernelTunables`,
  `RestrictNamespaces`, ...): systemd sets `NoNewPrivs` anyway when they are present, which makes sudo refuse to run (measured on the QEMU
  image). klipper and moonraker keep `true`.
* **Mock mode** swaps the helper for `mock/eitri_access.py` (same command lines and JSON, state in the scratch data directory).

## Web interface sign-in and port 80

The Options step asks who may use the web interface: *Trust my home network* (the default, devices on private addresses need no sign-in) or
*Require sign-in*. Slicers and apps use **port 80**, like the browser. Moonraker's own port listens on loopback only, and no screen tells
anyone to use it (a test keeps the number out of the UI). SSH is a separate, later and optional decision.

## Camera sign-in (separate page)

The RTSP password that `camera/bin/eitri-go2rtc-auth ensure` generates (`/data/printer_data/config/go2rtc-auth.env`, user `eitri`)
is **not part of the wizard**. The Done step links to `camera.html`, a page of its own that shows the address, user name and password with
Show and Copy buttons (the clipboard API does not exist over plain http, so a selection fallback is used). `GET /api/camera/rtsp` needs a paired
browser, is never cached, and the password is in no other response (bootstrap, state, preview). The page explains when the gateway is off or
the file does not exist yet. Open it from the Done step within ten minutes, or unlock first.

## Safety properties

* One exclusive job at a time; flashing, detection, writing the configuration, calibration and restarting Klipper or Moonraker refuse to start while Moonraker reports a print.
* Flashing needs an explicit `confirm`, uses the **server-side** detection result (the client never supplies a port), and is blocked unless both boards were identified with a serial-reachable bootloader (ROM-only or unidentified bootloaders block it, because recovery is unproven).
* A failed flash stops the sequence, leaves the other board untouched, and restarts Klipper.
* Z-offset nudges are whitelisted and large downward steps are refused near the bed. The probe's pre-triggered check is never suppressed: Z is lifted first.
* Heater runaway protection is not an option anywhere. The chamber heater is off by default and needs an explicit acknowledgement.
* State-changing requests need a paired session (above); a CSP forbids inline script and external hosts; no subprocess is started through a shell. Serial ports are matched in full (`fullmatch`, so a trailing newline is not a port).
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
(the source tree; the image itself carries no copy). The wizard supplies values only: the two MCU
serial ports from detection (`[mcu]` is the toolhead, `[mcu U_1]` the mainboard), `chamber_enabled`
(needs the acknowledgement), `plr_enabled` and `adxl_enabled`; everything else is the layer's default,
overridable per printer in `printer_data/config/eitri-vars.toml` (a `[vars]` table). After writing,
`overlay-install.sh` links the extras and macros into the Klipper/Kalico clone (skipped, with a
warning, while `/data/firmware/klipper` does not exist).

**Moonraker access** - `eitri-access.conf` (regenerated on every Apply from the wizard's *Access* option) holds
`[server]` and `[authorization]`; the seeded `moonraker.conf` includes it first, so options set in
`moonraker.conf` still win. Moonraker listens on `127.0.0.1` only and nginx (port 80) is the way in from the
network: Moonraker trusts `X-Real-IP` from whoever sends it (Tornado `xheaders`; it has no `trusted_proxies`
option), so a reachable port 7125 would let any caller claim to be `127.0.0.1`. *Trust my home network*
(default) trusts loopback and the private ranges; *Require sign-in* trusts loopback only and sets
`force_logins`. CORS lists only the printer's own names, no `*.lan`/`*.local` wildcards. Anyone Moonraker
trusts can edit `printer.cfg` and, through Klipper's shell commands, run programs as the service user, so
this decision matters.

**Files written outside the config dir** - `/data/eitri/flavor` (contract),
`/data/eitri/channel`, `/data/eitri/ui`, `/data/eitri/hostname`. Optional helpers
are run when present: `/usr/libexec/eitri/switch-flavor <klipper|kalico>` and
`/usr/libexec/eitri/toolchain install`.

**Klarity** - status comes from `systemctl is-active klarity` and the daemon's
`/run/klarity/status.json` (`connected`, `display`, `port`, `baud`, `version`; written by Klarity
every few seconds, `[klarity] status_file`).

**Camera** - the UI links `/webcam/stream` (nginx, from `camera/moonraker`).

**Privileges** - the unit runs as `eitri` with `NoNewPrivileges=false` (sudo for `eitri-access` only); it needs a polkit rule allowing that
user to start/stop/restart `klipper`, `moonraker`, `klarity`, `eitri-camera` and
`go2rtc`, and to call `hostnamectl set-hostname` and NetworkManager (`connection add/up/delete` for
the one `eitri-wifi` profile).

**Pairing display** - Klarity (or any other front end) reads `/data/eitri/configurator/pairing.json` (not `/run`), and unlocks with `sudo -n /usr/bin/eitri-configurator unlock`.

## Design

Dark "anvil" theme with a "chalk and steel" light theme (follows the OS, toggle persists). Instrument Serif
for display, Schibsted Grotesk for text, Martian Mono for technical values (all OFL, bundled in
`web/fonts`). The one recurring idea is tempered steel: a blue-black workshop, cold iron, and the blue
oxide sheen on a finished blade, used for progress, the active step and primary actions. Each step has an
Elder Futhark rune drawn as strokes (Thurisaz, the hammer, for flashing); the logo is an anvil. The UI
deliberately has no hot-metal or combustion imagery or wording, and a test (`tests/test_ui_wording.py`)
keeps it that way. Layout is a step rail on desktop and a segmented progress meter with a sticky action
bar on phones. Motion respects `prefers-reduced-motion`.
