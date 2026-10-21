"""scripts/render-config.py and the template/variables contract."""
import importlib.util
import re
import stat
import subprocess
import sys
import tomllib

import pytest

from conftest import CONFIG, SCRIPTS

spec = importlib.util.spec_from_file_location("render_config",
                                              SCRIPTS / "render-config.py")
render_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_config)

def code(text):
    """Config text without comment lines (notes may name removed features)."""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


TEMPLATE = (CONFIG / "printer.cfg.tmpl").read_text()
DEFAULTS = tomllib.loads((CONFIG / "eitri-variables.toml").read_text())["vars"]


def run(*args):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / "render-config.py"),
                           *args], capture_output=True, text=True)


def test_every_placeholder_has_a_default_and_every_default_is_used():
    used = set(re.findall(r"@@([A-Za-z_][A-Za-z0-9_]*)@@", TEMPLATE))
    used |= set(re.findall(r"^\s*#@if\s+!?([A-Za-z_]\w*)", TEMPLATE, re.M))
    assert used - set(DEFAULTS) == set(), "template uses undefined variables"
    assert set(DEFAULTS) - used == set(), "dead variables in eitri-variables.toml"


@pytest.mark.parametrize("chamber", [False, True])
def test_default_render_is_klipper_syntax(chamber):
    text = render_config.render(TEMPLATE, {**DEFAULTS, "chamber_enabled": chamber})
    assert "@@" not in text and "#@" not in text
    assert ("[heater_generic chamber]" in text) is chamber
    assert ("[qd_power_interlock]" in text) is chamber
    assert ("heater: chamber" in text) is chamber
    # sections are never duplicated by a rendering bug
    sections = re.findall(r"^\[([^\]]+)\]", text, re.M)
    assert len(sections) == len(set(sections))


def test_optional_blocks_follow_their_switches():
    on = render_config.render(TEMPLATE, DEFAULTS)
    off = render_config.render(TEMPLATE, {**DEFAULTS, "plr_enabled": False,
                                          "adxl_enabled": False})
    for section in ("[qd_plr]", "[adxl345]", "[resonance_tester]"):
        assert section in on and section not in off
    # Nothing else may depend on the pieces that were switched off
    assert "accel_chip" not in off and "save_interval" not in off


def test_rendered_values_use_klipper_literals():
    text = render_config.render(TEMPLATE, {**DEFAULTS, "chamber_enabled": True})
    assert "enable_force_move: True" in text
    assert "serial: /dev/ttyS2" in text and "serial: /dev/ttyS0" in text
    assert "pid_Kp: 33.555" in text
    assert "gear_ratio: 1517:170" in text


def test_no_heater_check_is_weakened_in_the_template():
    text = render_config.render(TEMPLATE, {**DEFAULTS, "chamber_enabled": True})
    # stock extruder/bed checks are used as is: no override sections at all
    assert "[verify_heater extruder]" not in text
    assert "[verify_heater heater_bed]" not in text
    # the chamber check is a plain Klipper one, no fork-only knobs
    block = re.search(r"^\[verify_heater chamber\]\n(.*?)\n\n", text,
                      re.M | re.S).group(1)
    assert "position_z" not in block
    assert "heating_gain: 2" in block and "hysteresis: 5" in block
    # nothing in the config or macros disables checks or accepts a triggered probe
    text = code(text)
    macros = code((CONFIG / "macros.cfg").read_text())
    for forbidden in ("position_z", "CLOSE_BED_DEBUG", "temp_ignore_limits",
                      "adc_ignore_limits", "verify_heater"):
        if forbidden == "verify_heater":
            assert "verify_heater" not in macros
        else:
            assert forbidden not in text and forbidden not in macros


