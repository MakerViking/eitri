# SPDX-License-Identifier: Apache-2.0
"""Linux side of the boot selection shared with U-Boot's boot.scr.

/boot/eitri/boot.env (vfat) holds five variables that boot.scr rewrites with
`env export`; this module reads and writes the same file.  See boot/boot.cmd for
the U-Boot half and docs/BOOT.md for the state machine.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from typing import Dict, Iterator, Optional

from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.fsutil import atomic_write
from eitri_mcuflash.proc import Runner, run

STATE_KEYS = ("eitri_mode", "eitri_slot", "eitri_tries", "eitri_state_A", "eitri_state_B")
SLOTS = ("A", "B")
MODES = ("eitri", "stage2", "stock")
MAX_TRIES = 3
# Where the FAT boot partition is mounted: the OS image's mount generator uses /boot/firmware; the stock system and
# anything that passes eitri.bootmount=/boot use /boot.
BOOT_DIRS = ("/boot/firmware", "/boot")


def detect_boot_dir() -> str:
    for candidate in BOOT_DIRS:
        if os.path.exists(os.path.join(candidate, "eitri", "boot.env")):
            return candidate
    return "/boot"


def parse(text: str) -> Dict[str, str]:
    """key=value lines; U-Boot terminates the file with NUL bytes, which are dropped."""
    out: Dict[str, str] = {}
    for line in text.replace("\0", "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def serialize(values: Dict[str, str]) -> str:
    return "".join("%s=%s\n" % (k, values[k]) for k in STATE_KEYS if k in values)


class BootControl:
    def __init__(
        self,
        boot_dir: Optional[str] = None,
        *,
        runner: Runner = run,
        cmdline_path: str = "/proc/cmdline",
        lock_path: str = "/run/eitri-bootctl.lock",
    ) -> None:
        boot_dir = boot_dir or detect_boot_dir()
        self.boot_dir = boot_dir
        self.path = os.path.join(boot_dir, "eitri", "boot.env")
        self.layout_path = os.path.join(boot_dir, "eitri", "layout.env")
        self.runner = runner
        self.cmdline_path = cmdline_path
        self.lock_path = lock_path

    # -- reading ---------------------------------------------------------------
    def read(self) -> Dict[str, str]:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                return parse(fh.read())
        except OSError as exc:
            raise FlashError("cannot read %s: %s" % (self.path, exc.strerror), code="bootenv") from exc

    def layout(self) -> Dict[str, str]:
        try:
            with open(self.layout_path, "r", encoding="utf-8", errors="replace") as fh:
                return parse(fh.read())
        except OSError:
            return {}

    def current_slot(self) -> str:
        """The slot this system booted from (boot.scr passes eitri.slot= on the kernel command line)."""
        try:
            with open(self.cmdline_path, "r") as fh:
                for token in fh.read().split():
                    if token.startswith("eitri.slot="):
                        slot = token.split("=", 1)[1]
                        if slot in SLOTS:
                            return slot
        except OSError:
            pass
        raise FlashError("not booted through the Eitri boot manager (no eitri.slot= on the kernel command line)", code="bootenv")

    # -- writing ---------------------------------------------------------------
    @contextlib.contextmanager
    def writable(self) -> Iterator[None]:
        """Serialise writers and make /boot writable for the duration, then restore its mode."""
        lock_dir = os.path.dirname(self.lock_path)
        if lock_dir and os.path.isdir(lock_dir):
            lockfh = open(self.lock_path, "w")
        else:
            lockfh = open(os.path.join(tempfile.gettempdir(), "eitri-bootctl.lock"), "w")
        fcntl.flock(lockfh, fcntl.LOCK_EX)
        remounted = False
        try:
            if not os.access(os.path.join(self.boot_dir, "eitri"), os.W_OK):
                res = self.runner(["mount", "-o", "remount,rw", self.boot_dir])
                if res.returncode != 0:
                    raise FlashError("cannot make %s writable: %s" % (self.boot_dir, res.output.strip()), code="bootenv")
                remounted = True
            yield
        finally:
            if remounted:
                self.runner(["sync"])
                self.runner(["mount", "-o", "remount,ro", self.boot_dir])
            fcntl.flock(lockfh, fcntl.LOCK_UN)
            lockfh.close()

    def _write(self, values: Dict[str, str]) -> None:
        for key, value in values.items():
            if key == "eitri_mode" and value not in MODES:
                raise FlashError("invalid mode %r" % value, code="bootenv")
            if key in ("eitri_slot",) and value not in SLOTS:
                raise FlashError("invalid slot %r" % value, code="bootenv")
            if key == "eitri_tries" and value not in [str(i) for i in range(MAX_TRIES + 1)]:
                raise FlashError("invalid tries %r" % value, code="bootenv")
            if key.startswith("eitri_state_") and value not in ("good", "bad", "unknown"):
                raise FlashError("invalid state %r" % value, code="bootenv")
        atomic_write(self.path, serialize(values), prefix="boot.env.")

    def update(self, **changes: str) -> Dict[str, str]:
        with self.writable():
            values = self.read()
            values.update(changes)
            self._write(values)
            return values

    # -- operations used by RAUC and eitri-update --------------------------------
    def get_primary(self) -> str:
        return self.read().get("eitri_slot", "A")

    def set_primary(self, slot: str) -> None:
        """Make *slot* the one to boot, with a fresh set of attempts (state is untouched)."""
        self.update(eitri_slot=slot, eitri_tries=str(MAX_TRIES), eitri_mode="eitri")

    def get_state(self, slot: str) -> str:
        return "good" if self.read().get("eitri_state_%s" % slot) == "good" else "bad"

    def set_state(self, slot: str, state: str) -> None:
        if state not in ("good", "bad"):
            raise FlashError("state must be good or bad", code="bootenv")
        changes = {"eitri_state_%s" % slot: state}
        values = self.read()
        if state == "good" and values.get("eitri_slot") == slot:
            changes["eitri_tries"] = str(MAX_TRIES)       # the slot proved itself: restore its full allowance
        if state == "bad" and values.get("eitri_slot") == slot:
            changes["eitri_tries"] = "0"                  # force the fallback on the next boot
        self.update(**changes)

    def other(self, slot: str) -> str:
        return "B" if slot == "A" else "A"
