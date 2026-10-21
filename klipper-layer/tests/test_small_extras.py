"""qd_chamber_fan, qd_zoffset_var and qd_runtime_patches against fakes."""
import sys
import types

import pytest

import fakes
from fakes import FakeConfig, FakeGcmd, FakeHeater, FakeHeaters, FakePrinter


# --- qd_chamber_fan -------------------------------------------------------

class FakeFan:
    def __init__(self, config):
        self.speeds = []

    def set_speed(self, value, print_time=None):
        self.speeds.append(value)

    def get_status(self, eventtime):
        return {"speed": self.speeds[-1] if self.speeds else 0., "rpm": None}


fan_mod = types.ModuleType("extras.fan")
fan_mod.Fan = FakeFan
chamber_fan = fakes.load_extra("qd_chamber_fan", sibling_modules={"fan": fan_mod})


def make_fan(printer=None, name="qd_chamber_fan chamber_fan", **opts):
    printer = printer or FakePrinter()
    heater = FakeHeater()
    printer.objects.setdefault("heaters", FakeHeaters(chamber=heater))
    values = {"heater": "chamber", "idle_timeout": 30, "idle_speed": 0.4}
    values.update(opts)
    obj = chamber_fan.QdChamberFan(FakeConfig(values, name, printer))
    for cb in printer.handlers["klippy:ready"]:
        cb()
    return obj, printer, printer.objects["heaters"].heaters["chamber"]


def test_fan_follows_target_then_idles_then_stops():
    obj, printer, heater = make_fan()
    t = 100.
    obj._callback(t)
    assert obj.fan.speeds == []                    # nothing wanted yet
    heater.target = 55.
    obj._callback(t + 1)
    assert obj.fan.speeds[-1] == 1.0
    heater.target = 0.
    obj._callback(t + 2)
    assert obj.fan.speeds[-1] == 0.4               # idle speed
    obj._callback(t + 1 + 29)   # 29 s after the last active tick
    assert obj.fan.speeds[-1] == 0.4
    obj._callback(t + 1 + 31)
    assert obj.fan.speeds[-1] == 0.0


def test_toggle_silences_and_restores():
    obj, printer, heater = make_fan()
    heater.target = 55.
    obj._callback(10.)
    gcmd = FakeGcmd({"ENABLE": 0})
    obj.cmd_TOGGLE_CHAMBER_FAN(gcmd)
    obj._callback(11.)
    assert obj.fan.speeds[-1] == 0.
    obj.cmd_TOGGLE_CHAMBER_FAN(FakeGcmd())          # plain toggle -> back on
    assert obj.enabled
    obj._callback(12.)
    assert obj.fan.speeds[-1] == 1.0


def test_heater_name_accepts_section_style():
    obj, _, _ = make_fan(heater="heater_generic chamber")
    assert obj.heater_names == ["chamber"]


def test_requires_a_heater():
    printer = FakePrinter()
    printer.objects["heaters"] = FakeHeaters()
    with pytest.raises(fakes.ConfigError):
        chamber_fan.QdChamberFan(FakeConfig({}, "qd_chamber_fan x", printer))


# --- qd_zoffset_var -------------------------------------------------------

zmod = fakes.load_extra("qd_zoffset_var")


class FakeSaveVariables:
    def __init__(self):
        self.vars = {}

    def get_status(self, eventtime):
        return {"variables": self.vars}


class FakeGcodeMove:
    def __init__(self, z=0.):
        self.z = z

    def get_status(self, eventtime=None):
        return {"homing_origin": (0., 0., self.z, 0.)}


def make_z(**opts):
    printer = FakePrinter()
    printer.objects["save_variables"] = FakeSaveVariables()
    printer.objects["gcode_move"] = FakeGcodeMove(-0.06)
    obj = zmod.QdZoffsetVar(FakeConfig(opts, "qd_zoffset_var", printer))
    return obj, printer


def test_save_uses_save_variable_with_a_literal():
    obj, printer = make_z()
    obj.cmd_QD_SAVE_ZOFFSET(FakeGcmd())
    assert printer.gcode.scripts == ["SAVE_VARIABLE VARIABLE=z_offset VALUE=-0.06"]


def test_save_refuses_out_of_range_offsets():
    obj, printer = make_z(max_offset=0.5)
    with pytest.raises(fakes.ConfigError, match="outside"):
        obj.cmd_QD_SAVE_ZOFFSET(FakeGcmd({"VALUE": 0.75}))
    with pytest.raises(fakes.ConfigError, match="outside"):
        obj.cmd_QD_SAVE_ZOFFSET(FakeGcmd({"VALUE": "nan"}))
    assert printer.gcode.scripts == []


