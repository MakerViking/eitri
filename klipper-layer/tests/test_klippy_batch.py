"""Integration: the real klippy (Klipper and Kalico) loads the rendered config
with the overlay installed and runs the Eitri macros and extras without
errors. Heaters and probes are simulated by klippy's batch mode, so these
tests prove syntax, wiring and control flow, not physical behaviour."""
import json
import re

import pytest

import klippy_harness as kh
from conftest import CONFIG, git

pytestmark = pytest.mark.skipif(not kh.klippy_deps_available(),
                                reason="klippy Python deps not importable")


@pytest.fixture(scope="session", autouse=True)
def _drop_chelper_cache():
    yield
    kh.pytest_cleanup()


@pytest.fixture
def env_factory(clone, flavor, tmp_path):
    def make(**kw):
        return kh.Env(clone, flavor, tmp_path, **kw)
    return make


def ok(result):
    assert result.returncode == 0, result.log[-3000:]
    assert result.errors() == [], result.errors()


@pytest.mark.parametrize("chamber", [False, True], ids=["no-chamber", "chamber"])
def test_config_loads_and_homes(env_factory, chamber):
    env = env_factory(chamber=chamber)
    res = env.run("G4 P1\nG28\nG28 X\nG28 Y\nG28 Z\nG28 X Y\nM84\n")
    ok(res)
    # the overlay must not have dirtied the clone's tracked state
    assert git("status", "--porcelain", cwd=env.clone) == ""


def test_optional_blocks_can_be_switched_off(env_factory):
    """Without qd_plr and the ADXL345 the config still loads and prints."""
    env = env_factory(plr_enabled="false", adxl_enabled="false")
    ok(env.run("G4 P1\nG28\nM84\n"))
    assert "qd_plr" not in env.cfg_path.read_text()


def test_rehoming_z_near_the_axis_limit_does_not_overrun(env_factory):
    """The pre-homing lift is clamped to the remaining travel."""
    env = env_factory()
    res = env.run("G28\nG1 Z284 F600\nG28 Z\n")
    ok(res)


def test_flavor_file_applies_trsync_timeout(env_factory, flavor):
    res = env_factory().run("G4 P1")
    ok(res)
    if flavor == "klipper":
        assert "qd_runtime_patches: TRSYNC_TIMEOUT 0.025 -> 0.100" in res.log
    else:
        assert "multi_mcu_trsync_timeout = 0.1" in res.log


def test_print_flow(env_factory):
    env = env_factory()
    res = env.run("""
PRINT_START BED=60 HOTEND=210 SOAK=0
G1 X100 Y100 F6000
PRINT_END
PRINT_START BED=100 HOTEND=250 MESH=full SOAK=1
PRINT_END
PRINT_START BED=60 HOTEND=210 MESH=skip SOAK=0
CANCEL_PRINT
""")
    ok(res)


# The chamber wait polls the (simulated, always 0) sensor in real time. This
# wrapper keeps the interlock logic and only skips the waiting.
NO_CHAMBER_WAIT = """
[gcode_macro QD_SET_CHAMBER]
rename_existing: BASE_QD_SET_CHAMBER
gcode:
    BASE_QD_SET_CHAMBER TARGET={params.TARGET}
    {% if params.WAIT is defined %}
        { action_respond_info("stub QD_SET_CHAMBER wait skipped") }
    {% endif %}
"""


def test_print_flow_with_chamber(env_factory):
    """PRINT_START/PRINT_END/CANCEL with a chamber target run through the
    interlock (the chamber wait itself is stubbed: batch sensors read 0)."""
    env = env_factory(chamber=True)
    env.append(NO_CHAMBER_WAIT)
    res = env.run("""
PRINT_START BED=60 HOTEND=250 CHAMBER=55 SOAK=0
G1 X100 Y100 F6000
PRINT_END
PRINT_START BED=60 HOTEND=250 CHAMBER=55 MESH=skip SOAK=0
CANCEL_PRINT
QD_POWER_INTERLOCK_STATUS
""", timeout=300)
    ok(res)
    assert "Raising bed target to 80C" in res.log   # 55 C chamber + 25 C
    assert "stub QD_SET_CHAMBER wait skipped" in res.log
    assert "chamber power BLOCKED" in res.log       # batch bed never reads hot


