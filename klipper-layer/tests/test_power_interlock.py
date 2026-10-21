"""qd_power_interlock decision logic and fail-closed behaviour (fake Klipper)."""
import pytest

import fakes
from fakes import FakeConfig, FakeGcmd, FakeHeater, FakeHeaters, FakePrinter

mod = fakes.load_extra("qd_power_interlock")


def build(**opts):
    printer = FakePrinter()
    bed = FakeHeater(max_temp=125.)
    chamber = FakeHeater(max_temp=70.)
    heaters = FakeHeaters(heater_bed=bed, chamber=chamber)
    printer.objects["heaters"] = heaters
    cfg = FakeConfig(opts, "qd_power_interlock", printer)
    il = mod.QdPowerInterlock(cfg)
    original_pwm = chamber.mcu_pwm
    for cb in printer.handlers["klippy:connect"]:
        cb()
    return il, printer, bed, chamber, heaters, original_pwm


def tick(il, printer):
    il._tick(printer.reactor.monotonic())


def set_bed(bed, temp, target):
    bed.temp, bed.target = temp, target


def test_gate_is_closed_until_the_bed_is_ready():
    il, printer, bed, chamber, heaters, raw = build()
    chamber.target = 55.
    chamber.mcu_pwm.set_pwm(1., 0.4)
    assert raw.calls == [(1., 0.)]            # forced to zero, not forwarded
    set_bed(bed, 79.5, 80.)                   # within tolerance, target >= 70
    tick(il, printer)
    chamber.mcu_pwm.set_pwm(2., 0.4)
    assert raw.calls[-1] == (2., 0.4)


@pytest.mark.parametrize("temp,target", [
    (60., 60.),     # bed target below bed_min_target
    (70., 80.),     # target fine, bed 10 degrees short
    (0., 80.),      # stale sensor reads as 0
])
def test_gate_stays_closed(temp, target):
    il, printer, bed, chamber, heaters, raw = build()
    set_bed(bed, temp, target)
    tick(il, printer)
    assert not il.power_allowed()
    chamber.mcu_pwm.set_pwm(1., 1.0)
    assert raw.calls[-1][1] == 0.


def test_hysteresis_prevents_chatter():
    il, printer, bed, chamber, heaters, raw = build()
    set_bed(bed, 80., 80.)
    tick(il, printer)
    assert il.power_allowed()
    set_bed(bed, 77.4, 80.)       # below target - 2 but inside the hysteresis
    tick(il, printer)
    assert il.power_allowed()
    set_bed(bed, 76.9, 80.)       # now clearly out
    tick(il, printer)
    assert not il.power_allowed()
    set_bed(bed, 77.5, 80.)       # must climb back to target - tolerance
    tick(il, printer)
    assert not il.power_allowed()
    set_bed(bed, 78.0, 80.)
    tick(il, printer)
    assert il.power_allowed()


def test_zero_duty_always_passes_through():
    il, printer, bed, chamber, heaters, raw = build()
    chamber.mcu_pwm.set_pwm(1., 0.)
    assert raw.calls == [(1., 0.)]
    assert il.blocked_count == 0


def test_stale_monitor_closes_the_gate(monkeypatch):
    il, printer, bed, chamber, heaters, raw = build(stale_timeout=3.)
    set_bed(bed, 80., 80.)
    clock = [100.]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    tick(il, printer)
    assert il.power_allowed()
    clock[0] += 3.5               # the main-thread timer stopped running
    assert not il.power_allowed()
    chamber.mcu_pwm.set_pwm(5., 0.4)
    assert raw.calls[-1][1] == 0.


def test_closing_the_gate_forces_a_pwm_refresh():
    il, printer, bed, chamber, heaters, raw = build()
    set_bed(bed, 80., 80.)
    tick(il, printer)
    chamber.next_pwm_time = 999.
    set_bed(bed, 70., 80.)
    tick(il, printer)
    assert chamber.next_pwm_time == 0.


def test_shutdown_closes_the_gate():
    il, printer, bed, chamber, heaters, raw = build()
    set_bed(bed, 80., 80.)
    tick(il, printer)
    for cb in printer.handlers["klippy:shutdown"]:
        cb()
    assert not il.power_allowed()


