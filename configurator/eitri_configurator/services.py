"""systemd service control. In mock mode nothing is executed; calls are
recorded so tests (and the UI) can see what *would* have happened."""
from __future__ import annotations

import asyncio
import shutil
from typing import Any

from .proc import run
from .settings import Settings

# Services the wizard is allowed to touch; anything else is rejected before it
# reaches systemctl.
MANAGED = ("klipper", "moonraker", "klarity", "eitri-camera", "go2rtc")


class Services:
    def __init__(self, settings: Settings):
        self.s = settings
        self.mock_state: dict[str, str] = {
            "klipper": "active", "moonraker": "active", "klarity": "active",
            "eitri-camera": "active", "go2rtc": "inactive",
        }
        self.calls: list[tuple[str, str]] = []

    async def _systemctl(self, *args: str) -> tuple[int, str]:
        if not shutil.which("systemctl"):
            return 127, "systemctl not found"
        rc, out = await run("systemctl", *args, timeout=60)
        return rc, out.strip()

    async def status(self) -> dict[str, str]:
        if self.s.mock:
            return dict(self.mock_state)
        result = {}
        for name in MANAGED:
            try:
                _, out = await self._systemctl("is-active", f"{name}.service")
            except (asyncio.TimeoutError, OSError):
                out = "unknown"
            result[name] = out.splitlines()[0] if out else "unknown"
        return result

    async def act(self, action: str, name: str) -> dict[str, Any]:
        if name not in MANAGED or action not in ("start", "stop", "restart"):
            raise ValueError(f"refusing {action} {name}")
        self.calls.append((action, name))
        if self.s.mock:
            await asyncio.sleep(0.05 * min(self.s.mock_speed, 1) if self.s.mock_speed else 0)
            self.mock_state[name] = "inactive" if action == "stop" else "active"
            return {"ok": True, "service": name, "action": action}
        rc, out = await self._systemctl(action, f"{name}.service")
        return {"ok": rc == 0, "service": name, "action": action, "output": out}
