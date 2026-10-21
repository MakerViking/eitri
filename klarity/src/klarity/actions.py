"""Executes map actions: G-code, print control, page navigation, beeps, label updates."""

from __future__ import annotations

import contextlib
import logging
import re
import string
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .moonraker import MoonrakerClient, MoonrakerError
from .protocol import Cmd, ProtocolError
from .screen import Screen, ScreenError
from .screenmap import Action, ScreenMap
from .state import PrinterState

log = logging.getLogger(__name__)

#: Text read back from the panel ends up in G-code, so only inert characters are accepted
#: (no newlines, ``;`` comments or ``{}``): the screen is a user-input device, not trusted.
_SAFE_TEXT = re.compile(r"^[\w .+\-/]*$")
ERROR_BEEP_MS = 300


class ActionError(Exception):
    """An action could not be carried out; the message is safe to log."""


class _SafeFormatter(string.Formatter):
    def get_value(self, key: int | str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        if isinstance(key, int):
            raise ActionError("positional placeholders are not supported")
        if key not in kwargs:
            raise ActionError(f"unknown placeholder {{{key}}}")
        value = kwargs[key]
        if isinstance(value, str) and not _SAFE_TEXT.match(value):
            raise ActionError(f"refusing unsafe text for {{{key}}}: {value!r}")
        return value

    def format_field(self, value: Any, format_spec: str) -> str:
        if format_spec and not isinstance(value, int | float):
            raise ActionError("format spec on non-numeric value")
        return super().format_field(value, format_spec)


_fmt = _SafeFormatter()


class ActionRunner:
    def __init__(
        self,
        screen: Screen,
        moonraker: MoonrakerClient,
        state: PrinterState,
        smap: ScreenMap,
        *,
        dry_run: bool = False,
    ) -> None:
        self.screen = screen
        self.moonraker = moonraker
        self.state = state
        self.smap = smap
        self.dry_run = dry_run
        self._last_run: dict[int, float] = {}

    async def run(self, actions: Sequence[Action], context: Mapping[str, Any]) -> None:
        """Run actions in order.  A failing action aborts the rest of its list."""
        ctx = dict(context)
        for action in actions:
            try:
                await self._run_one(action, ctx)
            except (ActionError, MoonrakerError, ScreenError, ProtocolError) as exc:
                log.warning("action %s failed: %s", action.verb, exc)
                await self._beep(ERROR_BEEP_MS)
                return

    async def _run_one(self, action: Action, ctx: dict[str, Any]) -> None:
        if action.guard is not None and not action.guard.evaluate(self.state):
            log.info("action %s blocked by guard on %s", action.verb, action.guard.source)
            await self._beep(action.else_beep)
            return
        if action.cooldown:
            now = time.monotonic()
            if now - self._last_run.get(id(action), -1e9) < action.cooldown:
                log.debug("action %s suppressed by cooldown", action.verb)
                return
            self._last_run[id(action)] = now
        for name, expr in action.read.items():
            ctx[name] = await self.screen.get(expr)
        if (action.clamp is not None or action.scale is not None) and "value" in ctx:
            # Numeric entry: bound what the panel sent before it reaches G-code.
            ctx = {**ctx, "value": action.adjust(ctx["value"])}

        verb, arg = action.verb, action.arg
        if verb == "gcode":
            script = _fmt.vformat(arg, (), ctx).strip()
            log.info("gcode: %s", script.replace("\n", " | "))
            if not self.dry_run:
                await self.moonraker.gcode(script)
        elif verb == "print":
            log.info("print control: %s", arg)
            if not self.dry_run:
                await self._print(arg, action, ctx)
        elif verb == "page":
            page = self.smap.pages[arg]
            target = page.id if page.id is not None else page.name
            if target is None:
                raise ActionError(f"page {arg!r} has neither an id nor a name")
            await self.screen.goto(target)
        elif verb == "beep":
            await self._beep(arg)
        elif verb == "set":
            value = arg["value"]
            if isinstance(value, str):
                value = _fmt.vformat(value, (), ctx)
            await self.screen.send(Cmd.set_attr(arg["obj"], arg["attr"], value))
        elif verb == "log":
            log.info("map: %s", _fmt.vformat(arg, (), ctx))

    async def _print(self, verb: str, action: Action, ctx: dict[str, Any]) -> None:
        mr = self.moonraker
        if verb == "start":
            assert action.file is not None
            await mr.start_print(_fmt.vformat(action.file, (), ctx))
            return
        await {
            "pause": mr.pause,
            "resume": mr.resume,
            "cancel": mr.cancel,
            "emergency_stop": mr.emergency_stop,
            "firmware_restart": mr.firmware_restart,
            "restart": mr.restart,
        }[verb]()

    async def _beep(self, ms: int) -> None:
        if ms and not self.dry_run:
            with contextlib.suppress(ScreenError, ProtocolError):
                await self.screen.send(Cmd.beep(ms))
