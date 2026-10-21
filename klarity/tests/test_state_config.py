import pytest

from fakes import DEFAULT_STATUS
from klarity.config import ConfigError, load_config, parse_config
from klarity.state import MISSING, PrinterState


def state_with(**patch) -> PrinterState:
    st = PrinterState()
    st.set_connection(True)
    st.apply({k: dict(v) for k, v in DEFAULT_STATUS.items()})
    st.set_klippy("ready")
    for obj, fields in patch.items():
        st.apply({obj: fields})
    return st


def test_resolve_paths():
    st = state_with()
    assert st.resolve("extruder.temperature") == 25.0
    assert st.resolve("heater_generic chamber.temperature") == 26.0
    assert st.resolve("toolhead.position.2") == 5.0
    assert st.resolve("toolhead.position.9") is MISSING
    assert st.resolve("nope.x") is MISSING
    assert st.resolve("extruder.nope") is MISSING
    assert st.resolve("derived.nope") is MISSING


def test_apply_merges_fields_not_objects():
    st = state_with()
    st.apply({"extruder": {"target": 200.0}})
    assert st.resolve("extruder.target") == 200.0
    assert st.resolve("extruder.temperature") == 25.0


def test_listeners_fire_on_change():
    st = PrinterState()
    hits = []
    st.subscribe(lambda: hits.append(1))
    st.apply({"extruder": {"temperature": 1}})
    st.set_connection(True)
    assert len(hits) == 2


def test_derived_state_reflects_connectivity():
    st = state_with()
    assert st.resolve("derived.state") == "standby"
    st.set_klippy("shutdown")
    assert st.resolve("derived.state") == "shutdown"
    st.set_connection(False)
    assert st.resolve("derived.state") == "offline"


def test_derived_progress_eta_and_names():
    st = state_with(
        print_stats={
            "state": "printing",
            "filename": "sub/dir/benchy.gcode",
            "print_duration": 600.0,
            "info": {"current_layer": 3, "total_layer": 40},
        },
        virtual_sdcard={"progress": 0.25},
    )
    assert st.resolve("derived.progress") == 25.0
    assert st.resolve("derived.filename") == "benchy.gcode"
    assert st.resolve("derived.elapsed") == "10m"
    assert st.resolve("derived.remaining_s") == pytest.approx(1800.0)
    assert st.resolve("derived.remaining") == "30m"
    assert st.resolve("derived.layer") == 3 and st.resolve("derived.total_layers") == 40
    assert st.resolve("derived.is_printing") is True


def test_derived_remaining_unknown_before_progress():
    st = state_with(
        print_stats={"state": "printing", "print_duration": 5.0}, virtual_sdcard={"progress": 0.0}
    )
    assert st.resolve("derived.remaining") is MISSING


def test_derived_progress_zero_when_idle():
    assert state_with(virtual_sdcard={"progress": 1.0}).resolve("derived.progress") == 0.0


# ------------------------------------------------------------------------------ config


def test_config_defaults_and_tbd_serial():
    cfg = parse_config("")
    assert cfg.screen.serial_port == ""  # TBD until probed live
    assert cfg.moonraker.url == "ws://127.0.0.1:7125/websocket"
    assert cfg.camera.snapshot_url.endswith(":8080/snapshot")
    assert not cfg.allow_todo and not cfg.learn.enabled and not cfg.camera.push_enabled
    assert cfg.map_path.name == "default.yaml"


def test_config_full():
    cfg = parse_config(
        """
[klarity]
map: /tmp/m.yaml   # custom map
allow_todo: yes
log_level: debug
[screen]
serial_port: /dev/ttyS1
baud: 9600
quote_style: replace
[moonraker]
api_key = abc
[push]
min_interval: 0.5
[camera]
enabled: true
push_enabled: false
[learn]
enabled: true
window: 3.5
"""
    )
    assert str(cfg.map_path) == "/tmp/m.yaml"
    assert cfg.allow_todo and cfg.log_level == "DEBUG"
    assert (cfg.screen.serial_port, cfg.screen.baud, cfg.screen.quote_style) == (
        "/dev/ttyS1",
        9600,
        "replace",
    )
    assert cfg.moonraker.api_key == "abc" and cfg.push.min_interval == 0.5
    assert cfg.camera.enabled and cfg.learn.enabled and cfg.learn.window == 3.5


@pytest.mark.parametrize(
    "text, message",
    [
        ("[nope]\nx: 1", "unknown section"),
        ("[screen]\nbogus: 1", "unknown option"),
        ("[klarity]\nscreen: 1", "unknown option"),
        ("[screen]\nbaud: fast", "baud"),
        ("[klarity]\nallow_todo: maybe", "true/false"),
        ("[screen]\nbaud: 0", "positive"),
        ("[screen]\nquote_style: weird", "quote_style"),
        ("[screen]\nbkcmd: 7", "bkcmd"),
        ("[klarity]\nlog_level: loud", "log_level"),
        ("[push]\nmin_interval: 0.001", "min_interval"),
        ("no section header", "section"),
    ],
)
def test_config_errors(text, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(text)


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.cfg")
    p = tmp_path / "k.cfg"
    p.write_text("[screen]\nserial_port: /dev/ttyS3\n")
    assert load_config(p).screen.serial_port == "/dev/ttyS3"
