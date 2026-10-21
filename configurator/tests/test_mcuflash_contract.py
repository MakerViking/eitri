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
