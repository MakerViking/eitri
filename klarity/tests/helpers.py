"""Shared builders for tests that need a running Screen + map + state."""

from __future__ import annotations

import asyncio
import contextlib
import copy
from typing import Any

from fakes import DEFAULT_STATUS
from klarity.config import KlarityConfig
from klarity.screen import Screen
from klarity.screenmap import ScreenMap
from klarity.state import PrinterState
from klarity.transport import MemoryTransport

# A fully "confirmed" map (no todo flags) used by behaviour tests.
TEST_MAP: dict[str, Any] = {
    "version": 1,
    "screen": {"init": ["dim=100"]},
    "pages": {
        "home": {
            "id": 1,
            "name": "main",
            "on_enter": [{"log": "entered home"}],
            "widgets": {
                "state": {
                    "obj": "t_state",
                    "bind": {
                        "source": "derived.state",
                        "map": {
                            "standby": "Idle",
                            "printing": "Printing",
                            "paused": "Paused",
                            "offline": "Offline",
                            "_default": "?",
                        },
                    },
                },
                "nozzle": {
                    "obj": "t_noz",
                    "bind": {
                        "source": ["extruder.temperature", "extruder.target"],
                        "format": "{0:.0f} / {1:.0f}",
                    },
                },
                "bed": {
                    "obj": "t_bed",
                    "bind": {
                        "source": ["heater_bed.temperature", "heater_bed.target"],
                        "format": "{0:.0f} / {1:.0f}",
                    },
                },
                "progress": {
                    "obj": "j_prog",
                    "bind": {"source": "derived.progress", "attr": "val"},
                },
                "pause": {
                    "id": 10,
                    "obj": "b_pause",
                    "bind": {"source": "derived.state", "attr": "vis", "test": {"eq": "printing"}},
                    "on_release": [
                        {
                            "print": "pause",
                            "guard": {"source": "derived.state", "eq": "printing"},
                            "else_beep": 50,
                        }
                    ],
                },
                "resume": {
                    "id": 11,
                    "on_release": [
                        {"print": "resume", "guard": {"source": "derived.state", "eq": "paused"}}
                    ],
                },
                "cancel": {
                    "id": 12,
                    "on_release": [
                        {
                            "print": "cancel",
                            "guard": {"source": "derived.state", "in": ["printing", "paused"]},
                            "cooldown": 5,
                        }
                    ],
                },
                "home_all": {
                    "id": 13,
                    "on_release": [
                        {"gcode": "G28", "guard": {"source": "derived.state", "eq": "standby"}}
                    ],
                },
                "to_move": {"id": 14, "on_release": [{"page": "move"}]},
                "set_nozzle": {
                    "id": 15,
                    "on_release": [
                        {
                            "read": {"value": "n_noz.val"},
                            "gcode": "SET_HEATER_TEMPERATURE HEATER=extruder TARGET={value}",
                        }
                    ],
                },
                "set_name": {
                    "id": 16,
                    "on_release": [
                        {
                            "read": {"name": "t_name.txt"},
                            "gcode": "SET_GCODE_VARIABLE MACRO=x VARIABLE=n VALUE={name}",
                        }
                    ],
                },
            },
        },
        "move": {
            "id": 2,
            "widgets": {
                "x_plus": {
                    "id": 20,
                    "on_release": [
                        {
                            "gcode": "SAVE_GCODE_STATE NAME=j\nG91\nG1 X10 F6000\nRESTORE_GCODE_STATE NAME=j"  # noqa: E501
                        }
                    ],
                },
                "back": {"id": 21, "on_release": [{"page": "home"}]},
                "z": {"obj": "t_z", "bind": {"source": "derived.z", "format": "{:.1f}"}},
            },
            "on_number": [{"gcode": "M117 {value}"}],
        },
    },
    "markers": {0x91: {"name": "go_home", "page": "home"}},
}


def build_map(**kw: Any) -> ScreenMap:
    return ScreenMap.from_dict(copy.deepcopy(TEST_MAP), **kw)


def ready_state() -> PrinterState:
    st = PrinterState()
    st.set_connection(True)
    st.apply(copy.deepcopy(DEFAULT_STATUS))
    st.set_klippy("ready")
    return st


@contextlib.asynccontextmanager
async def live_screen():
    t = MemoryTransport()
    screen = Screen(t, reconnect_min=0.02, reconnect_max=0.05, frame_timeout=0.1)
    task = asyncio.create_task(screen.run())
    await asyncio.wait_for(screen.connected.wait(), 2)
    try:
        yield screen, t
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def fast_config(moonraker_url: str, **learn: Any) -> KlarityConfig:
    cfg = KlarityConfig()
    cfg.moonraker.url = moonraker_url
    cfg.moonraker.backoff_min = 0.05
    cfg.moonraker.backoff_max = 0.2
    cfg.push.min_interval = 0.02
    cfg.push.full_refresh = 60
    cfg.screen.frame_timeout = 0.1
    if learn:
        cfg.learn.enabled = True
        cfg.learn.log_path = learn["log_path"]
        cfg.learn.window = learn.get("window", 0.1)
    return cfg
