# SPDX-License-Identifier: Apache-2.0
"""Crash reports: a crash is found at boot, a report is prepared, and it leaves only as the owner allows."""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from conftest import OVERLAY

REPORT = OVERLAY / "usr/libexec/eitri/eitri-report"
SENDER = OVERLAY / "usr/libexec/eitri/eitri-report-sender"

SESSION = ("Start printer at Sat Oct 24 05:25:45 2026 (1781643714.4 15.6)\n"
           "Configured MCU 'mcu' (1024 moves)\n"
           "Stats 57094.2: gcodein=0 sysload=0.20 cputime=4052.8 memavail=714552 print_time=57099.9 buffer_time=1.150 "
           "host: temp=51.7 heater_bed: target=55 temp=55.0 pwm=0.303 extruder: target={t} temp=219.8 pwm=0.640 "
           "printing to 192.168.0.145\n")


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "root"
    pd = root / "data/printer_data"
    for d in ("logs", "comms", "config", "gcodes"):
        (pd / d).mkdir(parents=True)
    (root / "etc").mkdir()
    (root / "etc/os-release").write_text('VERSION_ID="2026.10.24"\n')
    (root / "etc/hostname").write_text("printer.home.arpa\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    pstore = tmp_path / "pstore.txt"
    (bin_ / "journalctl").write_text(f'#!/bin/sh\ncase "$*" in *systemd-pstore*) cat "{pstore}" 2>/dev/null; exit 0;; esac\n'
                                     'echo "journal line from 192.168.0.145"\n')
    for name in ("systemctl", "uname"):
        (bin_ / name).write_text("#!/bin/sh\necho stub\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("EITRI_")},
           "EITRI_REPORT_ROOT": str(root), "EITRI_REPORT_PATH": f"{bin_}:/usr/bin:/bin"}
    return {"root": root, "pd": pd, "env": env, "pstore": pstore}


def boot(box, previous_klippy=None):
    if previous_klippy is not None:
        (box["pd"] / "logs/klippy.log").write_text(previous_klippy)
    return subprocess.run([sys.executable, "-I", REPORT, "--boot-check"], env=box["env"], capture_output=True, text=True)


def shutdown(box):
    subprocess.run([sys.executable, "-I", REPORT, "--mark-clean"], env=box["env"], check=True)


def reports(box):
    return sorted(p.name for p in (box["pd"] / "logs").iterdir() if p.name.startswith("eitri-crash-report-"))


def test_first_boot_clean_shutdown_and_an_idle_power_cut_are_not_crashes(box):
    assert "first boot" in boot(box, SESSION.format(t=220)).stderr
    shutdown(box)
    assert "clean shutdown" in boot(box, SESSION.format(t=220)).stderr
    # No shutdown, but nothing was heating: someone switched the printer off at the mains.
    assert "idle" in boot(box, SESSION.format(t=0).replace("heater_bed: target=55", "heater_bed: target=0")).stderr
    assert reports(box) == [] and not (box["pd"] / "comms/eitri-crash.pending").exists()


def test_a_stop_while_heating_is_a_crash_with_a_scrubbed_report(box):
    boot(box, SESSION.format(t=220))
    result = boot(box, SESSION.format(t=220))                 # no clean shutdown in between, heaters on
    assert "previous boot crashed" in result.stderr, result.stderr
    [name] = reports(box)
    assert (box["pd"] / "comms/eitri-crash.pending").read_text().split() == [name, "crash"]
    body = json.loads((box["pd"] / "logs" / name).read_text())
    assert body["product"] == "eitri" and body["app_version"] == "2026.10.24" and body["os"] == "linux"
    assert body["title"].startswith("Crash: the printer stopped while in use")
    assert "heater_bed=55, extruder=220" in body["description"] and body["sidecar_connected"] is True
    assert len(body["fingerprint"]) == 16 and set(body["fingerprint"]) <= set("0123456789abcdef")
    assert len(body["breadcrumbs"]) <= 20 and all(len(b) <= 300 for b in body["breadcrumbs"])
    assert len(body["description"]) <= 4000 and len(body["log_tail"]) <= 120000
    text = json.dumps(body)
    assert "192.168.0.145" not in text and "printer.home.arpa" not in text and "<lan-ip-1>" in text


def test_a_kernel_crash_record_is_a_crash_even_after_a_clean_marker(box):
    boot(box, SESSION.format(t=0))
    shutdown(box)
    box["pstore"].write_text("Kernel panic - not syncing: softlockup: hung tasks\n")
    assert "kernel left a crash record" in boot(box).stderr
    [name] = reports(box)
    assert "Kernel panic" in json.loads((box["pd"] / "logs" / name).read_text())["log_tail"]


def test_a_failing_journalctl_is_not_a_crash_record(box, tmp_path):
    bad = tmp_path / "bin/journalctl"
    bad.write_text("#!/bin/sh\necho 'Failed to open journal' >&2\nexit 1\n")
    boot(box, SESSION.format(t=0))
    shutdown(box)
    assert "clean shutdown" in boot(box).stderr and reports(box) == []


# --- the sender ---------------------------------------------------------------------------------------------

class Fakes(http.server.BaseHTTPRequestHandler):
    """Moonraker (printer info, proc stats, gcode) and TinkerAtlas (bug-report) in one server."""
    posts: list = []
    scripts: list = []
    status = 200
    clients = 1

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/printer/info":
            self._json(200, {"result": {"state": "ready"}})
        elif self.path == "/machine/proc_stats":
            self._json(200, {"result": {"websocket_connections": Fakes.clients}})
        else:
            self._json(404, {})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/printer/gcode/script":
            Fakes.scripts.append(json.loads(body)["script"])
            self._json(200, {"result": "ok"})
        elif self.path == "/api/desktop/bug-report":
            Fakes.posts.append((body, dict(self.headers)))
            self._json(Fakes.status, {"success": True, "bug_id": "abcdef12-3456"} if Fakes.status == 200 else {})
        else:
            self._json(404, {})

    def log_message(self, *a):
        pass


@pytest.fixture
def fakes():
    Fakes.posts, Fakes.scripts, Fakes.status, Fakes.clients = [], [], 200, 1
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fakes)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def crash(box):
    boot(box, SESSION.format(t=220))
    boot(box, SESSION.format(t=220))
    [name] = reports(box)
    return name


