# SPDX-License-Identifier: Apache-2.0
"""Orchestration: probe -> plan/preflight -> prove recovery -> flash -> verify.

Dry-run is the default everywhere.  A dry run still talks to the MCUs (the
identify handshake is read-only) but never resets, erases or writes anything.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import firmware as fw
from .backup import BackupEntry, BackupStore
from .errors import FlashError
from .fsutil import atomic_write
from .guidance import recovery_steps
from .katapult_proto import KatapultClient
from .methods import (
    KATAPULT_METHOD,
    SDCARD_FILE,
    SDCARD_SERIAL,
    FlashContext,
    KatapultMethod,
    SdcardFileMethod,
    SdcardSerialMethod,
    _pct,
    choose_method,
    make_method,
)
from .proc import run
from .probe import (
    BUSY,
    KATAPULT,
    KLIPPER,
    PortProbe,
    RoleAssignment,
    assign_roles,
    probe_port,
    require_verified,
)
from .registry import BoardDef
from .stockcfg import find_printer_cfg, stock_port_roles

OK, WARN, BLOCK = "ok", "warn", "block"

EX_OK, EX_FAIL, EX_USAGE, EX_BLOCKED, EX_MANUAL = 0, 1, 2, 3, 4


@dataclass
class Check:
    id: str
    status: str
    detail: str
    board: Optional[str] = None
    remedy: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "status": self.status, "detail": self.detail, "board": self.board, "remedy": self.remedy}


@dataclass
class Options:
    flavor: str = "klipper"
    firmware_root: str = fw.DEFAULT_FIRMWARE_ROOT
    ports: List[str] = field(default_factory=list)
    assign: Dict[str, str] = field(default_factory=dict)
    printer_cfgs: List[str] = field(default_factory=list)
    state_dir: str = "/data/eitri/mcu"
    stop_services: List[str] = field(default_factory=list)
    roundtrip: bool = True
    stock_bins: Dict[str, str] = field(default_factory=dict)
    accept_no_mcu_backup: bool = False
    build: bool = False
    build_config_dir: Optional[str] = None
    check_version_lock: bool = True
    commit: Optional[str] = None          # pinned source commit (release.lock); the firmware must come from it
    max_passes: int = 3
    verify_timeout: float = 45.0
    sd_verify_timeout: float = 150.0
    only: List[str] = field(default_factory=list)
    origin: str = ""                       # "stock": backups taken now are of the ORIGINAL QIDI firmware (revert target)
    sd_tooling: bool = True                # check what spi_flash.py needs; off for pure backups (stage 1 on the stock system)


@dataclass
class BoardPlan:
    board: BoardDef
    probe: PortProbe
    role: RoleAssignment
    method: Optional[str] = None
    artifact: Optional[fw.FirmwareArtifact] = None
    checks: List[Check] = field(default_factory=list)
    roundtrip: bool = True
    build: Optional[Tuple[str, str]] = None  # (config file, output dir) when building on-device

    @property
    def blocked(self) -> bool:
        return any(c.status == BLOCK for c in self.checks)

    def steps(self) -> List[str]:
        if self.method == KATAPULT_METHOD:
            s = ["request bootloader", "read back + save current firmware"]
            s += ["write the saved image back and boot it (round trip)"] if self.roundtrip else []
            return s + ["request bootloader", "write new firmware", "verify flash read-back", "boot and check the version"]
        if self.method == SDCARD_SERIAL:
            return ["upload image to the board's SD card via spi_flash.py", "reset into SD bootloader", "wait for the new firmware and check the version"]
        if self.method == SDCARD_FILE:
            return ["copy image to the microSD card", "operator moves the card and presses RESET", "wait for the new firmware and check the version"]
        return []


@dataclass
class Plan:
    options: Options
    probes: List[PortProbe]
    roles: Dict[str, RoleAssignment]
    boards: List[BoardPlan]
    checks: List[Check]
    notes: List[str]

    @property
    def blocked(self) -> bool:
        return any(c.status == BLOCK for c in self.checks) or any(b.blocked for b in self.boards)

    def all_checks(self) -> List[Check]:
        out = list(self.checks)
        for b in self.boards:
            out.extend(b.checks)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flavor": self.options.flavor,
            "blocked": self.blocked,
            "ports": [p.to_dict() for p in self.probes],
            "roles": {k: v.to_dict() for k, v in self.roles.items()},
            "boards": {
                b.board.name: {
                    "port": b.role.port,
                    "method": b.method,
                    "steps": b.steps(),
                    "firmware": b.artifact.to_dict() if b.artifact else None,
                    "current_version": b.probe.dictionary.version if b.probe.dictionary else None,
                    "needs_flash": bool(b.artifact and (b.probe.dictionary is None or b.probe.dictionary.version != b.artifact.version)),
                }
                for b in self.boards
            },
            "checks": [c.to_dict() for c in self.all_checks()],
            "notes": self.notes,
        }


class Flasher:
    def __init__(self, ctx: FlashContext, options: Options) -> None:
        self.ctx = ctx
        self.opt = options
        self.events = ctx.events
        self.store = BackupStore(options.state_dir)

    # ---- state ------------------------------------------------------------------
    def _state_path(self, name: str) -> str:
        return os.path.join(self.opt.state_dir, name)

    def load_known_map(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self._state_path("mcu-map.json"), "r", encoding="utf-8") as fh:
                return json.load(fh).get("boards", {})
        except (OSError, ValueError):
            return {}

    def save_state(self, results: Dict[str, Dict[str, Any]]) -> None:
        known = self.load_known_map()
        for name, r in results.items():
            known[name] = {"port": r["port"], "mcu": r["mcu"], "version": r["version"], "flavor": r["flavor"]}
        atomic_write(self._state_path("mcu-map.json"), json.dumps({"schema": 1, "boards": known}, indent=2, sort_keys=True), mode=0o644)

    # ---- probing -----------------------------------------------------------------
    def candidate_ports(self) -> List[str]:
        if self.opt.ports:
            return list(self.opt.ports)
        ports: List[str] = []
        for b in self.ctx.registry.ordered():
            if b.serial_hint not in ports:
                ports.append(b.serial_hint)
        return ports

    def probe_all(self) -> List[PortProbe]:
        self.events.step("probe", "start", msg="identifying the MCUs on %s" % ", ".join(self.candidate_ports()))
        probes: List[PortProbe] = []
        app_baud = max((b.app_baud for b in self.ctx.registry.boards.values()), default=500000)
        kbauds = tuple(sorted({b.katapult_baud for b in self.ctx.registry.boards.values()}))
        for port in self.candidate_ports():
            p = probe_port(
                port, self.ctx.opener, app_baud=app_baud, katapult_bauds=kbauds, users_fn=self.ctx.users_fn,
                exists_fn=self.ctx.exists_fn, identify_timeout=self.ctx.identify_timeout, clock=self.ctx.clock, sleep=self.ctx.sleep,
            )
            probes.append(p)
            self.events.info(
                "%s: %s%s" % (port, p.state, (" (%s)" % p.mcu) if p.mcu else ""),
                step="probe",
                data=p.to_dict(),
            )
        self.events.step("probe", "ok", data={"ports": [p.to_dict() for p in probes]})
        return probes

    # ---- planning / preflight -------------------------------------------------------
    def make_plan(self, probes: List[PortProbe]) -> Plan:
        reg = self.ctx.registry
        checks: List[Check] = []
        self.events.step("preflight", "start")

        for p in probes:
            if p.state == BUSY:
                checks.append(Check("port", BLOCK, "%s is %s" % (p.path, p.detail),
                                    remedy="Stop the process (eitri-mcuflash --stop-services does this for klipper) and retry."))
            elif not p.alive:
                checks.append(Check("port", BLOCK, "%s: %s%s" % (p.path, p.state, (" (%s)" % p.detail) if p.detail else ""),
                                    remedy="Check that the printer is powered and the UART is not claimed by a serial console."))

        stock: Dict[str, Tuple[str, List[str]]] = {}
        for cfg in find_printer_cfg(self.opt.printer_cfgs):
            for port, (board, ev) in stock_port_roles(cfg, reg).items():
                prev = stock.get(port)
                if prev and prev[0] != board:
                    checks.append(
                        Check("roles", BLOCK, "printer.cfg files disagree about %s (%s vs %s)" % (port, prev[0], board))
                    )
                stock[port] = (board, (prev[1] if prev else []) + ev)
        roles, problems, notes = assign_roles(
            probes, reg, stock_evidence=stock, known_map=self.load_known_map(), overrides=self.opt.assign
        )
        for p in problems:
            checks.append(Check("roles", BLOCK, p, remedy="Use --assign mainboard=<port> toolhead=<port> after checking printer.cfg."))
        for w in require_verified(roles):
            checks.append(Check("roles", BLOCK, w))
        if roles and not problems and not require_verified(roles):
            checks.append(Check("roles", OK, "; ".join("%s=%s (%s)" % (r.board, r.port, r.confidence) for r in roles.values())))

        try:
            self.store.check_writable()
            checks.append(Check("backup_store", OK, "backups go to %s" % self.store.root))
        except FlashError as exc:
            checks.append(Check("backup_store", BLOCK, exc.message, remedy=exc.recovery))

        by_port = {p.path: p for p in probes}
        board_plans: List[BoardPlan] = []
        wanted = set(self.opt.only)
        for board in reg.ordered():
            if wanted and board.name not in wanted:
                continue
            role = roles.get(board.name)
            if role is None:
                continue
            bp = BoardPlan(board=board, probe=by_port[role.port], role=role)
            bp.roundtrip = self.opt.roundtrip
            self._check_board(bp)
            board_plans.append(bp)
        for name in wanted:
            if name not in reg.boards:
                checks.append(Check("roles", BLOCK, "unknown board %r" % name))

        plan = Plan(self.opt, probes, roles, board_plans, checks, notes)
        for c in plan.all_checks():
            self.events.log(
                {"ok": "info", "warn": "warn", "block": "error"}[c.status],
                "%s: %s" % (c.id, c.detail),
                board=c.board,
                step="preflight",
                data=c.to_dict(),
            )
        self.events.step(
            "preflight",
            "failed" if plan.blocked else "ok",
            msg="refusing to flash: %d blocking issue(s)" % sum(1 for c in plan.all_checks() if c.status == BLOCK) if plan.blocked else None,
        )
        return plan

    def _check_commit(self, bp: BoardPlan) -> None:
        """--commit: the firmware must be built from exactly the commit the release pins."""
        want = (self.opt.commit or "").lower()
        if not want:
            return
        name, add = bp.board.name, bp.checks.append
        if bp.build:                                     # building on the device: the clone's HEAD is what gets built
            res = self.ctx.runner(["git", "-C", self.ctx.klipper_dir, "rev-parse", "HEAD"])
            have = res.output.strip().lower() if res.returncode == 0 else ""
        elif bp.artifact is not None:
            have = bp.artifact.source_commit.lower()
        else:
            return
        if not have:
            add(Check("commit", WARN, "cannot tell which commit the firmware comes from; wanted %s" % want[:12], name))
        elif have.startswith(want) or want.startswith(have):
            add(Check("commit", OK, "firmware is built from %s" % have[:12], name))
        else:
            add(Check("commit", BLOCK, "firmware is built from %s but %s is pinned" % (have[:12], want[:12]), name,
                      "Use the firmware that belongs to the pinned release, or update the pin deliberately."))

    def _stock_image_problem(self, board, path: str) -> Tuple[str, str]:
        """(blocking problem, warning) for an operator-supplied stock image."""
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            return "cannot read the stock image %s: %s" % (path, exc.strerror), ""
        if len(blob) < 1024:
            return "the stock image %s is only %d bytes: not a firmware image" % (path, len(blob)), ""
        doc = fw.extract_embedded_dictionary(blob)
        if doc is None:
            return "", "no Klipper data dictionary inside, so its chip could not be checked"
        mcu = str((doc.get("config") or {}).get("MCU", ""))
        if mcu and not board.mcu_matches(mcu):
            return "the stock image %s is built for %s, not for the %s board" % (path, mcu, board.name), ""
        return "", ""

    def _check_board(self, bp: BoardPlan) -> None:
        board, probe, add = bp.board, bp.probe, bp.checks.append
        name = board.name
        add(Check("port", OK, "%s answers as %s%s" % (probe.path, probe.state, (", MCU " + probe.mcu) if probe.mcu else ""), name))
        if probe.dictionary is not None and probe.dictionary.app and probe.dictionary.app != "Klipper":
            add(Check("firmware_app", WARN, "board reports app %r, not Klipper" % probe.dictionary.app, name))

        # firmware
        try:
            if self.opt.build:
                cfg = os.path.join(self.opt.build_config_dir or os.path.join(self.opt.firmware_root, "configs", self.opt.flavor), name + ".config")
                out = os.path.join(self.opt.state_dir, "build", name)
                bp.artifact = None
                add(Check("firmware", OK, "will build %s from %s with %s" % (name, self.ctx.klipper_dir, cfg), name))
                bp.build = (cfg, out)
            else:
                bp.artifact = fw.load_artifact(self.opt.firmware_root, self.opt.flavor, name, expect_mcu=probe.mcu or None)
                add(Check("firmware", OK, "%s %s (%s, sha256 %s…)" % (self.opt.flavor, bp.artifact.version, bp.artifact.mcu, bp.artifact.sha256[:12]), name))
        except FlashError as exc:
            add(Check("firmware", BLOCK, exc.message, name, exc.recovery))

        self._check_commit(bp)

        # method
        bp.method = choose_method(self.ctx, board, probe)
        if bp.method is None:
            add(Check("method", BLOCK, "no usable flash method for the %s board" % name, name,
                      "The board shows neither Katapult nor SD-card flashing support. Do not erase it; see the ST-Link recovery notes."))
            return
        add(Check("method", OK, bp.method, name))
        m = make_method(bp.method, self.ctx, board, bp.role.port)
        if isinstance(m, SdcardSerialMethod) and self.opt.sd_tooling:
            problems = m.preflight_problems()
            if problems and self.ctx.sd_mount and SDCARD_FILE in board.methods:
                # no way to run spi_flash.py here, but the operator offered a microSD card: carry the image by hand
                for p in problems:
                    add(Check("method", WARN, p + " - falling back to the microSD card", name))
                bp.method = SDCARD_FILE
            else:
                for p in problems:
                    add(Check("method", BLOCK, p, name))

        # geometry
        art = bp.artifact
        if art is not None:
            total = self.ctx.registry.flash_size(art.mcu)
            if probe.state == KATAPULT and probe.katapult and art.app_address is not None and art.app_address != probe.katapult.app_start:
                add(Check("offset", BLOCK,
                          "firmware is linked for 0x%08x but Katapult expects the application at 0x%08x" % (art.app_address, probe.katapult.app_start),
                          name, "Rebuild with the matching bootloader offset."))
            elif bp.method == KATAPULT_METHOD:
                add(Check("offset", WARN, "application offset is checked once the bootloader answers", name))
            if total is not None and art.size > total:
                add(Check("fit", BLOCK, "image (%d B) is larger than the chip's flash (%d B)" % (art.size, total), name))
            elif total is None:
                add(Check("fit", WARN, "unknown chip %s: flash size not checked" % art.mcu, name))

            # version lock: the point of all this.
            if self.opt.check_version_lock:
                if os.path.isdir(os.path.join(self.ctx.klipper_dir, ".git")):
                    try:
                        hv = fw.host_version(self.ctx.klipper_dir, self.ctx.runner)
                        if fw.versions_match(hv, art.version):
                            add(Check("version_lock", OK, "MCU image and host are both %s" % hv, name))
                        else:
                            add(Check("version_lock", BLOCK, "MCU image is %s but the host Klipper is %s" % (art.version, hv), name,
                                      "Use firmware built from the host's commit (or update the host)."))
                    except FlashError as exc:
                        add(Check("version_lock", WARN, exc.message, name))
                else:
                    add(Check("version_lock", WARN, "no Klipper clone at %s yet; host/MCU version lock not checked" % self.ctx.klipper_dir, name))

        # recovery
        if bp.method == KATAPULT_METHOD:
            if probe.state == KATAPULT:
                add(Check("recovery", OK, "board is already in Katapult; re-flash is always possible", name))
            else:
                add(Check("recovery", WARN, "recovery is proven at apply time: the board must enter Katapult and its current firmware must be read back before anything is erased", name))
        else:
            stock = self.store.stock_image(name) or self.opt.stock_bins.get(name)
            if stock:
                problem, warning = self._stock_image_problem(board, stock)
                if problem:
                    add(Check("recovery", BLOCK, problem, name, "Pass the right QIDI image with --stock-bin %s=FILE." % name))
                elif warning:
                    add(Check("recovery", WARN, "stock image on hand: %s (%s)" % (stock, warning), name))
                else:
                    add(Check("recovery", OK, "stock image on hand: %s" % stock, name))
            elif self.opt.accept_no_mcu_backup:
                add(Check("recovery", WARN, "no stock image; relying on a stock qd_mcu.bin from QIDI's update package (acknowledged)", name))
            else:
                add(Check("recovery", BLOCK,
                          "the SD-card bootloader cannot be read back, and no stock image was supplied",
                          name,
                          "Pass --stock-bin %s=<stock qd_mcu.bin> (from QIDI's firmware package) or --accept-no-mcu-backup." % name))

    # ---- execution ---------------------------------------------------------------
    def execute(self, plan: Plan, apply: bool) -> Tuple[int, Dict[str, Any]]:
        data = plan.to_dict()
        data["dry_run"] = not apply
        if plan.blocked:
            self.events.result(False, msg="blocked by preflight", data=data)
            return EX_BLOCKED, data
        if not plan.boards:
            self.events.result(False, msg="no boards to flash", data=data)
            return EX_FAIL, data
        if not apply:
            for bp in plan.boards:
                self.events.emit("plan", board=bp.board.name, msg="would flash %s via %s" % (bp.board.name, bp.method),
                                 data={"steps": bp.steps(), "port": bp.role.port})
            self.events.result(True, msg="dry run: nothing was changed", data=data)
            return EX_OK, data

        results: Dict[str, Dict[str, Any]] = {}
        try:
            # Phase A: prove recovery for EVERY board before erasing ANY of them.
            for bp in plan.boards:
                self._build_if_needed(bp)
                self.prove_recovery(bp)
            # Phase B: flash.
            for bp in plan.boards:
                results[bp.board.name] = self.flash_board(bp)
        except FlashError as exc:
            self.events.step(exc.step or "flash", "failed", board=exc.board, msg=exc.message, data=exc.to_dict())
            data["error"] = exc.to_dict()
            data["results"] = results
            data["recovery"] = self._recovery_text(plan)
            if results:
                self.save_state(results)
            self.events.result(False, msg=str(exc), data=data)
            return (EX_MANUAL if exc.code == "manual_action" else EX_FAIL), data
        self.save_state(results)
        data["results"] = results
        self.events.result(True, msg="all MCUs flashed and verified", data=data)
        return EX_OK, data

    def backup_only(self, plan: Plan, apply: bool) -> Tuple[int, Dict[str, Any]]:
        """Phase A on its own: prove (and keep) a way back for every board without flashing anything.

        The in-place installer runs this on the stock system, before its point of no return."""
        data = plan.to_dict()
        data["dry_run"] = not apply
        if plan.blocked:
            self.events.result(False, msg="blocked by preflight", data=data)
            return EX_BLOCKED, data
        if not apply:
            for bp in plan.boards:
                self.events.emit("plan", board=bp.board.name, msg="would back up %s (%s)" % (bp.board.name, bp.method), data={"port": bp.role.port})
            self.events.result(True, msg="dry run: nothing was changed", data=data)
            return EX_OK, data
        saved: Dict[str, str] = {}
        try:
            for bp in plan.boards:
                self._build_if_needed(bp)
                entry = self.prove_recovery(bp)
                saved[bp.board.name] = entry.directory if entry else ""
        except FlashError as exc:
            self.events.step(exc.step or "backup", "failed", board=exc.board, msg=exc.message, data=exc.to_dict())
            data["error"] = exc.to_dict()
            data["backups"] = saved
            self.events.result(False, msg=str(exc), data=data)
            return EX_FAIL, data
        self.save_state({bp.board.name: {"port": bp.role.port, "mcu": bp.probe.mcu, "version": bp.probe.dictionary.version if bp.probe.dictionary else "",
                                         "flavor": self.opt.flavor} for bp in plan.boards})
        data["backups"] = saved
        self.events.result(True, msg="every board has a verified way back", data=data)
        return EX_OK, data

    def _recovery_text(self, plan: Plan) -> Dict[str, List[str]]:
        out = {}
        for bp in plan.boards:
            out[bp.board.name] = recovery_steps(bp.board, bp.method or "", self.store.latest_restorable(bp.board.name) is not None)
        return out

    def _build_if_needed(self, bp: BoardPlan) -> None:
        if not bp.build:
            return
        cfg, out = bp.build
        self.events.step("build", "start", board=bp.board.name)
        bp.artifact = fw.build_artifact(
            bp.board.name, self.opt.flavor, klipper_dir=self.ctx.klipper_dir, config_file=cfg, out_dir=out,
            runner=self.ctx.runner, on_line=lambda l: self.events.log("debug", l, board=bp.board.name, step="build"),
            check_toolchain=self.ctx.runner is run,
        )
        self.events.step("build", "ok", board=bp.board.name, data=bp.artifact.to_dict())

    # -- phase A ---------------------------------------------------------------------
    def prove_recovery(self, bp: BoardPlan) -> Optional[BackupEntry]:
        name = bp.board.name
        self.events.step("backup", "start", board=name)
        info = {
            "port": bp.role.port,
            "method": bp.method,
            "state": bp.probe.state,
            "mcu": bp.probe.mcu,
            "katapult": bp.probe.katapult.summary() if bp.probe.katapult else None,
            "dictionary": bp.probe.dictionary.summary() if bp.probe.dictionary else None,
        }
        dict_raw = bp.probe.dictionary.raw if bp.probe.dictionary else None
        if self.opt.origin:
            info["origin"] = self.opt.origin
        if bp.method != KATAPULT_METHOD:
            # This board's flash cannot be read back, so the only way back is the operator-supplied image.
            # Keep our own copy now: the rollback paths (and revert.sh) look only in the state directory.
            supplied = self.opt.stock_bins.get(name)
            if supplied:
                info["stock_image"] = self.store.add_stock_image(name, supplied)
            entry = self.store.save(name, app=None, dictionary=dict_raw, info=info)
            self.events.step("backup", "ok", board=name, msg="recorded firmware identity; flash cannot be read back on this board",
                             data={"dir": entry.directory})
            return entry
        method = KatapultMethod(self.ctx, bp.board, bp.role.port)
        in_bl = bp.probe.state == KATAPULT
        try:
            with method.bootloader(already_in_bootloader=in_bl) as client:
                self._check_bootloader_against_firmware(bp, client)
                app = method.read_application(client, bp.probe.mcu, _pct(self.events, "backup", name))
                info["katapult"] = client.info.summary() if client.info else None
                info["app_start"] = client.info.app_start if client.info else None
                entry = self.store.save(name, app=app, dictionary=dict_raw, info=info)
                self.events.step("backup", "ok", board=name, data={"dir": entry.directory, "bytes": len(app)})
                if in_bl:
                    pass  # no application to restore; the board stays in the bootloader for the flash phase
                elif self.opt.roundtrip:
                    self.events.step("roundtrip", "start", board=name, msg="writing the saved image back to prove the reflash path")
                    method.flash_image(client, app, entry.app_path or "", _pct(self.events, "roundtrip", name))
                else:
                    self.events.step("roundtrip", "skipped", board=name)
                    client.complete()
        except FlashError as exc:
            raise exc.with_context(board=name, port=bp.role.port, bootloader="katapult", step=exc.step or "backup") from exc
        if not in_bl:
            self._wait_for_app(bp, expect_version=bp.probe.dictionary.version if bp.probe.dictionary else None, timeout=self.opt.verify_timeout, step="roundtrip")
            if self.opt.roundtrip:
                self.events.step("roundtrip", "ok", board=name)
        return entry

    def _check_bootloader_against_firmware(self, bp: BoardPlan, client: KatapultClient) -> None:
        info = client.info
        art = bp.artifact
        if info is None or art is None:
            return
        if info.mcu and art.mcu and info.mcu.lower() != art.mcu.lower():
            raise FlashError(
                "Katapult reports MCU %s but the firmware is built for %s" % (info.mcu, art.mcu),
                code="mcu_mismatch", step="preflight",
                recovery="Use firmware built for this chip; nothing has been erased.")
        if art.app_address is not None and art.app_address != info.app_start:
            raise FlashError(
                "firmware is linked for 0x%08x but Katapult starts applications at 0x%08x" % (art.app_address, info.app_start),
                code="offset_mismatch", step="preflight",
                recovery="Rebuild with the bootloader offset the board actually has; nothing has been erased.")
        total = self.ctx.registry.flash_size(info.mcu or art.mcu)
        if total is not None:
            room = self.ctx.registry.flash_base(info.mcu or art.mcu) + total - info.app_start
            if art.size > room:
                raise FlashError("image (%d B) does not fit above the bootloader (%d B free)" % (art.size, room),
                                 code="fit", step="preflight")

    # -- phase B ---------------------------------------------------------------------
    def flash_board(self, bp: BoardPlan) -> Dict[str, Any]:
        name, art = bp.board.name, bp.artifact
        assert art is not None
        self.events.step("flash", "start", board=name, msg="%s via %s" % (art.version, bp.method))
        last: Optional[FlashError] = None
        for attempt in range(1, self.opt.max_passes + 1):
            try:
                self._flash_once(bp, art)
                self._wait_for_app(
                    bp, expect_version=art.version, expect_mcu=art.mcu,
                    timeout=self.opt.sd_verify_timeout if bp.method != KATAPULT_METHOD else self.opt.verify_timeout, step="verify",
                )
                self.events.step("flash", "ok", board=name)
                return {"port": bp.role.port, "mcu": art.mcu, "version": art.version, "flavor": self.opt.flavor,
                        "sha256": art.sha256, "method": bp.method, "attempts": attempt}
            except FlashError as exc:
                last = exc.with_context(board=name, port=bp.role.port, step="flash")
                if exc.code == "manual_action":
                    raise
                self.events.warn("pass %d/%d failed: %s" % (attempt, self.opt.max_passes, exc.message), board=name, step="flash")
                if exc.code in ("mcu_mismatch", "offset_mismatch", "fit", "no_bootloader"):
                    break
        assert last is not None
        self.events.step("rollback", "start", board=name)
        try:
            self.rollback_board(bp)
            self.events.step("rollback", "ok", board=name, msg="previous firmware restored")
            last.recovery = (last.recovery or "") + " The previous firmware was restored automatically."
        except FlashError as rb:
            self.events.step("rollback", "failed", board=name, msg=rb.message)
            last.recovery = "Automatic rollback failed (%s). " % rb.message + " | ".join(recovery_steps(bp.board, bp.method or "", True))
        raise last

    def _flash_once(self, bp: BoardPlan, art: fw.FirmwareArtifact) -> None:
        name = bp.board.name
        prog = _pct(self.events, "flash", name)
        if bp.method == KATAPULT_METHOD:
            method = KatapultMethod(self.ctx, bp.board, bp.role.port)
            with method.bootloader() as client:
                self._check_bootloader_against_firmware(bp, client)
                method.flash_image(client, art.read(), art.path, prog)
        elif bp.method == SDCARD_SERIAL:
            SdcardSerialMethod(self.ctx, bp.board, bp.role.port).flash_image(art.path, art, prog)
        elif bp.method == SDCARD_FILE:
            dest = SdcardFileMethod(self.ctx, bp.board, bp.role.port).stage(art.path)
            self.events.emit("prompt", board=name, msg="Move the microSD card (%s) to the %s board and press its RESET button." % (dest, name))
        else:
            raise FlashError("no flash method", code="method", board=name)

    def _wait_for_app(self, bp: BoardPlan, *, expect_version: Optional[str], timeout: float, step: str,
                      expect_mcu: Optional[str] = None) -> PortProbe:
        name = bp.board.name
        self.events.step(step, "start", board=name, msg="waiting for the firmware to answer")
        deadline = self.ctx.clock() + timeout
        last = "no answer"
        while self.ctx.clock() < deadline:
            self.ctx.sleep(1.0)
            p = probe_port(bp.role.port, self.ctx.opener, app_baud=bp.board.app_baud,
                           katapult_bauds=(bp.board.katapult_baud,), users_fn=lambda _p: [],
                           exists_fn=self.ctx.exists_fn, identify_timeout=self.ctx.identify_timeout,
                           clock=self.ctx.clock, sleep=self.ctx.sleep)
            if p.state == KLIPPER and p.dictionary is not None:
                d = p.dictionary
                if expect_mcu and d.mcu.lower() != expect_mcu.lower():
                    last = "MCU reports %s, expected %s" % (d.mcu, expect_mcu)
                elif expect_version and d.version != expect_version:
                    last = "firmware version is %s, expected %s" % (d.version, expect_version)
                else:
                    self.events.step(step, "ok", board=name, msg="running %s" % d.version)
                    return p
            elif p.state == KATAPULT:
                last = "board stayed in the bootloader (no valid application)"
            else:
                last = p.detail or p.state
        raise FlashError(
            "board did not come back with the expected firmware: %s" % last,
            code="verify_failed", step=step, board=name, port=bp.role.port,
            recovery=" | ".join(recovery_steps(bp.board, bp.method or "", True)),
        )

    # -- rollback ----------------------------------------------------------------------
    def rollback_problems(self, bp: BoardPlan, original: bool = False) -> List[str]:
        """Everything that would stop rollback_board from succeeding, found WITHOUT touching the board.  revert.sh
        runs this for every board before it flashes any, so a board that cannot go back never leaves the other one
        already reverted."""
        name = bp.board.name
        if bp.method == KATAPULT_METHOD:
            entry = self.store.original_restorable(name) if original else self.store.latest_restorable(name)
            if entry is None or not entry.app_path:
                damaged = self.store.damaged(name)
                return ["no saved firmware image for the %s board%s" % (name, (" (unreadable backup entries: %s)" % ", ".join(damaged)) if damaged else "")]
            try:
                entry.verify_image()
            except FlashError as exc:
                return [exc.message]
            return []
        stock = self.store.stock_image(name) or self.opt.stock_bins.get(name)
        if bp.method == SDCARD_SERIAL:
            if not stock:
                return ["no stock firmware image for the %s board (pass --stock-bin %s=FILE)" % (name, name)]
            return ["the %s board: %s" % (name, p) for p in SdcardSerialMethod(self.ctx, bp.board, bp.role.port).preflight_problems()]
        return ["the %s board cannot be rolled back automatically (method %s)" % (name, bp.method or "unknown")]

    def rollback_board(self, bp: BoardPlan, original: bool = False) -> None:
        """Put back the firmware saved before the last flash - or, with *original*, the one the printer
        shipped with (the revert-to-stock target, whatever Eitri updates happened since)."""
        name = bp.board.name
        if bp.method == KATAPULT_METHOD:
            entry = self.store.original_restorable(name) if original else self.store.latest_restorable(name)
            if entry is None or not entry.app_path:
                damaged = self.store.damaged(name)
                raise FlashError("no saved firmware image for the %s board%s" % (
                    name, (" (unreadable backup entries: %s)" % ", ".join(damaged)) if damaged else ""), code="no_backup", step="rollback", board=name)
            entry.verify_image()
            method = KatapultMethod(self.ctx, bp.board, bp.role.port)
            image = open(entry.app_path, "rb").read()
            with method.bootloader() as client:
                method.flash_image(client, image, entry.app_path, _pct(self.events, "rollback", name))
            prev = (entry.meta.get("dictionary") or {}).get("version")
            self._wait_for_app(bp, expect_version=prev, timeout=self.opt.verify_timeout, step="rollback")
            return
        stock = self.store.stock_image(name) or self.opt.stock_bins.get(name)
        if bp.method == SDCARD_SERIAL and stock:
            SdcardSerialMethod(self.ctx, bp.board, bp.role.port).flash_image(stock, None)
            self._wait_for_app(bp, expect_version=None, timeout=self.opt.sd_verify_timeout, step="rollback")
            return
        raise FlashError(
            "the %s board cannot be rolled back automatically" % name, code="no_backup", step="rollback", board=name,
            recovery=" | ".join(recovery_steps(bp.board, bp.method or "", False)),
        )

