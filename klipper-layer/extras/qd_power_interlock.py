# Chamber heater <-> bed heater power interlock
#
# The QIDI X-Plus 4 powers the chamber heater from the same supply budget as
# the bed heater. Qidi's fork enforced this by editing heaters.py; here the
# same rule is applied without touching any Klipper file:
#
#   * The chamber heater is only powered while the bed target is at least
#     bed_min_target and the bed temperature is within bed_tolerance of that
#     target. Otherwise the chamber PWM output is forced to zero.
#   * QD_SET_CHAMBER (used by the M141/M191 macros) raises the bed target to
#     chamber target + bed_offset first and only arms the chamber heater once
#     the bed is ready, so the stock verify_heater check never sees a heater
#     that was asked to heat but was held off.
#
#   * If the gate closes while the chamber is already armed (a slicer lowers
#     M140 after the first layers, the bed sags, ...), the chamber target is
#     parked at 0 after park_delay seconds and restored once the bed is back in
#     its window. Without that, the chamber would sit gated off with a target
#     it cannot reach until the stock [verify_heater chamber] check ends the
#     print with a shutdown. A bed target of 0 is a real turn-off: the chamber
#     is switched off and nothing is restored.
#
# SAFETY: this module only ever *removes* heater power. It does not touch
# [verify_heater chamber] or any other heater check, it does not change the
# chamber heater's PID, and it fails closed: if the gate cannot be installed
# the printer refuses to start, and if the monitoring timer stops the gate
# closes itself after stale_timeout seconds.
#
# Mechanism: at klippy:connect the chamber Heater's mcu_pwm object is wrapped
# in a proxy whose set_pwm() clamps the duty to 0 while the interlock is open.
# The proxy is invoked from the MCU serial thread, so the decision is a single
# tuple (allowed, timestamp) refreshed from the main-thread timer; the serial
# thread only reads it. This keeps all toolhead/heater state access on the
# reactor thread (the fork read the toolhead from the sensor thread).
#
# The gate sits on the heater output, so SET_HEATER_TEMPERATURE HEATER=chamber
# is also gated, but it does NOT raise the bed first and is not range-checked
# like QD_SET_CHAMBER. Use QD_SET_CHAMBER / M141 for normal operation.
#
# PID_CALIBRATE HEATER=chamber goes through the same gate, so run it only once
# the bed is in its window (for example M141 S55 first and wait for the bed).
#
# Works on Klipper and Kalico: both build Heater.mcu_pwm from the same
# setup_pin('pwm') call and route every output through mcu_pwm.set_pwm().
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import time

TIMER_INTERVAL = 0.5
WAIT_POLL_INTERVAL = 1.0


class _GatedPwm:
    """Proxy for a heater's MCU_pwm that forces 0 while the gate is closed."""
    def __init__(self, pwm, interlock):
        self._pwm = pwm
        self._interlock = interlock

    def set_pwm(self, print_time, value):
        if value > 0. and not self._interlock.power_allowed():
            self._interlock.note_blocked()
            value = 0.
        self._pwm.set_pwm(print_time, value)

    def __getattr__(self, name):
        return getattr(self._pwm, name)


