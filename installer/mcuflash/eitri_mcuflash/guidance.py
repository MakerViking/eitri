# SPDX-License-Identifier: Apache-2.0
"""Plain-language recovery steps, shown on failure and in `rollback --explain`."""
from __future__ import annotations

from typing import List

from .registry import BoardDef


def recovery_steps(board: BoardDef, method: str, has_backup: bool) -> List[str]:
    steps: List[str] = []
    if method == "katapult":
        if has_backup:
            steps.append("Re-flash the saved image: eitri-mcuflash rollback %s --apply --yes" % board.name)
        steps.append(
            "If the board does not answer: power-cycle the printer, then run 'eitri-mcuflash probe'. "
            "A board stuck in Katapult is still recoverable the same way."
        )
        steps.append(
            "Only if Katapult itself was overwritten: connect an ST-Link V2 to the board's SWD header "
            "(soldered 6-pin header on the toolhead board) and write a Katapult image at 0x08000000."
        )
    elif method in ("sdcard-serial", "sdcard-file"):
        steps.append(board.recovery)
        steps.append(
            "QIDI's stock qd_mcu.bin is in the official firmware package for your model; "
            "keep a copy next to the printer before you flash."
        )
    else:
        steps.append(board.recovery)
    return steps
