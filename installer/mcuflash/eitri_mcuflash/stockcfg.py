# SPDX-License-Identifier: Apache-2.0
"""Which MCU owns which printer function, according to a working printer.cfg.

A Klipper config that drives the printer today is physical ground truth for the
board wiring: if `[stepper_x]` uses `U_1:PB4` pins, the MCU declared by
`[mcu U_1]` is the one with the X driver on it.  That makes it the strongest
available discriminator between two boards (the chip name in the data dictionary comes first; this settles a tie).
"""
from __future__ import annotations

import glob
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from .registry import Registry

_SECTION = re.compile(r"^\[\s*([^\]]+?)\s*\]\s*$")
_PIN_KEY = re.compile(r"(^|_)pin$|_pin_?\d*$|^pins$")


def _strip_comment(line: str) -> str:
    for marker in ("#", ";"):
        idx = line.find(marker)
        if idx == 0:
            return ""
        if idx > 0 and line[idx - 1] in " \t":
            line = line[:idx]
    return line.rstrip()


def _read_with_includes(path: str, seen: Set[str], depth: int = 0) -> List[str]:
    real = os.path.realpath(path)
    if real in seen or depth > 8:
        return []
    seen.add(real)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out: List[str] = []
    base = os.path.dirname(path)
    for line in lines:
        m = _SECTION.match(_strip_comment(line))
        if m and m.group(1).split(None, 1)[0] == "include" and len(m.group(1).split(None, 1)) == 2:
            pattern = m.group(1).split(None, 1)[1].strip()
            for inc in sorted(glob.glob(os.path.join(base, pattern))):
                out.extend(_read_with_includes(inc, seen, depth + 1))
        else:
            out.append(line)
    return out


def parse_ownership(path: str) -> Dict[str, Dict[str, object]]:
    """Return {mcu_name: {"serial": str|None, "owns": set(section_roots)}}.

    The unnamed `[mcu]` is reported as "mcu".
    """
    lines = _read_with_includes(path, set())
    mcus: Dict[str, Dict[str, object]] = {}
    sections: List[Tuple[str, List[str]]] = []
    current: Optional[Tuple[str, List[str]]] = None
    for raw in lines:
        line = _strip_comment(raw)
        if not line.strip():
            continue
        m = _SECTION.match(line)
        if m:
            current = (m.group(1), [])
            sections.append(current)
            continue
        if current is not None and (line[0] not in " \t"):
            current[1].append(line)
    for header, body in sections:
        parts = header.split(None, 1)
        if parts[0] == "mcu":
            name = parts[1].strip() if len(parts) == 2 else "mcu"
            serial = None
            for line in body:
                key, _, value = line.partition(":")
                if key.strip() == "serial":
                    serial = value.strip()
            mcus[name] = {"serial": serial, "owns": set()}
    for header, body in sections:
        root = header.split(None, 1)[0]
        if root == "mcu":
            continue
        for line in body:
            sep = ":" if ":" in line else "="
            key, _, value = line.partition(sep)
            if not _PIN_KEY.search(key.strip()):
                continue
            value = value.strip().lstrip("!^~ ")
            owner = "mcu"
            if ":" in value:
                prefix = value.split(":", 1)[0].strip()
                if prefix in mcus:
                    owner = prefix
                else:
                    continue  # e.g. "probe:z_virtual_endstop" - not a physical pin
            if owner in mcus:
                mcus[owner]["owns"].add(root)  # type: ignore[union-attr]
    return mcus


def stock_port_roles(path: str, registry: Registry) -> Dict[str, Tuple[str, List[str]]]:
    """{serial_port: (board_name, evidence)} for ports that clearly map to one board."""
    result: Dict[str, Tuple[str, List[str]]] = {}
    owners = parse_ownership(path)
    for mcu_name, info in owners.items():
        serial = info.get("serial")
        if not serial:
            continue
        owns: Set[str] = info["owns"]  # type: ignore[assignment]
        scored = []
        for board in registry.boards.values():
            hits = sorted(owns.intersection(board.stock_cfg_owns))
            if hits:
                scored.append((len(hits), board.name, hits))
        scored.sort(reverse=True)
        if not scored:
            continue
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            continue  # tie: not evidence
        best = scored[0]
        result[str(serial)] = (
            best[1],
            [
                "%s: %s on %s drives %s"
                % (os.path.basename(path), "[mcu]" if mcu_name == "mcu" else "[mcu %s]" % mcu_name, serial, ", ".join(best[2]))
            ],
        )
    return result


DEFAULT_CONFIG_PATHS = (
    "/data/printer_data/config/printer.cfg",
    "/home/mks/printer_data/config/printer.cfg",
    "/home/mks/klipper_config/printer.cfg",
)


def find_printer_cfg(extra: Optional[List[str]] = None) -> List[str]:
    candidates = list(extra or []) + list(DEFAULT_CONFIG_PATHS) + sorted(glob.glob("/home/*/printer_data/config/printer.cfg"))
    seen: List[str] = []
    for c in candidates:
        if c not in seen and os.path.isfile(c):
            seen.append(c)
    return seen
