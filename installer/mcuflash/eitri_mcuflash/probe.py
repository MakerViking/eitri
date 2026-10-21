# SPDX-License-Identifier: Apache-2.0
"""Find out what is on each UART, and which board is which.

Port to board binding is never taken from a constant: the registry's
`serial_hint` is only a prior, and flashing requires at least one piece of
independent evidence (stock printer.cfg ownership, a discriminating chip name,
a previously verified mapping, or an explicit operator override).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import ProtocolError, TransportError
from .katapult_proto import KatapultClient, KatapultInfo
from .klipper_proto import McuDictionary, read_dictionary
from .registry import Registry
from .transport import Transport, port_users

KLIPPER = "klipper"
KATAPULT = "katapult"
SILENT = "silent"
BUSY = "busy"
MISSING = "missing"

CONF_OVERRIDE = "override"
CONF_MAPPED = "mapped"
CONF_VERIFIED = "verified"
CONF_HINT = "hint"
_CONF_RANK = {CONF_HINT: 0, CONF_VERIFIED: 1, CONF_MAPPED: 2, CONF_OVERRIDE: 3}


@dataclass
class PortProbe:
    path: str
    state: str
    baud: Optional[int] = None
    dictionary: Optional[McuDictionary] = None
    katapult: Optional[KatapultInfo] = None
    users: List[Tuple[int, str]] = field(default_factory=list)
    detail: str = ""

    @property
    def mcu(self) -> str:
        if self.dictionary and self.dictionary.mcu:
            return self.dictionary.mcu
        if self.katapult:
            return self.katapult.mcu
        return ""

    @property
    def alive(self) -> bool:
        return self.state in (KLIPPER, KATAPULT)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"port": self.path, "state": self.state, "baud": self.baud, "mcu": self.mcu, "detail": self.detail}
        if self.dictionary:
            out["klipper"] = self.dictionary.summary()
        if self.katapult:
            out["katapult"] = self.katapult.summary()
        if self.users:
            out["held_by"] = [{"pid": p, "cmd": c} for p, c in self.users]
        return out


Opener = Callable[[str, int], Transport]


def probe_port(
    path: str,
    opener: Opener,
    *,
    app_baud: int = 500000,
    katapult_bauds: Tuple[int, ...] = (250000,),
    users_fn: Callable[[str], List[Tuple[int, str]]] = port_users,
    exists_fn: Callable[[str], bool] = os.path.exists,
    identify_timeout: float = 1.0,
    clock=time.monotonic,
    sleep=time.sleep,
) -> PortProbe:
    """Read-only probe of one UART: Klipper identify, then Katapult connect."""
    if not exists_fn(path):
        return PortProbe(path, MISSING, detail="device node does not exist")
    users = users_fn(path)
    if users:
        names = ", ".join("%s (pid %d)" % (c.split()[0] if c else "?", p) for p, c in users)
        return PortProbe(path, BUSY, users=users, detail="held open by " + names)
    errors: List[str] = []
    # 1) a running Klipper (or Qidi's Klipper build) answers `identify`.
    try:
        with opener(path, app_baud) as tp:
            for _attempt in range(2):
                try:
                    dictionary = read_dictionary(tp, timeout=identify_timeout, clock=clock)
                    return PortProbe(path, KLIPPER, baud=app_baud, dictionary=dictionary)
                except ProtocolError as exc:
                    errors.append("klipper@%d: %s" % (app_baud, exc.message))
    except TransportError as exc:
        return PortProbe(path, "error", detail=exc.message)
    # 2) Katapult in the bootloader.
    for baud in dict.fromkeys(katapult_bauds + (app_baud,)):
        try:
            with opener(path, baud) as tp:
                info = KatapultClient(tp, clock=clock, sleep=sleep).connect(tries=2, timeout=0.6)
                return PortProbe(path, KATAPULT, baud=baud, katapult=info)
        except ProtocolError as exc:
            errors.append("katapult@%d: %s" % (baud, exc.message))
        except TransportError as exc:
            return PortProbe(path, "error", detail=exc.message)
    return PortProbe(path, SILENT, detail="; ".join(errors))


@dataclass
class RoleAssignment:
    board: str
    port: str
    confidence: str
    evidence: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"board": self.board, "port": self.port, "confidence": self.confidence, "evidence": self.evidence}


def assign_roles(
    probes: List[PortProbe],
    registry: Registry,
    *,
    stock_evidence: Optional[Dict[str, Tuple[str, List[str]]]] = None,
    known_map: Optional[Dict[str, Dict[str, Any]]] = None,
    overrides: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, RoleAssignment], List[str], List[str]]:
    """Bind boards to ports.  Returns (assignments, problems, notes).

    *overrides* is {board: port}; *known_map* is {board: {"port":..., "mcu":...}}
    from an earlier verified run; *stock_evidence* is stockcfg.stock_port_roles().
    """
    stock_evidence = stock_evidence or {}
    known_map = known_map or {}
    overrides = overrides or {}
    problems: List[str] = []
    live = {p.path: p for p in probes if p.alive}

    # votes[board][port] = (rank, [evidence...])
    votes: Dict[str, Dict[str, Tuple[int, List[str]]]] = {b: {} for b in registry.boards}

    def vote(board: str, port: str, conf: str, why: str) -> None:
        rank, ev = votes[board].get(port, (-1, []))
        votes[board][port] = (max(rank, _CONF_RANK[conf]), ev + [why])

    for board, port in overrides.items():
        if board not in registry.boards:
            problems.append("unknown board in --assign: %s" % board)
        elif port not in live:
            problems.append("--assign %s=%s but nothing answers on that port" % (board, port))
        else:
            vote(board, port, CONF_OVERRIDE, "operator assignment")
    for board, info in known_map.items():
        port = info.get("port")
        if board in votes and port in live and (not info.get("mcu") or info.get("mcu") == live[port].mcu):
            vote(board, port, CONF_MAPPED, "previously verified mapping")
    for port, (board, evidence) in stock_evidence.items():
        if board in votes and port in live:
            for why in evidence:
                vote(board, port, CONF_VERIFIED, why)
    # A chip name that fits exactly one board is evidence; one that fits both is not.
    for port, probe in live.items():
        fitting = [b for b in registry.boards.values() if b.mcu_matches(probe.mcu)] if probe.mcu else []
        if len(fitting) == 1 and len(registry.boards) > 1:
            vote(fitting[0].name, port, CONF_VERIFIED, "MCU %s only fits the %s board" % (probe.mcu, fitting[0].name))
    for board in registry.boards.values():
        if board.serial_hint in live:
            vote(board.name, board.serial_hint, CONF_HINT, "default wiring of this printer (%s)" % board.serial_hint)

    assignments: Dict[str, RoleAssignment] = {}
    used: Dict[str, str] = {}
    for board in sorted(registry.boards.values(), key=lambda b: b.order):
        cand = votes[board.name]
        if not cand:
            problems.append("no live MCU found for the %s board" % board.name)
            continue
        best_rank = max(rank for rank, _ in cand.values())
        best_ports = [p for p, (rank, _) in cand.items() if rank == best_rank]
        if len(best_ports) != 1:
            problems.append(
                "%s board is ambiguous: ports %s have equal evidence; pass --assign %s=<port>"
                % (board.name, ", ".join(sorted(best_ports)), board.name)
            )
            continue
        port = best_ports[0]
        rank, evidence = cand[port]
        # The chip that answers is the final word: a port can never be bound to a board whose chip it is not.
        answered = live[port].mcu
        if answered and not board.mcu_matches(answered):
            problems.append(
                "%s board cannot be on %s: the board there answers as %s (expected %s)"
                % (board.name, port, answered, "/".join(board.mcu_candidates))
            )
            continue
        # Strongest evidence for a *different* board on the same port is a conflict.
        conf = [k for k, v in _CONF_RANK.items() if v == rank][0]
        if port in used:
            problems.append("ports conflict: %s and %s both resolve to %s" % (used[port], board.name, port))
            continue
        used[port] = board.name
        assignments[board.name] = RoleAssignment(board.name, port, conf, evidence)

    # Evidence-based disagreement with the hint is worth surfacing even if resolved.
    notes: List[str] = []
    for name, a in assignments.items():
        hint = registry.boards[name].serial_hint
        if a.port != hint and a.confidence != CONF_OVERRIDE:
            notes.append("%s board found on %s, not its usual %s (%s)" % (name, a.port, hint, "; ".join(a.evidence)))
    return assignments, problems, notes


def require_verified(assignments: Dict[str, RoleAssignment]) -> List[str]:
    """Reasons flashing must not proceed (hint-only identification)."""
    return [
        "%s board on %s is identified only by the default wiring hint; confirm with --assign or a printer.cfg"
        % (a.board, a.port)
        for a in assignments.values()
        if _CONF_RANK[a.confidence] < _CONF_RANK[CONF_VERIFIED]
    ]