class QdPowerInterlock:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.printer.load_object(config, "heaters")
        self.chamber_name = config.get("chamber_heater", "chamber").split()[-1]
        self.bed_name = config.get("bed_heater", "heater_bed").split()[-1]
        self.bed_min_target = config.getfloat("bed_min_target", 70., above=0.)
        self.bed_tolerance = config.getfloat("bed_tolerance", 2., minval=0.)
        self.bed_hysteresis = config.getfloat("bed_hysteresis", 1., minval=0.)
        self.bed_offset = config.getfloat("bed_offset", 25., minval=0.)
        self.chamber_min_target = config.getfloat("chamber_min_target", 0.,
                                                  minval=0.)
        self.chamber_max_target = config.getfloat(
            "chamber_max_target", 0., minval=0.)
        self.blocked_timeout = config.getfloat("blocked_timeout", 900.,
                                               minval=0.)
        self.wait_tolerance = config.getfloat("wait_tolerance", 2., minval=0.)
        self.wait_timeout = config.getfloat("wait_timeout", 3600., minval=0.)
        self.stale_timeout = config.getfloat("stale_timeout", 3., above=0.)
        # How long an armed chamber may be gated off before its target is
        # parked. Must stay well below the verify_heater check window.
        self.park_delay = config.getfloat("park_delay", 30., minval=0.)
        self.chamber = self.bed = self.pheaters = None
        self.gate = (False, 0.)          # (allowed, monotonic timestamp)
        self.reason = "starting"
        self.pending_target = None       # chamber target waiting for the bed
        self.parked_target = None        # armed target parked while gated off
        self.closed_since = None         # armed chamber, gate closed since
        self.blocked_since = None
        self.blocked_count = 0
        self.timer = None
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
        self.gcode.register_command(
            "QD_SET_CHAMBER", self.cmd_QD_SET_CHAMBER,
            desc=self.cmd_QD_SET_CHAMBER_help)
        self.gcode.register_command(
            "QD_POWER_INTERLOCK_STATUS", self.cmd_QD_POWER_INTERLOCK_STATUS,
            desc=self.cmd_QD_POWER_INTERLOCK_STATUS_help)

    # Setup -----------------------------------------------------------

    def _handle_connect(self):
        self.pheaters = self.printer.lookup_object("heaters")
        self.chamber = self.pheaters.lookup_heater(self.chamber_name)
        self.bed = self.pheaters.lookup_heater(self.bed_name)
        if self.chamber_max_target <= 0.:
            self.chamber_max_target = self.chamber.max_temp
        pwm = getattr(self.chamber, "mcu_pwm", None)
        if pwm is None or not hasattr(pwm, "set_pwm"):
            # Fail closed: never run an un-interlocked chamber heater
            raise self.printer.config_error(
                "qd_power_interlock cannot attach to heater '%s': this "
                "Klipper/Kalico version does not expose Heater.mcu_pwm"
                % (self.chamber_name,))
        if not isinstance(pwm, _GatedPwm):
            self.chamber.mcu_pwm = _GatedPwm(pwm, self)

    def _handle_ready(self):
        reactor = self.printer.get_reactor()
        self.timer = reactor.register_timer(self._timer_event, reactor.NOW)

    def _handle_shutdown(self):
        self._close_gate("printer shutdown")

    # Gate (called from the MCU serial thread) -------------------------

    def power_allowed(self):
        allowed, stamp = self.gate
        return allowed and (time.monotonic() - stamp) <= self.stale_timeout

    def note_blocked(self):
        self.blocked_count += 1

    def _close_gate(self, reason):
        self.reason = reason
        self.gate = (False, time.monotonic())

    def _kick_heater(self):
        # Heater.set_pwm() suppresses updates that look unchanged; clearing
        # next_pwm_time makes the next sensor report re-send the (now zero)
        # duty immediately instead of up to MAX_HEAT_TIME later.
        if hasattr(self.chamber, "next_pwm_time"):
            self.chamber.next_pwm_time = 0.

    # Interlock logic (main thread) ------------------------------------

    def _evaluate(self, eventtime):
        bed_temp, bed_target = self.bed.get_temp(eventtime)
        was_allowed = self.gate[0]
        floor = bed_target - self.bed_tolerance
        if was_allowed:
            floor -= self.bed_hysteresis
        if bed_target < self.bed_min_target:
            allowed, reason = False, ("bed target %.0f below %.0f"
                                      % (bed_target, self.bed_min_target))
        elif bed_temp < floor:
            allowed, reason = False, ("bed %.1f not within %.1f of target %.0f"
                                      % (bed_temp, self.bed_tolerance,
                                         bed_target))
        else:
            allowed, reason = True, "ok"
        self.reason = reason
        self.gate = (allowed, time.monotonic())
        if was_allowed and not allowed:
            self._kick_heater()
        return allowed, bed_target

    def _timer_event(self, eventtime):
        try:
            self._tick(eventtime)
        except Exception:
            logging.exception("qd_power_interlock: monitor error")
            self._close_gate("monitor error")
            self._kick_heater()
        return eventtime + TIMER_INTERVAL

    def _tick(self, eventtime):
        allowed, bed_target = self._evaluate(eventtime)
        chamber_target = self.chamber.get_temp(eventtime)[1]
        if self.pending_target is not None:
            if bed_target < self.bed_min_target:
                # Someone (TURN_OFF_HEATERS, M140 S0, ...) turned the bed off
                # after we raised it: the request is stale, drop it.
                self._respond("Chamber request %.0fC dropped: bed was turned "
                              "off" % (self.pending_target,))
                self.pending_target = None
            elif allowed:
                target, self.pending_target = self.pending_target, None
                self.pheaters.set_temperature(self.chamber, target)
                chamber_target = target
        if self.parked_target is not None:
            if chamber_target > 0.:
                # The chamber was set again meanwhile; that request wins
                self.parked_target = None
            elif allowed:
                target, self.parked_target = self.parked_target, None
                self.pheaters.set_temperature(self.chamber, target)
                chamber_target = target
                self._respond("Chamber heater resumed at %.0fC: bed is back "
                              "in its interlock window" % (target,))
        if chamber_target > 0. and not allowed:
            if self.closed_since is None:
                self.closed_since = eventtime
            elif eventtime - self.closed_since > self.park_delay:
                self._park(chamber_target, bed_target)
                chamber_target = 0.
                self.closed_since = None
        else:
            self.closed_since = None
        wanted = chamber_target > 0. or self.pending_target is not None
        if wanted and not allowed:
            if self.blocked_since is None:
                self.blocked_since = eventtime
            elif (self.blocked_timeout
                  and eventtime - self.blocked_since > self.blocked_timeout):
                self.pending_target = None
                self.pheaters.set_temperature(self.chamber, 0.)
                self.blocked_since = None
                self._respond("Chamber heater turned off: bed did not reach "
                              "its interlock window within %d s (%s)"
                              % (self.blocked_timeout, self.reason))
        else:
            self.blocked_since = None

    def _park(self, chamber_target, bed_target):
        """Take an armed chamber target off while the gate is closed."""
        self.pheaters.set_temperature(self.chamber, 0.)
        if bed_target > 0.:
            self.parked_target = chamber_target
            self._respond("Chamber heater paused (target %.0fC kept): %s. It "
                          "resumes when the bed is back in its interlock "
                          "window." % (chamber_target, self.reason))
        else:
            self.parked_target = None
            self._respond("Chamber heater turned off: the bed was turned off "
                          "(%s)" % (self.reason,))

    def _respond(self, msg):
        logging.info("qd_power_interlock: %s", msg)
        self.gcode.respond_info(msg)

    # G-Code ----------------------------------------------------------

    cmd_QD_SET_CHAMBER_help = (
        "Set the chamber target through the bed interlock: QD_SET_CHAMBER "
        "TARGET=<C> [WAIT=1] [TIMEOUT=<s>]")

    def cmd_QD_SET_CHAMBER(self, gcmd):
        target = gcmd.get_float("TARGET", minval=0.)
        wait = gcmd.get_int("WAIT", 0, minval=0, maxval=1)
        timeout = gcmd.get_float("TIMEOUT", self.wait_timeout, minval=0.)
        self.parked_target = None
        if target == 0.:
            self.pending_target = None
            self.pheaters.set_temperature(self.chamber, 0.)
            return
        if target < self.chamber_min_target or target > self.chamber_max_target:
            raise gcmd.error("Chamber target %.1f out of range (%.1f:%.1f)"
                             % (target, self.chamber_min_target,
                                self.chamber_max_target))
        bed_needed = max(self.bed_min_target, target + self.bed_offset)
        if bed_needed > self.bed.max_temp:
            raise gcmd.error("Chamber target %.1f needs a bed target of %.1f, "
                             "above the bed's max_temp %.1f"
                             % (target, bed_needed, self.bed.max_temp))
        eventtime = self.printer.get_reactor().monotonic()
        if self.bed.get_temp(eventtime)[1] < bed_needed:
            gcmd.respond_info("Raising bed target to %.0fC for the chamber "
                              "interlock" % (bed_needed,))
            self.pheaters.set_temperature(self.bed, bed_needed)
        allowed, _ = self._evaluate(eventtime)
        armed = self.chamber.get_temp(eventtime)[1] > 0.
        if allowed or armed:
            self.pending_target = None
            self.pheaters.set_temperature(self.chamber, target)
        else:
            self.pending_target = target
            gcmd.respond_info("Chamber target %.0fC will be applied once the "
                              "bed is ready (%s)" % (target, self.reason))
        if wait:
            self._wait_for_chamber(gcmd, target, timeout)

    def _wait_for_chamber(self, gcmd, target, timeout):
        reactor = self.printer.get_reactor()
        start = eventtime = reactor.monotonic()
        while not self.printer.is_shutdown():
            temp, cur_target = self.chamber.get_temp(eventtime)
            # A parked target is still wanted: keep waiting (until timeout)
            if (self.pending_target is None and self.parked_target is None
                    and cur_target != target):
                raise gcmd.error("Chamber heating cancelled (%s)"
                                 % (self.reason,))
            if self.pending_target is None and temp >= (target
                                                        - self.wait_tolerance):
                return
            if timeout and eventtime - start > timeout:
                raise gcmd.error("Timed out after %d s waiting for chamber "
                                 "%.0fC (now %.1fC, interlock: %s)"
                                 % (timeout, target, temp, self.reason))
            gcmd.respond_raw("C:%.1f /%.1f interlock:%s"
                             % (temp, target, self.reason))
            eventtime = reactor.pause(eventtime + WAIT_POLL_INTERVAL)

    cmd_QD_POWER_INTERLOCK_STATUS_help = "Report the chamber power interlock"

    def cmd_QD_POWER_INTERLOCK_STATUS(self, gcmd):
        st = self.get_status(self.printer.get_reactor().monotonic())
        gcmd.respond_info(
            "qd_power_interlock: chamber power %s (%s); pending target: %s; "
            "PWM updates blocked: %d"
            % ("ALLOWED" if st["power_allowed"] else "BLOCKED", st["reason"],
               st["pending_target"], st["blocked_count"]))

    def get_status(self, eventtime):
        return {
            "power_allowed": self.power_allowed(),
            "reason": self.reason,
            "pending_target": self.pending_target,
            "parked_target": self.parked_target,
            "blocked_count": self.blocked_count,
            "bed_min_target": self.bed_min_target,
            "chamber_min_target": self.chamber_min_target,
            "chamber_max_target": self.chamber_max_target,
        }


def load_config(config):
    return QdPowerInterlock(config)
