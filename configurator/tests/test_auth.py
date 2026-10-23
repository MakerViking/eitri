"""Pairing, sessions, CSRF and the lock after setup (eitri_configurator/auth.py + the guard in app.py)."""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import urllib.request
from http.cookies import SimpleCookie

import pytest

from eitri_configurator import auth as auth_mod
from eitri_configurator.auth import Auth, PairError

WRITES = [
    ("PUT", "/api/state", {"step": "network"}),
    ("POST", "/api/reset", {}),
    ("POST", "/api/complete", {}),
    ("POST", "/api/logout", {}),
    ("POST", "/api/network/wifi", {"ssid": "Forge-5G", "password": "longenough1"}),
    ("POST", "/api/network/hostname", {"hostname": "anvil"}),
    ("PUT", "/api/options", {"camera": True}),
    ("POST", "/api/detect", {}),
    ("POST", "/api/preview", {}),
    ("POST", "/api/apply", {"confirm": True}),
    ("POST", "/api/flash", {"confirm": True}),
    ("POST", "/api/services/restart", {"service": "klipper"}),
    ("POST", "/api/calibrate/zoffset/start", {}),
    ("POST", "/api/calibrate/shaper", {}),
    ("POST", "/api/calibrate/mesh", {}),
    ("POST", "/api/calibrate/save", {}),
]


def shown(srv) -> str:
    return json.loads((srv.settings.configurator_dir / "pairing.json").read_text())["code"]


def pair(srv, code=None, **kw):
    return srv.req("POST", "/api/pair", {"code": code if code is not None else shown(srv)}, auth=False, **kw)


def test_reads_stay_open_but_every_write_needs_a_session(srv):
    st, boot = srv.req("GET", "/api/bootstrap", auth=False)[:2]
    assert st == 200 and boot["auth"]["paired"] is False and boot["auth"]["pairing_open"] is True
    assert srv.req("GET", "/api/state", auth=False)[0] == 200
    for method, path, data in WRITES:
        st, body, _ = srv.req(method, path, data, auth=False)
        assert st == 401 and body["pairing_required"], (method, path, st, body)
    # nothing above changed anything
    assert srv.ctx.state.data["step"] == "welcome" and not srv.ctx.state.data["completed"]


def test_unknown_or_forged_tokens_are_refused(srv):
    for tok in ("nope", srv.token[:-1] + ("A" if srv.token[-1] != "A" else "B"), ""):
        st, body, _ = srv.req("PUT", "/api/state", {"step": "network"}, auth=False, headers={"Authorization": f"Bearer {tok}"})
        assert st == 401, tok


def test_pairing_file_and_secret_storage(srv):
    cfg = srv.settings.configurator_dir
    mode = stat.S_IMODE((cfg / "auth.json").stat().st_mode)
    assert mode == 0o600, oct(mode)
    stored = json.loads((cfg / "auth.json").read_text())
    assert srv.token not in json.dumps(stored)                       # only digests are stored
    assert hashlib.sha256(srv.token.encode()).hexdigest() in stored["sessions"]
    doc = json.loads((cfg / "pairing.json").read_text())
    assert doc["state"] == "pairing" and len(doc["code"]) == 9 and doc["code"][4] == "-"
    assert doc["code"].replace("-", "") == stored["code"]


def test_code_is_one_time_and_rotates(srv):
    old = shown(srv)
    st, body, _ = pair(srv, old)           # already rotated by the fixture's own pairing: the shown code is the new one
    assert st == 200 and body["token"] and body["csrf"]
    assert shown(srv) != old
    st, body, _ = pair(srv, old)
    assert st == 401 and "not right" in body["error"]


def test_code_accepts_spaces_and_dashes_but_not_junk(srv):
    code = shown(srv)
    assert pair(srv, "")[0] == 401
    assert pair(srv, "0000-0000" if code != "0000-0000" else "1111-1111")[0] == 401
    assert srv.req("POST", "/api/pair", {"code": None}, auth=False)[0] == 401
    assert srv.req("POST", "/api/pair", {}, auth=False)[0] == 401
    assert pair(srv, " " + code.replace("-", " ") + " ")[0] == 200


