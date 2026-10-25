"""Learn mode: record every screen event so the stock .tft can be mapped on live hardware.

For each inbound event a JSON line is appended to the learn log, flagged ``known`` when the
screen map already handles it.  After a touch, the recorder waits ``window`` seconds and logs
what visibly changed on the printer side (state fields, G-code responses): that is the
quickest way to discover which Moonraker call a stock button triggers when the stock host
daemon is driving a printer you can watch.  ``summary()`` renders a paste-ready YAML skeleton
of the widgets seen but not yet in the map.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any, TextIO

import yaml

from .protocol import (
    Event,
    Garbage,
    MarkerEvent,
    NumberEvent,
    PageEvent,
    StringEvent,
    TouchEvent,
    TOUCH_PRESS,
    TOUCH_RELEASE,
    TOUCH_TAP,
    TouchXYEvent,
    UnknownFrame,
)
from .screenmap import ScreenMap
from .state import PrinterState

log = logging.getLogger(__name__)

EDGE_NAME = {TOUCH_PRESS: "press", TOUCH_RELEASE: "release", TOUCH_TAP: "tap"}

#: Fields that change constantly and would drown out the effect of a button press.
NOISY_FIELDS = frozenset(
    {
        "temperature",
        "power",
        "speed",
        "position",
        "gcode_position",
        "estimated_print_time",
        "print_duration",
        "total_duration",
        "filament_used",
        "progress",
        "file_position",
        "live_position",
        "live_velocity",
        "live_extruder_velocity",
        "mcu_awake",
        "mcu_task_avg",
        "last_temp_time",
        "eventtime",
        "printing_time",
    }
)


class LearnRecorder:
    def __init__(
        self, smap: ScreenMap, state: PrinterState, log_path: str | None, window: float = 2.0
    ) -> None:
        self.smap = smap
        self.state = state
        self.window = window
        self.seen: Counter[tuple[int, int, int]] = Counter()
        self.unknown_markers: Counter[int] = Counter()
        self.unknown_frames: Counter[str] = Counter()
        self._fh: TextIO | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        if log_path:
            try:
                path = Path(log_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = path.open("a", encoding="utf-8", buffering=1)
            except OSError as exc:
                log.warning("learn log %s unavailable (%s); logging to journal only", log_path, exc)

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._fh:
            self._fh.close()
            self._fh = None

    # ------------------------------------------------------------------------ recording
    def record(self, event: Event, page: int | None) -> None:
        info: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "page": page,
            "raw": event.raw.hex(" "),
        }
        known = True
        if isinstance(event, TouchEvent):
            known = self.smap.is_known_touch(event.page, event.widget) and (
                self.smap.actions_for_touch(event.page, event.widget, event.type) is not None
            )
            self.seen[(event.page, event.widget, event.type)] += 1
            info.update(kind="touch", page=event.page, widget=event.widget, type=event.type)
            if event.type != TOUCH_PRESS:
                self._schedule_effect_probe(info)
        elif isinstance(event, MarkerEvent):
            known = self.smap.marker(event.code) is not None
            if not known:
                self.unknown_markers[event.code] += 1
            info.update(kind="marker", code=event.code)
        elif isinstance(event, NumberEvent):
            info.update(kind="number", value=event.value)
        elif isinstance(event, StringEvent):
            info.update(kind="string", text=event.text)
        elif isinstance(event, PageEvent):
            info.update(kind="page", page=event.page)
        elif isinstance(event, TouchXYEvent):
            info.update(kind="touch_xy", x=event.x, y=event.y, type=event.type)
        elif isinstance(event, UnknownFrame):
            known = False
            self.unknown_frames[event.raw.hex(" ")] += 1
            info.update(kind="unknown", id=event.id, payload=event.payload.hex(" "))
        elif isinstance(event, Garbage):
            known = False
            info.update(kind="garbage", reason=event.reason)
        info["known"] = known
        self._write(info)
        if not known:
            log.info("LEARN unmapped event: %s", json.dumps(info, ensure_ascii=False))
        else:
            log.debug("learn event: %s", info)

    def _write(self, obj: dict[str, Any]) -> None:
        if self._fh:
            try:
                self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.warning("learn log write failed: %s", exc)

    def _schedule_effect_probe(self, touch: dict[str, Any]) -> None:
        before = self.state.flat()
        started = time.monotonic()
        task = asyncio.get_running_loop().create_task(self._effect(touch, before, started))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _effect(self, touch: dict[str, Any], before: dict[str, Any], started: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(self.window)
            after = self.state.flat()
            changes = {
                key: [before.get(key), after.get(key)]
                for key in sorted(before.keys() | after.keys())
                if before.get(key) != after.get(key) and key.rpartition(".")[2] not in NOISY_FIELDS
            }
            gcode = [line for t, line in self.state.gcode_responses if t >= started]
            if changes or gcode:
                self._write(
                    {
                        "kind": "effect",
                        "of": {k: touch[k] for k in ("page", "widget", "type")},
                        "state_changes": changes,
                        "gcode_responses": gcode,
                    }
                )

    # ------------------------------------------------------------------------- reporting
    def summary(self) -> str:
        """YAML skeleton of every (page, widget) seen that the map does not handle yet."""
        pages: dict[str, Any] = {}
        for (page, widget, typ), count in sorted(self.seen.items()):
            if self.smap.actions_for_touch(page, widget, typ) is not None:
                continue
            entry = pages.setdefault(f"page_{page}", {"id": page, "todo": True, "widgets": {}})
            w = entry["widgets"].setdefault(f"widget_{widget}", {"id": widget, "todo": True})
            w.setdefault("seen", {})[EDGE_NAME.get(typ, str(typ))] = count
        out: dict[str, Any] = {"pages": pages}
        if self.unknown_markers:
            out["markers"] = {
                code: {"name": f"marker_{code:#04x}", "todo": True}
                for code in sorted(self.unknown_markers)
            }
        return yaml.safe_dump(out, sort_keys=False)
