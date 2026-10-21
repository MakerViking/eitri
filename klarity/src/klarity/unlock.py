"""The "Unlock setup" action: reopen the configurator's pairing and show the code on the panel.

Decision 8: the configurator exits after setup (``--retire-when-locked``).  To change settings
later the owner taps a repurposed stock button (see ``maps/default.yaml``, page ``network``,
widget ``unlock_setup``).  This module then

1. runs the privileged helper ``sudo -n /usr/bin/eitri-configurator unlock`` (a narrow sudoers rule;
   it only creates the maintenance flag under ``/data/eitri/configurator``),
2. starts ``eitri-configurator.service`` (the eitri user may do that through polkit; a no-op if it
   is already running, in which case its watcher republishes the code within a few seconds),
3. waits for ``pairing.json`` (``state: pairing``, ``code: dddd-dddd``, from the configurator) and
4. returns the code and the address a browser on the LAN should open.

No shell is involved: both commands are split with :mod:`shlex` and executed directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .config import UnlockConfig

log = logging.getLogger(__name__)

#: Pairing codes are published as ``dddd-dddd`` (configurator ``auth.display_code``).
_CODE = re.compile(r"^\d{4}-\d{4}$")
#: What may be shown as the address: IPv4 literals and host names, nothing else.
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
#: A documentation-range address: connecting a UDP socket to it sends nothing but makes the kernel
#: pick the source address of the default route.
_ROUTE_PROBE = ("192.0.2.1", 9)
POLL_INTERVAL = 0.25

#: ``(argv, timeout) -> (returncode, combined output)``; replaced in tests.
Spawn = Callable[[list[str], float], Awaitable[tuple[int, str]]]


class UnlockError(Exception):
    """Unlocking failed; the message is safe to log and does not contain the pairing code."""


@dataclass(frozen=True, slots=True)
class UnlockResult:
    code: str  # "1234-5678"
    host: str  # "192.168.0.5:8099" (what fits on the panel)
    url: str  # "http://192.168.0.5:8099"


async def run_command(argv: list[str], timeout: float) -> tuple[int, str]:
    """Run ``argv`` without a shell, stdin closed, output captured (bounded), killed on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group, so a timeout can stop its children too
        )
    except OSError as exc:
        raise UnlockError(f"cannot run {argv[0]}: {exc.strerror or exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # a setuid child (sudo) may refuse; then kill below
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 2)
        raise UnlockError(f"{argv[0]} timed out after {timeout:g}s") from None
    return proc.returncode if proc.returncode is not None else -1, out[:2000].decode(
        "utf-8", "replace"
    )


def local_address() -> str | None:
    """IPv4 address of the default route, or None when the printer has no network."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(_ROUTE_PROBE)  # UDP connect: no packet is sent
            addr = sock.getsockname()[0]
    except OSError:
        return None
    return None if addr.startswith("0.") else addr


class SetupUnlocker:
    def __init__(
        self,
        cfg: UnlockConfig,
        *,
        spawn: Spawn = run_command,
        address: Callable[[], str | None] = local_address,
        hostname: Callable[[], str] = socket.gethostname,
    ) -> None:
        self.cfg = cfg
        self._spawn = spawn
        self._address = address
        self._hostname = hostname
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ pieces
    def _argv(self, command: str, what: str) -> list[str]:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise UnlockError(f"[unlock] {what} is not a valid command: {exc}") from exc
        if not argv:
            raise UnlockError(f"[unlock] {what} is empty")
        return argv

    def read_pairing(self) -> tuple[str, str | None]:
        """(state, code) from ``pairing.json``; ("unknown", None) if unreadable."""
        try:
            doc = json.loads(Path(self.cfg.pairing_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "unknown", None
        if not isinstance(doc, dict):
            return "unknown", None
        state = doc.get("state")
        code = doc.get("code")
        if state == "pairing" and isinstance(code, str) and _CODE.match(code):
            return "pairing", code
        return (state if isinstance(state, str) else "unknown"), None

    def printer_host(self) -> str:
        """``host:port`` for a LAN browser: the route's IPv4 address, else ``<name>.local``."""
        host = self.cfg.host.strip()
        if not host:
            host = self._address() or ""
        if not host:
            name = self._hostname().split(".")[0]
            host = f"{name}.local" if name else ""
        if not host or not _HOST.match(host):
            raise UnlockError("cannot work out the printer's network address")
        return f"{host}:{self.cfg.port}"

    def result_for(self, code: str) -> UnlockResult:
        host = self.printer_host()
        return UnlockResult(code=code, host=host, url=f"http://{host}")

    # ------------------------------------------------------------------- actions
    def show(self) -> UnlockResult:
        """Read-only: the current code if pairing is open (used by ``--dry-run``)."""
        state, code = self.read_pairing()
        if code is None:
            raise UnlockError(f"setup is locked (pairing state: {state})")
        return self.result_for(code)

    async def unlock(self) -> UnlockResult:
        if not self.cfg.enabled:
            raise UnlockError("unlocking from the screen is disabled ([unlock] enabled)")
        # One tap at a time: a second tap while the first waits just waits for the same outcome.
        async with self._lock:
            return await self._unlock()

    async def _unlock(self) -> UnlockResult:
        unlock_cmd = self._argv(self.cfg.command, "command")
        start_cmd = None
        if self.cfg.start_command:
            start_cmd = self._argv(self.cfg.start_command, "start_command")
        rc, out = await self._spawn(unlock_cmd, self.cfg.timeout)
        if rc != 0:
            log.warning("unlock helper failed (exit %d): %s", rc, out.strip()[:200])
            raise UnlockError(f"unlock helper exited with status {rc}")
        if start_cmd is not None:
            # The configurator retires itself while locked; start it again.  A failure here is not
            # fatal: it may already be running (the start is then a no-op or merely denied).
            rc, out = await self._spawn(start_cmd, self.cfg.timeout)
            if rc != 0:
                log.warning("starting the configurator failed (exit %d): %s", rc, out.strip()[:200])
        deadline = time.monotonic() + self.cfg.wait
        while True:
            state, code = self.read_pairing()
            if code is not None:
                log.info("setup unlocked; pairing code shown on the panel")
                return self.result_for(code)
            if time.monotonic() >= deadline:
                raise UnlockError(
                    f"the configurator did not publish a pairing code (state: {state}); "
                    "is eitri-configurator.service installed?"
                )
            await asyncio.sleep(POLL_INTERVAL)
