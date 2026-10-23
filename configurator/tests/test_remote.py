"""Optional remote access (SSH key / password through the eitri-access helper), the camera sign-in page's API,
and the retire-when-locked behaviour."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import stat
import struct
import subprocess
import sys
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

from eitri_configurator import access, auth as auth_mod, options as opts, rtsp
from eitri_configurator.access import AccessError, normalize_status, parse_public_key, validate_password
from eitri_configurator.app import retire_due
from eitri_configurator.options import ValidationError

ROOT = Path(__file__).resolve().parent.parent


# --- key builders (no ssh-keygen needed) ------------------------------------------------------

#: Sandbox options that systemd implements with seccomp. For a unit whose user lacks CAP_SYS_ADMIN, systemd then sets
#: NoNewPrivs on the process even with NoNewPrivileges=false (measured with systemd 258 on the QEMU image), and sudo
#: refuses to run. Units that call sudo must not use any of them.
SECCOMP_OPTIONS = {
    "ProtectKernelTunables", "ProtectKernelModules", "RestrictNamespaces", "LockPersonality", "RestrictAddressFamilies",
    "MemoryDenyWriteExecute", "SystemCallFilter", "SystemCallArchitectures", "RestrictRealtime", "RestrictSUIDSGID",
    "ProtectClock", "ProtectHostname", "ProtectKernelLogs",
}

def s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def mpint(n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 8) // 8, "big")
    return s(raw)


def line(ktype: str, blob: bytes, comment: str = "") -> str:
    return f"{ktype} {base64.b64encode(blob).decode()}" + (f" {comment}" if comment else "")


def ed25519(seed: int = 1, comment: str = "thomas@laptop") -> str:
    return line("ssh-ed25519", s(b"ssh-ed25519") + s(hashlib.sha256(bytes([seed])).digest()), comment)


def rsa(bits: int, comment: str = "") -> str:
    n = (1 << (bits - 1)) | 12345
    return line("ssh-rsa", s(b"ssh-rsa") + mpint(65537) + mpint(n), comment)


def ecdsa(curve: str = "nistp256", size: int = 65) -> str:
    return line(f"ecdsa-sha2-{curve}", s(f"ecdsa-sha2-{curve}".encode()) + s(curve.encode()) + s(b"\x04" + bytes(size - 1)))


def fp_of(key_line: str) -> str:
    blob = base64.b64decode(key_line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


# --- parse_public_key --------------------------------------------------------------------------

def test_valid_keys_parse_with_the_ssh_keygen_fingerprint():
    k = parse_public_key("  " + ed25519() + "\n")
    assert k.type == "ssh-ed25519" and k.comment == "thomas@laptop" and k.fingerprint == fp_of(ed25519())
    assert k.line == ed25519()                         # one normalised line, no newline
    assert parse_public_key(rsa(2048)).bits == 2048
    assert parse_public_key(rsa(4096, "ci")).bits == 4096
    assert parse_public_key(ecdsa()).type == "ecdsa-sha2-nistp256"
    assert parse_public_key(ecdsa("nistp384", 97)).type.endswith("nistp384")
    assert parse_public_key(ecdsa("nistp521", 133)).type.endswith("nistp521")
    fido = line("sk-ssh-ed25519@openssh.com", s(b"sk-ssh-ed25519@openssh.com") + s(bytes(32)) + s(b"ssh:"))
    assert parse_public_key(fido).type == "sk-ssh-ed25519@openssh.com"


@pytest.mark.skipif(not __import__("shutil").which("ssh-keygen"), reason="no ssh-keygen")
def test_fingerprint_matches_ssh_keygen(tmp_path):
    f = tmp_path / "k.pub"
    f.write_text(ed25519(7) + "\n")
    out = subprocess.run(["ssh-keygen", "-l", "-f", str(f)], capture_output=True, text=True, check=True).stdout
    assert parse_public_key(ed25519(7)).fingerprint in out


def test_comment_is_sanitised_not_trusted():
    k = parse_public_key(ed25519(2, 'x"; rm -rf / `id` $(id) \\'))
    assert re.fullmatch(r"[A-Za-z0-9 ._@+=:,/()\-]*", k.comment) and "`" not in k.line and "$(" not in k.line and '"' not in k.line
    assert len(parse_public_key(ed25519(3, "a" * 500)).comment) <= 80


@pytest.mark.parametrize("bad", [
    None, 5, ["ssh-ed25519"], "", "   ", "ssh-ed25519",                               # not a key at all
    'command="id" ' + ed25519(),                                                      # options prefix
    'from="*" ' + ed25519(),
    "ssh-dss " + base64.b64encode(s(b"ssh-dss")).decode(),                            # weak type
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n-----END OPENSSH PRIVATE KEY-----",
    ed25519() + "\n" + ed25519(2),                                                    # two keys
    ed25519() + "\x00",                                                               # control char
    "ssh-ed25519 ###notbase64### x",
    "ssh-ed25519 " + base64.b64encode(s(b"ssh-ed25519") + s(b"short")).decode(),     # wrong key length
    line("ssh-ed25519", s(b"ssh-rsa") + s(bytes(32))),                                # body does not match its type
    line("ssh-ed25519", s(b"ssh-ed25519") + s(bytes(32)) + b"\x00"),                  # trailing garbage
    rsa(1024), rsa(512),
    ecdsa("nistp256", 64), ecdsa("nistp256", 97),
    "ssh-ed25519 " + "A" * 5000,
])
def test_bad_keys_are_refused_without_echoing_them(bad):
    with pytest.raises(ValidationError) as e:
        parse_public_key(bad)
    msg = e.value.errors["key"]
    assert msg and (not isinstance(bad, str) or len(bad) < 40 or bad[:40] not in msg)


def test_private_key_gets_its_own_warning():
    with pytest.raises(ValidationError) as e:
        parse_public_key("-----BEGIN OPENSSH PRIVATE KEY-----\nabc")
    assert "PRIVATE" in e.value.errors["key"]


# --- passwords ---------------------------------------------------------------------------------

def test_password_rules():
    good = "correct horse battery"
    assert validate_password(good, good) == good
    for pw, confirm, field in [
        (None, None, "password"), ("", "", "password"),
        (good, "something else", "confirm"),
        ("short1!", "short1!", "password"),
        ("x" * 129, "x" * 129, "password"),
        (" leading space pw", " leading space pw", "password"),
        ("trailing space pw ", "trailing space pw ", "password"),
        ("line\nbreak-password", "line\nbreak-password", "password"),
        ("tab\tin-password", "tab\tin-password", "password"),
        ("aaaaaaaaaaaa", "aaaaaaaaaaaa", "password"),
        ("MyPassword123", "MyPassword123", "password"),
        ("eitri-printer-1", "eitri-printer-1", "password"),
    ]:
        with pytest.raises(ValidationError) as e:
            validate_password(pw, confirm)
        assert field in e.value.errors, (pw, e.value.errors)
        assert not pw or not isinstance(pw, str) or pw not in "".join(e.value.errors.values())


def test_normalize_status_is_defensive():
    assert normalize_status(None)["keys"] == 0 and normalize_status("junk")["enabled"] is False
    fp = "SHA256:" + "A" * 43
    doc = {"ok": True, "schema": 1, "keys": [{"type": "ssh-ed25519", "fingerprint": fp, "comment": "x"}, "nonsense", {"fingerprint": "x"}],
           "password": {"set": True, "ssh_login": True}, "pending": True, "generation": 4, "extra": "secret"}
    st = normalize_status(doc)
    assert st["keys"] == 3 and st["fingerprints"] == [fp] and st["enabled"] and st["password"] and st["password_auth"] and st["pending"]
    assert "extra" not in st and "generation" not in st
    # the helper's real shape: a password with SSH login off is a password, but SSH does not accept it
    st = normalize_status({"keys": [], "password": {"set": True, "ssh_login": False}, "pending": False})
    assert st["password"] is True and st["password_auth"] is False and st["enabled"] and not st["pending"]
    assert normalize_status({"keys": [], "password": {"set": False, "ssh_login": True}})["password_auth"] is False       # no password, nothing to accept
    assert normalize_status({"keys": 2})["keys"] == 2 and normalize_status({"keys": True})["keys"] == 0 and normalize_status({"keys": -1})["keys"] == 0
    assert normalize_status({"password": True})["password"] is True and normalize_status({"password": "yes"})["password"] is False


# --- the API -----------------------------------------------------------------------------------

def test_status_defaults_to_off_and_needs_a_paired_browser(srv):
    st, body = srv.get("/api/access")
    assert st == 200 and body["available"] and body["keys"] == 0 and not body["password"] and not body["enabled"]
    st, body, _ = srv.req("GET", "/api/access", auth=False)
    assert st == 401 and body["pairing_required"]


def test_all_access_writes_need_pairing_and_csrf(srv):
    for path, data in (("/api/access/key", {"key": ed25519()}), ("/api/access/password", {"password": "correct horse battery", "confirm": "correct horse battery"}),
                       ("/api/access/disable", {"confirm": True})):
        assert srv.req("POST", path, data, auth=False)[0] == 401, path
        assert srv.req("POST", path, data, headers={"Origin": "http://evil.example"})[0] == 403, path
    # a browser (cookie) must echo the CSRF token
    code = json.loads((srv.settings.configurator_dir / "pairing.json").read_text())["code"]
    st, body, headers = srv.req("POST", "/api/pair", {"code": code}, auth=False)
    jar = {"Cookie": "eitri_session=" + SimpleCookie(headers["Set-Cookie"])["eitri_session"].value}
    assert srv.req("POST", "/api/access/key", {"key": ed25519()}, auth=False, headers=jar)[0] == 403
    assert srv.req("POST", "/api/access/key", {"key": ed25519()}, auth=False, headers={**jar, "X-Eitri-CSRF": body["csrf"]})[0] == 200
    # nothing happened for the refused calls
    assert srv.get("/api/access")[1]["keys"] == 1


def test_locked_configurator_refuses_access_reads_and_writes(srv):
    srv.post("/api/complete")
    srv.ctx.auth.clock = lambda: auth_mod.time.time() + auth_mod.GRACE_TTL + 5
    assert srv.get("/api/access")[0] == 423
    assert srv.post("/api/access/key", {"key": ed25519()})[0] == 423


def test_add_key_returns_only_the_fingerprint(srv):
    key = ed25519(4, "me@pc")
    st, body = srv.post("/api/access/key", {"key": key})
    assert st == 200 and body["added"]["fingerprint"] == fp_of(key) and body["status"]["keys"] == 1 and body["status"]["enabled"]
    assert key.split()[1] not in json.dumps(body)
    st, body = srv.get("/api/access")
    assert body["keys"] == 1 and body["fingerprints"] == [fp_of(key)]
    assert srv.post("/api/access/key", {"key": key})[1]["status"]["keys"] == 1       # adding it again changes nothing


def test_bad_key_is_422_with_a_field_message(srv):
    st, body = srv.post("/api/access/key", {"key": "ssh-rsa AAAA"})
    assert st == 422 and "key" in body["fields"]
    st, body = srv.post("/api/access/key", {"key": "-----BEGIN OPENSSH PRIVATE KEY-----"})
    assert st == 422 and "PRIVATE" in body["fields"]["key"]
    assert srv.post("/api/access/key", {})[0] == 422
    assert srv.get("/api/access")[1]["keys"] == 0


def test_password_flow_never_echoes_logs_or_stores_the_secret(srv, caplog):
    pw = "correct horse battery"
    caplog.set_level(logging.DEBUG)
    st, body = srv.post("/api/access/password", {"password": pw, "confirm": pw})
    assert st == 200 and body["status"]["password"] and body["status"]["enabled"]
    assert body["status"]["password_auth"] is False                       # SSH password login is a separate, explicit choice
    assert pw not in json.dumps(body) and pw not in caplog.text
    assert pw not in json.dumps(srv.ctx.state.data)
    srv.put("/api/state", {"step": "remote"})                           # make sure the state file exists, then look at every file
    on_disk = [p for p in srv.settings.data_dir.rglob("*") if p.is_file()]
    assert on_disk and not [p for p in on_disk if pw.encode() in p.read_bytes()]     # even the stand-in helper only keeps a hash
    assert srv.get("/api/access")[1]["password"] is True
    st, body = srv.post("/api/access/password", {"password": pw, "confirm": pw, "ssh_login": True})
    assert st == 200 and body["status"]["password_auth"] is True and pw not in json.dumps(body)
    assert srv.post("/api/access/password", {"password": pw, "confirm": pw, "ssh_login": "yes"})[0] == 422
    for bad in ({"password": pw, "confirm": "nope"}, {"password": "short", "confirm": "short"}, {"password": pw}):
        st, body = srv.post("/api/access/password", bad)
        assert st == 422 and pw not in json.dumps(body), bad


def test_disable_needs_confirm_and_removes_everything(srv):
    srv.post("/api/access/key", {"key": ed25519()})
    srv.post("/api/access/password", {"password": "correct horse battery", "confirm": "correct horse battery"})
    assert srv.post("/api/access/disable", {})[0] == 422
    assert srv.get("/api/access")[1]["enabled"]
    st, body = srv.post("/api/access/disable", {"confirm": True})
    assert st == 200 and body["status"] == {**body["status"], "keys": 0, "password": False, "enabled": False}


def test_admin_key_goes_to_the_admin_account_during_first_setup(srv):
    st, body = srv.get("/api/access")
    assert body["admin"] == {"window_open": True, "keys": 0, "fingerprints": [], "user": "admin"}
    key = ed25519(7, "owner@laptop")
    st, body = srv.post("/api/access/key", {"key": key, "admin": True})
    assert st == 200 and body["added"]["user"] == "admin"
    assert body["status"]["admin"]["keys"] == 1 and body["status"]["admin"]["fingerprints"] == [fp_of(key)]
    assert body["status"]["keys"] == 0                                   # not a key for the service account
    assert key.split()[1] not in json.dumps(body)
    assert srv.post("/api/access/key", {"key": key, "admin": "yes"})[0] == 422


def test_completing_setup_closes_the_admin_window_for_good(srv):
    srv.post("/api/access/key", {"key": ed25519(7), "admin": True})
    srv.post("/api/complete")
    status = asyncio.run(srv.ctx.access.status())                     # read directly: the configurator is locked now
    assert status["admin"]["window_open"] is False and status["admin"]["keys"] == 1


def test_admin_key_is_refused_once_the_window_is_closed(srv):
    asyncio.run(srv.ctx.access.close_admin_window())
    st, body = srv.post("/api/access/key", {"key": ed25519(8), "admin": True})
    assert st == 422 and "first setup" in body["fields"]["key"]
    assert srv.get("/api/access")[1]["admin"]["keys"] == 0


def test_disable_keeps_the_administrator_keys(srv):
    srv.post("/api/access/key", {"key": ed25519(7), "admin": True})
    srv.post("/api/access/key", {"key": ed25519(9)})
    st, body = srv.post("/api/access/disable", {"confirm": True})
    assert st == 200 and body["status"]["keys"] == 0 and body["status"]["admin"]["keys"] == 1


def test_reset_does_not_touch_remote_access(srv):
    srv.post("/api/access/key", {"key": ed25519()})
    srv.post("/api/reset")
    assert srv.get("/api/access")[1]["keys"] == 1


# --- how the helper is called ---------------------------------------------------------------

def recorder(tmp_path, exit_code=0, stdout='{"keys": 0, "password": false}', stderr="", status_too=True):
    """A fake eitri-access that records argv, stdin and (parts of) the environment."""
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "fake-access"
    script.write_text(f"""#!{sys.executable} -I
