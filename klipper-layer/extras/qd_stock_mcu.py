# Quiet the "MCU has deprecated code" warnings while the boards run QIDI's stock firmware
#
# Eitri can leave both X-Plus 4 MCUs on the firmware QIDI shipped (a Klipper
# v0.12 build) under a current Klipper host. That works, but the host then lists
# one "has deprecated code (it is missing feature ...)" warning per missing
# optimisation and board, and Fluidd/Mainsail show them as a banner that covers
# most of the dashboard. They are accurate and change nothing until the boards
# are flashed, which Eitri does separately.
#
#   [qd_stock_mcu]
#   # version_marker: -mkspi     # substring of QIDI's stock MCU version string
#
# Only warnings of type "deprecated_mcu_code" from an MCU whose version contains
# version_marker are removed; each one is still written to klippy.log. Every
# other warning stays. Once a board runs firmware built by Eitri its version no
# longer matches and its warnings (if any) are shown again. Delete the section
# to see everything.
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging


class QdStockMcu:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.marker = config.get("version_marker", "-mkspi")
        if not self.marker.strip():
            raise config.error("qd_stock_mcu: version_marker must not be empty")
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _is_stock(self, mcu_name):
        try:
            mcu = self.printer.lookup_object(
                "mcu" if mcu_name == "mcu" else "mcu " + mcu_name)
            version = mcu.get_status()["mcu_version"]
        except Exception:
            return False
        return self.marker in version

    def _handle_ready(self):
        configfile = self.printer.lookup_object("configfile")
        keep = []
        for warning in configfile.status_warnings:
            if (warning.get("type") == "deprecated_mcu_code"
                    and self._is_stock(warning.get("mcu", ""))):
                logging.info("qd_stock_mcu: not shown (stock QIDI MCU firmware): %s",
                             warning.get("message"))
                continue
            keep.append(warning)
        # A new list, like configfile itself does, so status diffing notices the change.
        configfile.status_warnings = keep


def load_config(config):
    return QdStockMcu(config)
