# STUB -- qd_probe: single-offset probe helpers for the QIDI X-Plus 4
#
# STATUS: design notes only. Loading [qd_probe] fails on purpose; the stock
# [probe] section already covers everything needed to print (see "What works
# today"). Do not remove the guard at the bottom until the open items below
# have been implemented AND validated on a printer.
#
# Background
# ----------
# QIDI's fork ships two sensors on the toolhead path:
#   * a nozzle-contact sensor ([smart_effector] pin, mainboard MCU) used to
#     measure the first-layer Z offset, and
#   * an offset probe ([qdprobe] pin PA10, toolhead MCU) used for Z homing and
#     bed meshes.
# The fork's qdprobe.py swapped between them with QIDI_PROBE_PIN_1/2 by
# monkey-patching methods of probe.PrinterProbe.mcu_probe. That cannot be
# ported: mainline reworked probe.py in 2024-25 into composable helpers
# (PrinterProbe -> ProbeEndstopWrapper, ProbeCommandHelper,
# ProbeParameterHelper, ProbeOffsetsHelper, HomingViaProbeHelper,
# SampleAveragingHelper, start_probe_session()/run_probe()/
# pull_probed_results()), so there is no single method left to patch, and any
# patch would silently rot with the next refactor. Kalico tracks the same
# structure.
#
# Design: SINGLE offset probe, no probe-pin switching
# ---------------------------------------------------
# Eitri defines exactly one probe, the PA10 offset probe, as an ordinary
# [probe] section (see config/printer.cfg.tmpl). Z homing
# (probe:z_virtual_endstop), bed meshes, Z_TILT_ADJUST and PROBE_CALIBRATE all
# use it unmodified, so nothing here can interfere with upstream's safety
# behaviour: a probe that is already triggered before a move is still a hard
# error ("Probe triggered prior to movement") and sample-tolerance retries
# stay finite.
#
# What works today (no code needed)
#   * z_offset of the offset probe: PROBE_CALIBRATE + SAVE_CONFIG, or
#     Z_OFFSET_APPLY_PROBE after a baby-stepped first layer.
#   * Per-print baby-stepping persistence: qd_zoffset_var.
#   * Sample filtering: samples_result: median (replaces the fork's
#     "submaxmin"), finite samples_tolerance_retries (replaces the fork's
#     endless retry loop).
#
# What qd_probe would add (open items)
#   1. QD_PROBE_PREPARE hook: run a configurable G-code snippet (for example
#      Z_VIBRATE to shake off ooze) before each probing *session*. Implement
#      by registering a handler on the 'probe:...' events if upstream has
#      them, otherwise by wrapping PrinterProbe.start_probe_session() on the
#      instance created by [probe] -- one wrapper around a documented public
#      entry point, installed at klippy:connect, never a method swap on
#      mcu_probe. If the entry point is missing the module must refuse to
#      load (fail closed), as qd_power_interlock does.
#   2. QD_NOZZLE_CONTACT_CALIBRATE (optional hardware): measure the Z where the
#      contact sensor fires and where the offset probe fires at the same XY,
#      and store the difference as the offset probe's z_offset through
#      configfile.set(). Needs its own mcu_endstop built from the second pin
#      via ppins.setup_pin('endstop', ...) and a probing move run through
#      probe.run_single_probe-style logic on that endstop. It must enforce the
#      "lift first" order so a pre-triggered sensor aborts instead of being
#      accepted.
#   3. SAVE_Z_OFFSET_TO_BED_MESH equivalent: subtract the centre sample from a
#      mesh. Prefer bed_mesh's zero_reference_position; only write code if that
#      proves insufficient on hardware.
#
# Hardware validation needed before this stops being a stub
#   * Confirm which MCU/pin carries which sensor on the shipping X-Plus 4 and
#     the pin polarity (the fork used PA10 with inversion, '!PA10').
#   * Compare PROBE_ACCURACY of the offset probe at the mesh speeds used.
#   * Verify that contact-sensor and offset-probe Z agree to within the mesh
#     noise over a heated bed (thermal drift).
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.

STUB = True


def load_config(config):
    raise config.error(
        "[qd_probe] is a design stub and is not implemented. Use the stock "
        "[probe] section (see klipper-layer/config/printer.cfg.tmpl); the "
        "design notes are at the top of klipper-layer/extras/qd_probe.py.")
