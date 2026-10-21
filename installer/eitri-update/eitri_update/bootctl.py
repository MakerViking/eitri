# SPDX-License-Identifier: Apache-2.0
"""eitri-bootctl: RAUC "custom" bootloader backend and a small operator tool.

RAUC calls the handler configured as `bootloader-custom-backend` with:

    get-primary | set-primary SLOT | get-state SLOT | set-state SLOT good|bad | get-current

where SLOT is the bootname (A or B).  Extra operator commands: status, mark-good.  Works on both layouts (see
bootenv.py): the in-place boot.env, and the flash-once image's raw state sector via rauc-bootloader.
"""
from __future__ import annotations

import sys
from typing import List, Optional

from eitri_mcuflash.errors import FlashError

from .bootenv import BootControl


def main(argv: Optional[List[str]] = None, control: Optional[BootControl] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write("usage: eitri-bootctl get-primary|set-primary SLOT|get-state SLOT|set-state SLOT STATE|get-current|status|mark-good\n")
        return 2
    ctl = control or BootControl()
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "get-primary":
            print(ctl.get_primary())
        elif cmd == "set-primary" and len(rest) == 1:
            ctl.set_primary(rest[0])
        elif cmd == "get-state" and len(rest) == 1:
            print(ctl.get_state(rest[0]))
        elif cmd == "set-state" and len(rest) == 2:
            ctl.set_state(rest[0], rest[1])
        elif cmd == "get-current":
            print(ctl.current_slot())
        elif cmd == "mark-good":
            ctl.set_state(ctl.current_slot(), "good")
        elif cmd == "status":
            for key, value in sorted(ctl.status().items()):
                print("%s=%s" % (key, value))
        else:
            sys.stderr.write("eitri-bootctl: bad arguments\n")
            return 2
    except FlashError as exc:
        sys.stderr.write("eitri-bootctl: %s\n" % exc.message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
