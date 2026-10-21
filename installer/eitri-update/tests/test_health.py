# SPDX-License-Identifier: Apache-2.0
"""The post-update health check against canned Moonraker answers (no network)."""
import json

from eitri_update import health


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(info, printer_msg="", timeout=20.0):
    clock = Clock()

    def fetch(url):
        if url.endswith("/server/info"):
            seq = info if isinstance(info, list) else [info]
            return json.dumps({"result": seq[min(int(clock.t // 3), len(seq) - 1)]}).encode()
        return json.dumps({"result": {"state_message": printer_msg}}).encode()

    return health.check("http://x", timeout=timeout, interval=3.0, clock=clock, sleep=clock.sleep, fetch=fetch)


def test_ready_is_healthy():
    assert run({"klippy_connected": True, "klippy_state": "ready"}).ok


def test_startup_is_not_good_enough_until_it_becomes_ready():
    h = run([{"klippy_connected": True, "klippy_state": "startup"}] * 2 + [{"klippy_connected": True, "klippy_state": "ready"}])
    assert h.ok
    stuck = run({"klippy_connected": True, "klippy_state": "startup"})
    assert not stuck.ok and "startup" in stuck.detail


def test_mcu_link_failure_after_an_update_counts_against_it():
    h = run({"klippy_connected": True, "klippy_state": "error"}, "mcu 'mcu': Unable to connect\nOnce the underlying issue is corrected...")
    assert not h.ok and "Unable to connect" in h.detail


def test_version_mismatch_counts_against_it():
    assert not run({"klippy_connected": True, "klippy_state": "error"}, "Protocol error: Unknown command").ok


def test_heater_fault_is_not_the_updates_fault():
    h = run({"klippy_connected": True, "klippy_state": "shutdown"}, "Heater extruder not heating at expected rate")
    assert h.ok


def test_moonraker_down_is_unhealthy():
    clock = Clock()

    def fetch(url):
        raise OSError("refused")

    assert not health.check("http://x", timeout=10, interval=3, clock=clock, sleep=clock.sleep, fetch=fetch).ok