def test_fail_closed_when_heater_has_no_pwm_object():
    printer = FakePrinter()
    chamber = FakeHeater()
    del chamber.mcu_pwm
    printer.objects["heaters"] = FakeHeaters(heater_bed=FakeHeater(125.),
                                             chamber=chamber)
    mod.QdPowerInterlock(FakeConfig({}, "qd_power_interlock", printer))
    with pytest.raises(fakes.ConfigError, match="cannot attach"):
        for cb in printer.handlers["klippy:connect"]:
            cb()


def test_wrapping_is_not_applied_twice():
    il, printer, bed, chamber, heaters, raw = build()
    first = chamber.mcu_pwm
    for cb in printer.handlers["klippy:connect"]:
        cb()
    assert chamber.mcu_pwm is first
    assert chamber.mcu_pwm.get_mcu() == "mcu"        # attribute passthrough


def test_set_chamber_raises_bed_then_arms_when_ready():
    il, printer, bed, chamber, heaters, raw = build(chamber_max_target=65)
    gcmd = FakeGcmd({"TARGET": 55})
    il.cmd_QD_SET_CHAMBER(gcmd)
    assert bed.target == 80.                    # 55 + bed_offset(25)
    assert chamber.target == 0.                 # not armed yet
    assert il.pending_target == 55.
    set_bed(bed, 79., 80.)
    tick(il, printer)
    assert chamber.target == 55.
    assert il.pending_target is None


def test_set_chamber_rejects_out_of_range_and_unreachable_bed():
    il, printer, bed, chamber, heaters, raw = build(
        chamber_min_target=45, chamber_max_target=65)
    with pytest.raises(fakes.ConfigError, match="out of range"):
        il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 70}))
    with pytest.raises(fakes.ConfigError, match="out of range"):
        il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 30}))
    bed.max_temp = 85.
    with pytest.raises(fakes.ConfigError, match="max_temp"):
        il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 65}))     # needs 90
    assert chamber.target == 0.


def test_pending_request_dies_when_the_bed_is_turned_off():
    il, printer, bed, chamber, heaters, raw = build(chamber_max_target=65)
    il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 50}))
    assert il.pending_target == 50.
    set_bed(bed, 30., 0.)                  # e.g. TURN_OFF_HEATERS ran
    tick(il, printer)
    assert il.pending_target is None
    set_bed(bed, 80., 80.)                 # bed heated again later by hand
    tick(il, printer)
    assert chamber.target == 0.            # the stale request did not revive


def test_blocked_too_long_turns_the_chamber_off(monkeypatch):
    # park_delay is pushed out so this exercises the blocked_timeout fallback
    il, printer, bed, chamber, heaters, raw = build(blocked_timeout=60.,
                                                    park_delay=1e6)
    chamber.target = 50.
    set_bed(bed, 40., 80.)
    printer.reactor.now = 1000.
    tick(il, printer)
    assert chamber.target == 50.
    printer.reactor.now = 1061.
    tick(il, printer)
    assert chamber.target == 0.
    assert any("turned off" in m for m in printer.gcode.infos)


def test_set_chamber_zero_clears_pending_and_target():
    il, printer, bed, chamber, heaters, raw = build(chamber_max_target=65)
    il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 50}))
    il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 0}))
    assert il.pending_target is None and chamber.target == 0.


def test_wait_returns_when_chamber_reaches_target_and_aborts_when_cancelled():
    il, printer, bed, chamber, heaters, raw = build(chamber_max_target=65)
    set_bed(bed, 80., 80.)
    tick(il, printer)
    chamber.temp = 54.
    il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 55, "WAIT": 1}))   # within 2 C
    # cancelled: the target is wiped while waiting
    chamber.temp = 30.
    gcmd = FakeGcmd({"TARGET": 55, "WAIT": 1})
    original_pause = printer.reactor.pause

    def pause_then_cancel(t):
        chamber.target = 0.
        return original_pause(t)
    printer.reactor.pause = pause_then_cancel
    with pytest.raises(fakes.ConfigError, match="cancelled"):
        il.cmd_QD_SET_CHAMBER(gcmd)