def test_wrong_codes_back_off_then_rotate_the_code(srv):
    now = [1000.0]
    srv.ctx.auth.clock = lambda: now[0]
    start = shown(srv)
    wrong = "0000-0000" if start != "0000-0000" else "1111-1111"
    for _ in range(4):
        assert pair(srv, wrong)[0] == 401
    assert pair(srv, wrong)[0] == 401                               # 5th failure starts the back-off
    st, body, headers = pair(srv, start)                            # even the right code waits
    assert st == 429 and int(headers["Retry-After"]) >= 1
    now[0] += 31
    for _ in range(4):
        assert pair(srv, wrong)[0] in (401, 429)
        now[0] += 1000
    st, body, _ = pair(srv, wrong)                                   # 10th failure
    assert shown(srv) != start, "the code must be replaced after repeated guessing"
    now[0] += 1000
    assert pair(srv, start)[0] == 401                                # the guessed-at code is dead
    assert pair(srv, shown(srv))[0] == 200


def test_browser_cookie_needs_the_csrf_header(srv):
    st, body, headers = pair(srv)
    assert st == 200
    cookie = SimpleCookie(headers["Set-Cookie"])["eitri_session"]
    assert cookie["httponly"] and cookie["samesite"] == "Strict" and cookie["path"] == "/"
    jar = {"Cookie": f"eitri_session={cookie.value}"}
    st, b, _ = srv.req("PUT", "/api/state", {"step": "network"}, auth=False, headers=jar)
    assert st == 403 and "CSRF" in b["error"]
    st, b, _ = srv.req("PUT", "/api/state", {"step": "network"}, auth=False, headers={**jar, "X-Eitri-CSRF": "x" * 64})
    assert st == 403
    st, b, _ = srv.req("PUT", "/api/state", {"step": "network"}, auth=False, headers={**jar, "X-Eitri-CSRF": body["csrf"]})
    assert st == 200
    st, a, _ = srv.req("GET", "/api/auth", auth=False, headers=jar)           # the page recovers its CSRF token after a reload
    assert a["paired"] and a["csrf"] == body["csrf"]
    st, a, _ = srv.req("GET", "/api/auth", auth=False)
    assert not a["paired"] and "csrf" not in a


def test_logout_revokes_the_session(srv):
    assert srv.post("/api/logout")[0] == 200
    assert srv.put("/api/state", {"step": "network"})[0] == 401