def test_low_slicer_chamber_target_is_raised_not_fatal(env_factory):
    """Klarity and slicers send M141 S40; the interlock minimum is 45."""
    env = env_factory(chamber=True)
    env.append(NO_CHAMBER_WAIT)
    res = env.run("""
M141 S40
M191 S30
PRINT_START BED=100 HOTEND=250 CHAMBER=40 MESH=skip SOAK=0
M141 S0
""", timeout=300)
    ok(res)
    assert "M141: 40.0C is outside the chamber range, using 45.0C" in res.log
    assert "M191: 30.0C is outside the chamber range, using 45.0C" in res.log
    # the request reached the interlock at 45 (bed needs 45 + 25 = 70)
    assert "Chamber target 45C will be applied once the bed is ready" in res.log


def test_gated_chamber_pwm_reaches_the_mcu_as_zero(env_factory):
    """Real Heater.set_pwm -> _GatedPwm -> MCU output, with the gate closed."""
    env = env_factory(chamber=True)
    env.add_test_extra("qd_test_pwm")
    res = env.run("""
SET_HEATER_TEMPERATURE HEATER=chamber TARGET=50
QD_TEST_PWM VALUE=0.4
SET_HEATER_TEMPERATURE HEATER=chamber TARGET=0
""")
    ok(res)
    assert "QD_TEST_PWM requested=0.400 reached_mcu=[0.0]" in res.log


def print_time(result):
    return float(re.search(r"Exiting \(print time ([\d.]+)s\)",
                           result.log).group(1))


def test_heat_soak_is_applied(env_factory):
    env = env_factory()
    base = print_time(env.run("PRINT_START BED=60 HOTEND=210 SOAK=0"))
    soaked = print_time(env.run("PRINT_START BED=60 HOTEND=210 SOAK=3"))
    assert 175 < soaked - base < 185           # three minutes of dwell
    # SOAK defaults by bed temperature: none below 60 C, 5 min from 90 C
    cold = print_time(env.run("PRINT_START BED=50 HOTEND=210"))
    hot = print_time(env.run("PRINT_START BED=100 HOTEND=210"))
    assert 290 < hot - cold < 310


def test_print_start_refuses_uncalibrated_probe(env_factory):
    env = env_factory(probe_z_offset="0.0")
    res = env.run("PRINT_START BED=60 HOTEND=210")
    assert res.returncode != 0
    assert "uncalibrated" in res.log
    # ... and it is a single documented switch
    env.append("[gcode_macro _EITRI_CFG]\nvariable_require_probe_calibration: False\n"
               "gcode:\n")
    ok(env.run("PRINT_START BED=60 HOTEND=210 SOAK=0"))


@pytest.mark.parametrize("cmd", [
    "PRINT_START BED=60", "PRINT_START HOTEND=200 MESH=bogus",
    "M4031", "QD_PURGE HOTEND=210",
])
def test_bad_input_is_rejected_not_ignored(env_factory, cmd):
    res = env_factory().run(cmd)
    assert res.returncode != 0, res.log[-2000:]


def test_pause_resume_cancel(env_factory):
    env = env_factory()
    res = env.run("G28\nM140 S60\nM104 S200\nPAUSE\nRESUME\nPAUSE\nCANCEL_PRINT\n")
    ok(res)