import json, os, sys
stdin = sys.stdin.read()
open({str(log)!r}, "a").write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin, "env": sorted(os.environ)}}) + "\\n")
sys.stderr.write({stderr!r})
sys.stdout.write({stdout!r})
sys.exit({exit_code} if ({status_too!r} or sys.argv[1:] != ["status"]) else 0)
""")
    script.chmod(0o755)
    return [str(script)], log


def calls(log):
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_secrets_travel_on_stdin_only(make_server, tmp_path):
    srv = make_server()
    cmd, log = recorder(tmp_path)
    srv.settings.access_cmd = cmd
    key, pw = ed25519(5, "me@pc"), "correct horse battery"
    assert srv.post("/api/access/key", {"key": key})[0] == 200
    assert srv.post("/api/access/password", {"password": pw, "confirm": pw})[0] == 200
    assert srv.post("/api/access/disable", {"confirm": True})[0] == 200
    c = calls(log)
    set_key = next(x for x in c if x["argv"] == ["set-key"])
    set_pw = next(x for x in c if x["argv"] == ["set-password"])
    assert set_key["stdin"] == key + "\n" and set_pw["stdin"] == pw + "\n"
    assert [x["argv"] for x in c if x["argv"] not in (["status"],)] == [["set-key"], ["set-password"], ["disable"]]
    blob = json.dumps(c)
    for x in c:                                                       # argv is just the verb; the environment is minimal
        assert len(x["argv"]) == 1 and set(x["env"]) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "PWD", "SHLVL", "_"}
        assert key.split()[1] not in " ".join(x["argv"]) and pw not in " ".join(x["argv"])
    assert blob.count(pw) == 1


def test_ssh_password_login_is_only_requested_when_chosen(make_server, tmp_path):
    srv = make_server()
    cmd, log = recorder(tmp_path)
    srv.settings.access_cmd = cmd
    pw = "correct horse battery"
    srv.post("/api/access/password", {"password": pw, "confirm": pw})
    srv.post("/api/access/password", {"password": pw, "confirm": pw, "ssh_login": True})
    argvs = [x["argv"] for x in calls(log) if x["argv"][0] != "status"]
    assert argvs == [["set-password"], ["set-password", "--ssh-login"]]       # exactly the two command lines sudoers allows


def test_helper_rejection_becomes_a_field_message(make_server, tmp_path):
    srv = make_server()
    cmd, _ = recorder(tmp_path, exit_code=3, stdout=json.dumps({"ok": False, "error": "at most 8 keys can be added; run disable first", "kind": "invalid"}))
    srv.settings.access_cmd = cmd
    st, body = srv.post("/api/access/key", {"key": ed25519()})
    assert st == 422 and "at most 8 keys" in body["fields"]["key"]
    pw = "correct horse battery"
    cmd, _ = recorder(tmp_path / "..", exit_code=3, stdout=json.dumps({"ok": False, "error": f"bad {pw}"}))
    srv.settings.access_cmd = cmd
    st, body = srv.post("/api/access/password", {"password": pw, "confirm": pw})
    assert st == 422 and pw not in json.dumps(body)


def test_saved_but_not_yet_applied_is_success_with_pending(make_server, tmp_path):
    srv = make_server()
    cmd, _ = recorder(tmp_path, exit_code=4, stdout=json.dumps({"keys": [], "password": {"set": True, "ssh_login": False}, "pending": True}),
                      stderr="eitri-access: saved, but not applied yet\n", status_too=False)
    srv.settings.access_cmd = cmd
    pw = "correct horse battery"
    st, body = srv.post("/api/access/password", {"password": pw, "confirm": pw})
    assert st == 200 and body["status"]["pending"] is True and body["status"]["password"] is True


HELPER_SRC = ROOT.parent / "os/board/eitri-plus4/rootfs-overlay/usr/libexec/eitri/eitri-access"


@pytest.mark.skipif(not HELPER_SRC.exists(), reason="the os lane's eitri-access is not in this tree")
def test_against_the_real_helper_contract(make_server, tmp_path):
    """The configurator's exact calls against the image's real helper (its own unprivileged test mode: a scratch root)."""
    root = tmp_path / "fakeroot"
    (root / "etc").mkdir(parents=True)
    (root / "etc/shadow").write_text("root:*:19000:0:99999:7:::\neitri:*:19000:0:99999:7:::\n")
    (root / "etc/passwd").write_text("root:x:0:0::/root:/bin/sh\neitri:x:1000:1000::/data/home:/bin/sh\n")
    (root / "data/eitri/state/access").mkdir(parents=True)
    srv = make_server()
    srv.settings.access_cmd = [sys.executable, "-I", str(HELPER_SRC)]
    srv.settings.access_env = {"EITRI_ACCESS_ROOT": str(root), "EITRI_ACCESS_APPLY": "inline", "EITRI_ACCESS_SYSTEMCTL": "/bin/true",
                               "EITRI_ACCESS_HASH_PREFIX": "$6$"}
    st = srv.get("/api/access")[1]
    assert st["available"] and st["keys"] == 0 and not st["password"] and not st["enabled"]
    key = ed25519(9, "me@pc")
    st, body = srv.post("/api/access/key", {"key": key})
    assert st == 200, body
    assert body["status"]["keys"] == 1 and body["status"]["fingerprints"] == [fp_of(key)] and body["added"]["fingerprint"] == fp_of(key)
    assert srv.post("/api/access/key", {"key": key})[1]["status"]["keys"] == 1           # same key twice: no-op
    pw = "correct horse battery"
    st, body = srv.post("/api/access/password", {"password": pw, "confirm": pw})
    assert st == 200, body
    assert body["status"]["password"] and body["status"]["password_auth"] is False
    st, body = srv.post("/api/access/password", {"password": pw, "confirm": pw, "ssh_login": True})
    assert st == 200 and body["status"]["password_auth"] is True
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and pw.encode() in p.read_bytes()]        # only the hash is stored
    st, body = srv.post("/api/access/disable", {"confirm": True})
    assert st == 200 and body["status"]["keys"] == 0 and not body["status"]["password"] and not body["status"]["enabled"]


def test_missing_helper_means_not_available(make_server, tmp_path):
    srv = make_server()
    srv.settings.access_cmd = [str(tmp_path / "does-not-exist")]
    st, body = srv.get("/api/access")
    assert st == 200 and body["available"] is False and body["enabled"] is False and "no remote-access helper" in body["reason"]
    st, body = srv.post("/api/access/key", {"key": ed25519()})
    assert st == 503 and "helper" in body["error"]


@pytest.mark.parametrize("stderr,expect", [
    ("sudo: a password is required\n", "sudo rule"),
    ('sudo: The "no new privileges" flag is set, which prevents sudo from running as root.\n', "NoNewPrivileges"),
    ("sudo: eitri-access: command not found\n", "no remote-access helper"),
])
def test_sudo_failures_are_explained(make_server, tmp_path, stderr, expect):
    srv = make_server()
    cmd, _ = recorder(tmp_path, exit_code=1, stderr=stderr)
    srv.settings.access_cmd = cmd
    st, body = srv.post("/api/access/key", {"key": ed25519()})
    assert st == 503 and expect in body["error"]
    assert srv.get("/api/access")[1]["available"] is False


def test_helper_failure_text_never_leaks_the_password(make_server, tmp_path):
    srv = make_server()
    pw = "correct horse battery"
    cmd, _ = recorder(tmp_path, exit_code=1, stderr=f"chpasswd: bad password {pw}\n")
    srv.settings.access_cmd = cmd
    st, body = srv.post("/api/access/password", {"password": pw, "confirm": pw})
    assert st == 502 and pw not in json.dumps(body)


def test_garbage_status_output_is_not_available(make_server, tmp_path):
    srv = make_server()
    cmd, _ = recorder(tmp_path, stdout="not json")
    srv.settings.access_cmd = cmd
    assert srv.get("/api/access")[1]["available"] is False


def test_default_command_is_the_narrow_sudo_rule():
    from eitri_configurator.settings import Settings
    assert Settings().access_cmd == ["sudo", "-n", "/usr/libexec/eitri/eitri-access"] == access.default_cmd()


# --- camera sign-in ------------------------------------------------------------------------------

def test_rtsp_credentials_need_a_session_and_are_not_cached(srv):
    st, body, _ = srv.req("GET", "/api/camera/rtsp", auth=False)
    assert st == 401
    st, body, headers = srv.req("GET", "/api/camera/rtsp")
    assert st == 200 and body["configured"] and body["user"] == "eitri" and len(body["password"]) == 36
    assert body["port"] == 8554 and body["stream"] == "eitri_cam" and headers["Cache-Control"] == "no-store"
    assert body["file"].endswith("/eitri/camera/go2rtc-auth.env")


def test_rtsp_credentials_are_not_in_the_wizard_state_or_bootstrap(srv):
    pw = srv.get("/api/camera/rtsp")[1]["password"]
    for path in ("/api/bootstrap", "/api/state", "/api/preview"):
        st, body = (srv.post(path) if path == "/api/preview" else srv.get(path))
        assert pw not in json.dumps(body), path


def test_rtsp_missing_and_partial_files(srv):
    f = srv.settings.camera_auth_file
    f.unlink()
    body = srv.get("/api/camera/rtsp")[1]
    assert body == {**body, "configured": False, "user": None, "password": None}
    f.write_text("EITRI_RTSP_USER=eitri\n")                          # no password yet
    assert srv.get("/api/camera/rtsp")[1]["configured"] is False
    f.write_text("EITRI_RTSP_USER=a\nEITRI_RTSP_PASS=old\nEITRI_RTSP_PASS='new secret'\n")
    assert srv.get("/api/camera/rtsp")[1]["password"] == "new secret"       # the last assignment wins, like the shell helper


def test_rtsp_reports_whether_the_gateway_is_enabled(srv):
    assert srv.get("/api/camera/rtsp")[1]["gateway_enabled"] is False        # go2rtc is off by default
    srv.put("/api/options", {"go2rtc": True})
    assert srv.get("/api/camera/rtsp")[1]["gateway_enabled"] is True
    srv.put("/api/options", {"camera": False})
    body = srv.get("/api/camera/rtsp")[1]
    assert body["gateway_enabled"] is False and body["camera_enabled"] is False


def test_rtsp_reader_rejects_control_characters(tmp_path):
    f = tmp_path / "a.env"
    f.write_text("EITRI_RTSP_USER=eitri\nEITRI_RTSP_PASS=ab\x01cd\n")
    assert rtsp.read_credentials(f) is None


def test_camera_page_and_remote_step_are_served(srv):
    st, page, headers = srv.req("GET", "/camera.html", auth=False)
    assert st == 200 and b"camera-page.js" in page and "default-src 'self'" in headers["Content-Security-Policy"]
    for asset in ("/js/camera-page.js", "/js/steps/remote.js", "/css/remote.css"):
        assert srv.req("GET", asset, auth=False)[0] == 200, asset
    assert b"remote.css" in srv.req("GET", "/", auth=False)[1]
    # the password is fetched by script, never inside the HTML
    assert srv.get("/api/camera/rtsp")[1]["password"].encode() not in page


# --- the wizard's UI contract ----------------------------------------------------------------------

def test_step_list_matches_the_frontend():
    from eitri_configurator.state import STEPS
    main = (ROOT / "web/js/main.js").read_text()
    order = re.search(r"const STEPS = \[([^\]]*)\]", main).group(1).replace(" ", "").split(",")
    ids = []
    for m in order:
        f = re.search(rf"import \* as {m} from '\./steps/(\w+)\.js'", main).group(1)
        ids.append(re.search(r"meta = \{\s*id: '(\w+)'", (ROOT / "web/js/steps" / f"{f}.js").read_text()).group(1))
    assert ids == STEPS and STEPS[-2:] == ["remote", "done"]
    dom = (ROOT / "web/js/dom.js").read_text()
    for step in STEPS:
        assert re.search(rf"^  {step}:\s+'M", dom, re.M), f"no rune for {step}"


def test_remote_access_is_optional_and_off_by_default_in_the_ui():
    js = (ROOT / "web/js/steps/remote.js").read_text()
    assert "Skip, keep it off" in js and "Optional" in js
    assert "autocomplete', 'new-password'" in js or "'new-password'" in js
    assert "localStorage" not in js and "sessionStorage" not in js          # secrets never leave the form
    assert "type: 'password'" in js


def test_web_login_choice_defaults_to_trusting_the_home_network():
    f = opts.FIELDS["api_access"]
    assert f["default"] == "lan" and f["choices"][0] == {"value": "lan", "label": "Trust my home network", "help": f["choices"][0]["help"]}
    assert {c["value"] for c in f["choices"]} == {"lan", "signin"}
    assert opts.validate({})["api_access"] == "lan"
    group = next(g for g in opts.SCHEMA if g["id"] == "access")
    assert "sign-in" in group["title"].lower()                              # not confusable with the SSH step


def test_ui_never_sends_users_to_moonraker_port_7125():
    texts = [p.read_text() for p in (ROOT / "web").rglob("*") if p.suffix in (".js", ".html", ".css")]
    texts.append(json.dumps(opts.SCHEMA))
    texts.append(json.dumps([opts.FLAVORS]))
    assert not [t for t in texts if "7125" in t]
    assert "port 80" in json.dumps(opts.SCHEMA)
    readme = (ROOT / "README.md").read_text()
    for ln in readme.splitlines():
        if "7125" in ln:                                                    # only as the reason it is not reachable
            assert "reachable" in ln or "loopback" in ln or "127.0.0.1" in ln, ln


# --- retire when locked ----------------------------------------------------------------------------

def test_retire_default_is_on_except_in_mock_mode():
    from eitri_configurator.__main__ import build_settings
    assert build_settings([]).retire_when_locked is True
    assert build_settings(["--no-retire-when-locked"]).retire_when_locked is False
    assert build_settings(["--mock"]).retire_when_locked is False
    assert build_settings(["--mock", "--retire-when-locked"]).retire_when_locked is True
    unit = (ROOT / "systemd/eitri-configurator.service").read_text()
    assert "--no-retire-when-locked" not in unit                            # the unit relies on the default


def test_retire_due_only_when_locked_idle_and_nobody_is_paired(make_server):
    s = make_server()
    ctx = s.ctx
    assert not retire_due(ctx)                                              # setup not complete
    s.post("/api/complete")
    assert not retire_due(ctx)                                              # the finishing browser still has its grace
    base = ctx.auth.clock()
    ctx.auth.clock = lambda: base + auth_mod.GRACE_TTL + 5
    assert retire_due(ctx)
    auth_mod.unlock(s.settings.data_dir)
    assert not retire_due(ctx)                                              # maintenance is open
    auth_mod.lock(s.settings.data_dir)
    assert retire_due(ctx)


def test_configurator_exits_after_setup(make_server):
    import threading
    gone = threading.Event()
    s = make_server(retire_when_locked=True, retire_poll=0.05, retire_hook=gone.set)
    assert not gone.wait(0.3)                                               # not before setup is complete
    s.post("/api/complete")
    assert not gone.wait(0.3)                                               # not during the grace period
    base = s.ctx.auth.clock()
    s.ctx.auth.clock = lambda: base + auth_mod.GRACE_TTL + 5
    assert gone.wait(5), "the configurator should retire once setup is complete and the grace period is over"


def test_configurator_stays_while_maintenance_is_open(make_server):
    import threading
    gone = threading.Event()
    s = make_server(retire_when_locked=True, retire_poll=0.05, retire_hook=gone.set)
    s.post("/api/complete")
    auth_mod.unlock(s.settings.data_dir)
    base = s.ctx.auth.clock()
    s.ctx.auth.clock = lambda: base + auth_mod.GRACE_TTL + 5
    assert not gone.wait(0.5)


# --- unlock ---------------------------------------------------------------------------------------------

def stub_systemctl(tmp_path, data_dir, active: bool, start_rc: int = 0):
    """A systemctl that reports `active` and, on `start`, publishes the pairing file like the service would."""
    pf = data_dir / "eitri" / "configurator" / "pairing.json"
    script = tmp_path / "systemctl"
    script.write_text(f"""#!{sys.executable} -I