def test_cross_site_and_cross_origin_writes_are_refused_even_when_paired(srv):
    for headers in ({"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
        st, _ = srv.put("/api/state", {"step": "network"}, headers=headers)
        assert st == 403, headers
    assert srv.put("/api/state", {"step": "network"}, headers={"Origin": srv.base, "Sec-Fetch-Site": "same-origin"})[0] == 200
    # the pairing call is a write too
    st, _, _ = pair(srv, headers={"Origin": "http://evil.example"})
    assert st == 403


def test_bodies_must_be_json(srv):
    st, body, _ = srv.req("PUT", "/api/state", None, headers={"Content-Type": "text/plain"})
    assert st == 200                                                    # no body: nothing to misparse
    r = urllib.request.Request(srv.base + "/api/state", data=b'{"step":"network"}', method="PUT",
                               headers={"Content-Type": "text/plain", "Authorization": f"Bearer {srv.token}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 415
    r = urllib.request.Request(srv.base + "/api/pair", data=b"code=1", method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 415


def wrong_code(auth):
    return "00000000" if auth.code != "00000000" else "11111111"


def test_one_device_guessing_cannot_lock_the_owner_out(srv):
    # The lockout is per client: a device on the LAN that keeps guessing blocks only itself.
    auth = srv.ctx.auth
    auth.clock = lambda: 1000.0
    right, wrong = auth.code, wrong_code(auth)
    for _ in range(5):
        with pytest.raises(PairError) as exc:
            auth.pair(wrong, "192.168.0.66")
        assert exc.value.status == 401
    with pytest.raises(PairError) as exc:
        auth.pair(right, "192.168.0.66")                                 # backing off, even with the right code
    assert exc.value.status == 429
    assert auth.status(None, "192.168.0.66")["retry_after"] > 0
    assert auth.status(None, "192.168.0.10")["retry_after"] == 0
    token, _ = auth.pair(right, "192.168.0.10")                          # the owner's browser pairs at once
    assert token


def test_wrong_codes_from_many_addresses_still_rotate_the_code(srv):
    auth = srv.ctx.auth
    auth.clock = lambda: 1000.0
    start, wrong = auth.code, wrong_code(auth)
    rotated = []
    for i in range(50):                                                   # one guess each from 50 addresses
        with pytest.raises(PairError) as exc:
            auth.pair(wrong, f"10.0.0.{i}")
        rotated.append(exc.value.extra.get("rotated", False))
    assert rotated == [False] * 49 + [True] and auth.code != start


def test_tracked_clients_are_bounded(srv):
    auth = srv.ctx.auth
    auth.clock = lambda: 1000.0
    for i in range(300):
        with pytest.raises(PairError):
            auth.pair("x", f"10.1.{i // 256}.{i % 256}")
    assert len(auth._clients) <= 256


def test_completing_setup_locks_the_configurator(make_server):
    s = make_server()
    first = s.token
    second = pair(s)[1]["token"]                                        # a second browser paired with the next code
    st, body = s.post("/api/complete")
    assert st == 200 and body["completed"] and body["auth"]["locked"]
    # the other browser is signed out at once; this one keeps a short grace
    assert s.req("PUT", "/api/state", {"step": "done"}, auth=False, headers={"Authorization": f"Bearer {second}"})[0] == 423
    assert s.put("/api/state", {"step": "done"})[0] == 200
    # reading is still open, pairing is closed, and no code is shown
    assert s.req("GET", "/api/bootstrap", auth=False)[1]["auth"]["pairing_open"] is False
    st, b, _ = pair(s, "12345678")
    assert st == 423 and b["locked"]
    doc = json.loads((s.settings.configurator_dir / "pairing.json").read_text())
    assert doc == {"state": "locked", "code": None, "updated": doc["updated"]}
    # grace ends
    now = [s.ctx.auth.clock()]
    s.ctx.auth.clock = lambda: now[0] + auth_mod.GRACE_TTL + 5
    assert s.put("/api/state", {"step": "done"})[0] == 423
    # the printer's owner opens maintenance on the printer, then pairing works with the on-screen code again
    auth_mod.unlock(s.settings.data_dir)
    s.ctx.auth.clock = lambda: now[0]
    st, b = s.req("GET", "/api/auth", auth=False)[:2]
    assert b["pairing_open"] is True
    doc = json.loads((s.settings.configurator_dir / "pairing.json").read_text())
    assert doc["state"] == "pairing" and doc["code"]
    st, b, _ = pair(s)
    assert st == 200
    # finishing again locks again and removes the flag
    assert s.req("POST", "/api/complete", {}, auth=False, headers={"Authorization": f"Bearer {b['token']}"})[0] == 200
    assert not s.ctx.auth.maintenance() and s.ctx.auth.locked
    assert first  # (the first token was only the fixture's)


def test_reset_reopens_pairing(srv):
    srv.post("/api/complete")
    assert srv.ctx.auth.locked
    assert srv.post("/api/reset")[0] == 200            # still inside the grace window
    assert not srv.ctx.auth.locked


def test_sessions_survive_a_restart_and_expire(srv):
    again = Auth(srv.settings, srv.ctx.state)
    assert again.session(srv.token) is not None
    expired = Auth(srv.settings, srv.ctx.state, clock=lambda: srv.ctx.auth.clock() + auth_mod.SESSION_TTL + 1)
    assert expired.session(srv.token) is None
    assert again.code == srv.ctx.auth.code                          # the code survives too: the screen keeps showing it


def test_only_the_newest_sessions_are_kept(srv):
    for _ in range(auth_mod.MAX_SESSIONS + 3):
        assert pair(srv)[0] == 200
    stored = json.loads((srv.settings.configurator_dir / "auth.json").read_text())["sessions"]
    assert len(stored) == auth_mod.MAX_SESSIONS


def test_flash_apply_and_restart_refuse_while_printing(detected):
    s = detected
    req = urllib.request.Request(s.ctx.settings.moonraker_url + "/_mock/state", data=json.dumps({"state": "printing"}).encode(), method="POST")
    urllib.request.urlopen(req).read()
    for path, data in (("/api/apply", {"confirm": True}), ("/api/flash", {"confirm": True}), ("/api/services/restart", {"service": "klipper"}),
                       ("/api/services/restart", {"service": "moonraker"})):
        st, b = s.post(path, data)
        assert st == 409 and "print is printing" in b["error"], path
    assert s.post("/api/services/restart", {"service": "klarity"})[0] == 200      # the screen daemon does not touch the print


def test_cli_pairing_unlock_lock(tmp_path):
    def cli(*args):
        return subprocess.run([sys.executable, "-m", "eitri_configurator", *args, "--data-dir", str(tmp_path)], capture_output=True, text=True, check=True).stdout
    assert "No code is shown" in cli("pairing")
    cfg = tmp_path / "eitri" / "configurator"
    cfg.mkdir(parents=True)
    (cfg / "pairing.json").write_text(json.dumps({"state": "pairing", "code": "1234-5678"}))
    assert "1234-5678" in cli("pairing")
    cli("unlock")
    assert (cfg / "unlock").exists()
    cli("lock")
    assert not (cfg / "unlock").exists()