def test_marlin_style_commands(env_factory):
    ok(env_factory().run("""
G28
M106 S128
M106 P2 S255
M106 P3 S0
M107
M104 S210
M109 S210
M140 S60
M190 S60
M141 S50
M191 S50
M141 S0
BEEP
M900 K0.03
M600
RESUME
G1 X10 Y10 Z10 F3000
M0
"""))


def test_chamber_interlock_wiring(env_factory):
    env = env_factory(chamber=True)
    res = env.run("""
QD_POWER_INTERLOCK_STATUS
M141 S50
QD_POWER_INTERLOCK_STATUS
TOGGLE_CHAMBER_FAN
TOGGLE_CHAMBER_FAN ENABLE=1
M141 S0
M141 S80
""")
    # M141 S80 is clamped to the chamber maximum, not an error
    ok(res)
    assert "chamber power BLOCKED" in res.log
    assert "Raising bed target to 75C" in res.log
    assert "will be applied once the bed is ready" in res.log


def test_chamber_target_above_maximum_is_rejected(env_factory):
    env = env_factory(chamber=True)
    res = env.run("QD_SET_CHAMBER TARGET=69")
    assert res.returncode != 0 and "out of range" in res.log


def test_zoffset_persists_across_restarts(env_factory):
    env = env_factory()
    ok(env.run("SET_GCODE_OFFSET Z=-0.085\nQD_SAVE_ZOFFSET"))
    saved = env.pd / "config" / "saved_variables.cfg"
    assert "z_offset = -0.085" in saved.read_text()
    ok(env.run("QD_LOAD_ZOFFSET\nSET_GCODE_OFFSET Z_ADJUST=0\nQD_SAVE_ZOFFSET"))
    assert "z_offset = -0.085" in saved.read_text()
    res = env.run("SET_GCODE_OFFSET Z=3\nQD_SAVE_ZOFFSET")
    assert res.returncode != 0 and "outside" in res.log


@pytest.mark.parametrize("section", ["qd_probe", "qd_reverse_home"])
def test_stubs_refuse_to_load(env_factory, section):
    env = env_factory()
    env.append("[%s]\n" % section)
    res = env.run("G4 P1")
    assert res.returncode != 0
    assert "design stub" in res.log


SHELL_CFG = """
[qd_shell_test touch_it]
command: /usr/bin/touch
allow_params: True
[qd_shell_test say]
command: /bin/echo
allow_params: True
[qd_shell_test slow]
command: /bin/sleep
allow_params: True
timeout: 0.5
[qd_shell_test fail]
command: /bin/false
[qd_shell_test missing]
command: /nonexistent/binary
"""


def test_gcode_shell_command_semantics(env_factory, tmp_path):
    env = env_factory()
    env.link_extra("qd_shell_test.py", "gcode_shell_command.py")
    env.append(SHELL_CFG)
    marker = tmp_path / "made"
    pwned = tmp_path / "pwned"
    res = env.run("""
RUN_SHELL_COMMAND CMD=touch_it PARAMS="%s"
RUN_SHELL_COMMAND CMD=say PARAMS="hello; touch %s"
RUN_SHELL_COMMAND CMD=slow PARAMS=30
RUN_SHELL_COMMAND CMD=fail
""" % (marker, pwned), timeout=60)
    ok(res)
    assert marker.exists()
    assert not pwned.exists()                      # no shell involved
    assert "hello; touch" in res.log               # argv passed through verbatim
    assert "Command {slow} timed out" in res.log   # killed after 0.5 s
    assert "Command {fail} finished with exit code 1" in res.log


def test_gcode_shell_command_start_failure_is_a_gcode_error(env_factory):
    env = env_factory()
    env.link_extra("qd_shell_test.py", "gcode_shell_command.py")
    env.append(SHELL_CFG)
    res = env.run("RUN_SHELL_COMMAND CMD=missing")
    assert res.returncode != 0
    assert "Error running command {missing}" in res.log


