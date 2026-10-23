# qd_probe: run a G-code snippet before each probing session (QIDI X-Plus 4)
#
# QIDI's fork ran a short routine (a Z vibrate to shake ooze off the nozzle)
# before probing, so the first contact is clean. Mainline's [probe] has no such
# hook. This adds one without touching probe.py:
#
#   [qd_probe]
#   prepare_gcode:
#       # runs once at the start of every probing session (G28 Z, PROBE,
#       # PROBE_CALIBRATE, PROBE_ACCURACY, bed mesh, Z_TILT_ADJUST).
#       # Default: nothing. During G28 Z it runs before Z is homed, so it
#       # must not move Z.
#       M117 cleaning nozzle
#       # ... your wipe / Z-hop / vibrate here ...
#
# How it works: at klippy:connect the single [probe] object's public
# start_probe_session() is wrapped so the snippet runs first, then the original
# session begins. It is one wrapper around a documented public entry point,
# installed once -- never a method swap on mcu_probe, so upstream's safety
# behaviour (a probe already triggered before a move is still a hard error,
# finite sample retries) is untouched. Without a [probe] section the section
# refuses to load (fail closed), like qd_power_interlock.
#
# Kalico still has the older probe.py with no start_probe_session (its session
# begins in multi_probe_begin, an event handler inside homing, which is no
# place to run G-code). There, an empty prepare_gcode (the default) loads as a
# no-op so the shipped config works on both bases, and a non-empty one refuses
# to load rather than silently never running.
#
# QD_PROBE_PREPARE runs the same snippet by hand.
#
# Not implemented here (need the second sensor and a printer; see the stub
# history in git): QD_NOZZLE_CONTACT_CALIBRATE (measure the contact-sensor vs
# offset-probe Z difference and store it as z_offset) and a save-to-mesh helper.
# bed_mesh's zero_reference_position already covers the common case of the
# latter.
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.


class QdProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.enabled = config.getboolean("enable", True)
        gcode_macro = self.printer.load_object(config, "gcode_macro")
        self.prepare_template = gcode_macro.load_template(config, "prepare_gcode", "")
        self.has_snippet = bool(config.get("prepare_gcode", "").strip())
        self._probe = None
        self._orig_start = None
        self._running = False       # re-entrancy guard: the snippet must not probe
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command("QD_PROBE_PREPARE", self.cmd_QD_PROBE_PREPARE,
                               desc="Run the [qd_probe] prepare_gcode now")

    def _handle_connect(self):
        probe = self.printer.lookup_object("probe", None)
        if probe is None:
            raise self.printer.config_error(
                "[qd_probe] needs a [probe] section (its start_probe_session is "
                "the hook point); none was found.")
        if not callable(getattr(probe, "start_probe_session", None)):
            if self.has_snippet:
                raise self.printer.config_error(
                    "[qd_probe] prepare_gcode needs [probe] start_probe_session "
                    "(mainline Klipper); this Klipper base has none. Leave "
                    "prepare_gcode empty, or call QD_PROBE_PREPARE from your "
                    "own macros instead.")
            return          # nothing to run before a session: stay unhooked
        self._probe = probe
        self._orig_start = probe.start_probe_session
        probe.start_probe_session = self._start_probe_session

    def _run_prepare(self, gcmd=None):
        # Rendered fresh each time so printer variables are current; a blank
        # template (the default) is a no-op. The guard stops a snippet that
        # itself probes from recursing forever.
        if self._running:
            return
        script = self.prepare_template.render().strip()
        if not script:
            return
        self._running = True
        try:
            self.printer.lookup_object("gcode").run_script_from_command(script)
        finally:
            self._running = False

    def _start_probe_session(self, gcmd):
        if self.enabled and not self._running:
            self._run_prepare(gcmd)
        return self._orig_start(gcmd)

    def cmd_QD_PROBE_PREPARE(self, gcmd):
        self._run_prepare(gcmd)


def load_config(config):
    return QdProbe(config)
