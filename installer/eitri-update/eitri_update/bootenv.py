# SPDX-License-Identifier: Apache-2.0
"""Linux side of the boot selection.  Two layouts share one interface.

in-place   (converted stock disk): THE user layout (installer/install.sh keeps the stock U-Boot).
           /boot/eitri/boot.env (vfat) holds five variables that boot.scr rewrites with
           `env export`; this module reads and writes the same file.  See boot/boot.cmd for the U-Boot half and
           docs/BOOT.md for the state machine.  Slot state is a real tri-state (good / bad / unknown).
flash-once (GPT image): bench boards, microSD cards and CI only.
           U-Boot keeps BOOT_ORDER and BOOT_<slot>_LEFT in one raw sector, owned by
           /usr/libexec/eitri/rauc-bootloader.  Everything is delegated to that script.  It only knows "attempts
           left", so a slot that has never proven itself (the freshly written one, the never-written one) would look
           "good"; a slot is therefore only reported good once eitri-update recorded a healthy boot of it
           (state/slot-confirmed.json on /data).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from typing import Dict, Iterator, Optional

from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.fsutil import atomic_write
from eitri_mcuflash.proc import Runner, run

STATE_KEYS = ("eitri_mode", "eitri_slot", "eitri_tries", "eitri_state_A", "eitri_state_B")
# What boot.scr (installer/boot/boot.cmd) takes over from the two files: `env import -t ADDR SIZE <these>` only, so a
# hand-edited file cannot set anything else (where fatwrite writes, what `run` executes).  Every writer of the files
# (this module, installer/install.sh, installer/stage2/stage2.sh, installer/revert.sh, os/bench/mk-sd-bench.sh) must
# stay within these lists; installer/tests/test_boot_script.py checks both sides.
BOOT_SCR_STATE_KEYS = STATE_KEYS + ("eitri_s2_tries",)
LAYOUT_KEYS = ("eitri_part_A", "eitri_part_B", "eitri_partuuid_A", "eitri_partuuid_B", "eitri_cmdline", "eitri_hide_emmc",
               "eitri_s2_kernel", "eitri_s2_fdt", "eitri_s2_initrd", "eitri_s2_args")
SLOTS = ("A", "B")
MODES = ("eitri", "stage2", "stock")
MAX_TRIES = 3
# Where the FAT boot partition is mounted: the OS image's mount generator uses /boot/firmware; the stock system and
# anything that passes eitri.bootmount=/boot use /boot.
BOOT_DIRS = ("/boot/firmware", "/boot")
RAUC_BOOTLOADER = "/usr/libexec/eitri/rauc-bootloader"
CONFIRMED_PATH = "/data/eitri/state/slot-confirmed.json"


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
        helper: str = RAUC_BOOTLOADER,
        helper_env: Optional[Dict[str, str]] = None,
        confirmed_path: str = CONFIRMED_PATH,
    ) -> None:
        boot_dir = boot_dir or detect_boot_dir()
        self.boot_dir = boot_dir
        self.path = os.path.join(boot_dir, "eitri", "boot.env")
        self.layout_path = os.path.join(boot_dir, "eitri", "layout.env")
        self.runner = runner
        self.cmdline_path = cmdline_path
        self.lock_path = lock_path
        self.helper = helper
        self.helper_env = helper_env
        self.confirmed_path = confirmed_path

    @property
    def backend(self) -> str:
        """"inplace" when the vfat boot.env exists, "gpt" when only the raw-sector helper does.  Neither -> "inplace",
        whose read() then reports the missing file."""
        if os.path.exists(self.path):
            return "inplace"
        if os.access(self.helper, os.X_OK):
            return "gpt"
        return "inplace"

    # -- the flash-once (GPT) backend ---------------------------------------------------
    def _helper(self, *args: str) -> str:
        res = self.runner([self.helper] + list(args), env=self.helper_env) if self.helper_env else self.runner([self.helper] + list(args))
        if res.returncode != 0:
            raise FlashError("%s %s failed: %s" % (os.path.basename(self.helper), " ".join(args), res.output.strip()), code="bootenv")
        lines = res.output.strip().splitlines()
        return lines[-1].strip() if lines else ""

    def confirmed(self) -> Dict[str, bool]:
        try:
            with open(self.confirmed_path) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return {}
        return {s: True for s in SLOTS if isinstance(doc, dict) and doc.get(s) is True}

    def _set_confirmed(self, slot: str, value: bool) -> None:
        with self._lock():
            doc = self.confirmed()
            if value:
                doc[slot] = True
            else:
                doc.pop(slot, None)
            atomic_write(self.confirmed_path, json.dumps(doc, sort_keys=True) + "\n")

    def forget_confirmation(self, slot: str) -> None:
        """A new system is about to be written into *slot*: whatever was proven about the old one no longer holds."""
        if slot not in SLOTS:
            raise FlashError("invalid slot %r" % slot, code="bootenv")
        if self.backend == "gpt":
            self._set_confirmed(slot, False)

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
        """The slot this system booted from: boot.scr passes eitri.slot= (in-place), the GPT image's U-Boot rauc.slot=."""
        found: Dict[str, str] = {}
        try:
            with open(self.cmdline_path, "r") as fh:
                for token in fh.read().split():
                    for key in ("eitri.slot", "rauc.slot"):
                        if token.startswith(key + "=") and token.split("=", 1)[1] in SLOTS:
                            found[key] = token.split("=", 1)[1]
        except OSError:
            pass
        slot = found.get("eitri.slot") or found.get("rauc.slot")
        if slot:
            return slot
        raise FlashError("not booted through the Eitri boot manager (no eitri.slot= or rauc.slot= on the kernel command line)", code="bootenv")

    # -- writing ---------------------------------------------------------------
    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        lock_dir = os.path.dirname(self.lock_path)
        if lock_dir and os.path.isdir(lock_dir):
            lockfh = open(self.lock_path, "w")
        else:
            lockfh = open(os.path.join(tempfile.gettempdir(), "eitri-bootctl.lock"), "w")
        fcntl.flock(lockfh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfh, fcntl.LOCK_UN)
            lockfh.close()

    @contextlib.contextmanager
    def writable(self) -> Iterator[None]:
        """Serialise writers and make /boot writable for the duration, then restore its mode."""
        with self._lock():
            with self._remounted():
                yield

    @contextlib.contextmanager
    def _remounted(self) -> Iterator[None]:
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
        if self.backend == "gpt":
            slot = self._helper("get-primary")
            if slot not in SLOTS:
                raise FlashError("unexpected primary slot %r" % slot, code="bootenv")
            return slot
        return self.read().get("eitri_slot", "A")

    def set_primary(self, slot: str) -> None:
        """Make *slot* the one to boot, with a fresh set of attempts (state is untouched)."""
        if slot not in SLOTS:
            raise FlashError("invalid slot %r" % slot, code="bootenv")
        if self.backend == "gpt":
            self._helper("set-primary", slot)
            return
        self.update(eitri_slot=slot, eitri_tries=str(MAX_TRIES), eitri_mode="eitri")

    def get_state(self, slot: str) -> str:
        """"good" only for a slot that proved itself.  (GPT: attempts left AND a recorded healthy boot.)"""
        if slot not in SLOTS:
            raise FlashError("invalid slot %r" % slot, code="bootenv")
        if self.backend == "gpt":
            return "good" if self._helper("get-state", slot) == "good" and self.confirmed().get(slot) else "bad"
        return "good" if self.read().get("eitri_state_%s" % slot) == "good" else "bad"

    def set_state(self, slot: str, state: str) -> None:
        if state not in ("good", "bad"):
            raise FlashError("state must be good or bad", code="bootenv")
        if slot not in SLOTS:
            raise FlashError("invalid slot %r" % slot, code="bootenv")
        if self.backend == "gpt":
            if state == "bad":
                self._set_confirmed(slot, False)        # drop the proof first: the safe direction if the next step fails
                self._helper("set-state", slot, "bad")
            else:
                self._helper("set-state", slot, "good")
                self._set_confirmed(slot, True)
            return
        changes = {"eitri_state_%s" % slot: state}
        values = self.read()
        if state == "good" and values.get("eitri_slot") == slot:
            changes["eitri_tries"] = str(MAX_TRIES)       # the slot proved itself: restore its full allowance
        if state == "bad" and values.get("eitri_slot") == slot:
            changes["eitri_tries"] = "0"                  # force the fallback on the next boot
        self.update(**changes)

    def status(self) -> Dict[str, str]:
        if self.backend == "gpt":
            out = {"backend": "gpt", "primary": self.get_primary()}
            for slot in SLOTS:
                out["state_%s" % slot] = self.get_state(slot)
            return out
        return self.read()

    def other(self, slot: str) -> str:
        return "B" if slot == "A" else "A"
