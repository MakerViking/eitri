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
    katapult_baud: int = 250000
    sdcard: Dict[str, str] = field(default_factory=dict)

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
            katapult_baud=int(raw.get("katapult_baud", 250000)),
            sdcard=dict(raw.get("sdcard", {})),
        )
    return Registry(boards=boards, chips=doc.get("chips", {}))
