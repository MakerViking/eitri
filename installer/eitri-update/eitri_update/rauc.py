# SPDX-License-Identifier: Apache-2.0
"""Thin wrapper around the `rauc` command line."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, List, Optional

from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.proc import Runner, run

_PROGRESS = re.compile(r"^\s*(\d{1,3})%\s+(.*)$")


@dataclass
class Slot:
    name: str          # e.g. rootfs.0
    bootname: str      # A / B
    state: str         # booted | active | inactive
    boot_status: str   # good | bad | ...
    version: str = ""
    device: str = ""   # e.g. /dev/mmcblk1p5


@dataclass
class RaucStatus:
    compatible: str
    booted: Optional[str]
    slots: List[Slot]

    def slot(self, bootname: str) -> Optional[Slot]:
        for s in self.slots:
            if s.bootname == bootname:
                return s
        return None


def _json_document(output: str) -> dict:
    """The JSON object in rauc's output. The runner hands over stdout and stderr together, and on the printer rauc
    logs informational lines to stderr first ("rauc-Message: ... Using central status file ..."), so the output
    is not JSON from its first character: every update failed there on real hardware (2026-10-24) while the test
    and QEMU runs, which print no such lines, passed. Take the first line that opens an object and decode one
    document from it, ignoring anything around it."""
    lines = output.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            doc, _ = json.JSONDecoder().raw_decode("".join(lines[i:]).lstrip())
            if isinstance(doc, dict):
                return doc
            break
    raise ValueError("no JSON object in rauc output")


class RaucClient:
    def __init__(self, runner: Runner = run, binary: str = "rauc") -> None:
        self.runner = runner
        self.binary = binary

    def status(self) -> RaucStatus:
        res = self.runner([self.binary, "status", "--output-format=json"])
        if res.returncode != 0:
            raise FlashError("rauc status failed: %s" % res.output.strip(), code="rauc")
        try:
            doc = _json_document(res.output)
        except ValueError as exc:
            raise FlashError("rauc status returned unparsable output", code="rauc") from exc
        slots: List[Slot] = []
        for entry in doc.get("slots", []):
            for name, info in entry.items():
                if not info.get("bootname"):
                    continue
                bundle = (info.get("slot_status") or {}).get("bundle") or {}
                slots.append(Slot(name, info["bootname"], info.get("state", ""), info.get("boot_status", ""), bundle.get("version", ""),
                          str(info.get("device", ""))))
        return RaucStatus(doc.get("compatible", ""), doc.get("booted"), slots)

    def install(self, bundle: str, on_progress: Optional[Callable[[int, str], None]] = None) -> None:
        def on_line(line: str) -> None:
            m = _PROGRESS.match(line)
            if m and on_progress:
                on_progress(min(100, int(m.group(1))), m.group(2).strip())

        res = self.runner([self.binary, "install", bundle], on_line=on_line)
        if res.returncode != 0:
            tail = "\n".join(res.output.strip().splitlines()[-6:])
            raise FlashError(
                "rauc could not install the bundle:\n" + tail,
                code="rauc_install",
                recovery="The running system was not touched. Re-download the bundle or check free space on /data.",
            )

    def mark(self, state: str, slot: str = "booted") -> None:
        if state not in ("good", "bad", "active"):
            raise ValueError(state)
        res = self.runner([self.binary, "status", "mark-" + state, slot])
        if res.returncode != 0:
            raise FlashError("rauc status mark-%s failed: %s" % (state, res.output.strip()), code="rauc")
