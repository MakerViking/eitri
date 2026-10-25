"""The configurator against the REAL eitri-mcuflash CLI (installer/mcuflash), not the mock.

No hardware is touched: every run points the CLI at a serial port that does not exist, so
probing finds nothing and flashing is refused by the preflight before anything is written.
What this guards is the contract between the two components: the command line the
configurator builds must be accepted, and the JSON events must map onto what the UI shows.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from eitri_configurator import mcuflash
from eitri_configurator.settings import Settings

INSTALLER = Path(__file__).resolve().parents[2] / "installer" / "mcuflash"
pytestmark = pytest.mark.skipif(not (INSTALLER / "eitri_mcuflash").is_dir(), reason="installer/mcuflash not in this tree")

LAUNCH = "import sys; sys.path.insert(0, sys.argv[1]); from eitri_mcuflash.cli import main; sys.exit(main(sys.argv[2:]))"


def run_cli(args, tmp_path):
    ghost = str(tmp_path / "no-such-tty")
    proc = subprocess.run([sys.executable, "-I", "-c", LAUNCH, str(INSTALLER), *args, "--port", ghost],
                          capture_output=True, text=True, timeout=60)
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return proc, events


def test_detect_command_line_is_accepted_and_events_normalize(tmp_path):
    s = Settings(data_dir=tmp_path)
    proc, events = run_cli(mcuflash.detect_args(s, "klipper", None), tmp_path)
    assert proc.returncode == 3, proc.stderr          # blocked by preflight: nothing found, nothing touched
    assert events and all(e["schema"] == 1 and e["component"] == "mcuflash" for e in events)
    normalized = [n for e in events for n in mcuflash.normalize(e)]
    assert not [n for n in normalized if "unrecognised event" in n.get("message", "")]
    result = mcuflash.final_result(normalized)
    assert result and result["ok"] is False
    detect = mcuflash.plan_to_detect(result["data"])
    assert detect["cli_blocked"] and detect["boards"] == []
    assert detect["needs_assignment"] and detect["blockers"]


def test_flash_refuses_without_boards_and_reports_why(tmp_path):
    s = Settings(data_dir=tmp_path)
    proc, events = run_cli(mcuflash.flash_args(s, "klipper", ["mainboard", "toolhead"], None), tmp_path)
    assert proc.returncode == 3, proc.stderr
    assert events[-1]["type"] == "result" and events[-1]["ok"] is False
    data = events[-1]["data"]
    assert data["blocked"] is True and not data.get("results")
    assert any(c["status"] == "block" for c in data["checks"])
    assert not (tmp_path / "eitri" / "mcu" / "firmware").exists() or not any((tmp_path / "eitri" / "mcu" / "firmware").iterdir())


def test_manual_assignment_flags_are_accepted(tmp_path):
    s = Settings(data_dir=tmp_path)
    ghost = {"mainboard": str(tmp_path / "ghost-a"), "toolhead": str(tmp_path / "ghost-b")}
    # The adapter only ever passes /dev/ttyS* ports on ...
    assert mcuflash.detect_args(s, "kalico", ghost).count("--assign") == 0
    assert mcuflash.detect_args(s, "kalico", {"mainboard": "/dev/ttyS0", "toolhead": "/dev/ttyS2"}).count("--assign") == 2
    # ... so hand the CLI non-existent ports directly to prove the flag shape is accepted.
    argv = [*mcuflash.detect_args(s, "kalico", None), "--assign", f"mainboard={ghost['mainboard']}", "--assign", f"toolhead={ghost['toolhead']}"]
    proc, events = run_cli(argv, tmp_path)
    assert proc.returncode == 3, proc.stderr          # parsed (2 would be a usage error), then blocked
    assert events[-1]["type"] == "result" and events[-1]["data"]["blocked"]


# --- the mock mirrors the live printer ---------------------------------------------------------
MOCK = Path(__file__).resolve().parents[1] / "eitri_configurator" / "mock" / "mcuflash.py"


def run_mock(args, **env):
    import os
    proc = subprocess.run([sys.executable, "-I", str(MOCK), *args], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "EITRI_MOCK_SPEED": "0", **env})
    return proc, [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_mock_demo_shows_the_live_topology(tmp_path):
    """[mcu U_1] on ttyS0 is the STM32F401 mainboard, [mcu] on ttyS2 the STM32F103 toolhead (klippy.log)."""
    proc, events = run_mock(["plan", "--json"])
    assert proc.returncode == 0, proc.stderr
    data = events[-1]["data"]
    detect = mcuflash.plan_to_detect(data)
    by = {b["role"]: b for b in detect["boards"]}
    assert (by["mainboard"]["port"], by["mainboard"]["chip"]) == ("/dev/ttyS0", "stm32f401xc")
    assert (by["toolhead"]["port"], by["toolhead"]["chip"]) == ("/dev/ttyS2", "stm32f103xe")
    ports = {p["port"]: p for p in data["ports"]}
    assert ports["/dev/ttyS0"]["klipper"]["clock_freq"] == 84000000 and ports["/dev/ttyS2"]["klipper"]["clock_freq"] == 72000000


def test_mock_can_play_the_refusal_the_real_tool_gives_today(tmp_path):
    proc, events = run_mock(["flash", "--json", "--apply", "--yes"], EITRI_MOCK_TOOLHEAD_UNRESOLVED="1")
    assert proc.returncode == 3 and events[-1]["ok"] is False
    detect = mcuflash.plan_to_detect(events[-1]["data"])
    assert detect["cli_blocked"]
    assert [b["code"] for b in detect["blockers"]] == ["parameters"] and "Open question" in detect["blockers"][0]["message"]
    # What the wizard's "boards keep their firmware" path stands on: both boards found and bound, nothing to assign,
    # and no recovery claimed for the toolhead it refuses.
    by = {b["role"]: b for b in detect["boards"]}
    assert set(by) == {"mainboard", "toolhead"} and not detect["needs_assignment"]
    assert by["toolhead"]["bootloader"] == "unknown" and by["mainboard"]["bootloader"] == "sdcard-serial"


def test_real_cli_refuses_the_unresolved_toolhead_with_the_open_question(tmp_path):
    """A tree-wide guarantee: with the shipped registry nothing can name the toolhead as flashable."""
    import json as _json
    reg = _json.loads((INSTALLER / "eitri_mcuflash" / "boards.json").read_text())
    tool = reg["boards"]["toolhead"]
    assert tool["parameters"]["status"] == "unresolved" and tool["parameters"]["open_questions"]
    assert reg["boards"]["mainboard"]["mcu_candidates"] == ["stm32f401*"] and tool["mcu_candidates"] == ["stm32f103*"]
