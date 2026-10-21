import signal
import subprocess
import sys
import time

from klarity.__main__ import EX_CONFIG, main


def test_check_map_ok(capsys):
    assert main(["--check-map"]) == 0
    assert "map OK" in capsys.readouterr().out


def test_check_map_reports_invalid_map(tmp_path, caplog):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 9\npages: {}\n")
    assert main(["--check-map", "--map", str(bad)]) == EX_CONFIG


def test_bad_config_exits_ex_config(tmp_path, capsys):
    cfg = tmp_path / "k.cfg"
    cfg.write_text("[screen]\nbaud: nope\n")
    assert main(["-c", str(cfg)]) == EX_CONFIG
    assert "config error" in capsys.readouterr().err


def test_missing_config_file_is_an_error(tmp_path):
    assert main(["-c", str(tmp_path / "nope.cfg")]) == EX_CONFIG


def test_unset_serial_port_exits_ex_config(tmp_path):
    cfg = tmp_path / "k.cfg"
    cfg.write_text("[klarity]\nlog_level: ERROR\n")
    assert main(["-c", str(cfg)]) == EX_CONFIG


def test_simulate_runs_and_stops_cleanly_on_sigterm(tmp_path):
    cfg = tmp_path / "k.cfg"
    cfg.write_text(
        "[moonraker]\nurl: ws://127.0.0.1:9/websocket\nbackoff_min: 0.1\n"
        f"[learn]\nlog_path: {tmp_path / 'learn.jsonl'}\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "klarity", "-c", str(cfg), "--simulate", "--learn"],
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.0)
    assert proc.poll() is None  # keeps running although Moonraker is unreachable
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert "still TODO" in err and "learn summary" in err