# The real section name: mainline runs the Eitri module, Kalico its own module
# wrapped by [qd_shell_guard] (included from flavors/kalico.cfg). Same policy.
NATIVE_SHELL_CFG = """
[gcode_shell_command plain]
command: /usr/bin/touch
[gcode_shell_command open]
command: /usr/bin/touch
allow_params: True
[gcode_shell_command pinned]
command: /usr/bin/touch
params_pattern: .*/ok-[a-z]+
"""


def test_params_are_denied_by_default_on_both_flavors(env_factory, tmp_path):
    env = env_factory()
    env.append(NATIVE_SHELL_CFG)
    evil = tmp_path / "evil"
    res = env.run("RUN_SHELL_COMMAND CMD=plain PARAMS=%s" % evil, timeout=60)
    assert res.returncode != 0
    assert "does not accept PARAMS" in res.log and "allow_params" in res.log
    assert not evil.exists()
    # without PARAMS the configured command still runs
    ok(env.run("RUN_SHELL_COMMAND CMD=plain", timeout=60))


def test_params_opt_in_options_work_on_both_flavors(env_factory, tmp_path):
    env = env_factory()
    env.append(NATIVE_SHELL_CFG)
    free, good, bad = (tmp_path / n for n in ("free", "ok-yes", "no-way"))
    ok(env.run("RUN_SHELL_COMMAND CMD=open PARAMS=%s\n"
               "RUN_SHELL_COMMAND CMD=pinned PARAMS=%s" % (free, good),
               timeout=60))
    assert free.exists() and good.exists()
    res = env.run("RUN_SHELL_COMMAND CMD=pinned PARAMS=%s" % bad, timeout=60)
    assert res.returncode != 0 and "params_pattern" in res.log
    assert not bad.exists()


def test_contradictory_params_options_fail_the_config(env_factory):
    env = env_factory()
    env.append("[gcode_shell_command x]\ncommand: /bin/true\n"
               "allow_params: False\nparams_pattern: a\n")
    res = env.run("G4 P1")
    assert res.returncode != 0 and "contradict" in res.log


PLR_JOB = """\
G90
M83
M104 S200
G28
G1 X50 Y60 Z5 F6000
SET_PRINT_STATS_INFO TOTAL_LAYER=3
G1 X60 E1 F1200
G1 X70 E1
RUN_SHELL_COMMAND CMD=pad PARAMS=2.2
G1 X80 E1
RUN_SHELL_COMMAND CMD=powercut
G1 X90 E1
G1 X99 Y99 E1
"""


def test_power_loss_recovery_round_trip(env_factory):
    """SIGKILL mid-print (a power cut) leaves a record; QD_PLR_RESUME
    rebuilds a resume file from it and the resumed print runs to the end."""
    env = env_factory()
    env.link_extra("qd_shell_test.py", "gcode_shell_command.py")
    env.append("[qd_shell_test pad]\ncommand: /bin/sleep\nallow_params: True\ntimeout: 10\n"
               "verbose: False\n"
               "[qd_shell_test powercut]\ncommand: /bin/sh -c 'kill -9 $PPID'\n"
               "verbose: False\n")
    (env.gcodes / "job.gcode").write_text(PLR_JOB)
    state = env.gcodes / ".plr" / "state.json"

    # 1. the host dies hard in the middle of the print
    first = env.run("SDCARD_PRINT_FILE FILENAME=job.gcode\n",
                    wait_for=r"never matches")
    assert first.returncode == -9, first.log[-2000:]
    rec = json.loads(state.read_text())
    assert rec["file"] == "job.gcode" and rec["version"] == 1
    assert rec["extruder_target"] == 200.0
    pad_line = PLR_JOB.index("RUN_SHELL_COMMAND CMD=pad")
    assert 0 < rec["position"] <= pad_line

    # 2. a fresh start offers the recovery and refuses to act unconfirmed
    plain = env.run("QD_PLR_STATUS\nQD_PLR_RESUME")
    ok(plain)
    assert "Interrupted print: job.gcode" in plain.log
    assert "QD_PLR_RESUME CONFIRM=1" in plain.log
    assert not (env.gcodes / ".plr" / "resume-a.gcode").exists()

    # 3. confirmed (and the cause of the failure removed): header + rest of job
    env.append("[qd_shell_test powercut]\ncommand: /bin/true\n")
    res = env.run("QD_PLR_RESUME CONFIRM=1\n",
                  wait_for=r"Exiting SD card print")
    assert res.returncode == 0, res.log[-3000:]
    assert res.errors() == [], res.errors()
    resume = (env.gcodes / ".plr" / "resume-a.gcode").read_text()
    head, sep, tail = resume.partition("; ---- end of resume header ----\n")
    assert sep
    assert tail == PLR_JOB.encode()[rec["position"]:].decode()
    assert "M109 S200" in head and "QD_PURGE LEN=15" in head
    assert "SET_KINEMATIC_POSITION Z=" in head and "SET_HOMED=z" in head
    assert head.index("G28 X Y") < head.index("G1 X")


