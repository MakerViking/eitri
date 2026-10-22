# Driver temperature for TMC stepper drivers that have one, also while the motor is off
#
# Klipper reads a TMC2240's built-in temperature only while its stepper is
# enabled, so in Fluidd/Mainsail the X/Y driver temperatures vanish whenever the
# motors are off (for example before a print). This module keeps them visible:
#
#   [qd_tmc_driver]
#
# With the section present, every configured TMC driver that has a temperature
# sensor (adc_temp register, e.g. TMC2240) reports its temperature in its own
# status ("tmc2240 stepper_x" etc.) all the time, so the UI shows one entry per
# driver, always filled. While the motor is enabled Klipper's own reading is
# used unchanged; while it is off the register is read once per second through
# the driver's own (locked) bus helper. A failed read is logged and retried; it
# never stops the printer. No Klipper file is changed: the driver object's status
# function is wrapped at start-up (the driver already exposes it as an attribute).
#
# The section also adds a sensor type, for a temperature_fan that follows a driver:
#
#   [temperature_fan board_fan]
#   sensor_type: qd_tmc_driver
#   tmc: tmc2240 stepper_x        # the driver's config section
#   ...                           # Klipper shuts down above max_temp, like any sensor
#
# Put [qd_tmc_driver] above any section that uses the sensor type.
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

POLL_TIME = 1.0


def _tmc2240_celsius(adc):
    # Same conversion as klippy/extras/tmc2240.py (_adc_to_celsius).
    return (adc - 2038) / 7.7


class _DriverTemp:
    """One driver: polls its temperature while Klipper does not, and fills it into its status."""

    def __init__(self, printer, name, tmc):
        self.printer = printer
        self.name = name
        self.tmc = tmc
        self.mcu_tmc = tmc.mcu_tmc
        self.fields = self.mcu_tmc.get_fields()
        self.reg = self.fields.lookup_register("adc_temp")
        self.temp = None
        self._read_failed = False
        self._orig_get_status = tmc.get_status
        tmc.get_status = self.get_status

    def get_status(self, eventtime=None):
        status = self._orig_get_status(eventtime)
        if status.get("temperature") is None and self.temp is not None:
            status = dict(status, temperature=self.temp)
        return status

    def klipper_temperature(self):
        return self._orig_get_status().get("temperature")

    def poll(self):
        polled = self.klipper_temperature()
        if polled is not None:
            self.temp = polled
            return
        try:
            raw = self.mcu_tmc.get_register(self.reg)
        except self.printer.command_error as e:
            if not self._read_failed:
                logging.warning("qd_tmc_driver: cannot read the temperature of %s: %s",
                                self.name, e)
            self._read_failed = True
            self.temp = None
            return
        self._read_failed = False
        self.temp = round(_tmc2240_celsius(self.fields.get_field("adc_temp", raw)), 2)


class QdTmcDriver:
    """[qd_tmc_driver]: driver temperatures always reported, plus `sensor_type: qd_tmc_driver`."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.drivers = {}
        pheaters = self.printer.load_object(config, "heaters")
        pheaters.add_sensor_factory("qd_tmc_driver", QdTmcDriverTemperature)
        if self.printer.get_start_args().get("debugoutput") is not None:
            return
        self.timer = self.reactor.register_timer(self._poll)
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_connect(self):
        for name, obj in self.printer.lookup_objects():
            mcu_tmc = getattr(obj, "mcu_tmc", None)
            if (name.split()[0].startswith("tmc") and mcu_tmc is not None
                    and callable(getattr(obj, "get_status", None))
                    and mcu_tmc.get_fields().lookup_register("adc_temp") is not None):
                self.drivers[name] = _DriverTemp(self.printer, name, obj)

    def _handle_ready(self):
        if self.drivers:
            self.reactor.update_timer(self.timer, self.reactor.NOW)

    def lookup_driver(self, name):
        return self.drivers.get(name)

    def _poll(self, eventtime):
        if self.printer.is_shutdown():
            return self.reactor.NEVER
        for driver in self.drivers.values():
            driver.poll()
        return eventtime + POLL_TIME


class QdTmcDriverTemperature:
    """`sensor_type: qd_tmc_driver`: a driver's temperature as a sensor (e.g. for a temperature_fan)."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.tmc_name = config.get("tmc")
        if len(self.tmc_name.split()) != 2:
            raise config.error("qd_tmc_driver: tmc must name a driver section, "
                               "for example 'tmc2240 stepper_x'")
        self.driver = None
        self.min_temp = self.max_temp = 0.0
        self._callback = None
        if self.printer.get_start_args().get("debugoutput") is not None:
            return
        self.timer = self.reactor.register_timer(self._sample)
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_connect(self):
        manager = self.printer.lookup_object("qd_tmc_driver")
        self.driver = manager.lookup_driver(self.tmc_name)
        if self.driver is None:
            raise self.printer.config_error(
                "qd_tmc_driver: [%s] is not a configured TMC driver with a "
                "temperature sensor" % (self.tmc_name,))

    def _handle_ready(self):
        self.reactor.update_timer(self.timer, self.reactor.NOW)

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, cb):
        self._callback = cb

    def get_report_time_delta(self):
        return POLL_TIME

    def _sample(self, eventtime):
        if self.printer.is_shutdown():
            return self.reactor.NEVER
        temp = self.driver.temp
        if temp is None:
            return eventtime + POLL_TIME
        if temp > self.max_temp:
            self.printer.invoke_shutdown(
                "%s driver temperature %.1f above maximum temperature of %.1f."
                % (self.tmc_name, temp, self.max_temp))
            return self.reactor.NEVER
        if self._callback is not None:
            mcu = self.printer.lookup_object("mcu")
            self._callback(mcu.estimated_print_time(eventtime), temp)
        return eventtime + POLL_TIME


def load_config(config):
    return QdTmcDriver(config)
