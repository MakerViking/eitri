# SPDX-License-Identifier: Apache-2.0
"""Board registry: the data that drives probing, method choice and recovery text."""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import FlashError

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boards.json")


@dataclass
class BoardDef:
    name: str
    label: str
    serial_hint: str
    app_baud: int
    mcu_candidates: List[str]
    stock_cfg_owns: List[str]
    methods: List[str]
    order: int
    recovery: str
    katapult_bauds: List[int] = field(default_factory=lambda: [250000])
    allow_katapult: bool = True
    sdcard: Dict[str, str] = field(default_factory=dict)
    # Constants the live printer reports for this board (klippy.log); a firmware image must carry the same ones.
    expect_config: Dict[str, Any] = field(default_factory=dict)
    # What is settled about the flash layout.  status "unresolved" means the board is never written.
    params_status: str = "unresolved"
    params_confidence: str = ""
    resolved_by: Optional[str] = None            # the observation that settles an unresolved board ("katapult")
    app_offset: Optional[int] = None             # absolute application address when resolved and fixed
    open_questions: List[str] = field(default_factory=list)

    @property
    def katapult_baud(self) -> int:
        """The first Katapult baud rate to try (kept for callers that need exactly one)."""
        return self.katapult_bauds[0] if self.katapult_bauds else 250000

    @property
    def resolved(self) -> bool:
        return self.params_status == "resolved"

    def mcu_matches(self, mcu: str) -> bool:
        return any(fnmatch.fnmatch(mcu.lower(), pat) for pat in self.mcu_candidates)


@dataclass
class Registry:
    boards: Dict[str, BoardDef]
    chips: Dict[str, Dict[str, Any]]

    def ordered(self) -> List[BoardDef]:
        return sorted(self.boards.values(), key=lambda b: b.order)

    def flash_size(self, mcu: str) -> Optional[int]:
        chip = self.chips.get(mcu.lower())
        return int(chip["flash_kib"]) * 1024 if chip else None

    def flash_base(self, mcu: str) -> int:
        chip = self.chips.get(mcu.lower())
        return int(chip["flash_base"], 0) if chip else 0x08000000


def load_registry(path: Optional[str] = None) -> Registry:
    path = path or DEFAULT_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise FlashError("cannot read board registry %s: %s" % (path, exc), code="registry") from exc
    if doc.get("schema") != 1:
        raise FlashError("unsupported board registry schema %r" % doc.get("schema"), code="registry")
    boards = {}
    for name, raw in doc["boards"].items():
        params = raw.get("parameters") or {}
        boards[name] = BoardDef(
            name=name,
            label=raw["label"],
            serial_hint=raw["serial_hint"],
            app_baud=int(raw["app_baud"]),
            mcu_candidates=list(raw["mcu_candidates"]),
            stock_cfg_owns=list(raw.get("stock_cfg_owns", [])),
            methods=list(raw["methods"]),
            order=int(raw["order"]),
            recovery=raw["recovery"],
            katapult_bauds=[int(b) for b in raw.get("katapult_bauds", [250000])],
            allow_katapult=bool(raw.get("allow_katapult", True)),
            sdcard=dict(raw.get("sdcard", {})),
            expect_config=dict(raw.get("expect_config", {})),
            params_status=str(params.get("status", "unresolved")),   # a registry that says nothing is not resolved
            params_confidence=str(params.get("confidence", "")),
            resolved_by=params.get("resolved_by"),
            app_offset=int(params["app_offset"], 0) if params.get("app_offset") else None,
            open_questions=[str(q) for q in params.get("open_questions", [])],
        )
        if boards[name].params_status not in ("resolved", "unresolved"):
            raise FlashError("board %s: parameters.status must be resolved or unresolved" % name, code="registry")
    return Registry(boards=boards, chips=doc.get("chips", {}))