# --- homing never leaves Z flagged homed at an invented height ---------------

def test_partial_homing_does_not_fake_a_homed_z(env_factory):
    """G28 X / G28 Y from cold lift from an assumed Z but must not leave Z
    homed: QD_PURGE refuses and M604 homes first."""
    env = env_factory()
    res = env.run("G28 X\nG28 Y\nQD_PURGE HOTEND=210")
    assert res.returncode != 0
    assert "needs the printer homed" in res.log
    # the assumed Z was set to lift, then cleared again
    assert "set_homed=z clear_homed=" in res.log
    assert "set_homed= clear_homed=z" in res.log
    # a following filament load still homes properly
    ok(env.run("G28 X\nG28 Y\nM604 S210"))


def test_cold_single_axis_homing_homes_only_that_axis(env_factory):
    env = env_factory()
    ok(env.run("G28 X"))
    ok(env.run("G28 Y"))
    ok(env.run("G28 Z"))                     # needs X/Y: homes them itself
    res = env.run("G28 X\nG1 Z10 F600")
    assert res.returncode != 0 and "Must home axis first" in res.log


def test_full_homing_leaves_everything_homed(env_factory):
    ok(env_factory().run("G28\nQD_PURGE HOTEND=210\nG28 X\nQD_PURGE HOTEND=210"))


def test_pre_triggered_probe_is_an_error_and_never_a_reference(env_factory):
    env = env_factory()
    env.add_test_extra("qd_test_probe")
    res = env.run("G28")
    assert res.returncode != 0
    assert "still reads triggered" in res.log
    # Z was never measured: it must not be left flagged homed
    assert "set_homed= clear_homed=z" in res.log
    res = env.run("G28 X")
    assert res.returncode != 0 and "still reads triggered" in res.log


# --- pause / idle timeout / resume ---------------------------------------------

def test_a_long_pause_stays_resumable(env_factory):
    """The idle timeout turns heaters off during a pause but keeps the print
    recoverable: RESUME reheats and carries on."""
    env = env_factory()
    ok(env.run("""
G28
M140 S60
M104 S200
G1 X100 Y100 F6000
PAUSE
_QD_IDLE_OFF
RESUME
"""))


REPORT_CFG = """
[gcode_macro T_REPORT]
gcode:
    {% set se = printer.stepper_enable.steppers %}
    { action_respond_info("EITRI_MOTORS homed=%s x=%s y=%s z=%s z1=%s bed=%s hotend=%s"
        % (printer.toolhead.homed_axes, se.stepper_x, se.stepper_y,
           se.stepper_z, se.stepper_z1, printer.heater_bed.target,
           printer.extruder.target)) }
"""


