# SPDX-License-Identifier: Apache-2.0
"""Consistency of the OS tree: units, presets, packages, pins, scripts."""
from __future__ import annotations

import configparser
import re
import subprocess
import sys

import pytest
from conftest import BOARD, OS_DIR, OVERLAY, REPO_ROOT, need, run

UNIT_DIR = OVERLAY / "usr/lib/systemd/system"
# klarity.service and eitri-configurator.service belong to their components; post-build.sh installs them.
COMPONENT_UNITS = [REPO_ROOT / "klarity/systemd/klarity.service", REPO_ROOT / "configurator/systemd/eitri-configurator.service"]
UNITS = sorted(UNIT_DIR.glob("*.service")) + COMPONENT_UNITS


def parse(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    text = path.read_text().replace("\\\n", " ")
    parser.read_string(text)
    return parser


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_unit_is_well_formed(unit):
    parser = parse(unit)
    assert {"Unit", "Service", "Install"} <= set(parser.sections()), unit.name
    assert parser["Unit"]["Description"]


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_unit_executables_exist_in_the_overlay_or_are_documented_app_paths(unit):
    parser = parse(unit)
    for key in ("ExecStart", "ExecStop", "ExecStartPre"):
        if key not in parser["Service"]:
            continue
        command = parser["Service"][key].lstrip("-@+!").split()[0]
        if command.startswith("/usr/libexec/eitri/"):
            script = OVERLAY / command.lstrip("/")
            assert script.is_file() and script.stat().st_mode & 0o111, f"{unit.name}: {command}"
        else:
            assert command.startswith(("/usr/bin/", "/data/")), f"{unit.name}: unexpected {command}"


def test_every_preset_names_a_unit_we_or_a_package_provide():
    preset = (OVERLAY / "usr/lib/systemd/system-preset/50-eitri.preset").read_text()
    ours = {u.name for u in UNITS}
    from_packages = {"eitri-camera.service", "go2rtc.service", "nginx.service", "sshd.service",
                     "NetworkManager.service", "avahi-daemon.service", "chrony.service", "rauc.service",
                     "dnsmasq.service", "systemd-networkd.service", "systemd-networkd-wait-online.service",
                     "systemd-network-generator.service", "systemd-resolved.service",
                     "systemd-timesyncd.service", "avahi-dnsconfd.service"}
    for line in preset.splitlines():
        if line.startswith(("enable ", "disable ")):
            assert line.split()[1] in ours | from_packages, line


def test_all_units_we_ship_are_preset_enabled_unless_optional():
    preset = (OVERLAY / "usr/lib/systemd/system-preset/50-eitri.preset").read_text()
    for unit in UNITS:
        assert f"enable {unit.name}" in preset, f"{unit.name} is not enabled by the preset"


def test_services_use_the_contract_paths():
    klipper = (UNIT_DIR / "klipper.service").read_text()
    for path in ("/data/firmware/klipper/klippy/klippy.py", "/data/venv/klippy/bin/python",
                 "/data/printer_data/config/printer.cfg", "/data/printer_data/comms/klippy.sock"):
        assert path in klipper
    moonraker = (UNIT_DIR / "moonraker.service").read_text()
    for path in ("/data/firmware/moonraker/moonraker/moonraker.py", "/data/venv/moonraker/bin/python",
                 "-d /data/printer_data"):
        assert path in moonraker
    assert "/data/eitri/klarity" in COMPONENT_UNITS[0].read_text()
    assert "--port 8099" in COMPONENT_UNITS[1].read_text()


def test_no_unit_weakens_heater_safety():
    # Klipper must come back after a crash and must not be starved by the OOM killer.
    klipper = parse(UNIT_DIR / "klipper.service")["Service"]
    assert klipper["Restart"] == "always"
    assert int(klipper["OOMScoreAdjust"]) < 0


def test_overlay_scripts_are_executable_and_pass_sh_syntax():
    scripts = list((OVERLAY / "usr/libexec/eitri").iterdir()) + [
        OVERLAY / "usr/lib/systemd/system-generators/eitri-generator"]
    for script in scripts:
        assert script.stat().st_mode & 0o111, f"{script} is not executable"
        assert run(["sh", "-n", script]).returncode == 0, script
        assert script.read_text().startswith("#!/bin/sh"), script


def test_board_scripts_pass_bash_syntax():
    for script in [*BOARD.glob("*.sh"), *(OS_DIR / "scripts").glob("*.sh")]:
        assert run(["bash", "-n", script]).returncode == 0, script
        assert script.stat().st_mode & 0o111, f"{script} is not executable"


def test_python_tools_compile():
    for script in (OS_DIR / "scripts").glob("*.py"):
        compile(script.read_text(), str(script), "exec")


def test_pins_match_release_lock():
    result = run([sys.executable, "-I", OS_DIR / "scripts/check-pins.py"])
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_package_has_config_makefile_and_hash():
    for pkg in sorted((OS_DIR / "package").iterdir()):
        assert (pkg / "Config.in").is_file() and (pkg / f"{pkg.name}.mk").is_file(), pkg.name
        mk = (pkg / f"{pkg.name}.mk").read_text()
        if "SITE_METHOD = local" not in mk:
            assert (pkg / f"{pkg.name}.hash").is_file(), f"{pkg.name} downloads a source and needs a .hash"
    config_in = (OS_DIR / "Config.in").read_text()
    for pkg in (OS_DIR / "package").iterdir():
        assert f"package/{pkg.name}/Config.in" in config_in, pkg.name


def test_hash_files_are_well_formed():
    for hash_file in (OS_DIR).rglob("*.hash"):
        for line in hash_file.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            assert re.fullmatch(r"sha256  [0-9a-f]{64}  \S+", line), f"{hash_file}: {line}"


def test_users_table_defines_the_contract_user():
    line = [ln for ln in (BOARD / "users.table").read_text().splitlines() if ln.startswith("eitri ")][0].split()
    assert line[:3] == ["eitri", "1000", "eitri"] and line[3] == "1000"
    assert set(line[7].split(",")) == {"dialout", "video", "plugdev", "tty"}


def test_no_closed_vendor_artifacts_in_the_tree():
    banned = {".tft", ".hmi", ".so", ".o", ".ko"}
    bad = [p for p in OS_DIR.rglob("*") if p.is_file() and p.suffix.lower() in banned]
    assert not bad, bad
    for p in OS_DIR.rglob("*.bin"):
        pytest.fail(f"binary blob committed: {p}")


def test_polkit_rule_is_valid_javascript_shape():
    rule = (OVERLAY / "usr/share/polkit-1/rules.d/50-eitri.rules").read_text()
    assert rule.count("{") == rule.count("}") and rule.count("(") == rule.count(")")
    need("node")
    # polkit evaluates ES5; parse the rule with a stub polkit to catch syntax errors.
    stub = "var polkit={addRule:function(f){globalThis.rule=f;},Result:{YES:'yes',NOT_HANDLED:'no'}};"
    probe = (stub + rule + "var a=function(id,unit){return {id:id,lookup:function(){return unit;}};};"
             "var s={user:'eitri'};"
             "console.log(rule(a('org.freedesktop.systemd1.manage-units','klipper.service'),s),"
             "rule(a('org.freedesktop.systemd1.manage-units','sshd.service'),s),"
             "rule(a('org.freedesktop.login1.reboot',undefined),s),"
             "rule(a('org.freedesktop.login1.reboot',undefined),{user:'nobody'}));")
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["yes", "no", "yes", "no"]
