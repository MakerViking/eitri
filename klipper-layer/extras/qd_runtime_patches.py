# Runtime tunables that mainline Klipper only exposes as module constants
#
# Currently one: the multi-MCU homing sync timeout (mcu.TRSYNC_TIMEOUT). The
# X-Plus 4 homes Z with the probe on the toolhead MCU while the Z steppers sit
# on the mainboard MCU, over two 500 kbaud UARTs; QIDI's fork raised the
# constant from 0.025 s to 0.10 s to avoid false "Communication timeout during
# homing" errors. Mainline offers no config option for it, so this module sets
# the constant when the config is read, leaving every Klipper file untouched.
#
#   [qd_runtime_patches]
#   trsync_timeout: 0.1        # seconds, 0.025 (Klipper's own value) to 0.5
#
# Kalico has a real option for this ([danger_options] multi_mcu_trsync_timeout)
# and already routes the value itself; there this module refuses to load so
# the setting cannot be applied twice. flavors/kalico.cfg uses the option.
#
# A larger timeout only delays when the MCU stops the steppers if the host
# stops talking *during a homing move*; it does not touch heaters or any other
# check. Keep it as small as reliable homing allows.
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

MIN_TIMEOUT = 0.025
MAX_TIMEOUT = 0.5


class QdRuntimePatches:
    def __init__(self, config):
        self.printer = config.get_printer()
        timeout = config.getfloat("trsync_timeout", None,
                                  minval=MIN_TIMEOUT, maxval=MAX_TIMEOUT)
        if timeout is None:
            return
        if self._is_kalico():
            raise config.error(
                "qd_runtime_patches: Kalico has [danger_options] "
                "multi_mcu_trsync_timeout; use that instead")
        try:
            import mcu
        except ImportError:
            raise config.error("qd_runtime_patches: cannot import the mcu "
                               "module")
        if not hasattr(mcu, "TRSYNC_TIMEOUT"):
            # Fail loudly: the tunable moved, the user must know it is unset
            raise config.error(
                "qd_runtime_patches: this Klipper has no mcu.TRSYNC_TIMEOUT; "
                "the module needs updating")
        logging.info("qd_runtime_patches: TRSYNC_TIMEOUT %.3f -> %.3f",
                     mcu.TRSYNC_TIMEOUT, timeout)
        mcu.TRSYNC_TIMEOUT = timeout

    @staticmethod
    def _is_kalico():
        try:
            from . import danger_options  # noqa: F401
            return True
        except ImportError:
            return False

    def get_status(self, eventtime):
        return {}


def load_config(config):
    return QdRuntimePatches(config)