def test_motors_stay_energised_through_a_long_pause(env_factory):
    """Decision: when the idle timeout fires during a pause the heaters go off
    but X/Y/Z stay energised and homed, so the part position is never lost and
    RESUME reheats and continues."""
    env = env_factory()
    env.append(REPORT_CFG)
    res = env.run("""
G28
M140 S60
M104 S200
G1 X100 Y100 F6000
PAUSE
T_REPORT
_QD_IDLE_OFF
T_REPORT
RESUME
T_REPORT
""")
    ok(res)
    states = re.findall(r"EITRI_MOTORS (homed=x.*bed=\S+ hotend=\S+)\n", res.log)
    assert len(states) == 3, states
    paused, idle_off, resumed = states
    assert "homed=xyz x=True y=True z=True z1=True" in paused
    assert "bed=60.0 hotend=200.0" in paused
    # the idle timeout action: heaters off, motors and homed state kept
    assert "homed=xyz x=True y=True z=True z1=True" in idle_off
    assert "bed=0.0 hotend=0.0" in idle_off
    # RESUME reheats the previous targets
    assert "bed=60.0 hotend=200.0" in resumed
    assert "homed=xyz" in resumed


def test_idle_timeout_action_is_the_motor_policy_macro(flavor):
    """The idle_timeout section must run the macro that keeps a pause's motors
    energised; a different gcode there would silently drop the policy."""
    text = (CONFIG / "printer.cfg.tmpl").read_text()
    m = re.search(r"^\[idle_timeout\]\n(?:[^\[]*?)gcode:\n\s+(\S+)", text, re.M)
    assert m and m.group(1) == "_QD_IDLE_OFF"


def test_idle_timeout_still_releases_the_motors_when_not_paused(env_factory):
    env = env_factory()
    res = env.run("G28\n_QD_IDLE_OFF\nQD_PURGE HOTEND=210")
    assert res.returncode != 0 and "needs the printer homed" in res.log


def test_resume_refuses_before_heating_when_the_position_is_lost(env_factory):
    env = env_factory()
    res = env.run("""
G28
M140 S60
M104 S200
PAUSE
M84
RESUME
""")
    assert res.returncode != 0
    assert "RESUME needs the printer homed" in res.log
    # the error came before any re-heat wait
    tail = res.log.split("RESUME needs the printer homed")[0]
    assert "stub TEMPERATURE_WAIT" not in tail
    # ... whereas a RESUME that can proceed does wait for the heaters
    ok_run = env.run("G28\nM140 S60\nM104 S200\nPAUSE\nRESUME")
    ok(ok_run)
    assert "stub TEMPERATURE_WAIT SENSOR=extruder" in ok_run.log


# --- M290 / saved Z offset --------------------------------------------------------

def test_m290_is_limited_to_the_zoffset_range(env_factory):
    env = env_factory()
    ok(env.run("G28\nM290 Z-0.05\nM290 Z0.1"))
    for cmd in ("M290 Z-3", "M290 Z2"):
        res = env.run("G28\nSET_GCODE_OFFSET Z=-0.5 MOVE=0\n" + cmd)
        assert res.returncode != 0 and "M290" in res.log, cmd
    res = env.run("M290 Z-0.1")
    assert res.returncode != 0 and "needs the printer homed" in res.log


def test_saved_zoffset_is_dropped_after_probe_recalibration(env_factory):
    env = env_factory()
    saved = env.pd / "config" / "saved_variables.cfg"
    ok(env.run("SET_GCODE_OFFSET Z=-0.085 MOVE=0\nQD_SAVE_ZOFFSET"))
    text = saved.read_text()
    assert "z_offset = -0.085" in text and "z_offset_probe_ref = 1.5" in text
    # same calibration: applied
    res = env.run("QD_LOAD_ZOFFSET\nQD_SAVE_ZOFFSET")
    ok(res)
    assert "z_offset = -0.085" in saved.read_text()
    # PROBE_CALIBRATE + SAVE_CONFIG changed the probe z_offset
    cfg = env.cfg_path.read_text()
    assert "z_offset: 1.5" in cfg
    env.cfg_path.write_text(cfg.replace("z_offset: 1.5", "z_offset: 1.62", 1))
    res = env.run("QD_LOAD_ZOFFSET\nGET_POSITION")
    ok(res)
    assert "Saved Z offset -0.0850 discarded" in res.log
    text = saved.read_text()
    assert "z_offset = 0.0" in text and "z_offset_probe_ref = 1.62" in text
    # and the next print starts clean
    res = env.run("QD_LOAD_ZOFFSET")
    ok(res)
    assert "discarded" not in res.log


