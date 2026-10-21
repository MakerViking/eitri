# Progress events

`install.sh`, `revert.sh`, `eitri-mcuflash` and `eitri-update` all emit the same JSON-lines stream with `--json`
(stdout; human text goes to stderr) and/or `--events-file FILE` (appended). One object per line:

| field | meaning |
|---|---|
| `schema` | always `1` |
| `ts` | UTC timestamp, millisecond resolution |
| `run` | id shared by every line of one invocation (`EITRI_RUN_ID` to share it across tools) |
| `component` | `install`, `revert`, `mcuflash`, `update`, `stage2` |
| `type` | `plan`, `step`, `progress`, `log`, `prompt`, `result` |
| `step` | machine name: `preflight`, `manifest`, `layout`, `backup`, `backup-verify`, `stage`, `mcu`, `arm`, `probe`, `bootloader`, `roundtrip`, `flash`, `verify`, `rollback`, `health`, … |
| `status` | for `step`: `start`, `ok`, `failed`, `skipped` |
| `board` | `mainboard`, `toolhead` or absent |
| `pct` | 0-100 for `progress` |
| `level` | `debug`, `info`, `warn`, `error` for `log` |
| `msg` | human text (safe to show) |
| `data` | structured payload; on `result` events the full plan / outcome |
| `ok` | boolean on `result` (the last line of every invocation) |

A `prompt` event asks the human to do something physical (e.g. move a microSD card). Errors carry
`data = {code, message, step, board, port, bootloader, recovery}`; `recovery` is always a concrete next action.

Stage 2 (RAM) cannot stream to a client; it writes `http://<printer>:8099/status.json`
(`{"state": "running|done|failed", "pct", "step", "msg", "ts"}`) and a small page that polls it.
The log is also saved to `/boot/eitri/stage2.log`, a failure to `/boot/eitri/LAST_ERROR`
(`install.sh --status` prints both).

Exit codes. `install.sh`: 0 ok, 2 usage, 3 not confirmed, 10 preflight, 11 backup, 12 download/signature,
13 MCU recovery not proven, 20 missing tools. `eitri-mcuflash`: 0, 1 failed, 2 usage, 3 blocked by preflight
(nothing touched), 4 operator action needed. `eitri-update`: 0, 1 failed, 2 usage, 3 refused by policy
(up to date, downgrade, upstream mode).
