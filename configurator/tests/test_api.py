import json
import urllib.request


def test_bootstrap_shape(srv):
    st, b = srv.get("/api/bootstrap")
    assert st == 200
    assert b["app"]["mock"] is True
    assert b["steps"][0] == "welcome" and b["steps"][-1] == "done" and len(b["steps"]) == 10 and b["steps"][-2] == "remote"
    assert {f["id"] for f in b["flavors"]} == {"klipper", "kalico"}
    assert all(f["version"] and f["pros"] and f["cons"] for f in b["flavors"])
    assert b["options_schema"] and b["klarity"]["connected"] is True
    assert b["system"]["board"]["model"].startswith("Firefly")


def test_static_frontend_and_security_headers(srv):
    st, body, headers = srv.req("GET", "/")
    assert st == 200 and b"<title>" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert srv.req("GET", "/js/main.js")[0] == 200
    assert srv.req("GET", "/fonts/instrument-serif-latin-400-normal.woff2")[0] == 200
    assert srv.req("GET", "/%2e%2e/pyproject.toml")[0] in (403, 404)
    assert srv.req("GET", "/../pyproject.toml")[0] in (403, 404)


def test_cross_origin_writes_refused(srv):
    st, body = srv.put("/api/state", {"step": "network"}, headers={"Origin": "http://evil.example"})
    assert st == 403
    st, _ = srv.put("/api/state", {"step": "network"}, headers={"Origin": srv.base})
    assert st == 200


def test_state_validation_and_furthest(srv):
    assert srv.put("/api/state", {"step": "bogus"})[0] == 422
    assert srv.put("/api/state", {"detect": {}})[0] == 422          # server-owned key
    assert srv.put("/api/state", {"hostname": "bad host"})[0] == 422
    st, s = srv.put("/api/state", {"step": "flavor", "hostname": "Forge"})
    assert s["step"] == "flavor" and s["furthest"] == "flavor" and s["hostname"] == "forge"
    st, s = srv.put("/api/state", {"step": "network"})
    assert s["furthest"] == "flavor"                                 # going back keeps progress
    # persisted to disk
    assert json.loads(srv.settings.state_file.read_text())["hostname"] == "forge"


def test_options_roundtrip_and_validation(srv):
    st, r = srv.put("/api/options", {"chamber_heater": True})
    assert st == 422 and "chamber_ack" in r["fields"]
    st, r = srv.put("/api/options", {"chamber_heater": True, "chamber_ack": True, "ui": "mainsail"})
    assert st == 200 and r["values"]["ui"] == "mainsail"
    assert srv.get("/api/state")[1]["options"]["chamber_heater"] is True


def test_network_and_hostname(srv):
    st, n = srv.get("/api/network")
    assert any(i["kind"] == "ethernet" and i["up"] for i in n["interfaces"])
    st, scan = srv.get("/api/network/wifi")
    assert scan["supported"] and scan["networks"][0]["signal"] >= scan["networks"][-1]["signal"]
    assert srv.post("/api/network/wifi", {"ssid": "Forge-5G", "password": "wrongpass1"})[0] == 422
    assert srv.post("/api/network/wifi", {"ssid": "Forge-5G", "password": "short"})[0] == 422
    assert srv.post("/api/network/wifi", {"ssid": "Forge-5G", "password": "hammertime"})[1]["ok"]
    assert srv.post("/api/network/hostname", {"hostname": "-no"})[0] == 422
    st, r = srv.post("/api/network/hostname", {"hostname": "anvil"})
    assert r["hostname"] == "anvil"
    assert srv.settings.hostname_file.read_text().strip() == "anvil"


def test_detect_job_streams_events_and_stores_result(srv):
    st, body = srv.post("/api/detect")
    j = srv.job((st, body))
    assert j["status"] == "done" and j["result"]["flash_ready"] is True
    roles = {b["role"]: b["port"] for b in j["result"]["boards"]}
    assert set(roles) == {"mainboard", "toolhead"} and roles["mainboard"] != roles["toolhead"]
    events = srv.sse(body["job"]["id"])
    stages = [(e["stage"], e["status"]) for e in events if e["type"] == "detect.stage"]
    assert ("mcus", "done") in stages and ("camera", "done") in stages and ("screen", "done") in stages
    assert events[-1]["type"] == "job.end" and events[-1]["ok"]
    # resume from the middle of the stream (reconnect)
    tail = srv.sse(body["job"]["id"], since=events[3]["seq"])
    assert tail[0]["seq"] == events[4]["seq"]
    assert srv.get("/api/state")[1]["detect"]["flash_ready"] is True