def test_load_applies_saved_value_and_can_move():
    obj, printer = make_z()
    printer.objects["save_variables"].vars["z_offset"] = -0.115
    obj.cmd_QD_LOAD_ZOFFSET(FakeGcmd())
    assert printer.gcode.scripts[-1] == "SET_GCODE_OFFSET Z=-0.1150 MOVE=0"
    obj.cmd_QD_LOAD_ZOFFSET(FakeGcmd({"MOVE": 1, "SPEED": 10}))
    assert printer.gcode.scripts[-1] == \
        "SET_GCODE_OFFSET Z=-0.1150 MOVE=1 MOVE_SPEED=10.000"


def test_load_without_saved_value_is_not_an_error_unless_required():
    obj, printer = make_z()
    gcmd = FakeGcmd()
    obj.cmd_QD_LOAD_ZOFFSET(gcmd)
    assert printer.gcode.scripts == [] and gcmd.info
    with pytest.raises(fakes.ConfigError):
        obj.cmd_QD_LOAD_ZOFFSET(FakeGcmd({"REQUIRED": 1}))


def test_load_ignores_junk_in_the_variables_file():
    obj, printer = make_z()
    for junk in ("text", True, None, float("inf")):
        printer.objects["save_variables"].vars["z_offset"] = junk
        assert obj.get_status(0)["saved"] is None


def test_bad_variable_name_is_a_config_error():
    printer = FakePrinter()
    printer.objects["save_variables"] = FakeSaveVariables()
    with pytest.raises(fakes.ConfigError):
        zmod.QdZoffsetVar(FakeConfig({"variable": "Z Offset"}, "q", printer))


# Baby-step vs probe recalibration

class FakeConfigfile:
    def __init__(self, z_offset=-2.0):
        self.settings = {"probe": {"z_offset": z_offset}}

    def get_status(self, eventtime):
        return {"settings": self.settings}


def make_zp(probe_z=-2.0, **opts):
    obj, printer = make_z(**opts)
    printer.objects["configfile"] = FakeConfigfile(probe_z)
    return obj, printer


def test_save_records_the_probe_reference_after_the_value():
    obj, printer = make_zp(-2.0)
    obj.cmd_QD_SAVE_ZOFFSET(FakeGcmd())
    assert printer.gcode.scripts == [
        "SAVE_VARIABLE VARIABLE=z_offset VALUE=-0.06",
        "SAVE_VARIABLE VARIABLE=z_offset_probe_ref VALUE=-2.0"]


def test_load_applies_the_baby_step_for_the_same_calibration():
    obj, printer = make_zp(-2.0)
    printer.objects["save_variables"].vars.update(
        z_offset=-0.115, z_offset_probe_ref=-2.0)
    obj.cmd_QD_LOAD_ZOFFSET(FakeGcmd())
    assert printer.gcode.scripts == ["SET_GCODE_OFFSET Z=-0.1150 MOVE=0"]
    assert obj.get_status(0)["stale"] is False


def test_load_discards_the_baby_step_after_the_probe_was_recalibrated():
    obj, printer = make_zp(-2.31)                    # PROBE_CALIBRATE + SAVE_CONFIG
    printer.objects["save_variables"].vars.update(
        z_offset=-0.115, z_offset_probe_ref=-2.0)
    gcmd = FakeGcmd()
    assert obj.get_status(0)["stale"] is True
    obj.cmd_QD_LOAD_ZOFFSET(gcmd)
    assert not any(c.startswith("SET_GCODE_OFFSET") for c in printer.gcode.scripts)
    # cleared and re-referenced to the new calibration
    assert printer.gcode.scripts == [
        "SAVE_VARIABLE VARIABLE=z_offset VALUE=0.0",
        "SAVE_VARIABLE VARIABLE=z_offset_probe_ref VALUE=-2.31"]
    assert "discarded" in gcmd.info[0] and "-2.3100" in gcmd.info[0]


def test_load_discards_a_baby_step_with_no_recorded_reference():
    obj, printer = make_zp(-2.0)
    printer.objects["save_variables"].vars["z_offset"] = -0.115   # old install
    gcmd = FakeGcmd()
    obj.cmd_QD_LOAD_ZOFFSET(gcmd)
    assert not any(c.startswith("SET_GCODE_OFFSET") for c in printer.gcode.scripts)
    assert "discarded" in gcmd.info[0]