def test_fork_only_options_are_gone():
    text = code(render_config.render(TEMPLATE, DEFAULTS))
    for fork_only in ("endstop_pin_reverse", "position_endstop_reverse",
                      "homing_speed_reverse", "homing_positive_dir_reverse",
                      "max_accel_to_decel", "heat_with_heater_bed",
                      "target_min_temp", "[qdprobe]", "[smart_effector]",
                      "[gcode_macro_break]", "box_", "samples_result: submaxmin",
                      "[chamber_fan"):
        assert fork_only not in text, fork_only
    macros = code((CONFIG / "macros.cfg").read_text())
    for fork_only in ("QIDI_PROBE_PIN", "REVERSE_HOMING", "SAVE_CONFIG_QD",
                      "TOOL_CHANGE", "BOX_", "CLEAR_FLUSH", "ACCEL_TO_DECEL",
                      "SAVE_Z_OFFSET_TO_BED_MESH", "get_zoffset"):
        assert fork_only not in macros, fork_only


def test_overrides_and_errors():
    out = run("--set", "mcu_baud=250000", "--set", "chamber_enabled=true")
    assert out.returncode == 0, out.stderr
    assert "baud: 250000" in out.stdout and "[heater_generic chamber]" in out.stdout
    assert run("--set", "nope=1").returncode == 1
    assert run("--set", "oops").returncode == 1


def test_vars_file_overrides_defaults(tmp_path):
    f = tmp_path / "v.toml"
    f.write_text('[vars]\nmcu_toolhead_serial = "/dev/ttyUSB7"\n')
    out = run("--vars", str(f))
    assert "serial: /dev/ttyUSB7" in out.stdout


def test_unknown_placeholder_and_unterminated_if():
    with pytest.raises(render_config.RenderError):
        render_config.render("x: @@missing@@\n", {})
    with pytest.raises(render_config.RenderError):
        render_config.render("#@if a\nx\n", {"a": True})
    with pytest.raises(render_config.RenderError):
        render_config.render("#@endif\n", {})


def test_nested_conditionals():
    t = "#@if a\nA\n#@if b\nAB\n#@else\nA!B\n#@endif\n#@else\n!A\n#@endif\n"
    assert render_config.render(t, {"a": True, "b": True}) == "A\nAB\n"
    assert render_config.render(t, {"a": True, "b": False}) == "A\nA!B\n"
    assert render_config.render(t, {"a": False, "b": True}) == "!A\n"


def test_output_is_not_overwritten_without_force(tmp_path):
    out = tmp_path / "printer.cfg"
    assert run("--output", str(out)).returncode == 0
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o644
    out.write_text("mine\n")
    again = run("--output", str(out))
    assert again.returncode == 1 and out.read_text() == "mine\n"
    assert run("--output", str(out), "--force").returncode == 0
    assert out.read_text().startswith("# Generated by")


def test_chamber_backstop_limit_is_85_and_above_the_chamber_limit():
    """Decision: the second chamber thermistor is a real backstop at 85 C
    (the stock 140 C sat far above every other limit)."""
    assert DEFAULTS["chamber_backstop_max_temp"] == 85
    text = render_config.render(TEMPLATE, {**DEFAULTS, "chamber_enabled": True})
    sensor = re.search(
        r"^\[temperature_sensor Chamber_Thermal_Protection_Sensor\]\n(.*?)\n\n",
        text, re.M | re.S).group(1)
    assert "max_temp: 85" in sensor
    heater = re.search(r"^\[heater_generic chamber\]\n(.*?)\n\n", text,
                       re.M | re.S).group(1)
    chamber_max = float(re.search(r"max_temp: (\S+)", heater).group(1))
    assert chamber_max < DEFAULTS["chamber_backstop_max_temp"]
    # no stale reference to the old default anywhere in the executable config
    assert "max_temp: 140" not in code(text)
    # and the old value is not described as the default any more
    variables = (CONFIG / "eitri-variables.toml").read_text()
    assert "chamber_backstop_max_temp = 85" in variables
    assert "chamber_backstop_max_temp = 140" not in variables


def test_backstop_can_still_be_raised_per_printer():
    out = run("--set", "chamber_enabled=true", "--set",
              "chamber_backstop_max_temp=95")
    assert out.returncode == 0, out.stderr
    assert "max_temp: 95" in out.stdout


def test_shell_guard_is_in_both_flavor_files():
    """The PARAMS policy must be active on whichever host is installed."""
    for flavor in ("klipper", "kalico"):
        text = (CONFIG / "flavors" / f"{flavor}.cfg").read_text()
        assert re.search(r"^\[qd_shell_guard\]$", text, re.M), flavor
