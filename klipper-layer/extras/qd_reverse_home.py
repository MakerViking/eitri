# STUB -- qd_reverse_home: sensorless Z-max "reverse homing" for the X-Plus 4
#
# STATUS: design notes only. Loading [qd_reverse_home] fails on purpose. This
# moves heavy axes against a hard stop with a tuned stall threshold; it must
# not ship before it has been validated on a printer (see "Bring-up").
#
# What the stock firmware did
# ---------------------------
# The Plus 4 has two independent Z lead screws (stepper_z, stepper_z1), each on
# a TMC2209 with its DIAG pin wired to the mainboard MCU (U_1:PA13 and
# U_1:PC12 in the stock config, driver_SGTHRS: 100, StealthChop always on
# because StallGuard4 needs it). QIDI's fork patched core stepper.py to add
# endstop_pin_reverse / position_endstop_reverse / homing_speed_reverse /
# homing_positive_dir_reverse and a REVERSE_HOMING command. M4031 runs it at
# 0.7x run current, twice, to drive both Z screws into the lower hard stop so
# the bed is square before the normal probe-based Z homing. That patch edits
# core Klipper and is incompatible with the clean-tree rule (and mainline
# rejects the *_reverse options), so it is re-expressed as an add-on.
#
# Design (no core edits, no monkey patches)
# -----------------------------------------
# Config:
#   [qd_reverse_home]
#   steppers: stepper_z, stepper_z1     # each needs a [tmc2209 <name>] with
#                                       # diag_pin and driver_SGTHRS
#   current_scale: 0.7                  # fraction of run_current while stalling
#   speed: 10                           # mm/s towards the stop
#   max_travel: 40                      # hard limit per run, mm
#   backoff: 5                          # mm to retract afterwards
#   settle_time: 0.5
#
# Command: QD_REVERSE_HOME [CURRENT_SCALE=] [SPEED=] [MAX_TRAVEL=]
#  1. Refuse unless force_move is enabled, the printer is idle (no print
#     active) and Z has a position estimate, so that max_travel is meaningful.
#  2. Lower both Z currents via the TMC driver's set_current helper
#     (TMCCurrentHelper) to current_scale * run_current, remembering the old
#     values; restore them in a finally block on every exit path, including
#     errors and shutdown.
#  3. Build a virtual endstop per stepper: ppins.setup_pin('endstop',
#     'tmc2209_<name>:virtual_endstop'), add_stepper() it, then run each
#     stepper through the same machinery manual_stepper.py uses
#     (ManualStepper.do_homing_move: its own trapq, homing.HomingMove, the
#     stepper detached from the toolhead trapq for the duration) -- NOT via
#     force_move.manual_move, which has no endstop support. The virtual
#     endstop's pin helper enables StallGuard (TMCVirtualPinHelper) for the
#     duration of the move only.
#  4. Move one stepper at a time (so a stall on one screw is unambiguous),
#     with a hard max_travel and a single attempt: on timeout or
#     "no trigger within travel" raise an error. No retries. A trigger that
#     is already active before motion starts is an error, never accepted
#     (mirror of Klipper's own probe/endstop pre-trigger rule).
#  5. Back off by `backoff` mm, restore currents, re-sync the toolhead
#     position (toolhead.set_position / SET_KINEMATIC_POSITION semantics) and
#     leave Z un-homed so the normal G28 Z (probe) runs next.
#
# Kalico and Klipper both ship manual_stepper, force_move and the TMC virtual
# endstop helper with the same roles; the module-path import style differs
# (extras.x vs klippy.extras.x) so import them relatively (`from . import
# manual_stepper, force_move, homing`) as the other qd_* modules do.
#
# Bring-up (needs the printer, record in the hardware checklist)
#   * Tune driver_SGTHRS at the reduced current: DUMP_TMC SG_RESULT while
#     moving at `speed`, bed empty, repeat cold and at bed 100 C.
#   * Verify both screws stall at the same height (square) and that the stop
#     is a mechanical hard stop, not the lead nut or the frame.
#   * Confirm a stall is detected within max_travel from the worst-case
#     starting height and that no skipped-step damage occurs at 0.7x current.
#   * Decide whether the squaring step is needed at all: z_tilt with the
#     probe may be sufficient on the unit.
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.

STUB = True


def load_config(config):
    raise config.error(
        "[qd_reverse_home] is a design stub and is not implemented. Use "
        "Z_TILT_ADJUST for bed squaring until sensorless reverse homing has "
        "been validated on hardware; see the design notes at the top of "
        "klipper-layer/extras/qd_reverse_home.py.")