def test_tiny_probe_difference_is_tolerated_and_zero_is_always_fine():
    obj, printer = make_zp(-2.0004)
    printer.objects["save_variables"].vars.update(
        z_offset=-0.115, z_offset_probe_ref=-2.0)
    obj.cmd_QD_LOAD_ZOFFSET(FakeGcmd())
    assert printer.gcode.scripts == ["SET_GCODE_OFFSET Z=-0.1150 MOVE=0"]
    printer.gcode.scripts.clear()
    printer.objects["save_variables"].vars.update(z_offset=0.0)
    printer.objects["save_variables"].vars.pop("z_offset_probe_ref")
    obj.cmd_QD_LOAD_ZOFFSET(FakeGcmd())
    assert printer.gcode.scripts == ["SET_GCODE_OFFSET Z=0.0000 MOVE=0"]


def test_load_on_ready_also_refuses_a_stale_baby_step():
    obj, printer = make_zp(-2.31, load_on_ready=True)
    printer.objects["save_variables"].vars.update(
        z_offset=-0.115, z_offset_probe_ref=-2.0)
    obj._load_on_ready(0.)
    assert not any(c.startswith("SET_GCODE_OFFSET") for c in printer.gcode.scripts)
    assert any("discarded" in m for m in printer.gcode.infos)


def test_probe_check_can_be_switched_off():
    obj, printer = make_zp(-2.31, probe_section="")
    printer.objects["save_variables"].vars["z_offset"] = -0.115
    obj.cmd_QD_LOAD_ZOFFSET(FakeGcmd())
    assert printer.gcode.scripts == ["SET_GCODE_OFFSET Z=-0.1150 MOVE=0"]
    obj.cmd_QD_SAVE_ZOFFSET(FakeGcmd())
    assert all("probe_ref" not in c for c in printer.gcode.scripts)


# --- qd_runtime_patches ---------------------------------------------------

def test_runtime_patches_sets_trsync_timeout(monkeypatch):
    fake_mcu = types.ModuleType("mcu")
    fake_mcu.TRSYNC_TIMEOUT = 0.025
    monkeypatch.setitem(sys.modules, "mcu", fake_mcu)
    patches = fakes.load_extra("qd_runtime_patches")
    patches.QdRuntimePatches(FakeConfig({"trsync_timeout": 0.1},
                                        "qd_runtime_patches", FakePrinter()))
    assert fake_mcu.TRSYNC_TIMEOUT == 0.1
    # nothing configured -> nothing touched
    fake_mcu.TRSYNC_TIMEOUT = 0.025
    patches.QdRuntimePatches(FakeConfig({}, "qd_runtime_patches", FakePrinter()))
    assert fake_mcu.TRSYNC_TIMEOUT == 0.025


def test_runtime_patches_fails_loudly_when_constant_is_gone(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcu", types.ModuleType("mcu"))
    patches = fakes.load_extra("qd_runtime_patches")
    with pytest.raises(fakes.ConfigError, match="TRSYNC_TIMEOUT"):
        patches.QdRuntimePatches(FakeConfig({"trsync_timeout": 0.1},
                                            "qd_runtime_patches", FakePrinter()))


# --- gcode_shell_command PARAMS restriction ---------------------------------

def make_shell(**opts):
    mod = fakes.load_extra("gcode_shell_command")
    printer = FakePrinter()
    values = {"command": "/bin/echo"}
    values.update(opts)
    return mod.ShellCommand(FakeConfig(values, "gcode_shell_command say",
                                       printer)), printer


def test_shell_params_pattern_refuses_other_arguments():
    obj, printer = make_shell(params_pattern=r"[a-z]+\.wav")
    gcmd = FakeGcmd({"PARAMS": "--evil /etc/passwd"})
    with pytest.raises(fakes.ConfigError, match="params_pattern"):
        obj.cmd_RUN_SHELL_COMMAND(gcmd)
    # a partial match is not enough
    with pytest.raises(fakes.ConfigError, match="params_pattern"):
        obj.cmd_RUN_SHELL_COMMAND(FakeGcmd({"PARAMS": "beep.wav --evil"}))


def test_shell_empty_pattern_allows_no_arguments_at_all():
    obj, printer = make_shell(params_pattern="")
    with pytest.raises(fakes.ConfigError, match="params_pattern"):
        obj.cmd_RUN_SHELL_COMMAND(FakeGcmd({"PARAMS": "x"}))


def test_shell_bad_pattern_is_a_config_error():
    with pytest.raises(fakes.ConfigError, match="params_pattern"):
        make_shell(params_pattern="[unclosed")
