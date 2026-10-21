# SPDX-License-Identifier: Apache-2.0
"""Post-update health check: did the new system come up well enough to keep?"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

# Klippy messages that point at the MCU firmware or the link to it - the things an update (and the MCU
# re-flash that comes with it) can break.  A heater or endstop fault is the user's hardware, not ours.
MISMATCH_MARKERS = (
    "mismatch", "unknown command", "protocol error", "firmware version",
    "unable to connect", "unable to open serial", "timeout on connect", "lost communication", "mcu_unconnected",
    "error during config",
)


@dataclass
class Health:
    ok: bool
    detail: str
    klippy_state: str = ""


def check(
    moonraker_url: str = "http://127.0.0.1:7125",
    *,
    timeout: float = 120.0,
    interval: float = 3.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    fetch: Optional[Callable[[str], bytes]] = None,
) -> Health:
    """Healthy = Moonraker answers and Klippy is connected without a host/MCU mismatch.

    A printer sitting in `error` because of a hardware condition (heater, endstop)
    is not the update's fault, so only version-lock and MCU-link failures count against it.
    "startup" is not healthy yet: Klippy is still connecting to the MCUs, which is exactly what a bad
    firmware flash breaks, so the check keeps waiting for "ready" until the timeout.
    """
    def default_fetch(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - local URL
            return resp.read()

    get = fetch or default_fetch
    deadline = clock() + timeout
    last = "Moonraker did not answer"
    while clock() < deadline:
        try:
            info = json.loads(get(moonraker_url.rstrip("/") + "/server/info")).get("result", {})
            state = str(info.get("klippy_state", ""))
            if info.get("klippy_connected") and state == "ready":
                return Health(True, "klippy is ready", state)
            if info.get("klippy_connected"):
                try:
                    msg = json.loads(get(moonraker_url.rstrip("/") + "/printer/info")).get("result", {}).get("state_message", "")
                except (ValueError, OSError):
                    msg = ""
                if state in ("error", "shutdown") and any(m in msg.lower() for m in MISMATCH_MARKERS):
                    return Health(False, "MCU/host problem: %s" % (msg.strip().splitlines() or ["?"])[0], state)
                if state in ("error", "shutdown"):
                    return Health(True, "klippy reports %s for a non-update reason" % state, state)
            last = "klippy state %r, connected=%s" % (state, info.get("klippy_connected"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = "Moonraker not reachable (%s)" % getattr(exc, "reason", exc)
        sleep(interval)
    return Health(False, last)