import json, sys, time
args = sys.argv[1:]
open({str(tmp_path / 'systemctl.log')!r}, "a").write(" ".join(args) + "\\n")
if "is-active" in args:
    sys.exit(0 if {active!r} else 3)
if "start" in args:
    if {start_rc!r}:
        sys.stderr.write("Failed to start: Unit not found.\\n")
        sys.exit({start_rc!r})
    open({str(pf)!r}, "w").write(json.dumps({{"state": "pairing", "code": "4321-8765", "updated": int(time.time())}}))
""")
    script.chmod(0o755)
    return (str(script),)


def test_unlock_starts_a_retired_configurator_and_reports_the_code(tmp_path):
    data = tmp_path / "data"
    cfg = data / "eitri" / "configurator"
    cfg.mkdir(parents=True)
    (cfg / "pairing.json").write_text(json.dumps({"state": "locked", "code": None, "updated": 1}))
    res = auth_mod.unlock_and_start(data, systemctl=stub_systemctl(tmp_path, data, active=False), timeout=5, sleep=lambda s: None)
    assert (cfg / "unlock").exists() and res["started"] and res["error"] is None
    assert res["pairing"]["state"] == "pairing" and res["pairing"]["code"] == "4321-8765"
    assert (tmp_path / "systemctl.log").read_text().split("\n")[:2] == ["is-active --quiet eitri-configurator.service", "start eitri-configurator.service"]


def test_unlock_does_not_restart_a_running_configurator(tmp_path):
    data = tmp_path / "data"
    cfg = data / "eitri" / "configurator"
    cfg.mkdir(parents=True)
    (cfg / "pairing.json").write_text(json.dumps({"state": "pairing", "code": "1111-2222", "updated": 5}))
    res = auth_mod.unlock_and_start(data, systemctl=stub_systemctl(tmp_path, data, active=True), timeout=5, sleep=lambda s: None)
    assert not res["started"] and res["pairing"]["code"] == "1111-2222"
    assert "start" not in (tmp_path / "systemctl.log").read_text().split()           # a running wizard (maybe mid-flash) is left alone


def test_unlock_reports_when_the_service_cannot_start(tmp_path):
    data = tmp_path / "data"
    (data / "eitri" / "configurator").mkdir(parents=True)
    res = auth_mod.unlock_and_start(data, systemctl=stub_systemctl(tmp_path, data, active=False, start_rc=5), timeout=1, sleep=lambda s: None)
    assert res["error"] and "Unit not found" in res["error"] and (data / "eitri" / "configurator" / "unlock").exists()


def test_unlock_gives_up_waiting_but_stays_unlocked(tmp_path):
    data = tmp_path / "data"
    (data / "eitri" / "configurator").mkdir(parents=True)
    t = [0.0]
    sys_ctl = tmp_path / "systemctl"
    sys_ctl.write_text("#!/bin/sh\nexit 0\n")
    sys_ctl.chmod(0o755)
    res = auth_mod.unlock_and_start(data, systemctl=(str(sys_ctl),), timeout=2, sleep=lambda s: t.__setitem__(0, t[0] + 1), clock=lambda: t[0])
    assert res["error"] is None and res["pairing"].get("code") is None and (data / "eitri" / "configurator" / "unlock").exists()


def test_cli_unlock_with_custom_data_dir_does_not_touch_systemd(tmp_path):
    out = subprocess.run([sys.executable, "-m", "eitri_configurator", "unlock", "--data-dir", str(tmp_path)], capture_output=True, text=True, check=True, cwd=ROOT)
    assert "Unlocked" in out.stdout and (tmp_path / "eitri" / "configurator" / "unlock").exists()


def test_admin_commands_need_no_web_server_libraries():
    """pairing/unlock/lock run on the image's plain python, which may be a sudo-root process: aiohttp must not be imported."""
    code = ("import sys; sys.path.insert(0, %r); import eitri_configurator.__main__ as m; "
            "assert m.admin(['pairing', '--data-dir', '/nonexistent']) == 0; assert 'aiohttp' not in sys.modules, sorted(sys.modules)" % str(ROOT))
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_privileged_wrapper_only_runs_image_code():
    # The command lives in the OS overlay (os/tests/test_access.py covers it in depth); here: it is the one that the
    # image-only rule describes, and it runs this checkout's real command when pointed at it (test hooks, honoured
    # only when not root).
    wrapper = ROOT.parent / "os" / "board" / "eitri-plus4" / "rootfs-overlay" / "usr" / "bin" / "eitri-configurator"
    text = wrapper.read_text()
    assert stat.S_IMODE(wrapper.stat().st_mode) & 0o111
    assert text.startswith("#!/usr/bin/python3 -I\n") and "/usr/lib/eitri/configurator" in text
    code = text.split('"""', 2)[2]
    assert "/data" not in code.replace("EITRI_CONFIGURATOR_DATA", "")       # no code is ever loaded from /data
    env = {k: v for k, v in os.environ.items() if not k.startswith("EITRI_")}
    env.update(EITRI_CONFIGURATOR_LIB=str(ROOT), EITRI_CONFIGURATOR_DATA="/nonexistent")
    out = subprocess.run([sys.executable, "-I", str(wrapper), "pairing"], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "No code is shown" in out.stdout, out.stderr


def test_root_drops_to_the_service_user_before_touching_files(monkeypatch):
    import pwd
    from eitri_configurator import __main__ as m
    seen = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", lambda n: type("P", (), {"pw_uid": 1234, "pw_gid": 2345})())
    monkeypatch.setattr(os, "initgroups", lambda n, g: seen.append(("groups", n, g)))
    monkeypatch.setattr(os, "setgid", lambda g: seen.append(("gid", g)))
    monkeypatch.setattr(os, "setuid", lambda u: seen.append(("uid", u)))
    assert m.drop_to_service_user() is True
    assert seen == [("groups", "eitri", 2345), ("gid", 2345), ("uid", 1234)]          # groups and gid first, uid last
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert m.drop_to_service_user() is False


# --- systemd units ---------------------------------------------------------------------------------------

def unit(name):
    out: dict[str, str] = {}
    for ln in (ROOT / "systemd" / name).read_text().splitlines():
        if "=" in ln and not ln.startswith(("#", "[")):
            k, _, v = ln.partition("=")
            out[k] = v
    return out


def test_configurator_unit_can_use_sudo_for_the_access_helper_only():
    u = unit("eitri-configurator.service")
    assert u["NoNewPrivileges"] == "false"                                 # sudo is setuid: NNP=true would silently break remote access
    assert u["User"] == "eitri" and "--host 0.0.0.0 --port 8099" in u["ExecStart"]
    assert u["ProtectSystem"] == "strict" and u["ReadWritePaths"] == "/data"
    assert not SECCOMP_OPTIONS & set(u)                                    # these make systemd set NoNewPrivs anyway


def test_unlock_without_a_terminal_does_not_wait_for_the_code(tmp_path):
    """Klarity runs `sudo -n eitri-configurator unlock` with a 10 s timeout and polls pairing.json itself."""
    code = ("import sys; sys.path.insert(0, %r); from eitri_configurator import __main__ as m, auth; "
            "calls = []; auth.unlock_and_start = lambda d, timeout: calls.append(timeout) or {'error': None, 'pairing': {}, 'flag': d}; "
            "m.admin(['unlock', '--data-dir', '/data']); print(calls)" % str(ROOT))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert out.returncode == 0 and "[0.0]" in out.stdout, (out.stdout, out.stderr)