def sender(box, url, mode=None, wait=None, timeout=20):
    if mode is not None:
        (box["pd"] / "config/eitri-reports.txt").write_text(f"# comment kept\n{mode}\n")
    env = {**box["env"], "EITRI_REPORT_DATA": str(box["root"] / "data"), "EITRI_MOONRAKER": url,
           "EITRI_REPORT_ENDPOINT": url + "/api/desktop/bug-report", "EITRI_REPORT_POLL": "0.1",
           "EITRI_REPORT_RETRY": "0.2"}
    proc = subprocess.Popen([sys.executable, "-I", SENDER], env=env, stderr=subprocess.PIPE, text=True)
    if wait:
        wait(proc)
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return proc.returncode, err


def test_off_and_no_setting_send_nothing(box, fakes):
    name = crash(box)
    rc, err = sender(box, fakes)                               # no eitri-reports.txt at all
    assert rc == 0 and "crash reports are off" in err
    assert Fakes.posts == [] and (box["pd"] / "logs" / name).exists()
    assert not (box["pd"] / "comms/eitri-crash.pending").exists()
    for mode in ("off", "sometimes"):                          # anything unknown counts as off
        boot(box, SESSION.format(t=220))
        sender(box, fakes, mode)
    assert Fakes.posts == []


def test_always_sends_exactly_the_file_in_logs(box, fakes):
    name = crash(box)
    on_disk = (box["pd"] / "logs" / name).read_bytes()
    rc, err = sender(box, fakes, "always")
    assert rc == 0 and len(Fakes.posts) == 1, err
    body, headers = Fakes.posts[0]
    assert body == on_disk                                     # what the owner can read is what is sent
    assert headers["Content-Type"] == "application/json" and headers["User-Agent"] == "Eitri/2026.10.24"
    assert reports(box) == [name.replace(".json", ".sent.json")]
    assert any("Report sent, thank you (reference abcdef12)" in s for s in Fakes.scripts)


def test_ask_waits_for_the_owner_and_discard_sends_nothing(box, fakes):
    name = crash(box)
    decision = box["pd"] / "comms/eitri-crash.decision"

    def answer(proc):
        deadline = time.time() + 10
        while not any("action:prompt_show" in s for s in Fakes.scripts) and time.time() < deadline:
            time.sleep(0.05)
        decision.write_text("discard\n")

    rc, err = sender(box, fakes, "ask", wait=answer)
    assert rc == 0 and Fakes.posts == []
    dialog = next(s for s in Fakes.scripts if "action:prompt_show" in s)
    assert f"Logs, {name}" in dialog and "EITRI_CRASH_REPORT ACTION=send" in dialog and "ACTION=always" in dialog
    assert ";" not in dialog.replace("\n", "") and "#" not in dialog       # G-code would cut the line there
    assert (box["pd"] / "logs" / name).exists() and any("Not sent" in s for s in Fakes.scripts)


