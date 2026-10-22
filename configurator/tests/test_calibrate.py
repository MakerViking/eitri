def test_status_ready(srv):
    st, b = srv.get("/api/calibrate/status")
    assert b["ready"] and b["accelerometer"] == "adxl345" and b["manual_probe"]["is_active"] is False


def test_zoffset_flow(srv):
    assert srv.post("/api/calibrate/zoffset/nudge", {"delta": 0.1})[0] == 409     # not started
    j = srv.job(srv.post("/api/calibrate/zoffset/start"))
    assert j["status"] == "done" and j["result"]["active"] is True and j["result"]["z"] == 3.0
    steps = [e["id"] for e in srv.sse(j["id"]) if e["type"] == "step" and e["status"] == "done"]
    assert steps == ["home", "lift"]                      # lifted clear before probing
    st, r = srv.post("/api/calibrate/zoffset/nudge", {"delta": -0.5})
    assert r["z"] == 2.5
    assert srv.post("/api/calibrate/zoffset/nudge", {"delta": 0.3})[0] == 409     # not an allowed step
    for _ in range(3):
        srv.post("/api/calibrate/zoffset/nudge", {"delta": -0.5})
    assert srv.get("/api/calibrate/status")[1]["manual_probe"]["z_position"] == 1.0
    st, r = srv.post("/api/calibrate/zoffset/nudge", {"delta": -0.5})
    assert st == 409 and "0.1" in r["error"]            # no big plunges near the bed
    assert srv.post("/api/calibrate/zoffset/nudge", {"delta": -0.05})[1]["z"] == 0.95
    assert srv.post("/api/calibrate/zoffset/accept")[1]["save_pending"]
    assert srv.get("/api/calibrate/status")[1]["save_pending"] is True


def test_zoffset_abort(srv):
    srv.job(srv.post("/api/calibrate/zoffset/start"))
    assert srv.post("/api/calibrate/zoffset/abort")[0] == 200
    assert srv.get("/api/calibrate/status")[1]["manual_probe"]["is_active"] is False


def test_shaper_run_returns_parsed_recommendations(srv):
    j = srv.job(srv.post("/api/calibrate/shaper"))
    r = j["result"]
    assert j["status"] == "done"
    assert r["noise"]["x"] < 100
    assert r["axes"]["x"]["recommended"] == {"type": "mzv", "freq": 53.8}
    assert r["axes"]["y"]["recommended"]["type"] == "ei"
    assert len(r["axes"]["y"]["fits"]) == 3


def test_shaper_needs_accelerometer(srv):
    srv.put("/api/options", {"accelerometer": "none"})
    st, b = srv.post("/api/calibrate/shaper")
    assert st == 409 and "accelerometer" in b["error"].lower()


def test_mesh_run_and_fetch(srv):
    assert srv.get("/api/calibrate/mesh")[1]["matrix"] is None
    j = srv.job(srv.post("/api/calibrate/mesh"))
    r = j["result"]
    assert len(r["matrix"]) == 7 and len(r["matrix"][0]) == 7
    assert r["stats"]["range"] > 0 and r["min"] and r["max"]
    assert srv.get("/api/calibrate/mesh")[1]["profile"] == "default"


def test_calibration_refused_while_printing(srv):
    import json
    import urllib.request
    req = urllib.request.Request(srv.ctx.settings.moonraker_url + "/_mock/state", data=json.dumps({"state": "printing"}).encode(), method="POST")
    urllib.request.urlopen(req).read()
    j = srv.job(srv.post("/api/calibrate/mesh"))
    assert j["status"] == "failed" and "print is printing" in j["result"]["error"]


def test_save_restarts_klipper_and_waits(srv):
    srv.job(srv.post("/api/calibrate/zoffset/start"))
    srv.post("/api/calibrate/zoffset/accept")
    j = srv.job(srv.post("/api/calibrate/save"))
    assert j["status"] == "done"
    assert srv.get("/api/calibrate/status")[1]["ready"]


def test_calibration_marks_persist_in_state(srv):
    st, s = srv.put("/api/state", {"calibration": {"mesh": "done", "shaper": "skipped"}})
    assert s["calibration"] == {"mesh": "done", "shaper": "skipped"}
    assert srv.put("/api/state", {"calibration": {"mesh": "maybe"}})[0] == 422


def test_shaper_is_refused_while_the_toolhead_runs_stock_firmware(srv):
    # The in-place installer's stage 2 records toolhead_firmware=stock when it left the MCU boards alone (--mcu skip).
    env = srv.settings.eitri_dir / "state" / "install.env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("mcu_flash=skip\ntoolhead_firmware=stock\n")
    st, b = srv.post("/api/calibrate/shaper")
    assert st == 409 and "stock firmware" in b["error"]
