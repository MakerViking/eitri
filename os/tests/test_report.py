# SPDX-License-Identifier: Apache-2.0
"""eitri-report: the diagnostics bundle holds what a bug needs and none of what identifies the printer."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time

import pytest
from conftest import OVERLAY

HELPER = OVERLAY / "usr/libexec/eitri/eitri-report"

HOST = "printer.home.arpa"
SSID = "Kreaweb-Test"
PSK = "hunter2-wifi-key"
SERIAL = "c3d9b8674f4b94f6"
MACHINE_ID = "9ddc3bde5775414283c88e17ffa2c147"
CAM_PASS = "Rtsp-S3cret-Pass"
GCODE = "flexyring (1)_PLA_45m2s.gcode"
MCU_SERIAL = "3E0034000851313433383331"

LEAKY = (f"Connected to 192.168.0.145 from 192.168.0.84 via wlan0 ({SSID}), gateway 192.168.0.1, mac 6c:5a:b0:12:34:56\n"
         f"public 81.166.20.7, ipv6 fe80::6e5a:b0ff:fe12:3456%wlan0 and 2001:db8::42, loopback 127.0.0.1 ::1, mdns 224.0.0.251\n"
         f"hostname {HOST}, serial {SERIAL}, machine {MACHINE_ID}\n"
         f"Starting print {GCODE} at 12:34:56 (Klipper v0.13.0-813-ga08fc79, version 2026.10.23)\n"
         f"mcu: /dev/serial/by-id/usb-Klipper_stm32f401xc_{MCU_SERIAL}-if00\n"
         f"rtsp://eitri:{CAM_PASS}@127.0.0.1:8554/cam api_key=abc123def token: zzz999 owner me@example.org\n"
         "Authorization: Bearer eyJhbGciOi.secret.sig\n"
         # Real lines from the printer that an earlier pattern got wrong (2026-10-24):
         "avahi-daemon[257]: Joining mDNS multicast group on interface eth0.IPv4 with address 192.168.0.199.\n"
         "systemd[1]: modprobe@configfs.service: Deactivated successfully.\n")


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "root"
    pd = root / "data/printer_data"
    for d in ("logs", "config", "gcodes/sub", "comms"):
        (pd / d).mkdir(parents=True)
    (root / "data/eitri/camera").mkdir(parents=True)
    (root / "etc/NetworkManager/system-connections").mkdir(parents=True)
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "sys/class/thermal/thermal_zone0").mkdir(parents=True)
    (root / "etc/hostname").write_text(HOST + "\n")
    (root / "etc/machine-id").write_text(MACHINE_ID + "\n")
    (root / "etc/os-release").write_text('NAME="Eitri OS"\nVERSION_ID="2026.10.23-dev"\n')
    (root / "etc/NetworkManager/system-connections/home.nmconnection").write_text(f"[connection]\nid={SSID}\n[wifi]\nssid={SSID}\n[wifi-security]\npsk={PSK}\n")
    (root / "proc/device-tree/serial-number").write_text(SERIAL + "\x00")
    (root / "sys/class/thermal/thermal_zone0/temp").write_text("52000\n")
    (root / "data/eitri/camera/go2rtc-auth.env").write_text(f"GO2RTC_RTSP_PASSWORD={CAM_PASS}\n")
    (root / "data/eitri/flavor").write_text("klipper\n")
    (pd / "gcodes" / GCODE).write_text("G28\n")
    (pd / "gcodes/sub/benchy-secret-project.gcode").write_text("G28\n")
    (pd / "logs/klippy.log").write_text("Start printer\n" + LEAKY + "Stats 100.0: sysload=0.2\n")
    (pd / "logs/klippy.log.2026-10-23").write_text("old log\n" + LEAKY)
    (pd / "logs/moonraker.log").write_text("moonraker\n" + LEAKY + "file sub/benchy-secret-project.gcode\n")
    (pd / "config/printer.cfg").write_text("[mcu]\nserial: /dev/serial/by-id/usb-Klipper_stm32f401xc_" + MCU_SERIAL + "-if00\n")
    (pd / "config/moonraker.conf").write_text("[secrets]\napi_key: SHOULD-NOT-APPEAR\n")
    (pd / "config/eitri-hosts.txt").write_text("voron.lan\n")
    (pd / "config/printer-20261022_074310.cfg").write_text("backup\n")

    bin_ = tmp_path / "bin"
    bin_.mkdir()
    stub = f"#!/bin/sh\nprintf '%s\\n' \"$0 $*\"\ncat <<'LEAK'\n{LEAKY}LEAK\n"
    for name in ("journalctl", "systemctl", "systemd-analyze", "uname", "free", "df", "lsusb", "ip"):
        (bin_ / name).write_text(stub)
        (bin_ / name).chmod(0o755)
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("EITRI_")},
           "EITRI_REPORT_ROOT": str(root), "EITRI_REPORT_PATH": f"{bin_}:/usr/bin:/bin"}
    return {"root": root, "pd": pd, "env": env, "tmp": tmp_path}


def report(box, *args):
    return subprocess.run([sys.executable, "-I", HELPER, *args], env=box["env"], capture_output=True)


def unpack(data: bytes) -> dict[str, str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name.split("/", 1)[1]: tar.extractfile(m).read().decode() for m in tar.getmembers() if m.isfile()}


def test_the_bundle_has_the_logs_and_none_of_the_identifiers(box):
    result = report(box, "--stdout")
    assert result.returncode == 0, result.stderr.decode()
    files = unpack(result.stdout)
    assert {"README.txt", "manifest.json", "system.txt", "journal-this-boot.txt", "journal-previous-boot.txt",
            "kernel-previous-boot.txt", "kernel-crash-records.txt", "logs/klippy.log", "logs/klippy.log.2026-10-23",
            "logs/moonraker.log", "config/printer.cfg"} <= set(files)
    everything = "\n".join(files.values())
    for secret in ("192.168.0.145", "192.168.0.84", "192.168.0.199", "81.166.20.7", "6c:5a:b0:12:34:56", "fe80::6e5a", "2001:db8::42",
                   HOST, SSID, PSK, SERIAL, MACHINE_ID, CAM_PASS, MCU_SERIAL, "abc123def", "zzz999", "me@example.org",
                   "eyJhbGciOi", "flexyring", "benchy-secret-project", "voron.lan", "SHOULD-NOT-APPEAR"):
        assert secret not in everything, secret
    # What helps debugging survives: versions, times, loopback, the address class and stable placeholders.
    klippy = files["logs/klippy.log"]
    assert "v0.13.0-813-ga08fc79" in klippy and "2026.10.23" in klippy and "12:34:56" in klippy
    assert "127.0.0.1" in klippy and "::1" in klippy and "224.0.0.251" in klippy
    assert "modprobe@configfs.service" in klippy                     # a unit instance is not an e-mail address
    assert "<lan-ip-" in klippy and "<ip-" in klippy and "<mac-" in klippy and "<gcode-" in klippy
    assert "usb-Klipper_stm32f401xc_<serial-" in files["config/printer.cfg"]
    # The same address keeps the same placeholder in every file.
    placeholder = klippy.split("Connected to ", 1)[1].split(" ", 1)[0]
    assert placeholder.startswith("<lan-ip-") and f"Connected to {placeholder} " in files["logs/moonraker.log"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["format"] == 1 and manifest["eitri_version"] == "2026.10.23-dev"
    assert manifest["removed"]["ipv4"] > 0 and manifest["removed"]["hostname"] > 0 and manifest["removed"]["gcode"] > 0
    assert "moonraker.conf" in manifest["not_included"]
    assert not any(n.startswith("config/printer-2026") or n.endswith(("moonraker.conf", "eitri-hosts.txt")) for n in files)


def test_a_planted_link_never_makes_it_read_another_file(box, tmp_path):
    outside = tmp_path / "shadow"
    outside.write_text("root:$6$secret-hash:19000::::::\n")
    log = box["pd"] / "logs/klippy.log"
    log.unlink()
    log.symlink_to(outside)
    (box["pd"] / "config/printer.cfg").unlink()
    (box["pd"] / "config/printer.cfg").symlink_to(outside)
    files = unpack(report(box, "--stdout").stdout)
    assert "secret-hash" not in "\n".join(files.values())
    assert "logs/klippy.log" not in files and "config/printer.cfg" not in files
    assert any("klippy.log" in n for n in json.loads(files["manifest.json"])["notes"])


def test_the_request_writes_into_logs_and_keeps_three(box):
    request = box["pd"] / "comms/eitri-report.request"
    for i in range(4):
        request.touch()
        result = report(box)
        assert result.returncode == 0, result.stderr.decode()
        assert not request.exists(), "the request is consumed"
        reports = sorted(p for p in (box["pd"] / "logs").iterdir() if p.name.startswith("eitri-report-"))
        newest = reports[-1]
        os.utime(newest, (time.time() - 3600, time.time() - 3600))        # past the one-a-minute limit
        # Distinct names: the next run must not land on the same second.
        newest.rename(newest.with_name(f"eitri-report-2026010{i + 1}T000000Z.tar.gz"))
    reports = sorted(p.name for p in (box["pd"] / "logs").iterdir() if p.name.startswith("eitri-report-"))
    assert len(reports) == 3
    assert oct((box["pd"] / "logs" / reports[-1]).stat().st_mode & 0o777) == "0o644"
    assert not [p for p in (box["pd"] / "logs").iterdir() if p.name.startswith(".")], "no temporary files left"


def test_one_report_a_minute(box):
    assert report(box).returncode == 0
    second = report(box)
    assert second.returncode == 0 and b"not making another yet" in second.stderr
    assert len([p for p in (box["pd"] / "logs").iterdir() if p.name.startswith("eitri-report-")]) == 1


def test_a_logs_folder_replaced_by_a_link_is_refused(box, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    logs = box["pd"] / "logs"
    for p in logs.iterdir():
        p.unlink()
    logs.rmdir()
    logs.symlink_to(target)
    result = report(box)
    assert result.returncode == 1 and b"cannot write into the Logs folder" in result.stderr
    assert list(target.iterdir()) == []


def test_old_report_removal_touches_only_report_files(box, tmp_path):
    logs = box["pd"] / "logs"
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    for i in range(3):
        (logs / f"eitri-report-2026010{i}T000000Z.tar.gz").write_bytes(b"x")
    (logs / "eitri-report-20250101T000000Z.tar.gz").symlink_to(victim)      # oldest name, but a link
    (logs / "eitri-report-notes.txt").write_text("not a report")
    for p in logs.glob("eitri-report-*"):
        os.utime(p, (time.time() - 3600, time.time() - 3600), follow_symlinks=False)
    assert report(box).returncode == 0
    assert victim.read_text() == "keep me"
    assert (logs / "eitri-report-notes.txt").exists()


def test_list_shows_what_would_be_collected_without_writing(box):
    result = report(box, "--list")
    assert result.returncode == 0
    out = result.stdout.decode()
    assert "journal-previous-boot.txt" in out and "removed:" in out and "not included:" in out
    assert not [p for p in (box["pd"] / "logs").iterdir() if p.name.startswith("eitri-report-")]


def test_the_test_hooks_do_not_exist_for_root():
    text = HELPER.read_text()
    assert "_TESTING = os.geteuid() != 0" in text
    for hook in ("EITRI_REPORT_ROOT", "EITRI_REPORT_PATH"):
        assert f'_TESTING and os.environ.get("{hook}")' in text, hook


def test_the_report_service_has_no_network_and_cannot_disturb_a_print():
    from test_tree import parse, UNIT_DIR
    unit = parse(UNIT_DIR / "eitri-report.service")
    svc = unit["Service"]
    assert svc["PrivateNetwork"] == "yes" and svc["ProtectSystem"] == "strict" and svc["NoNewPrivileges"] == "yes"
    assert set(svc["ReadWritePaths"].split()) == {"/data/printer_data/logs", "/data/printer_data/comms"}
    assert svc["IOSchedulingClass"] == "idle" and int(svc["Nice"]) > 0
    watched = re.findall(r"^PathExists=(\S+)$", (UNIT_DIR / "eitri-report.path").read_text(), re.M)
    assert watched == ["/data/printer_data/comms/eitri-report.request", "/data/printer_data/comms/eitri-report-send.request"]