def test_second_exclusive_job_is_rejected(make_server):
    s = make_server(mock_speed=20)            # slow enough to overlap
    first = s.post("/api/detect")
    assert first[0] == 202
    st, body = s.post("/api/detect")
    assert st == 409 and "already running" in body["error"]
    s.job(first)


def test_apply_requires_detect_and_confirmation(srv):
    assert srv.post("/api/apply", {})[0] == 422
    st, b = srv.post("/api/apply", {"confirm": True})
    assert st == 409 and "Detect" in b["error"]


def test_preview_apply_flow(detected):
    s = detected
    st, pv = s.post("/api/preview")
    paths = {f["path"]: f for f in pv["files"]}
    assert set(paths) == {"printer.cfg", "klarity.cfg", "moonraker.conf", "eitri-update-manager.conf", "eitri-access.conf"}
    assert paths["printer.cfg"]["foreign"] and paths["printer.cfg"]["status"] == "modified"
    assert paths["klarity.cfg"]["status"] == "new"
    assert "new_content" not in paths["klarity.cfg"] and paths["klarity.cfg"]["hunks"]
    roles = {b["role"]: b["port"] for b in s.get("/api/state")[1]["detect"]["boards"]}

    j = s.job(s.post("/api/apply", {"confirm": True}))
    assert j["status"] == "done", j
    assert "printer.cfg" in j["result"]["written"]
    cfg = s.settings.config_dir
    printer = (cfg / "printer.cfg").read_text()
    moonraker = (cfg / "moonraker.conf").read_text()
    assert "[webcam eitri]" in moonraker and "[include eitri-update-manager.conf]" in moonraker
    assert moonraker.index("[include eitri-access.conf]") < moonraker.index("[file_manager]")     # included first: user options win
    access = (cfg / "eitri-access.conf").read_text()
    assert f"klippy_uds_address: {s.settings.printer_data}/comms/klippy.sock" in access
    assert printer.index(f"serial: {roles['toolhead']}") < printer.index("[mcu U_1]") < printer.index(f"serial: {roles['mainboard']}")
    assert "z_offset = -0.012" in printer                              # SAVE_CONFIG block survived
    assert (cfg / ".eitri-backups").is_dir() and j["result"]["backed_up"] == ["printer.cfg"]
    assert (s.settings.flavor_file).read_text().strip() == "klipper"
    assert ("restart", "klarity") in s.ctx.services.calls
    # Klipper and Moonraker pick up the new files (and start for the first time on a fresh install), Klipper first
    calls = s.ctx.services.calls
    assert calls.index(("restart", "klipper")) < calls.index(("restart", "moonraker"))
    # idempotent: nothing left to write except the timestamped header
    st, pv2 = s.post("/api/preview")
    # moonraker.conf is yours once written: it no longer appears in the plan at all
    assert {f["path"]: f["status"] for f in pv2["files"]} == {"printer.cfg": "unchanged", "klarity.cfg": "unchanged", "eitri-access.conf": "unchanged"}


def test_flavor_choice_changes_rendered_header_and_marker(detected):
    s = detected
    assert s.put("/api/state", {"flavor": "kalico"})[0] == 200
    s.job(s.post("/api/apply", {"confirm": True}))
    assert s.settings.flavor_file.read_text().strip() == "kalico"
    assert "Kalico" in (s.settings.config_dir / "printer.cfg").read_text()


def test_flash_requires_confirm_and_detection(srv):
    assert srv.post("/api/flash", {})[0] == 422
    st, b = srv.post("/api/flash", {"confirm": True})
    assert st == 409 and "Detect" in b["error"]


def test_flash_success_records_outcome_and_restarts_klipper(detected):
    s = detected
    before = len(s.ctx.services.calls)
    st, body = s.post("/api/flash", {"confirm": True})
    j = s.job((st, body))
    assert j["status"] == "done" and j["result"]["complete"] is True
    ev = s.sse(body["job"]["id"])
    # one CLI run flashes every board; the mock mirrors the real order (toolhead first)
    flash_start = [e["board"] for e in ev if e["type"] == "phase" and e["phase"] == "flash" and e["status"] == "start"]
    assert flash_start == ["toolhead", "mainboard"]
    flash_progress = [e["percent"] for e in ev if e["type"] == "progress" and e["phase"] == "flash" and e["board"] == "toolhead"]
    assert flash_progress == sorted(flash_progress) and flash_progress[-1] == 100.0
    done = [e["phase"] for e in ev if e["type"] == "phase" and e["status"] == "done" and e["board"] == "toolhead"]
    assert done == ["build", "backup", "verify", "flash"]       # roundtrip is a log line, not a phase
    # recovery is proven for every board before any board is flashed
    first_flash = next(i for i, e in enumerate(ev) if e["type"] == "phase" and e["phase"] == "flash")
    assert all(any(e["type"] == "phase" and e["phase"] == "backup" and e["status"] == "done" and e["board"] == r for e in ev[:first_flash]) for r in ("toolhead", "mainboard"))
    results = {e["board"]: e for e in ev if e["type"] == "board.result"}
    assert results["mainboard"]["ok"] and results["toolhead"]["version"]
    assert [c for c in s.ctx.services.calls[before:] if c[1] == "klipper"] == [("stop", "klipper"), ("start", "klipper")]
    assert s.get("/api/state")[1]["flash"]["toolhead"]["ok"] is True


