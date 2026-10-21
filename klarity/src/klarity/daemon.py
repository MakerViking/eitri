"""Wires everything together: screen events -> actions, Moonraker state -> screen."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from . import __version__
from .actions import ActionRunner
from .camera import CameraPreview, SnapshotFetcher
from .config import ConfigError, KlarityConfig
from .learn import LearnRecorder
from .moonraker import MoonrakerClient
from .protocol import (
    Cmd,
    Event,
    MarkerEvent,
    NumberEvent,
    StringEvent,
    TouchEvent,
)
from .pusher import Pusher
from .screen import Screen, ScreenError
from .screenmap import ScreenMap
from .state import PrinterState
from .transport import SerialTransport, Transport

log = logging.getLogger(__name__)


class Klarity:
    def __init__(
        self,
        cfg: KlarityConfig,
        *,
        smap: ScreenMap | None = None,
        transport: Transport | None = None,
        learn: bool | None = None,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.smap = smap or ScreenMap.load(cfg.map_path, allow_todo=cfg.allow_todo)
        if transport is None:
            if not cfg.screen.serial_port:
                raise ConfigError(
                    "[screen] serial_port is not set. The panel's UART is TBD until probed on the "
                    "printer; set it in klarity.cfg (or use --simulate)."
                )
            transport = SerialTransport(cfg.screen.serial_port, cfg.screen.baud)

        self.state = PrinterState()
        self.screen = Screen(
            transport,
            encoding=cfg.screen.encoding,
            quote_style=cfg.screen.quote_style,
            frame_timeout=cfg.screen.frame_timeout,
            single_byte_ids=tuple(self.smap.markers),
        )
        self.moonraker = MoonrakerClient(
            cfg.moonraker.url,
            self.state,
            api_key=cfg.moonraker.api_key,
            objects=sorted(self.smap.required_objects()),
            request_timeout=cfg.moonraker.request_timeout,
            backoff_min=cfg.moonraker.backoff_min,
            backoff_max=cfg.moonraker.backoff_max,
        )
        self.runner = ActionRunner(
            self.screen, self.moonraker, self.state, self.smap, dry_run=dry_run
        )
        self.pusher = Pusher(
            self.screen,
            self.state,
            self.smap,
            min_interval=cfg.push.min_interval,
            max_commands_per_second=cfg.push.max_commands_per_second,
            full_refresh=cfg.push.full_refresh,
        )
        learning = cfg.learn.enabled if learn is None else learn
        self.learn: LearnRecorder | None = (
            LearnRecorder(self.smap, self.state, cfg.learn.log_path, cfg.learn.window)
            if learning
            else None
        )
        self.camera: CameraPreview | None = None
        if cfg.camera.enabled and self.smap.camera is not None:
            self.camera = CameraPreview(
                self.screen,
                self.smap,
                SnapshotFetcher(cfg.camera.snapshot_url, cfg.camera.timeout),
                push_enabled=cfg.camera.push_enabled,
            )
        self._tasks: set[asyncio.Task[None]] = set()
        self._ready_logged = False
        self.screen.on_connect = self._on_screen_connect
        self.screen.on_page = self._on_page_change

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        self._log_map_status()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self.screen.run(), name="screen")
                group.create_task(self.moonraker.run(), name="moonraker")
                group.create_task(self.pusher.run(), name="pusher")
                group.create_task(self._dispatch_loop(), name="dispatch")
                if self.cfg.status_file:
                    group.create_task(self._status_loop(), name="status")
                if self.camera is not None:
                    group.create_task(self.camera.run(), name="camera")
        finally:
            if self.learn:
                self.learn.close()

    def status(self) -> dict[str, Any]:
        """Link state for the configurator (written to ``[klarity] status_file``)."""
        return {
            "connected": self.screen.connected.is_set(),
            "display": self.smap.meta.get("display"),
            "port": self.cfg.screen.serial_port or None,
            "baud": self.cfg.screen.baud,
            "page": self.screen.current_page,
            "moonraker": self.state.connected,
            "version": __version__,
            "updated": int(time.time()),
        }

    def write_status(self) -> None:
        path = self.cfg.status_file
        tmp = f"{path}.tmp"
        try:
            Path(tmp).write_text(json.dumps(self.status()), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("cannot write %s: %s", path, exc)

    async def _status_loop(self, interval: float = 5.0) -> None:
        while True:
            self.write_status()
            await asyncio.sleep(interval)

    def _log_map_status(self) -> None:
        todo, total = self.smap.todo_summary()
        if not self.smap.has_active_content:
            log.warning(
                "screen map has no confirmed widgets (%d/%d are TODO placeholders): running in "
                "observe-only mode%s. Fill in maps/default.yaml (ScreenMap work / learn mode).",
                todo,
                total,
                "" if self.learn else " (enable [learn] to record the screen's events)",
            )
        elif todo:
            log.info("screen map: %d of %d widgets still TODO and ignored", todo, total)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()):
            log.error("background task failed", exc_info=exc)

    # ---------------------------------------------------------------- screen hooks
    async def _on_screen_connect(self) -> None:
        # Quiet the panel's per-instruction status replies, then ask where it is.
        for command in (Cmd.bkcmd(self.cfg.screen.bkcmd), *self.smap.init, Cmd.sendme()):
            await self.screen.send(command)
        self.pusher.invalidate()

    def _on_page_change(self, page: int | None) -> None:
        log.debug("panel page -> %s", page)
        self.pusher.invalidate()
        spec = self.smap.page_by_id(page)
        if spec is not None and spec.on_enter:
            self._spawn(self.runner.run(spec.on_enter, {"page": page}))

    # ------------------------------------------------------------------- dispatch
    async def _dispatch_loop(self) -> None:
        while True:
            event = await self.screen.events.get()
            try:
                self.handle_event(event)
            except Exception:
                log.exception("error handling %r", event)

    def handle_event(self, event: Event) -> None:
        if self.learn:
            page = event.page if isinstance(event, TouchEvent) else self.screen.current_page
            self.learn.record(event, page)

        if isinstance(event, TouchEvent):
            actions = self.smap.actions_for_touch(event.page, event.widget, event.type)
            if actions:
                self._spawn(self.runner.run(actions, {"page": event.page, "widget": event.widget}))
        elif isinstance(event, MarkerEvent):
            self._handle_marker(event)
        elif isinstance(event, NumberEvent | StringEvent):
            page = self.smap.page_by_id(self.screen.current_page)
            if isinstance(event, NumberEvent):
                # The stock .tft reports a typed number as (page, widget, uint16).
                src_page, src_widget, value = event.stock
                actions = self.smap.actions_for_number(src_page, src_widget)
                if actions:
                    ctx = {"value": value, "page": src_page, "widget": src_widget}
                    self._spawn(self.runner.run(actions, ctx))
                elif page is not None and page.on_number:
                    ctx = {"value": event.value, "page": page.id}
                    self._spawn(self.runner.run(page.on_number, ctx))
            elif page is not None:
                self._spawn(self.runner.run(page.on_string, {"text": event.text, "page": page.id}))

    def _handle_marker(self, event: MarkerEvent) -> None:
        marker = self.smap.marker(event.code)
        if marker is None:
            log.debug("unmapped marker %#04x", event.code)
            return
        if marker.page_key is not None:
            target = self.smap.pages[marker.page_key]
            self.screen.set_page(target.id)
        if marker.actions:
            self._spawn(self.runner.run(marker.actions, {"code": event.code}))

    def log_learn_summary(self) -> None:
        if self.learn:
            log.info("learn summary (paste into the map):\n%s", self.learn.summary())

    async def wait_idle(self) -> None:
        """Wait for in-flight action tasks (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


__all__ = ["Klarity", "ScreenError"]
