# SPDX-License-Identifier: Apache-2.0
"""Progress events.

One JSON object per line, schema version 1, shared by every Eitri installer
component (install.sh, eitri-mcuflash, eitri-update) so the configurator UI can
tail a single stream.  Fields:

    schema     1
    ts         ISO-8601 UTC timestamp
    run        run id (stable for the whole invocation)
    component  "mcuflash" | "install" | "update" | ...
    type       "plan" | "step" | "progress" | "log" | "prompt" | "result"
    step       machine name of the current step (probe, backup, flash, ...)
    status     for type=step: "start" | "ok" | "failed" | "skipped"
    board      "mainboard" | "toolhead" | null
    pct        0..100 for type=progress
    level      "debug" | "info" | "warn" | "error" for type=log
    msg        human readable text
    data       free-form object (never contains secrets)
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid
from typing import Any, Dict, IO, List, Optional

SCHEMA = 1


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class EventSink:
    def __init__(
        self,
        *,
        component: str = "mcuflash",
        json_out: Optional[IO[str]] = None,
        human_out: Optional[IO[str]] = None,
        events_file: Optional[str] = None,
        run_id: Optional[str] = None,
        keep: bool = False,
    ) -> None:
        self.component = component
        self.run_id = run_id or os.environ.get("EITRI_RUN_ID") or uuid.uuid4().hex[:12]
        self._json_out = json_out
        self._human_out = human_out
        self._file: Optional[IO[str]] = None
        if events_file:
            self._file = open(events_file, "a", buffering=1, encoding="utf-8")
        self._lock = threading.Lock()
        # Tests (and the configurator wrapper) can look at what was emitted.
        self.events: List[Dict[str, Any]] = [] if keep else None  # type: ignore[assignment]

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    def emit(self, type_: str, **fields: Any) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "schema": SCHEMA,
            "ts": _now(),
            "run": self.run_id,
            "component": self.component,
            "type": type_,
        }
        for key in ("step", "status", "board", "pct", "level", "msg", "data"):
            if key in fields and fields[key] is not None:
                event[key] = fields.pop(key)
        event.update(fields)
        with self._lock:
            line = json.dumps(event, sort_keys=False, separators=(",", ":"))
            if self._json_out:
                self._json_out.write(line + "\n")
                self._json_out.flush()
            if self._file:
                self._file.write(line + "\n")
            if self._human_out:
                self._human_out.write(self._format_human(event) + "\n")
                self._human_out.flush()
            if self.events is not None:
                self.events.append(event)
        return event

    @staticmethod
    def _format_human(ev: Dict[str, Any]) -> str:
        tag = ev.get("board")
        prefix = "[%s] " % tag if tag else ""
        t = ev["type"]
        if t == "step":
            status = ev.get("status", "")
            return "%s%s: %s%s" % (prefix, ev.get("step", "?"), status, (" - " + ev["msg"]) if ev.get("msg") else "")
        if t == "progress":
            return "%s%s %3d%% %s" % (prefix, ev.get("step", ""), ev.get("pct", 0), ev.get("msg", ""))
        if t == "log":
            lvl = ev.get("level", "info")
            mark = {"warn": "WARNING: ", "error": "ERROR: "}.get(lvl, "")
            return "%s%s%s" % (prefix, mark, ev.get("msg", ""))
        if t == "result":
            return "%sRESULT: %s" % (prefix, "ok" if ev.get("ok") else "FAILED")
        return "%s%s: %s" % (prefix, t, ev.get("msg", ""))

    # -- convenience wrappers -------------------------------------------------
    def step(self, step: str, status: str, *, board: Optional[str] = None, msg: Optional[str] = None, data: Any = None):
        return self.emit("step", step=step, status=status, board=board, msg=msg, data=data)

    def progress(self, step: str, pct: float, *, board: Optional[str] = None, msg: Optional[str] = None):
        return self.emit("progress", step=step, pct=int(max(0, min(100, pct))), board=board, msg=msg)

    def log(self, level: str, msg: str, *, board: Optional[str] = None, step: Optional[str] = None, data: Any = None):
        return self.emit("log", level=level, msg=msg, board=board, step=step, data=data)

    def info(self, msg: str, **kw: Any):
        return self.log("info", msg, **kw)

    def warn(self, msg: str, **kw: Any):
        return self.log("warn", msg, **kw)

    def error(self, msg: str, **kw: Any):
        return self.log("error", msg, **kw)

    def result(self, ok: bool, *, data: Any = None, msg: Optional[str] = None):
        return self.emit("result", ok=ok, data=data, msg=msg)