def test_ask_then_always_sends_and_remembers(box, fakes):
    crash(box)

    def answer(proc):
        deadline = time.time() + 10
        while not any("action:prompt_show" in s for s in Fakes.scripts) and time.time() < deadline:
            time.sleep(0.05)
        (box["pd"] / "comms/eitri-crash.decision").write_text("always\n")

    rc, err = sender(box, fakes, "ask", wait=answer)
    assert rc == 0 and len(Fakes.posts) == 1, err
    setting = (box["pd"] / "config/eitri-reports.txt").read_text()
    assert setting.splitlines() == ["# comment kept", "always"]


def test_the_dialog_comes_back_when_a_browser_connects(box, fakes):
    crash(box)

    def answer(proc):
        def shown():
            return sum("action:prompt_show" in s for s in Fakes.scripts)
        deadline = time.time() + 10
        while shown() < 1 and time.time() < deadline:
            time.sleep(0.05)
        Fakes.clients = 2                                      # someone opens Fluidd later
        while shown() < 2 and time.time() < deadline:
            time.sleep(0.05)
        (box["pd"] / "comms/eitri-crash.decision").write_text("send\n")

    sender(box, fakes, "ask", wait=answer)
    assert sum("action:prompt_show" in s for s in Fakes.scripts) == 2 and len(Fakes.posts) == 1


def test_a_refused_report_stays_in_logs_and_is_not_retried(box, fakes):
    name = crash(box)
    Fakes.status = 400
    rc, err = sender(box, fakes, "always")
    assert rc == 0 and len(Fakes.posts) == 1 and "refused the report (HTTP 400)" in err
    assert reports(box) == [name] and not (box["pd"] / "comms/eitri-crash.pending").exists()


def test_a_report_the_owner_asked_for_is_asked_about_even_when_off(box, fakes):
    boot(box, SESSION.format(t=0))
    (box["pd"] / "comms/eitri-report-send.request").touch()
    subprocess.run([sys.executable, "-I", REPORT], env=box["env"], check=True, capture_output=True)
    [name] = reports(box)
    assert (box["pd"] / "comms/eitri-crash.pending").read_text().split() == [name, "requested"]

    def answer(proc):
        deadline = time.time() + 10
        while not any("action:prompt_show" in s for s in Fakes.scripts) and time.time() < deadline:
            time.sleep(0.05)
        (box["pd"] / "comms/eitri-crash.decision").write_text("send\n")

    rc, err = sender(box, fakes, "off", wait=answer)
    assert rc == 0 and len(Fakes.posts) == 1, err
    dialog = next(s for s in Fakes.scripts if "action:prompt_show" in s)
    assert "You asked for a report" in dialog and "ACTION=always" not in dialog


def test_set_changes_only_the_mode_line(box):
    (box["pd"] / "config/eitri-reports.txt").write_text("# my notes\n\nask\n")
    env = {**box["env"], "EITRI_REPORT_DATA": str(box["root"] / "data")}
    subprocess.run([sys.executable, "-I", SENDER, "--set", "off"], env=env, check=True)
    assert (box["pd"] / "config/eitri-reports.txt").read_text() == "# my notes\noff\n"     # comments kept, mode replaced
    assert subprocess.run([sys.executable, "-I", SENDER, "--set", "sometimes"], env=env).returncode == 1


def test_only_the_sender_has_network_and_it_is_not_root():
    from test_tree import parse, UNIT_DIR
    for unit in ("eitri-crash-check.service", "eitri-report.service"):
        assert parse(UNIT_DIR / unit)["Service"]["PrivateNetwork"] == "yes", unit
    sender_unit = parse(UNIT_DIR / "eitri-report-sender.service")["Service"]
    assert sender_unit["User"] == "eitri" and sender_unit["NoNewPrivileges"] == "yes"
    check = parse(UNIT_DIR / "eitri-crash-check.service")
    assert "klipper.service" in check["Unit"]["Before"]       # klippy.log still ends with the previous boot
    assert check["Service"]["ExecStop"] == "/usr/libexec/eitri/eitri-report --mark-clean"
