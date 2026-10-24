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
PATH_UNITS = sorted(UNIT_DIR.glob("*.path"))
#: Services that only their .path unit starts (on request, never at boot), so they are not enabled.
PATH_ONLY = {"eitri-report.service", "eitri-report-sender.service"}


#: Sandbox options that systemd implements with seccomp. For a unit whose user lacks CAP_SYS_ADMIN, systemd then sets
#: NoNewPrivs on the process even with NoNewPrivileges=false (measured with systemd 258 on the QEMU image), and sudo
#: refuses to run. Units that call sudo must not use any of them.
SECCOMP_OPTIONS = {
    "ProtectKernelTunables", "ProtectKernelModules", "RestrictNamespaces", "LockPersonality", "RestrictAddressFamilies",
    "MemoryDenyWriteExecute", "SystemCallFilter", "SystemCallArchitectures", "RestrictRealtime", "RestrictSUIDSGID",
    "ProtectClock", "ProtectHostname", "ProtectKernelLogs",
}

def parse(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    text = path.read_text().replace("\\\n", " ")
    parser.read_string(text)
    return parser


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_unit_is_well_formed(unit):
    parser = parse(unit)
    # A template (name@.service) is started per device by udev, never enabled, so it has no [Install].
    wanted = {"Unit", "Service"} if "@." in unit.name or unit.name in PATH_ONLY else {"Unit", "Service", "Install"}
    assert wanted <= set(parser.sections()), unit.name
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
    ours = {u.name for u in UNITS} | {u.name for u in PATH_UNITS}
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
        if "@." in unit.name:
            continue  # templates are started by udev (61-eitri-admin-usb.rules), not enabled
        if unit.name in PATH_ONLY:
            assert f"Unit={unit.name}" in (UNIT_DIR / unit.name.replace(".service", ".path")).read_text()
            assert f"enable {unit.name}" not in preset, f"{unit.name} must not run at every boot"
            continue
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


# Tools in the overlay that are Python, not sh: the root helpers behind the sudo rules.
PYTHON_TOOLS = {"eitri-access", "eitri-hosts", "eitri-report", "eitri-report-sender"}


def test_overlay_scripts_are_executable_and_pass_sh_syntax():
    scripts = list((OVERLAY / "usr/libexec/eitri").iterdir()) + [
        OVERLAY / "usr/lib/systemd/system-generators/eitri-generator"]
    for script in scripts:
        assert script.stat().st_mode & 0o111, f"{script} is not executable"
        if script.name in PYTHON_TOOLS:
            assert script.read_text().startswith("#!/usr/bin/python3 -I\n"), script
            compile(script.read_text(), str(script), "exec")
            continue
        assert run(["sh", "-n", script]).returncode == 0, script
        assert script.read_text().startswith("#!/bin/sh"), script


def test_eitri_configurator_cli_is_an_isolated_python_script():
    cli = OVERLAY / "usr/bin/eitri-configurator"
    assert cli.stat().st_mode & 0o111
    text = cli.read_text()
    assert text.startswith("#!/usr/bin/python3 -I\n")
    compile(text, str(cli), "exec")
    # It runs as root through sudo: it must load the image's copy, never the venv under /data.
    assert 'LIB = "/usr/lib/eitri/configurator"' in text and "/data/" not in text.replace("/data/eitri/configurator", "")


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
            if line.startswith("admin "):
                # The administrator is root by design; it is a key-only login, never a service account.
                assert path.name == "admin" and line == "admin ALL=(ALL) NOPASSWD: ALL", line
                continue
            assert line.startswith("eitri "), f"{path.name}: rule for an unexpected account: {line}"
            assert "NOPASSWD" in line, f"{path.name}: eitri may not have any password rule (it can set its own): {line}"
            commands = [c.strip() for c in line.split("NOPASSWD:", 1)[1].split(",")]
            assert "ALL" not in commands, f"{path.name}: passwordless sudo for every command: {line}"
            assert all(c.startswith("/") for c in commands), line
    text = (OVERLAY / "etc/sudoers.d/eitri").read_text()
    assert "/usr/bin/eitri-update" in text
    if shutil.which("visudo"):
        for name in ("eitri", "admin"):
            assert run(["visudo", "-cf", OVERLAY / "etc/sudoers.d" / name]).returncode == 0, name


def test_sshd_takes_keys_only():
    text = (OVERLAY / "etc/ssh/sshd_config").read_text()
    main, _, match = text.partition("\nMatch User admin\n")
    config = dict(line.split(None, 1) for line in main.splitlines() if line.strip() and not line.startswith("#"))
    # The administrator: keys from the root-owned file only, never a password, and nothing after its block.
    admin = dict(line.split(None, 1) for line in match.splitlines() if line.strip() and not line.startswith("#"))
    assert admin == {"AuthorizedKeysFile": "/data/eitri/state/access/admin_authorized_keys", "PasswordAuthentication": "no"}
    assert config["AllowUsers"] == "eitri admin" and "Match" not in config
    assert config["PasswordAuthentication"] == "no"
    # The owner's optional choices (decision 4) come in through two fixed paths, and the Include must be read
    # before PasswordAuthentication because sshd takes the first value it sees.
    text = (OVERLAY / "etc/ssh/sshd_config").read_text()
    assert text.index("Include /data/eitri/state/access/sshd.conf") < text.index("PasswordAuthentication no")
    assert config["AuthorizedKeysFile"] == ".ssh/authorized_keys /data/eitri/state/access/authorized_keys"
    assert config["PermitRootLogin"] == "no" and config["AllowTcpForwarding"] == "no"
    assert int(config["MaxAuthTries"]) <= 3


def test_nginx_limits_uploads_and_is_lan_only():
    conf = (OVERLAY / "etc/nginx/nginx.conf").read_text()
    # Only one body-size rule outside the upload route may exist, and it is small.
    sizes = re.findall(r"client_max_body_size\s+(\S+);", conf)
    assert sizes[0] == "1m" and "0" not in sizes
    upload = re.search(r"location ~ \^/\(server/files/upload[^{]*\{(.*?)\n        \}", conf, re.S)
    assert upload and "client_max_body_size 1g;" in upload.group(1)
    assert "include /etc/nginx/eitri-lan-only.conf;" in conf
    acl = (OVERLAY / "etc/nginx/eitri-lan-only.conf").read_text().splitlines()
    rules = [ln.strip() for ln in acl if ln.strip() and not ln.startswith("#")]
    assert rules[-1] == "deny all;" and "allow 192.168.0.0/16;" in rules
    assert "allow 0.0.0.0/0;" not in rules and "allow all;" not in rules
    assert not any(":" in r for r in rules if r.startswith("allow")), "no IPv6 allow rule: there is no IPv6 listener"


def test_nginx_has_no_ipv6_listener():
    # Decision 7: the LAN allow-list is IPv4-only, so IPv6 must not be served at all.
    conf = (OVERLAY / "etc/nginx/nginx.conf").read_text()
    listens = re.findall(r"^\s*listen\s+([^;]+);", conf, re.M)
    assert listens, "no listen directive"
    for listen in listens:
        assert "[" not in listen and "::" not in listen, listen
        assert listen.split()[0] in ("80", "0.0.0.0:80"), listen


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


# --- the data seed, the klipper layer on /data, root access, the configurator's retirement ---------

def defconfig_lines():
    return (OS_DIR / "configs/eitri_plus4_defconfig").read_text().splitlines()


def test_klipper_layer_is_not_shipped_in_the_image():
    # Decision 2: /data/eitri/extras and /data/eitri/klipper-layer (from the seed) are the source of truth.
    assert not (OS_DIR / "package/eitri-klipper-layer").exists()
    assert "BR2_PACKAGE_EITRI_KLIPPER_LAYER=y" not in defconfig_lines()
    for text in ((OS_DIR / "Config.in").read_text(), (OS_DIR / "package/eitri-configurator/Config.in").read_text()):
        assert "KLIPPER_LAYER" not in text and "klipper-layer" not in text
    assert not (OVERLAY / "usr/lib/eitri/klipper-layer").exists()


def test_image_has_what_the_seed_needs():
    lines = defconfig_lines()
    # Moonraker imports libnacl at start-up and libnacl loads libsodium (seed/SPEC.md section 13).
    assert "BR2_PACKAGE_LIBSODIUM=y" in lines
    # eitri-seed-install and its callers use the zstd binary, not only the library.
    assert "BR2_PACKAGE_ZSTD=y" in lines
    for option in ("BR2_PACKAGE_GIT=y", "BR2_PACKAGE_PYTHON3_ZLIB=y", "BR2_PACKAGE_PYTHON3_SSL=y",
                   "BR2_PACKAGE_PYTHON3_SQLITE=y"):
        assert option in lines, option


def test_seed_units_run_before_the_programs_that_use_the_seed():
    seed = parse(UNIT_DIR / "eitri-seed.service")
    before = set(seed["Unit"]["Before"].split())
    assert {"klipper.service", "moonraker.service", "klarity.service", "eitri-configurator.service",
            "eitri-prepare.service"} <= before
    assert "eitri-data-init.service" in seed["Unit"]["After"].split()
    assert seed["Service"]["Type"] == "oneshot" and seed["Service"]["ExecStart"] == "/usr/libexec/eitri/seed-first-boot"
    prepare = dropin("eitri-prepare.service")["Unit"]
    assert "eitri-seed.service" in prepare["After"].split() and "eitri-seed.service" in prepare["Wants"].split()


def test_seeded_services_start_on_the_paths_the_seed_provides():
    # The conditions are the seed's own files (seed/SPEC.md section 3), so the units start as soon as the seed
    # is in place and the configurator has rendered the config, and not before.
    def conditions_of(unit):  # a unit may repeat the key, which configparser would collapse
        return re.findall(r"^ConditionPathExists=(\S+)", unit.read_text(), re.M)

    klipper = conditions_of(UNIT_DIR / "klipper.service")
    moonraker = conditions_of(UNIT_DIR / "moonraker.service")
    klarity = conditions_of(COMPONENT_UNITS[0])
    for conditions, want in ((klipper, ("/data/firmware/klipper/klippy/klippy.py", "/data/venv/klippy/bin/python")),
                             (moonraker, ("/data/firmware/moonraker/moonraker/moonraker.py", "/data/venv/moonraker/bin/python")),
                             (klarity, ("/data/eitri/klarity/venv/bin/klarity",))):
        for path in want:
            assert path in conditions, path


def test_configurator_retires_when_locked_by_default():
    # Decision 8. The flag may come from the component's own unit or from the OS drop-in (which then repeats the
    # unit's command line exactly, plus the flag).
    base = parse(COMPONENT_UNITS[1])["Service"]["ExecStart"].split()
    drop = parse(UNIT_DIR / "eitri-configurator.service.d/20-eitri-os.conf")["Service"]["ExecStart"].split()
    assert "--retire-when-locked" in drop
    assert [a for a in drop if a != "--retire-when-locked"] == [a for a in base if a != "--retire-when-locked"]
    # A clean exit must stay stopped; only a failure restarts.
    assert parse(COMPONENT_UNITS[1])["Service"]["Restart"] == "on-failure"
    text = (UNIT_DIR / "eitri-configurator.service.d/20-eitri-os.conf").read_text()
    assert "ExecStart=\nExecStart=" in text, "the drop-in must reset ExecStart before setting it"


def test_units_that_call_sudo_may_gain_privileges_and_the_others_may_not():
    # sudo is setuid: NoNewPrivileges would silently break `eitri-access` and `eitri-configurator unlock`.
    klarity = parse(COMPONENT_UNITS[0])["Service"]
    configurator = parse(UNIT_DIR / "eitri-configurator.service.d/20-eitri-os.conf")["Service"]
    assert klarity["NoNewPrivileges"] == "false" and configurator["NoNewPrivileges"] == "false"
    for unit in (klarity, configurator, parse(COMPONENT_UNITS[1])["Service"]):
        # systemd sets NoNewPrivs anyway for a unit that uses a seccomp-based option (measured on the QEMU image)
        assert not SECCOMP_OPTIONS & set(unit), SECCOMP_OPTIONS & set(unit)
    for name in ("klipper", "moonraker"):
        assert parse(UNIT_DIR / f"{name}.service")["Service"]["NoNewPrivileges"] == "true"


def sudo_rules():
    rules = {}
    for raw in (OVERLAY / "etc/sudoers.d/eitri").read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            rules.setdefault("password" if "NOPASSWD" not in line else "nopasswd", []).append(line)
    return rules


def test_sudo_rules_for_root_access_and_unlock_are_exact():
    rules = sudo_rules()
    commands = set()
    for line in rules["nopasswd"]:
        commands |= {c.strip() for c in line.split("NOPASSWD:", 1)[1].split(",")}
    assert commands == {
        "/usr/bin/eitri-update", "/usr/libexec/eitri/eitri-bootctl status",
        "/usr/libexec/eitri/eitri-bootctl get-current", "/usr/libexec/eitri/eitri-bootctl get-primary",
        "/usr/libexec/eitri/eitri-access set-key", "/usr/libexec/eitri/eitri-access disable",
        "/usr/libexec/eitri/eitri-access status", "/usr/libexec/eitri/eitri-access set-password",
        "/usr/libexec/eitri/eitri-access set-password --ssh-login",
        "/usr/libexec/eitri/eitri-access admin-set-key", "/usr/libexec/eitri/eitri-access admin-close-window",
        "/usr/bin/eitri-configurator unlock",
    }, "the apply subcommand in particular must not be reachable through sudo"
    for command in commands:
        if "eitri-access" in command or "eitri-configurator" in command:  # eitri-update/-bootctl: installer family
            assert (OVERLAY / command.split()[0].lstrip("/")).is_file(), command


def test_eitri_has_no_general_sudo_rule():
    # eitri can set its own password (eitri-access set-password), so ANY rule for all commands, with or without
    # a password, would let a program it runs make itself root. Only the specific passwordless helpers are left.
    rules = sudo_rules()
    assert "password" not in rules, rules.get("password")
    for line in rules["nopasswd"]:
        assert line.startswith("eitri ALL=(root) NOPASSWD: /"), line


def test_access_units_are_wired_to_the_helper():
    service = parse(UNIT_DIR / "eitri-access.service")
    assert service["Service"]["ExecStart"] == "/usr/libexec/eitri/eitri-access apply"
    assert "sshd.service" in service["Unit"]["Before"].split()
    assert "eitri-etc.service" in service["Unit"]["After"].split()
    path = parse(UNIT_DIR / "eitri-access.path")
    assert path["Path"]["PathChanged"] == "/data/eitri/state/access/generation"
    assert path["Path"]["Unit"] == "eitri-access.service"
    helper = (LIBEXEC_DIR / "eitri-access").read_text()
    assert 'F_GEN = STATE / "generation"' in helper and 'STATE = ROOT / "data/eitri/state/access"' in helper
    preset = (OVERLAY / "usr/lib/systemd/system-preset/50-eitri.preset").read_text().split("\n")
    for unit in ("eitri-access.path", "eitri-access.service", "eitri-seed.service"):
        assert f"enable {unit}" in preset


def test_the_journal_is_flushed_to_data_after_tmpfiles_links_it():
    # systemd-journal-flush.service is ordered before systemd-tmpfiles-setup.service, so on a tmpfs /var it
    # never sees /var/log/journal and the journal stayed in RAM: the 2026-10-24 host lockup left no log at all.
    unit = parse(UNIT_DIR / "eitri-journal.service")
    after = unit["Unit"]["After"].split()
    assert {"systemd-tmpfiles-setup.service", "systemd-journal-flush.service", "eitri-data-init.service",
            "eitri-etc.service"} <= set(after)
    # journald starts under a transient machine id (before /etc comes from /data); it must take the installation's
    # id, or each boot lands in a directory journalctl never lists (QEMU smoke, 2026-10-24: "1 boots listed").
    assert unit["Service"]["ExecStart"] == "/usr/libexec/eitri/journal-persist"
    helper = (LIBEXEC_DIR / "journal-persist").read_text()
    assert 'cat /etc/machine-id' in helper and '[ ! -d "/run/log/journal/$id" ]' in helper
    assert "systemctl restart systemd-journald.service" in helper and helper.rstrip().endswith("exec journalctl --flush")
    assert "/data/eitri/state/log/journal" in unit["Unit"]["RequiresMountsFor"]
    assert unit["Service"]["ExecStop"] == "/usr/bin/journalctl --relinquish-var"   # off /data before it unmounts
    assert unit["Install"]["WantedBy"] == "sysinit.target"
    preset = (OVERLAY / "usr/lib/systemd/system-preset/50-eitri.preset").read_text().splitlines()
    assert "enable eitri-journal.service" in preset
    tmpfiles = (OVERLAY / "etc/tmpfiles.d/eitri-journal.conf").read_text()
    assert re.search(r"^L\+ /var/log/journal .* /data/eitri/state/log/journal$", tmpfiles, re.M)
    # The effective value, as journald computes it: drop-ins from /usr/lib and /etc in file-name order (the same
    # name in /etc replaces the one in /usr/lib), the last setting wins. A second drop-in once overrode it.
    dropins = {}
    for d in ("usr/lib/systemd/journald.conf.d", "etc/systemd/journald.conf.d"):
        for f in sorted((OVERLAY / d).glob("*.conf")):
            dropins[f.name] = f
    storage = [m for name in sorted(dropins) for m in re.findall(r"^Storage=(\S+)$", dropins[name].read_text(), re.M)]
    assert storage and storage[-1] == "persistent", storage
    assert len(storage) == 1, f"one Storage= setting only, found {storage}"


def test_kernel_crash_records_go_to_the_persistent_journal():
    pstore = (OVERLAY / "etc/systemd/pstore.conf.d/10-eitri.conf").read_text()
    assert re.search(r"^Storage=journal$", pstore, re.M)      # the default (/var/lib/systemd/pstore) is a tmpfs here
    fragment = (BOARD / "linux.fragment").read_text().splitlines()
    for option in ("CONFIG_PSTORE_RAM=y", "CONFIG_SOFTLOCKUP_DETECTOR=y", "CONFIG_BOOTPARAM_SOFTLOCKUP_PANIC=y",
                   "CONFIG_HARDLOCKUP_DETECTOR=y", "CONFIG_BOOTPARAM_HARDLOCKUP_PANIC=y", "CONFIG_DW_WATCHDOG=y"):
        assert option in fragment, option
    watchdog = (OVERLAY / "etc/systemd/system.conf.d/10-eitri-watchdog.conf").read_text()
    assert re.search(r"^RuntimeWatchdogSec=1min$", watchdog, re.M) and re.search(r"^RebootWatchdogSec=", watchdog, re.M)
    assert "panic=10" in (BOARD / "uboot.env").read_text()


def test_ramoops_is_clear_of_the_boot_firmware_and_everything_u_boot_loads():
    # A boot that writes over the region destroys the crash it is meant to keep; below 0x200000 is the boot
    # firmware's (Linux's System RAM starts there on this board), and U-Boot proper runs from 0x200000.
    dts = (BOARD / "dts/rk3328-eitri-plus4.dts").read_text()
    m = re.search(r'ramoops@([0-9a-f]+) \{\s*compatible = "ramoops";\s*reg = <0x0 (0x[0-9a-f]+) 0x0 (0x[0-9a-f]+)>;', dts)
    assert m, "no ramoops node"
    start, size = int(m.group(2), 16), int(m.group(3), 16)
    assert int(m.group(1), 16) == start
    sizes = {k: int(v, 16) for k, v in re.findall(r"^\s+(record|console|pmsg)-size = <(0x[0-9a-f]+)>;", dts, re.M)}
    assert sizes["record"] and sizes["console"] + sizes["pmsg"] + sizes["record"] <= size
    env = dict(re.findall(r"^(\w+_addr_r|scriptaddr)=(0x[0-9a-f]+)$", (BOARD / "uboot.env").read_text(), re.M))
    used = [(0x0, 0x200000), (0x200000, 0x200000 + 0x200000)]                     # firmware; U-Boot before relocation
    used += [(int(env["scriptaddr"], 16), 0x100000), (0x600000, 0x100000)]          # boot script; pxefile_addr_r
    used += [(int(env[k], 16), 0x100000) for k in ("fdt_addr_r", "fdtoverlay_addr_r")]
    used += [(int(env["kernel_addr_r"], 16), 0x4000000), (int(env["ramdisk_addr_r"], 16), 0x2000000)]
    for base, length in used:
        assert start + size <= base or start >= base + length, (hex(base), hex(length))


def test_two_interfaces_on_one_network_do_not_answer_for_each_other():
    # Cable and Wi-Fi on one LAN: without these, wlan0 answers ARP for eth0's address (2026-10-24).
    conf = (OVERLAY / "usr/lib/sysctl.d/90-eitri.conf").read_text()
    for key, value in (("arp_ignore", "1"), ("arp_announce", "2")):
        for scope in ("all", "default"):
            assert re.search(rf"^net\.ipv4\.conf\.{scope}\.{key} = {value}$", conf, re.M), (scope, key)


def test_the_cpu_and_logic_rails_have_the_stock_ramp_delay():
    # Without it cpufreq clocks the cores up before vdd_arm has settled; the stock QIDI tree and
    # rk3328-rock64.dts set 12500 uV/us (suspected cause of the 2026-10-24 silent freezes).
    dts = (BOARD / "dts/rk3328-eitri-plus4.dts").read_text()
    for rail, minimum in (("vdd_arm", 950000), ("vdd_logic", 900000)):
        block = re.search(rf"^&{rail} \{{(.*?)^\}};", dts, re.M | re.S)
        assert block, rail
        assert "regulator-ramp-delay = <12500>;" in block.group(1), rail
        assert f"regulator-min-microvolt = <{minimum}>;" in block.group(1), rail


def test_the_fat_default_charset_is_one_the_kernel_has():
    # "IO charset ascii not found": every FAT mount without iocharset= failed (the admin-key USB stick, 2026-10-24).
    frag = (BOARD / "linux.fragment").read_text()
    m = re.search(r'^CONFIG_FAT_DEFAULT_IOCHARSET="([^"]+)"$', frag, re.M)
    assert m, "set the FAT default charset explicitly"
    built = {"iso8859-1": "CONFIG_NLS_ISO8859_1=y", "utf8": "CONFIG_NLS_UTF8=y", "ascii": "CONFIG_NLS_ASCII=y"}
    assert built[m.group(1)] in frag.splitlines()
    unit = (UNIT_DIR / "eitri-update-check.service").read_text() if (UNIT_DIR / "eitri-update-check.service").exists() else \
        (REPO_ROOT / "installer/eitri-update/systemd/eitri-update-check.service").read_text()
    assert re.search(r"^SuccessExitStatus=3$", unit, re.M)