def _interrupted_print(env, job):
    """Run `job` until the host is SIGKILLed mid-print; return its record."""
    # PRINTER_INIT (BED_MESH_CLEAR ...) is queued 0.2 s after startup; in batch
    # mode the job is already running by then and the init would run in the
    # middle of it, clearing the mesh under test.
    env.append("[delayed_gcode PRINTER_INIT]\ninitial_duration: 0\n"
               "gcode:\n    G4 P1\n")
    env.link_extra("qd_shell_test.py", "gcode_shell_command.py")
    env.append("[qd_shell_test pad]\ncommand: /bin/sleep\nallow_params: True\ntimeout: 10\n"
               "verbose: False\n"
               "[qd_shell_test powercut]\ncommand: /bin/sh -c 'kill -9 $PPID'\n"
               "verbose: False\n")
    # sample every second so the short job is recorded after its bed mesh
    cfg = env.cfg_path.read_text()
    assert "save_interval: 10" in cfg
    env.cfg_path.write_text(cfg.replace("save_interval: 10",
                                        "save_interval: 1", 1))
    (env.gcodes / "job.gcode").write_text(job)
    first = env.run("SDCARD_PRINT_FILE FILENAME=job.gcode\n",
                    wait_for=r"never matches")
    assert first.returncode == -9, first.log[-2000:]
    env.append("[qd_shell_test powercut]\ncommand: /bin/true\n")
    return json.loads((env.gcodes / ".plr" / "state.json").read_text())


MESH_JOB = PLR_JOB.replace(
    "G28\n", "G28\nBED_MESH_CALIBRATE PROFILE=adaptive ADAPTIVE=1\n", 1)


def test_power_loss_resume_rebuilds_the_unsaved_adaptive_mesh(env_factory):
    """The default adaptive mesh exists only in RAM, so a restart has no such
    profile: the record carries the mesh and the resume re-creates it."""
    env = env_factory()
    rec = _interrupted_print(env, MESH_JOB)
    assert rec["bed_mesh_profile"] == "adaptive", rec
    assert rec["bed_mesh"]["points"] and rec["bed_mesh"]["mesh_params"]
    res = env.run("QD_PLR_RESUME CONFIRM=1\n",
                  wait_for=r"Exiting SD card print")
    assert res.returncode == 0, res.log[-3000:]
    assert res.errors() == [], res.errors()
    head = (env.gcodes / ".plr" / "resume-a.gcode").read_text()
    assert "BED_MESH_PROFILE LOAD=plr-resume" in head
    assert "LOAD=adaptive" not in head


def test_power_loss_resume_without_a_recoverable_mesh_refuses_first(env_factory):
    env = env_factory()
    rec = _interrupted_print(env, MESH_JOB)
    state = env.gcodes / ".plr" / "state.json"
    rec.pop("bed_mesh")                    # a record from before the mesh was kept
    state.write_text(json.dumps(rec))
    res = env.run("QD_PLR_RESUME CONFIRM=1\n")
    assert res.returncode != 0
    assert "never saved" in res.log and "Nothing was heated or moved" in res.log
    assert not (env.gcodes / ".plr" / "resume-a.gcode").exists()


