# Chamber exhaust/circulation fan that follows chamber heater activity
#
# Behaves like Klipper's [heater_fan] but is driven by heater *targets*
# (the fan runs whenever any listed heater has a target) and keeps running at
# a reduced idle speed for a while after the last target is cleared, which is
# what the QIDI Plus 4 chamber fan needs. TOGGLE_CHAMBER_FAN lets a macro
# silence the fan temporarily (e.g. while the nozzle-contact probe is active).
#
# Adapted from the chamber_fan module of QIDI's public Klipper fork
# (https://github.com/QIDITECH/klipper, GPLv3), itself a clone of
# Klipper's heater_fan.py. Reworked here: idle time is measured with the
# reactor clock instead of counting timer ticks, speed changes go through
# fan.Fan's request queue, and the toggle command is a mux command so several
# chamber fans can coexist.
#
# Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-only
# (Derived from GPLv3 code, so this file is GPL-3.0-only, not "or later".)
# This file may be distributed under the terms of the GNU GPLv3 license.
from . import fan

UPDATE_INTERVAL = 1.0
START_DELAY = 0.1


class QdChamberFan:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.printer.load_object(config, "heaters")
        self.heater_names = [n.split()[-1] for n in config.getlist("heater")]
        if not self.heater_names:
            raise config.error("qd_chamber_fan %s: 'heater' is required"
                               % (self.name,))
        self.heaters = []
        self.fan = fan.Fan(config)
        self.fan_speed = config.getfloat("fan_speed", 1., minval=0.,
                                         maxval=1.)
        self.idle_speed = config.getfloat("idle_speed", self.fan_speed,
                                          minval=0., maxval=1.)
        self.idle_timeout = config.getfloat("idle_timeout", 30., minval=0.)
        self.enabled = True
        self.last_active_time = None
        self.last_speed = 0.
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "TOGGLE_CHAMBER_FAN", "FAN", self.name, self.cmd_TOGGLE_CHAMBER_FAN,
            desc=self.cmd_TOGGLE_CHAMBER_FAN_help)
        try:
            # The first chamber fan also answers a bare TOGGLE_CHAMBER_FAN
            gcode.register_mux_command(
                "TOGGLE_CHAMBER_FAN", "FAN", None,
                self.cmd_TOGGLE_CHAMBER_FAN,
                desc=self.cmd_TOGGLE_CHAMBER_FAN_help)
        except self.printer.config_error:
            pass

    def _handle_ready(self):
        pheaters = self.printer.lookup_object("heaters")
        self.heaters = [pheaters.lookup_heater(n) for n in self.heater_names]
        reactor = self.printer.get_reactor()
        reactor.register_timer(self._callback,
                               reactor.monotonic() + START_DELAY)

    def _wanted_speed(self, eventtime):
        if not self.enabled:
            return 0.
        if any(h.get_temp(eventtime)[1] for h in self.heaters):
            self.last_active_time = eventtime
            return self.fan_speed
        if (self.last_active_time is not None
                and eventtime - self.last_active_time < self.idle_timeout):
            return self.idle_speed
        return 0.

    def _callback(self, eventtime):
        speed = self._wanted_speed(eventtime)
        if speed != self.last_speed:
            self.last_speed = speed
            self.fan.set_speed(speed)
        return eventtime + UPDATE_INTERVAL

    cmd_TOGGLE_CHAMBER_FAN_help = (
        "Toggle (or ENABLE=0|1) automatic chamber fan operation")

    def cmd_TOGGLE_CHAMBER_FAN(self, gcmd):
        self.enabled = bool(gcmd.get_int("ENABLE", int(not self.enabled),
                                         minval=0, maxval=1))
        gcmd.respond_info("Chamber fan %s: automatic operation %s"
                          % (self.name, "enabled" if self.enabled
                             else "disabled"))

    def get_status(self, eventtime):
        status = dict(self.fan.get_status(eventtime))
        status["enabled"] = self.enabled
        return status


def load_config_prefix(config):
    return QdChamberFan(config)
