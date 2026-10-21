"""Mirror of the Moonraker/Klipper object model plus a few derived, display-oriented values."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

#: Objects always subscribed to (when Klipper reports them), regardless of the screen map.
DEFAULT_OBJECTS = (
    "webhooks",
    "print_stats",
    "virtual_sdcard",
    "display_status",
    "toolhead",
    "extruder",
    "heater_bed",
    "gcode_move",
    "idle_timeout",
    "pause_resume",
)

MISSING = object()


class PrinterState:
    """Latest known value of every subscribed Klipper object.

    Moonraker sends full objects once (subscribe reply) and then only changed fields, so
    updates are merged field-by-field.
    """

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.klippy_state: str = "disconnected"
        self.klippy_message: str = ""
        self.connected = False
        self.gcode_responses: deque[tuple[float, str]] = deque(maxlen=200)
        self.revision = 0
        self._listeners: list[Callable[[], None]] = []

    # -- change notification -----------------------------------------------------
    def subscribe(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _changed(self) -> None:
        self.revision += 1
        for cb in self._listeners:
            cb()

    # -- mutation ------------------------------------------------------------------
    def apply(self, delta: dict[str, Any]) -> None:
        for obj, fields in delta.items():
            if isinstance(fields, dict):
                self.data.setdefault(obj, {}).update(fields)
        self._changed()

    def reset(self) -> None:
        """Forget printer data (Klipper restarted / link lost); keep connection flags."""
        self.data.clear()
        self._changed()

    def set_connection(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            if not connected:
                self.klippy_state = "disconnected"
                self.data.clear()
            self._changed()

    def set_klippy(self, state: str, message: str = "") -> None:
        if (state, message) != (self.klippy_state, self.klippy_message):
            self.klippy_state, self.klippy_message = state, message
            if state != "ready":
                self.data.clear()
            self._changed()

    def add_gcode_response(self, line: str) -> None:
        self.gcode_responses.append((time.monotonic(), line))

    # -- lookup --------------------------------------------------------------------
    def resolve(self, path: str) -> Any:
        """Look up ``object.field[.sub|.index...]`` or ``derived.<name>``.

        The object name is everything before the first dot, so names with spaces such as
        ``heater_generic chamber.temperature`` work.  Returns :data:`MISSING` if absent.
        """
        head, _, rest = path.partition(".")
        if head == "derived":
            fn = DERIVED.get(rest)
            if fn is None:
                return MISSING
            try:
                return fn(self)
            except (KeyError, TypeError, ZeroDivisionError, IndexError):
                return MISSING
        node: Any = self.data.get(head, MISSING)
        if node is MISSING or not rest:
            return node
        for part in rest.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list | tuple) and part.lstrip("-").isdigit():
                try:
                    node = node[int(part)]
                except IndexError:
                    return MISSING
            else:
                return MISSING
        return node

    def flat(self) -> dict[str, Any]:
        """Flatten to ``object.field`` keys (used by learn mode to diff state)."""
        out: dict[str, Any] = {}
        for obj, fields in self.data.items():
            for key, value in fields.items():
                out[f"{obj}.{key}"] = value
        return out


# ----------------------------------------------------------------------- derived values


def _stats(s: PrinterState, key: str, default: Any = None) -> Any:
    return s.data.get("print_stats", {}).get(key, default)


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def d_state(s: PrinterState) -> str:
    """Single status word: print state, or why Klipper is unavailable."""
    if not s.connected:
        return "offline"
    if s.klippy_state != "ready":
        return s.klippy_state  # startup / shutdown / error / disconnected
    return _stats(s, "state", "standby")


def d_progress(s: PrinterState) -> float:
    """File progress in percent; 0 outside of an active job."""
    if _stats(s, "state") in ("standby", None):
        return 0.0
    return round(s.data["virtual_sdcard"]["progress"] * 100, 1)


def d_elapsed_s(s: PrinterState) -> float:
    return _stats(s, "print_duration", 0.0)


def d_remaining_s(s: PrinterState) -> float:
    progress = s.data["virtual_sdcard"]["progress"]
    elapsed = d_elapsed_s(s)
    if progress < 0.01 or elapsed <= 0:
        raise ZeroDivisionError
    return max(elapsed / progress - elapsed, 0.0)


DERIVED: dict[str, Callable[[PrinterState], Any]] = {
    "state": d_state,
    "progress": d_progress,
    "elapsed_s": d_elapsed_s,
    "elapsed": lambda s: format_duration(d_elapsed_s(s)),
    "remaining_s": d_remaining_s,
    "remaining": lambda s: format_duration(d_remaining_s(s)),
    "filename": lambda s: (_stats(s, "filename", "") or "").rsplit("/", 1)[-1],
    "layer": lambda s: _stats(s, "info", {}).get("current_layer") or 0,
    "total_layers": lambda s: _stats(s, "info", {}).get("total_layer") or 0,
    "message": lambda s: _stats(s, "message", "") or s.klippy_message,
    "is_printing": lambda s: d_state(s) == "printing",
    "is_paused": lambda s: d_state(s) == "paused",
    "speed_percent": lambda s: round(s.data["gcode_move"]["speed_factor"] * 100),
    "flow_percent": lambda s: round(s.data["gcode_move"]["extrude_factor"] * 100),
    # Feed rate actually requested, in mm/s: gcode_move.speed is mm/min before the override.
    "speed_mms": lambda s: round(
        s.data["gcode_move"]["speed"] * s.data["gcode_move"]["speed_factor"] / 60.0
    ),
    "z": lambda s: s.data["toolhead"]["position"][2],
    "homed": lambda s: s.data["toolhead"]["homed_axes"],
}
