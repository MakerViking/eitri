# SPDX-License-Identifier: Apache-2.0
"""eitri-access (the root helper behind the configurator's optional SSH key / password step) and the
eitri-configurator command that Klarity's "Unlock setup" runs through sudo.

Both are run as a normal user against a scratch root: the helper honours EITRI_ACCESS_ROOT only when it is
not root (see its source), so none of this needs privileges.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys

import pytest
from conftest import LIBEXEC, OVERLAY, REPO_ROOT, need, run

HELPER = LIBEXEC / "eitri-access"
PASSWORD = "correct horse battery"


def blob(ktype: str, *fields: bytes) -> str:
    raw = b"".join(struct.pack(">I", len(f)) + f for f in (ktype.encode(), *fields))
    return base64.b64encode(raw).decode()


ED25519 = "ssh-ed25519 " + blob("ssh-ed25519", bytes(range(32))) + " owner@laptop"
ED25519_B = "ssh-ed25519 " + blob("ssh-ed25519", bytes(range(1, 33)))
ECDSA = "ecdsa-sha2-nistp256 " + blob("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + bytes(64))


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    shadow = root / "etc/shadow"
    shadow.write_text("root:*:1:0:99999:7:::\neitri:*:20000:0:99999:7:::\nwww-data:*:1:0:99999:7:::\n")
    shadow.chmod(0o640)
    (tmp_path / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/systemctl.log"\nexit 0\n')
    (tmp_path / "systemctl").chmod(0o755)
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("EITRI_")},
           "EITRI_ACCESS_ROOT": str(root), "EITRI_ACCESS_APPLY": "inline",
           "EITRI_ACCESS_SYSTEMCTL": str(tmp_path / "systemctl")}
    return {"root": root, "env": env, "shadow": shadow, "state": root / "data/eitri/state/access",
            "log": tmp_path / "systemctl.log", "tmp": tmp_path}


def access(box, *args, stdin="", env=None):
    return run([sys.executable, "-I", HELPER, *args], input=stdin, env={**box["env"], **(env or {})})


def doc(result):
    return json.loads(result.stdout)


def shadow_hash(box):
    for line in box["shadow"].read_text().splitlines():
        if line.startswith("eitri:"):
            return line.split(":")[1]
    raise AssertionError("no eitri entry")


def libcrypt():
    for name in ("libcrypt.so.2", "libcrypt.so.1"):
        try:
            lib = ctypes.CDLL(name)
            lib.crypt_rn.restype = ctypes.c_char_p
            lib.crypt_rn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int]
            return lib
        except OSError:
            continue
    pytest.skip("needs libxcrypt")


def crypt(password: str, setting: str) -> str:
    lib = libcrypt()
    return lib.crypt_rn(password.encode(), setting.encode(), ctypes.create_string_buffer(32768), 32768).decode()


# --- set-key ---------------------------------------------------------------------------------------------

def test_set_key_installs_a_bare_key_for_sshd(box):
    result = access(box, "set-key", stdin=ED25519 + "\n")
    assert result.returncode == 0, result.stderr
    status = doc(result)
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(ED25519.split()[1])).digest()).decode().rstrip("=")
    assert status["keys"] == [{"type": "ssh-ed25519", "fingerprint": expected, "comment": "owner@laptop"}]
    keys = box["state"] / "authorized_keys"
    assert keys.read_text() == ED25519 + "\n"
    # sshd reads it as the eitri user: world-readable, root-owned, never group- or world-writable.
    assert stat.S_IMODE(keys.stat().st_mode) == 0o644 and stat.S_IMODE(box["state"].stat().st_mode) == 0o755


def test_set_key_is_idempotent_and_accumulates(box):
    access(box, "set-key", stdin=ED25519)
    again = access(box, "set-key", stdin=ED25519 + "\r\n")
    assert doc(again)["changed"] is False
    access(box, "set-key", stdin=ECDSA)
    assert len(doc(access(box, "status"))["keys"]) == 2
    assert (box["state"] / "authorized_keys").read_text().count("\n") == 2


def test_set_key_never_touches_etc_or_the_boot_partition_keys(box):
    before = box["shadow"].read_text()
    access(box, "set-key", stdin=ED25519)
    assert box["shadow"].read_text() == before and not box["log"].exists()


@pytest.mark.parametrize("line", [
    "",                                                    # nothing
    "\n\n",                                                # blank lines
    "ssh-ed25519",                                         # type only
    f'command="id" {ED25519}',                             # an authorized_keys option
    f'from="1.2.3.4",no-pty {ED25519}',
    f"# {ED25519}",                                        # a comment
    ED25519 + "\n" + ED25519_B,                            # two keys at once
    ED25519 + "\nssh-ed25519 AAAA",                        # a key and junk
    "ssh-dss " + blob("ssh-dss", b"p", b"q", b"g", b"y"),  # a type we do not accept
    "ssh-ed25519 " + blob("ssh-ed25519", b"short"),        # wrong key length
    "ssh-ed25519 " + blob("ssh-rsa", bytes(32)),           # blob type does not match
    "ssh-ed25519 " + blob("ssh-ed25519", bytes(32), b"extra"),  # extra field
    "ssh-ed25519 !!!notbase64!!!!!!!!!!!",
    "ssh-ed25519 " + base64.b64encode(struct.pack(">I", 500) + b"ssh").decode() * 2,  # truncated
    "ssh-rsa " + blob("ssh-rsa", b"\x01\x00\x01", b"\x00" + b"\xff" * 127),            # 1024-bit RSA
    ED25519 + " \x07bell",                                 # a control character in the comment
    "ssh-ed25519 " + blob("ssh-ed25519", bytes(32)) + " " + "x" * 200,                  # absurd comment
    "$(rm -rf /)",
    "ssh-ed25519 AAAA\0",
])
def test_set_key_rejects_anything_but_one_bare_key(box, line):
    result = access(box, "set-key", stdin=line)
    assert result.returncode == 3, (line, result.stdout, result.stderr)
    assert doc(result)["ok"] is False
    assert not (box["state"] / "authorized_keys").exists()


def test_set_key_rejects_an_oversized_input_and_unknown_arguments(box):
    assert access(box, "set-key", stdin="ssh-ed25519 " + "A" * 100000).returncode == 3
    assert access(box, "set-key", "--extra", stdin=ED25519).returncode == 3
    raw = subprocess.run([sys.executable, "-I", HELPER, "set-key"], input=b"ssh-ed25519 \xff\xfe\n", env=box["env"],
                         capture_output=True)
    assert raw.returncode == 3 and b"UTF-8" in raw.stderr


def test_set_key_limits_the_number_of_keys(box):
    for i in range(8):
        key = "ssh-ed25519 " + blob("ssh-ed25519", bytes([i + 1] * 32))
        assert access(box, "set-key", stdin=key).returncode == 0
    ninth = "ssh-ed25519 " + blob("ssh-ed25519", bytes([99] * 32))
    assert access(box, "set-key", stdin=ninth).returncode == 3


def test_a_rsa_key_of_enough_bits_is_accepted(box):
    modulus = b"\x00" + b"\xff" * 256  # 2048 bits
    line = "ssh-rsa " + blob("ssh-rsa", b"\x01\x00\x01", modulus)
    assert access(box, "set-key", stdin=line).returncode == 0


# --- set-password ----------------------------------------------------------------------------------------

def test_set_password_hashes_it_into_shadow_for_eitri_only(box):
    result = access(box, "set-password", stdin=PASSWORD + "\n")
    assert result.returncode == 0, result.stderr
    hashed = shadow_hash(box)
    assert hashed.startswith("$") and hashed != "*"
    assert crypt(PASSWORD, hashed) == hashed, "the stored hash must verify against the password"
    assert crypt("wrong horse battery", hashed) != hashed
    lines = box["shadow"].read_text().splitlines()
    assert lines[0] == "root:*:1:0:99999:7:::" and lines[2] == "www-data:*:1:0:99999:7:::"
    assert stat.S_IMODE(box["shadow"].stat().st_mode) == 0o640, "the mode of /etc/shadow is kept"
    status = doc(result)
    assert status["password"] == {"set": True, "ssh_login": False} and status["pending"] is False


def test_the_password_is_never_printed_or_stored_in_clear(box):
    result = access(box, "set-password", "--ssh-login", stdin=PASSWORD)
    assert PASSWORD not in result.stdout + result.stderr
    for path in box["root"].rglob("*"):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes(), path
    assert stat.S_IMODE((box["state"] / "password.hash").stat().st_mode) == 0o600


def test_password_login_over_ssh_is_a_separate_choice(box):
    access(box, "set-password", stdin=PASSWORD)
    conf = (box["state"] / "sshd.conf").read_text()
    assert "PasswordAuthentication" not in conf.replace("# sshd takes", "")
    assert not (box["state"] / "ssh-login").exists()
    access(box, "set-password", "--ssh-login", stdin=PASSWORD)
    assert "PasswordAuthentication yes" in (box["state"] / "sshd.conf").read_text()
    assert doc(access(box, "status"))["password"]["ssh_login"] is True
    # Setting the password again without the flag takes network password login away again.
    access(box, "set-password", stdin=PASSWORD)
    assert "PasswordAuthentication yes" not in (box["state"] / "sshd.conf").read_text()


def test_sshd_is_reloaded_only_when_it_runs(box):
    access(box, "set-password", stdin=PASSWORD)
    calls = box["log"].read_text().splitlines()
    assert "is-active --quiet sshd.service" in calls and "reload --no-block sshd.service" in calls
    box["log"].unlink()
    (box["tmp"] / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> "{box["tmp"]}/systemctl.log"\n[ "$1" != is-active ]\n')
    access(box, "set-password", "--ssh-login", stdin=PASSWORD)
    assert "reload" not in box["log"].read_text()


@pytest.mark.parametrize("password", ["short", "x" * 7, "x" * 129, "        ", "tab\tinside!!", "new\nline!!!!", "nul\0nul\0nul\0", "bell\x07bell\x07"])
def test_set_password_rejects_weak_or_odd_passwords(box, password):
    before = box["shadow"].read_text()
    result = access(box, "set-password", stdin=password)
    assert result.returncode == 3, (password, result.stderr)
    assert box["shadow"].read_text() == before and not (box["state"] / "password.hash").exists()
    assert password.strip() not in result.stdout + result.stderr or len(password.strip()) < 3


def test_set_password_accepts_unicode_and_spaces(box):
    result = access(box, "set-password", stdin="  pässwörd mit Leerzeichen  \n")
    assert result.returncode == 0, result.stderr
    assert crypt("  pässwörd mit Leerzeichen  ", shadow_hash(box)) == shadow_hash(box)


def test_set_password_rejects_unknown_flags(box):
    assert access(box, "set-password", "--shell", stdin=PASSWORD).returncode == 3
    assert access(box, "set-password", "--ssh-login", "x", stdin=PASSWORD).returncode == 3


def test_sha512_fallback_matches_openssl(box):
    need("openssl")
    access(box, "set-password", stdin=PASSWORD, env={"EITRI_ACCESS_HASH_PREFIX": "$6$"})
    hashed = shadow_hash(box)
    _, six, salt, _ = hashed.split("$")
    assert six == "6"
    reference = run(["openssl", "passwd", "-6", "-salt", salt, PASSWORD], check=True).stdout.strip()
    assert hashed == reference


def test_default_hash_is_yescrypt_where_the_library_has_it(box):
    access(box, "set-password", stdin=PASSWORD)
    assert shadow_hash(box).startswith(("$y$", "$6$"))


# --- disable ---------------------------------------------------------------------------------------------

def test_disable_removes_what_was_added_and_nothing_else(box):
    original = box["shadow"].read_text()
    access(box, "set-key", stdin=ED25519)
    access(box, "set-password", "--ssh-login", stdin=PASSWORD)
    result = access(box, "disable")
    assert result.returncode == 0, result.stderr
    status = doc(result)
    assert status["keys"] == [] and status["password"] == {"set": False, "ssh_login": False}
    assert not (box["state"] / "authorized_keys").exists()
    assert "PasswordAuthentication yes" not in (box["state"] / "sshd.conf").read_text()
    new = box["shadow"].read_text().splitlines()
    assert new[0] == original.splitlines()[0] and new[2] == original.splitlines()[2]
    assert new[1].split(":")[1] == "*", "the account is back to key login only"


def test_disable_does_not_touch_a_password_it_did_not_set(box):
    # A password set by the owner with passwd(1) is not ours to remove.
    box["shadow"].write_text("eitri:$6$own$hash:20000:0:99999:7:::\n")
    access(box, "set-key", stdin=ED25519)
    access(box, "disable")
    assert shadow_hash(box) == "$6$own$hash"


def test_disable_on_a_fresh_system_is_harmless(box):
    result = access(box, "disable")
    assert result.returncode == 0 and doc(result)["keys"] == []


# --- status and apply --------------------------------------------------------------------------------------

def test_status_needs_no_state_and_no_privileges(box):
    result = access(box, "status")
    assert result.returncode == 0
    assert doc(result) == {"ok": True, "schema": 1, "keys": [], "admin": {"window_open": False, "keys": []},
                           "password": {"set": False, "ssh_login": False},
                           "generation": 0, "applied": 0, "pending": False}


def test_request_without_the_unit_is_saved_and_reports_pending(box):
    # Not inline: the tool waits for eitri-access.service. Nothing runs it here, so it must say so, exit 4,
    # keep the request, and finish when the unit does run.
    env = {"EITRI_ACCESS_APPLY": "", "EITRI_ACCESS_WAIT": "1"}
    result = access(box, "set-password", stdin=PASSWORD, env=env)
    assert result.returncode == 4, result.stderr
    assert doc(result)["ok"] is False and doc(result)["pending"] is True
    assert doc(result)["generation"] == 1, "waiting must not rewrite the request counter (it once truncated it to 0)"
    assert shadow_hash(box) == "*", "nothing touches /etc/shadow until the unit runs"
    assert access(box, "apply").returncode == 0
    status = doc(access(box, "status"))
    assert status["pending"] is False and status["password"]["set"] is True
    assert crypt(PASSWORD, shadow_hash(box)) == shadow_hash(box)


def test_apply_runs_again_for_a_new_generation_and_is_idempotent(box):
    access(box, "set-password", stdin=PASSWORD, env={"EITRI_ACCESS_APPLY": "", "EITRI_ACCESS_WAIT": "1"})
    access(box, "apply")
    first = box["shadow"].read_text()
    access(box, "apply")
    assert box["shadow"].read_text() == first
    # The boot-time run re-asserts the password even when /etc came back without it.
    box["shadow"].write_text("eitri:*:20000:0:99999:7:::\n")
    access(box, "apply")
    assert shadow_hash(box).startswith("$")


def test_apply_with_nothing_requested_does_nothing(box):
    before = box["shadow"].read_text()
    assert access(box, "apply").returncode == 0
    assert box["shadow"].read_text() == before and not box["state"].exists()


def test_apply_refuses_a_shadow_without_exactly_one_eitri_entry(box):
    access(box, "set-password", stdin=PASSWORD, env={"EITRI_ACCESS_APPLY": "", "EITRI_ACCESS_WAIT": "1"})
    box["shadow"].write_text("root:*:1:0:99999:7:::\n")
    assert access(box, "apply").returncode == 1
    box["shadow"].write_text("eitri:*:1:0:99999:7:::\neitri:*:1:0:99999:7:::\n")
    assert access(box, "apply").returncode == 1


def test_a_damaged_stored_hash_is_not_written_to_shadow(box):
    access(box, "set-password", stdin=PASSWORD, env={"EITRI_ACCESS_APPLY": "", "EITRI_ACCESS_WAIT": "1"})
    (box["state"] / "password.hash").write_text("x:y:z\n")
    result = access(box, "apply")
    assert result.returncode == 1 and shadow_hash(box) == "*"


def test_usage_errors(box):
    assert access(box).returncode == 2
    assert access(box, "shell").returncode == 2
    assert access(box, "status", "x").returncode == 3


def test_it_refuses_to_run_unprivileged_against_the_real_root(box):
    env = {k: v for k, v in box["env"].items() if k != "EITRI_ACCESS_ROOT"}
    result = run([sys.executable, "-I", HELPER, "set-key"], input=ED25519, env=env)
    if os.geteuid() == 0:
        pytest.skip("running as root")
    assert result.returncode == 1 and "must run as root" in result.stderr
    assert not os.path.exists("/data/eitri/state/access")


def test_the_test_hooks_do_not_exist_for_root():
    # The sudo rule starts the tool as root with a clean environment; the hooks must not be reachable then.
    text = HELPER.read_text()
    assert "_TESTING = os.geteuid() != 0" in text
    for hook in ("EITRI_ACCESS_ROOT", "EITRI_ACCESS_APPLY", "EITRI_ACCESS_SYSTEMCTL", "EITRI_ACCESS_HASH_PREFIX",
                 "EITRI_ACCESS_WAIT"):
        assert any(f"_TESTING and os.environ.get(\"{hook}\")" in line or f"_TESTING and os.environ.get(\"{hook}\") ==" in line
                   or (hook in line and "_TESTING" in line) for line in text.splitlines()), hook


# --- what sshd and sudo make of the files ---------------------------------------------------------------------

def test_sshd_reads_the_state_files_first(box, tmp_path):
    need("sshd", "ssh-keygen")
    hostkey = tmp_path / "host_key"
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", hostkey], check=True)
    access(box, "set-key", stdin=ED25519)
    access(box, "set-password", stdin=PASSWORD)  # password set, SSH login not chosen
    config = (OVERLAY / "etc/ssh/sshd_config").read_text()

    def effective():
        cfg = tmp_path / "sshd_config"
        cfg.write_text(config.replace("/data/eitri/state/access", str(box["state"])))
        result = run(["sshd", "-T", "-f", cfg, "-h", hostkey])
        assert result.returncode == 0, result.stderr
        return {k.lower(): v for k, v in (line.split(None, 1) for line in result.stdout.splitlines() if " " in line)}

    seen = effective()
    assert seen["passwordauthentication"] == "no"
    assert str(box["state"] / "authorized_keys") in seen["authorizedkeysfile"] and ".ssh/authorized_keys" in seen["authorizedkeysfile"]
    access(box, "set-password", "--ssh-login", stdin=PASSWORD)
    assert effective()["passwordauthentication"] == "yes"
    access(box, "disable")
    assert effective()["passwordauthentication"] == "no"


def test_sshd_config_parses_when_the_state_directory_is_missing(tmp_path):
    need("sshd", "ssh-keygen")
    hostkey = tmp_path / "host_key"
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", hostkey], check=True)
    config = tmp_path / "sshd_config"
    config.write_text((OVERLAY / "etc/ssh/sshd_config").read_text().replace("/data/eitri/state/access", str(tmp_path / "nowhere")))
    result = run(["sshd", "-t", "-f", config, "-h", hostkey])
    assert result.returncode == 0, result.stderr


def test_sudoers_file_is_valid():
    need("visudo")
    assert run(["visudo", "-cf", OVERLAY / "etc/sudoers.d/eitri"]).returncode == 0


# --- eitri-configurator (the unlock command that Klarity runs through sudo) ---------------------------------------

CLI = OVERLAY / "usr/bin/eitri-configurator"


def cli(args, tmp_path, lib=None):
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("EITRI_")},
           "EITRI_CONFIGURATOR_LIB": str(lib or REPO_ROOT / "configurator"), "EITRI_CONFIGURATOR_DATA": str(tmp_path / "data")}
    return run([sys.executable, "-I", CLI, *args], env=env)


def need_aiohttp():
    if subprocess.run([sys.executable, "-c", "import aiohttp"], capture_output=True).returncode != 0:
        pytest.skip("needs aiohttp (the configurator's own dependency)")


def test_unlock_creates_the_maintenance_flag_where_the_configurator_looks(tmp_path):
    need_aiohttp()
    result = cli(["unlock"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "data/eitri/configurator/unlock").exists()
    assert cli(["lock"], tmp_path).returncode == 0
    assert not (tmp_path / "data/eitri/configurator/unlock").exists()
    assert cli(["pairing"], tmp_path).returncode == 0


@pytest.mark.parametrize("args", [[], ["unlock", "--data-dir", "/tmp/x"], ["--data-dir", "/tmp/x", "unlock"], ["serve"],
                                  ["unlock", "lock"], ["--mock"], ["--help"]])
def test_only_pairing_unlock_and_lock_are_accepted(tmp_path, args):
    result = cli(args, tmp_path)
    assert result.returncode == 2 and "usage:" in result.stderr
    assert not (tmp_path / "data").exists()


def test_a_missing_image_copy_is_reported_not_guessed(tmp_path):
    result = cli(["unlock"], tmp_path, lib=tmp_path / "nowhere")
    assert result.returncode == 1 and "cannot load" in result.stderr


def test_the_root_run_does_not_import_from_data(tmp_path):
    # The sudo rule runs this as root: a planted module in the working directory or PYTHONPATH must not load.
    need_aiohttp()
    plant = tmp_path / "plant"
    plant.mkdir()
    (plant / "eitri_configurator.py").write_text("raise SystemExit('planted code ran')\n")
    env = {**os.environ, "PYTHONPATH": str(plant), "EITRI_CONFIGURATOR_LIB": str(REPO_ROOT / "configurator"),
           "EITRI_CONFIGURATOR_DATA": str(tmp_path / "data")}
    result = subprocess.run([sys.executable, "-I", str(CLI), "pairing"], cwd=plant, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and "planted" not in result.stdout + result.stderr


def test_python_in_the_helper_scripts_has_no_third_party_imports():
    # They run before anything else is installed and as root: standard library only.
    import ast
    for path in (HELPER, CLI):
        tree = ast.parse(path.read_text())
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
        mods -= {"eitri_configurator"}  # the CLI imports the image's own copy on purpose
        assert mods <= set(sys.stdlib_module_names), (path.name, mods - set(sys.stdlib_module_names))


# --- the administrator account -------------------------------------------------------------------------

def open_window(box):
    box["state"].mkdir(parents=True, exist_ok=True)
    (box["state"] / "admin-window").write_text("")


def test_admin_key_is_refused_outside_the_first_setup_window(box):
    result = access(box, "admin-set-key", stdin=ED25519 + "\n")
    assert result.returncode == 3
    assert "first setup" in doc(result)["error"]
    assert not (box["state"] / "admin_authorized_keys").exists()


def test_admin_key_is_added_while_the_window_is_open(box):
    open_window(box)
    result = access(box, "admin-set-key", stdin=ED25519 + "\n")
    assert result.returncode == 0, result.stderr
    st = doc(result)
    assert st["admin"]["window_open"] is True and len(st["admin"]["keys"]) == 1
    assert st["keys"] == []                                         # not a key for "eitri"
    stored = (box["state"] / "admin_authorized_keys").read_text()
    assert stored == ED25519 + "\n"
    assert (box["state"] / "admin_authorized_keys").stat().st_mode & 0o777 == 0o644


def test_closing_the_window_ends_admin_keys_for_good(box):
    open_window(box)
    assert access(box, "admin-set-key", stdin=ED25519 + "\n").returncode == 0
    closed = access(box, "admin-close-window")
    assert closed.returncode == 0 and doc(closed)["changed"] is True
    assert doc(closed)["admin"]["window_open"] is False
    assert access(box, "admin-set-key", stdin=ED25519_B + "\n").returncode == 3
    again = access(box, "admin-close-window")                      # idempotent
    assert again.returncode == 0 and doc(again)["changed"] is False
    assert len(doc(access(box, "status"))["admin"]["keys"]) == 1    # closing keeps what was added


def test_disable_never_touches_the_administrator_keys(box):
    open_window(box)
    access(box, "admin-set-key", stdin=ED25519 + "\n")
    access(box, "set-key", stdin=ED25519_B + "\n")
    assert access(box, "disable").returncode == 0
    st = doc(access(box, "status"))
    assert st["keys"] == [] and len(st["admin"]["keys"]) == 1


def test_admin_key_parsing_is_as_strict_as_for_eitri(box):
    open_window(box)
    result = access(box, "admin-set-key", stdin='command="sh" ' + ED25519 + "\n")
    assert result.returncode == 3


@pytest.fixture
def stick(box):
    """A stand-in USB stick: mount copies its contents to the mount point, umount empties it."""
    src = box["tmp"] / "stick"
    src.mkdir()
    log = box["tmp"] / "mount.log"
    mount = box["tmp"] / "mount"
    mount.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nfor last; do :; done\ncp -r "{src}/." "$last"/\n')
    umount = box["tmp"] / "umount"
    umount.write_text(f'#!/bin/sh\necho umount "$@" >> "{log}"\nrm -rf "$1"/* "$1"/.[!.]* 2>/dev/null\nexit 0\n')
    mount.chmod(0o755)
    umount.chmod(0o755)
    env = {"EITRI_ACCESS_MOUNT": str(mount), "EITRI_ACCESS_UMOUNT": str(umount)}
    return {"src": src, "env": env, "log": log}


def test_usb_stick_adds_administrator_keys_without_the_window(box, stick):
    (stick["src"] / "eitri-admin-keys.txt").write_text("# my laptop\n\n" + ED25519 + "\n" + ED25519_B + "\n")
    result = access(box, "admin-import-usb", "/dev/sda1", env=stick["env"])
    assert result.returncode == 0, result.stderr
    assert doc(result)["added"] == 2 and len(doc(result)["admin"]["keys"]) == 2
    mounted = stick["log"].read_text()
    assert "-o ro,nosuid,nodev,noexec" in mounted and "/dev/sda1" in mounted and "umount" in mounted


def test_usb_stick_without_the_key_file_is_left_alone(box, stick):
    (stick["src"] / "photos.txt").write_text("nothing here")
    result = access(box, "admin-import-usb", "/dev/sda1", env=stick["env"])
    assert result.returncode == 0 and doc(result)["changed"] is False
    assert not (box["state"] / "admin_authorized_keys").exists()


@pytest.mark.parametrize("content", ['command="sh" ' + ED25519 + "\n", "not a key\n"])
def test_usb_key_file_with_anything_but_keys_is_refused_whole(box, stick, content):
    (stick["src"] / "eitri-admin-keys.txt").write_text(ED25519 + "\n" + content)
    result = access(box, "admin-import-usb", "/dev/sda1", env=stick["env"])
    assert result.returncode == 3
    assert not (box["state"] / "admin_authorized_keys").exists()


def test_usb_key_file_with_only_comments_is_refused(box, stick):
    (stick["src"] / "eitri-admin-keys.txt").write_text("# put your key here\n")
    assert access(box, "admin-import-usb", "/dev/sda1", env=stick["env"]).returncode == 3


@pytest.mark.parametrize("device", ["/dev/mmcblk0p1", "/data/x", "/dev/sda1;id", "sda1"])
def test_usb_import_takes_only_a_usb_disk_device(box, stick, device):
    assert access(box, "admin-import-usb", device, env=stick["env"]).returncode == 3