def test_power_loss_resume_keeps_a_homed_z(env_factory):
    env = env_factory()
    _interrupted_print(env, PLR_JOB)
    res = env.run("G28\nQD_PLR_RESUME CONFIRM=1\n",
                  wait_for=r"Exiting SD card print")
    assert res.returncode == 0, res.log[-3000:]
    assert res.errors() == [], res.errors()
    head = (env.gcodes / ".plr" / "resume-a.gcode").read_text()
    assert "SET_KINEMATIC_POSITION" not in head
    assert "Z is already homed" in res.log


SQUARE = "EXCLUDE_OBJECT_DEFINE NAME=part CENTER=150,150 POLYGON=[[120,130],[180,130],[180,170],[120,170]]"


def _said(res, text):
    # At a line start: the log also holds the config, i.e. the macro's own source.
    return re.search(r"^" + re.escape(text), res.log, re.M) is not None


def test_prime_line_in_front_of_the_print(env_factory):
    """QD_PRIME_LINE runs during PRINT_START and lays its line prime_gap in
    front of the labelled objects, starting at their left edge."""
    env = env_factory()
    res = env.run(SQUARE + """
PRINT_START BED=60 HOTEND=210 MESH=skip SOAK=0
QD_PRIME_LINE
GET_POSITION
""")
    ok(res)
    assert not _said(res, "QD_PRIME_LINE: no labelled objects")
    # Ends at x0 + prime_len (120 + 40), y = 130 - 5, z = 0.3 + 2
    assert re.search(r"^toolhead: X:160\.0+ Y:125\.0+ Z:2\.30*", res.log, re.M), res.log[-2000:]


def test_prime_line_skipped_without_objects(env_factory):
    env = env_factory()
    res = env.run("PRINT_START BED=60 HOTEND=210 MESH=skip SOAK=0")
    ok(res)
    assert _said(res, "QD_PRIME_LINE: no labelled objects in this file; skipped")


def test_prime_line_skipped_when_print_touches_the_front(env_factory):
    env = env_factory()
    res = env.run("""EXCLUDE_OBJECT_DEFINE NAME=front CENTER=150,10 POLYGON=[[120,3],[180,3],[180,20],[120,20]]
PRINT_START BED=60 HOTEND=210 MESH=skip SOAK=0
""")
    ok(res)
    assert _said(res, "QD_PRIME_LINE: no room in front of the print; skipped")


# --- the printer as shipped: both boards on QIDI's Klipper v0.12 firmware ---------------------------

STOCK = kh.pathlib.Path(__file__).parent / "fixtures" / "stock-dicts"
STOCK_DICTS = (STOCK / "toolhead-qidi-v0.12.dict", STOCK / "mainboard-qidi-v0.12.dict")


def test_stock_firmware_runs_the_eitri_config(env_factory):
    """The whole config, with the v0.12 accelerometer driver, against the real stock dictionaries."""
    env = env_factory(dicts=STOCK_DICTS, adxl_stock="true")
    res = env.run("""
PRINT_START BED=60 HOTEND=210 SOAK=0
G1 X100 Y100 F6000
PRINT_END
""")
    ok(res)
    assert "accel_chip = qd_adxl345_stock" in res.log


def test_stock_firmware_rejects_klippers_own_adxl345(env_factory):
    """Why qd_adxl345_stock exists: current Klipper's [adxl345] cannot talk to the v0.12 toolhead."""
    env = env_factory(dicts=STOCK_DICTS, adxl_stock="false")
    res = env.run("G4 P10")
    assert "query_adxl345" in res.log and "mismatch" in res.log, res.log[-2000:]


def test_v012_accelerometer_driver_refuses_current_firmware(env_factory):
    env = env_factory(adxl_stock="true")      # generic dictionary = current firmware
    res = env.run("G4 P10")
    assert "qd_adxl345_stock: MCU 'mcu' does not run Klipper v0.12 firmware" in res.log, res.log[-2000:]