def test_wait_times_out():
    il, printer, bed, chamber, heaters, raw = build(chamber_max_target=65)
    set_bed(bed, 80., 80.)
    tick(il, printer)
    chamber.temp = 30.
    with pytest.raises(fakes.ConfigError, match="Timed out"):
        il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 55, "WAIT": 1,
                                        "TIMEOUT": 5}))


# --- the gate closing while the chamber is already armed --------------------

def armed_print(**opts):
    """Chamber armed at 55 C with the bed in its window, mid-print."""
    il, printer, bed, chamber, heaters, raw = build(
        chamber_max_target=65, park_delay=30., **opts)
    set_bed(bed, 80., 80.)
    chamber.target = 55.
    printer.reactor.now = 1000.
    tick(il, printer)
    assert il.power_allowed() and chamber.target == 55.
    return il, printer, bed, chamber, heaters, raw


def test_lowered_bed_target_parks_the_chamber_then_restores_it():
    il, printer, bed, chamber, heaters, raw = armed_print()
    set_bed(bed, 75., 60.)             # slicer lowers M140 below bed_min_target
    printer.reactor.now = 1001.
    tick(il, printer)
    assert not il.power_allowed()
    assert chamber.target == 55.       # inside the grace period: untouched
    printer.reactor.now = 1032.
    tick(il, printer)                  # grace over, well before verify_heater
    assert chamber.target == 0. and il.parked_target == 55.
    assert any("paused" in m and "55" in m for m in printer.gcode.infos)
    printer.reactor.now = 1100.
    tick(il, printer)
    assert chamber.target == 0.        # bed still outside its window
    set_bed(bed, 80., 80.)             # bed target raised again
    printer.reactor.now = 1101.
    tick(il, printer)
    assert il.power_allowed()
    assert chamber.target == 55. and il.parked_target is None
    assert any("resumed" in m for m in printer.gcode.infos)


def test_short_bed_dip_inside_the_grace_period_changes_nothing():
    il, printer, bed, chamber, heaters, raw = armed_print()
    set_bed(bed, 70., 80.)
    printer.reactor.now = 1010.
    tick(il, printer)
    assert not il.power_allowed()
    set_bed(bed, 80., 80.)
    printer.reactor.now = 1020.
    tick(il, printer)
    printer.reactor.now = 1100.
    tick(il, printer)
    assert chamber.target == 55. and il.parked_target is None


def test_bed_turned_off_turns_the_chamber_off_and_is_not_restored():
    il, printer, bed, chamber, heaters, raw = armed_print()
    set_bed(bed, 60., 0.)              # a real turn-off of the bed
    printer.reactor.now = 1001.
    tick(il, printer)
    printer.reactor.now = 1040.
    tick(il, printer)
    assert chamber.target == 0. and il.parked_target is None
    assert any("turned off" in m for m in printer.gcode.infos)
    set_bed(bed, 80., 80.)             # heated again by hand much later
    printer.reactor.now = 1100.
    tick(il, printer)
    assert chamber.target == 0.


def test_parked_target_yields_to_a_new_request():
    il, printer, bed, chamber, heaters, raw = armed_print()
    set_bed(bed, 75., 60.)
    printer.reactor.now = 1001.
    tick(il, printer)
    printer.reactor.now = 1040.
    tick(il, printer)
    assert il.parked_target == 55.
    il.cmd_QD_SET_CHAMBER(FakeGcmd({"TARGET": 0}))   # M141 S0 at print end
    assert il.parked_target is None
    set_bed(bed, 80., 80.)
    printer.reactor.now = 1100.
    tick(il, printer)
    assert chamber.target == 0.


def test_wait_keeps_waiting_while_the_target_is_parked():
    il, printer, bed, chamber, heaters, raw = armed_print()
    set_bed(bed, 75., 60.)
    printer.reactor.now = 1001.
    tick(il, printer)
    printer.reactor.now = 1040.
    tick(il, printer)
    assert il.parked_target == 55.
    chamber.temp = 30.
    gcmd = FakeGcmd({"TARGET": 55, "WAIT": 1})
    # call the waiter directly: a parked target is not "cancelled", it times out
    with pytest.raises(fakes.ConfigError, match="Timed out"):
        il._wait_for_chamber(gcmd, 55., 5.)
