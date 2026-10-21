# Persist the live Z (baby-step) offset across restarts via [save_variables]
#
# Replaces the SAVE_ZOFFSET_TO_VARIABLE / LOAD_ZOFFSET_FROM_VARIABLE additions
# that QIDI's fork patched into gcode_move.py. Everything goes through public
# commands (SAVE_VARIABLE, SET_GCODE_OFFSET), so it behaves the same on
# Klipper and Kalico and never needs access to save_variables internals.
#
#   QD_SAVE_ZOFFSET [VALUE=<mm>]            store the current G-code Z offset
#   QD_LOAD_ZOFFSET [MOVE=0|1] [SPEED=<mm/s>] [REQUIRED=0|1]
#                                           apply the stored offset
#
# Config:
#   [save_variables]            # required, defined elsewhere in the config
#   filename: ...
#
#   [qd_zoffset_var]
#   variable: z_offset          # lower-case name inside the variables file
#   max_offset: 1.0             # refuse to save/load |offset| above this (mm)
#   load_on_ready: False        # apply the stored offset after every start
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
import math
import re

ROUND_DIGITS = 4


class QdZoffsetVar:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.printer.load_object(config, "save_variables")
        self.variable = config.get("variable", "z_offset")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", self.variable):
            raise config.error("qd_zoffset_var: 'variable' must be a "
                               "lower-case identifier")
        self.max_offset = config.getfloat("max_offset", 1., above=0.)
        self.load_on_ready = config.getboolean("load_on_ready", False)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.gcode.register_command(
            "QD_SAVE_ZOFFSET", self.cmd_QD_SAVE_ZOFFSET,
            desc=self.cmd_QD_SAVE_ZOFFSET_help)
        self.gcode.register_command(
            "QD_LOAD_ZOFFSET", self.cmd_QD_LOAD_ZOFFSET,
            desc=self.cmd_QD_LOAD_ZOFFSET_help)

    def _handle_ready(self):
        if self.load_on_ready:
            reactor = self.printer.get_reactor()
            reactor.register_callback(self._load_on_ready)

    def _load_on_ready(self, eventtime):
        saved = self._saved_value()
        if saved is not None and abs(saved) <= self.max_offset:
            self.gcode.run_script("SET_GCODE_OFFSET Z=%.*f MOVE=0"
                                  % (ROUND_DIGITS, saved))

    def _saved_value(self):
        sv = self.printer.lookup_object("save_variables")
        value = sv.get_status(None)["variables"].get(self.variable)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) else None

    def _check(self, gcmd, value):
        if not math.isfinite(value) or abs(value) > self.max_offset:
            raise gcmd.error("Z offset %.4f outside +/-%.2f mm limit"
                             % (value, self.max_offset))

    cmd_QD_SAVE_ZOFFSET_help = "Save the current G-code Z offset"

    def cmd_QD_SAVE_ZOFFSET(self, gcmd):
        gcode_move = self.printer.lookup_object("gcode_move")
        current = gcode_move.get_status(None)["homing_origin"][2]
        value = round(gcmd.get_float("VALUE", current), ROUND_DIGITS)
        self._check(gcmd, value)
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=%r" % (self.variable, value))
        gcmd.respond_info("Z offset %.*f saved as '%s'"
                          % (ROUND_DIGITS, value, self.variable))

    cmd_QD_LOAD_ZOFFSET_help = "Apply the saved Z offset (SET_GCODE_OFFSET)"

    def cmd_QD_LOAD_ZOFFSET(self, gcmd):
        move = gcmd.get_int("MOVE", 0, minval=0, maxval=1)
        speed = gcmd.get_float("SPEED", None, above=0.)
        saved = self._saved_value()
        if saved is None:
            if gcmd.get_int("REQUIRED", 0, minval=0, maxval=1):
                raise gcmd.error("No saved Z offset ('%s')" % (self.variable,))
            gcmd.respond_info("No saved Z offset; leaving the offset unchanged")
            return
        self._check(gcmd, saved)
        cmd = "SET_GCODE_OFFSET Z=%.*f MOVE=%d" % (ROUND_DIGITS, saved, move)
        if move and speed is not None:
            cmd += " MOVE_SPEED=%.3f" % (speed,)
        self.gcode.run_script_from_command(cmd)

    def get_status(self, eventtime):
        return {"variable": self.variable, "saved": self._saved_value(),
                "max_offset": self.max_offset}


def load_config(config):
    return QdZoffsetVar(config)
