# SPDX-License-Identifier: Apache-2.0
"""Consistency of the OS tree: units, presets, packages, pins, scripts."""
from __future__ import annotations

import configparser
import re
import shutil
import subprocess
import sys

import pytest
from conftest import BOARD, OS_DIR, OVERLAY, REPO_ROOT, need, run

UNIT_DIR = OVERLAY / "usr/lib/systemd/system"
LIBEXEC_DIR = OVERLAY / "usr/libexec/eitri"
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


# --- commit policy, privileges and resilience ----------------------------------

def dropin(unit, name="10-eitri-os.conf"):
    return parse(UNIT_DIR / f"{unit}.d" / name)


def test_only_eitri_verify_commits_a_slot_when_eitri_update_is_installed():
    # eitri-mark-good must not run next to eitri-update: it would commit within seconds of the boot
    # and refill the boot attempts, so a bad system never falls back.
    unit = parse(UNIT_DIR / "eitri-mark-good.service")
    assert unit["Unit"]["ConditionPathExists"] == "!/usr/bin/eitri-update"
    assert {"eitri-prepare.service", "eitri-verify.service"} <= set(unit["Unit"]["After"].split())
    # The OS health checks gate eitri-verify instead.
    verify = dropin("eitri-verify.service")
    assert verify["Service"]["ExecStartPre"] == "/usr/libexec/eitri/mark-good --check"
    assert "rauc.service" in verify["Unit"]["After"].split()
    assert "--check" in (LIBEXEC_DIR / "mark-good").read_text()


def test_prepare_waits_for_the_data_layout():
    unit = dropin("eitri-prepare.service")["Unit"]
    assert "eitri-data-init.service" in unit["After"].split() and "eitri-data-init.service" in unit["Wants"].split()


def test_update_units_the_os_adds_to_exist_in_the_installer_component():
    for unit in ("eitri-prepare.service", "eitri-verify.service"):
        assert (REPO_ROOT / "installer/eitri-update/systemd" / unit).is_file()


def test_sudo_is_scoped_not_blanket():
    for path in (OVERLAY / "etc/sudoers.d").iterdir():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "NOPASSWD" in line:
                commands = [c.strip() for c in line.split("NOPASSWD:", 1)[1].split(",")]
                assert "ALL" not in commands, f"{path.name}: passwordless sudo for every command: {line}"
                assert all(c.startswith("/") for c in commands), line
    text = (OVERLAY / "etc/sudoers.d/eitri").read_text()
    assert "/usr/bin/eitri-update" in text
    if shutil.which("visudo"):
        assert run(["visudo", "-cf", OVERLAY / "etc/sudoers.d/eitri"]).returncode == 0


def test_sshd_takes_keys_only():
    config = dict(
        line.split(None, 1) for line in (OVERLAY / "etc/ssh/sshd_config").read_text().splitlines()
        if line.strip() and not line.startswith("#"))
    assert config["PasswordAuthentication"] == "no"
    assert config["PermitRootLogin"] == "no" and config["AllowTcpForwarding"] == "no"
    assert int(config["MaxAuthTries"]) <= 3


def test_nginx_limits_uploads_and_is_lan_only():
    conf = (OVERLAY / "etc/nginx/nginx.conf").read_text()
    # Only one body-size rule outside the upload route may exist, and it is small.
    sizes = re.findall(r"client_max_body_size\s+(\S+);", conf)
    assert sizes[0] == "1m" and "0" not in sizes
    upload = re.search(r"location ~ \^/\(server/files/upload[^{]*\{(.*?)\}", conf, re.S)
    assert upload and "client_max_body_size 1g;" in upload.group(1)
    assert "include /etc/nginx/eitri-lan-only.conf;" in conf
    acl = (OVERLAY / "etc/nginx/eitri-lan-only.conf").read_text().splitlines()
    rules = [ln.strip() for ln in acl if ln.strip() and not ln.startswith("#")]
    assert rules[-1] == "deny all;" and "allow 192.168.0.0/16;" in rules
    assert "allow 0.0.0.0/0;" not in rules and "allow all;" not in rules


def test_root_is_not_checked_at_every_boot():
    for line in (OVERLAY / "etc/fstab").read_text().splitlines():
        fields = line.split()
        if fields and not line.startswith("#") and fields[1] == "/":
            assert fields[-1] == "0", "a pass of 1 makes systemd-fsck-root fail against the mounted read-only root"


def test_python_pip_is_not_in_the_image():
    # data-init creates the configurator venv with --without-pip; pip and setuptools are ~25 MB per slot.
    defconfig = (OS_DIR / "configs/eitri_plus4_defconfig").read_text().splitlines()
    assert "BR2_PACKAGE_PYTHON_PIP=y" not in defconfig
    assert "--without-pip" in (LIBEXEC_DIR / "data-init").read_text()


def test_klipper_is_never_limited_and_the_rest_only_softly():
    klipper = parse(UNIT_DIR / "klipper.service")
    for key in ("MemoryMax", "MemoryHigh", "MemoryLimit", "CPUQuota"):
        assert key not in klipper["Service"], key
    assert klipper["Unit"]["StartLimitIntervalSec"] == "0"
    limited = [UNIT_DIR / "moonraker.service"] + [
        UNIT_DIR / f"{n}.service.d/10-eitri-limits.conf" for n in ("eitri-camera", "go2rtc", "eitri-configurator")]
    for path in limited:
        service = parse(path)["Service"]
        assert "MemoryMax" not in service and service["MemoryHigh"].endswith("M"), path


def test_klipper_and_moonraker_cannot_gain_privileges():
    for name in ("klipper", "moonraker"):
        assert parse(UNIT_DIR / f"{name}.service")["Service"]["NoNewPrivileges"] == "true"