def test_flash_failure_reports_board_and_restarts_klipper(make_server):
    s = make_server(mock_fail="mainboard:flash")
    s.job(s.post("/api/detect"))
    st, body = s.post("/api/flash", {"confirm": True})
    j = s.job((st, body))
    assert j["status"] == "failed" and j["result"]["ok"] is False
    flash_state = s.get("/api/state")[1]["flash"]
    # toolhead flashes first in the real tool's order and had already succeeded
    assert flash_state["toolhead"]["ok"] is True and flash_state["mainboard"]["ok"] is False
    ev = s.sse(body["job"]["id"])
    res = [e for e in ev if e["type"] == "board.result" and e["board"] == "mainboard"][0]
    assert res["ok"] is False and "no acknowledgement" in res["message"] and "Power-cycle" in res["recovery"] and res["phase"] == "flash"
    assert s.ctx.services.mock_state["klipper"] == "active"          # restarted in `finally`
    s.settings.mock_fail = None
    j = s.job(s.post("/api/flash", {"confirm": True, "boards": ["mainboard"]}))
    assert j["result"]["complete"]


def test_unassigned_uarts_block_until_the_user_assigns_them(make_server):
    s = make_server(mock_unassigned=True)
    j = s.job(s.post("/api/detect"))
    r = j["result"]
    assert not r["flash_ready"] and r["needs_assignment"]
    assert any(b["code"] == "roles" and b["remedy"] for b in r["blockers"])
    assert s.post("/api/flash", {"confirm": True})[0] == 409
    assert s.post("/api/detect", {"assign": {"mainboard": "/dev/sda", "toolhead": "/dev/ttyS0"}})[0] == 422
    assert s.post("/api/detect", {"assign": {"mainboard": "/dev/ttyS0", "toolhead": "/dev/ttyS0"}})[0] == 422
    j = s.job(s.post("/api/detect", {"assign": {"mainboard": "/dev/ttyS2", "toolhead": "/dev/ttyS0"}}))
    assert j["result"]["flash_ready"]
    assert {b["role"]: b["confidence"] for b in j["result"]["boards"]} == {"mainboard": "override", "toolhead": "override"}
    assert s.get("/api/state")[1]["assign"] == {"mainboard": "/dev/ttyS2", "toolhead": "/dev/ttyS0"}


def test_flash_blocked_by_detection_results(make_server):
    s = make_server()
    s.job(s.post("/api/detect"))
    s.ctx.state.data["detect"]["flash_ready"] = False
    s.ctx.state.data["detect"]["blockers"] = [{"code": "rom-only", "message": "x"}]
    st, b = s.post("/api/flash", {"confirm": True})
    assert st == 409 and b["blockers"][0]["code"] == "rom-only"


def test_flash_refused_while_printing(detected):
    s = detected
    req = urllib.request.Request(s.ctx.settings.moonraker_url + "/_mock/state", data=json.dumps({"state": "printing"}).encode(), method="POST")
    urllib.request.urlopen(req).read()
    st, b = s.post("/api/flash", {"confirm": True})
    assert st == 409 and "print is printing" in b["error"]
    assert s.post("/api/flash", {"confirm": True, "boards": ["../../etc"]})[0] == 422


def test_changing_flavor_invalidates_flash_results(detected):
    s = detected
    s.job(s.post("/api/flash", {"confirm": True}))
    assert s.get("/api/state")[1]["flash"]
    s.put("/api/state", {"flavor": "kalico"})
    assert s.get("/api/state")[1]["flash"] is None


def test_reset_and_complete(srv):
    srv.put("/api/state", {"step": "options", "hostname": "x"})
    assert srv.post("/api/complete")[1]["completed"] is True
    st, s = srv.post("/api/reset")
    assert s["step"] == "welcome" and s["hostname"] is None and not s["completed"]


def test_rebinding_host_refused(srv):
    assert srv.req("GET", "/api/state", headers={"Host": "attacker.example.com"})[0] == 403
    assert srv.req("GET", "/api/state", headers={"Host": "eitri.local:8099"})[0] == 200
