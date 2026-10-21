# SPDX-License-Identifier: Apache-2.0
"""The release-channel client: check, upgrade, post-boot verification, rollback."""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.events import EventSink
from eitri_mcuflash.proc import Runner, run

from . import fetch, health as health_mod, minisign
from .bootenv import BootControl
from .manifest import Bundle, Manifest, parse_manifest, plan_upgrade, version_key
from .rauc import RaucClient
from .state import State

DEFAULT_MANIFEST_URL = "https://github.com/eitri-fw/eitri/releases/latest/download/manifest.json"
DEFAULT_KEY_DIRS = ["/usr/share/eitri/keys", "/etc/eitri/keys"]
CONF_PATH = "/etc/eitri/update.conf"


@dataclass
class Config:
    manifest_url: str = DEFAULT_MANIFEST_URL
    key_paths: List[str] = field(default_factory=lambda: list(DEFAULT_KEY_DIRS))
    data_root: str = "/data"
    allow_insecure: bool = False
    moonraker_url: str = "http://127.0.0.1:7125"
    health_timeout: float = 120.0
    mcu_firmware_root: str = "/data/eitri/mcu/firmware"
    mcu_state_dir: str = "/data/eitri/mcu"

    @property
    def eitri_root(self) -> str:
        return os.path.join(self.data_root, "eitri")

    @property
    def updates_dir(self) -> str:
        return os.path.join(self.eitri_root, "updates")

    @classmethod
    def load(cls, path: str = CONF_PATH) -> "Config":
        cfg = cls()
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if k == "manifest_url":
                        cfg.manifest_url = v
                    elif k == "key_dir":
                        cfg.key_paths = [v]
        except OSError:
            pass
        return cfg


class Updater:
    def __init__(
        self,
        config: Config,
        events: EventSink,
        *,
        runner: Runner = run,
        opener=fetch._open,
        rauc: Optional[RaucClient] = None,
        bootctl: Optional[BootControl] = None,
        state: Optional[State] = None,
        reboot: Optional[Callable[[], None]] = None,
        health_check: Callable[..., health_mod.Health] = health_mod.check,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg, self.events, self.runner, self.opener = config, events, runner, opener
        self.rauc = rauc or RaucClient(runner)
        self.bootctl = bootctl or BootControl(runner=runner)
        self.state = state or State(config.eitri_root)
        self._reboot = reboot or (lambda: self.runner(["systemctl", "reboot"]))
        self.health_check = health_check
        self.clock, self.sleep = clock, sleep
        self._mcu_partial: List[str] = []

    # ---- manifest ------------------------------------------------------------------
    def fetch_manifest(self, *, allow_older: bool = False) -> Manifest:
        url = self.cfg.manifest_url
        self.events.step("manifest", "start", msg="fetching %s" % url)
        data = fetch.fetch_small(url, opener=self.opener, allow_insecure=self.cfg.allow_insecure)
        sig_text = fetch.fetch_small(url + ".minisig", opener=self.opener, allow_insecure=self.cfg.allow_insecure, limit=4096)
        try:
            keys = minisign.load_keys(self.cfg.key_paths)
            sig = minisign.parse_signature(sig_text.decode("utf-8", "replace"))
            signer = minisign.verify_bytes(data, sig, keys)
        except minisign.SignatureError as exc:
            self.events.step("manifest", "failed", msg=str(exc))
            raise FlashError(
                "the channel manifest is not signed by a trusted Eitri key: %s" % exc,
                code="bad_signature",
                step="manifest",
                recovery="Nothing was changed. If you recently updated, the signing key may have rotated: update the eitri-keys package from a trusted source.",
            ) from exc
        manifest = parse_manifest(data)
        stored = self.state.load().get("manifest_sequence", -1)
        if manifest.sequence < stored and not allow_older:
            raise FlashError(
                "the manifest (sequence %d) is older than one already seen (%d): refusing a possible rollback attack" % (manifest.sequence, stored),
                code="stale_manifest", step="manifest")
        if manifest.expired() and datetime.datetime.now().year >= 2026:
            raise FlashError("the signed manifest has expired (%s); the mirror may be stale" % manifest.expires, code="expired_manifest", step="manifest")
        self.events.step("manifest", "ok", msg="signed by key %s: %s" % (signer.key_id_hex, sig.trusted_comment))
        return manifest

    # ---- check ---------------------------------------------------------------------------
    def check(self) -> Dict[str, Any]:
        manifest = self.fetch_manifest()
        installed = self.state.installed_version
        latest = manifest.latest()
        available = installed is None or version_key(latest.version) > version_key(installed)
        result = {
            "installed": installed, "latest": latest.version, "update_available": available, "channel_mode": self.state.channel,
            "flavor": self.state.flavor, "notes_url": latest.notes_url,
            "download_bytes": sum(b.size for b in latest.bundles_for(self.state.flavor)),
        }
        self.state.update(last_check=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), manifest_sequence=manifest.sequence, latest_seen=latest.version)
        self.events.result(True, data=result)
        return result

    # ---- upgrade ---------------------------------------------------------------------------
    def upgrade(self, *, target: Optional[str] = None, dry_run: bool = False, allow_downgrade: bool = False,
                reboot: bool = True) -> Dict[str, Any]:
        if self.state.channel == "upstream":
            raise FlashError(
                "this printer follows upstream; release bundles are not applied in that mode",
                code="upstream_mode", recovery="Run `eitri-update upstream disable` to return to the Eitri channel first.")
        manifest = self.fetch_manifest()
        installed = self.state.installed_version
        flavor = self.state.flavor
        release, bundles = plan_upgrade(manifest, installed, flavor, target, allow_downgrade)
        total = sum(b.size for b in bundles)
        plan = {"from": installed, "to": release.version, "flavor": flavor, "bundles": [{"kind": b.kind, "size": b.size, "url": b.url} for b in bundles]}
        self.events.emit("plan", msg="%s -> %s (%d MiB)" % (installed or "?", release.version, total >> 20), data=plan)
        if dry_run:
            self.events.result(True, msg="dry run: nothing was downloaded or installed", data=plan)
            return plan
        need = int(total * 1.1) + (64 << 20)
        free = fetch.free_bytes(self.cfg.updates_dir)
        if free < need:
            raise FlashError("not enough space for the update: need %d MiB, have %d MiB on /data" % (need >> 20, free >> 20), code="no_space",
                             recovery="Delete old gcode/timelapse files and try again.")
        status = self.rauc.status()
        if status.booted is None:
            raise FlashError("rauc reports no booted slot; this does not look like an Eitri A/B system", code="rauc")
        self._check_slot_layout(status)
        files = self._download(bundles)
        self.state.update(pending={"to": release.version, "from": installed, "flavor": flavor, "remaining": [], "mcu_flashed": [],
                                    "slot_before": status.booted, "phase": "installing",
                                    "commit": (release.flavors.get(flavor) or {}).get("commit")}, manifest_sequence=manifest.sequence)
        os_bundles = [b for b in bundles if b.kind == "os"]
        data_bundles = [b for b in bundles if b.kind == "data"]
        # OS first: it writes the inactive slot and flips the boot selection; the running system is untouched.
        for b in os_bundles:
            self._rauc_install(files[b.sha256], b)
        pending = self.state.load()["pending"]
        pending["remaining"] = [files[b.sha256] for b in data_bundles]
        pending["restart_services"] = sorted({s for b in data_bundles for s in b.restart_services})
        pending["phase"] = "await-reboot"
        self.state.update(pending=pending)
        self.events.step("install", "ok", msg="%s is installed in the inactive slot; it becomes active after a reboot" % release.version)
        if reboot:
            self.events.info("rebooting into %s" % release.version)
            self._reboot()
        self.events.result(True, msg="update staged", data=plan)
        return plan

    def _check_slot_layout(self, status) -> None:
        """An in-place conversion keeps its rootfs slots in logical partitions 5 and 6 (p3 is the extended
        container); RAUC writing to a stale p2/p3 from the flash-once layout would destroy the partition table.
        /boot/eitri/layout.env records the truth; RAUC's slots must agree with it before anything is written."""
        layout = self.bootctl.layout()
        for slot in ("A", "B"):
            want = layout.get("eitri_part_%s" % slot)
            if not want:
                continue
            info = status.slot(slot)
            m = re.search(r"(\d+)$", info.device) if info is not None else None
            if m is None or m.group(1) != want:
                raise FlashError(
                    "RAUC's slot %s is %s but this printer's slot %s is partition %s: refusing to write an update there"
                    % (slot, (info.device if info is not None and info.device else "unknown"), slot, want),
                    code="slot_layout",
                    step="install",
                    recovery="Nothing was changed. /etc/rauc/system.conf must name the in-place layout (partitions %s and %s); "
                             "the conversion writes that override into /data/eitri/state/etc/upper/rauc/system.conf."
                             % (layout.get("eitri_part_A", "?"), layout.get("eitri_part_B", "?")),
                )

    def _download(self, bundles: List[Bundle]) -> Dict[str, str]:
        files: Dict[str, str] = {}
        for b in bundles:
            dest = os.path.join(self.cfg.updates_dir, b.filename)
            step = "download-%s" % b.kind
            self.events.step(step, "start", msg="%s bundle, %d MiB" % (b.kind, b.size >> 20))
            fetch.download(b.url, dest, sha256=b.sha256, size=b.size, opener=self.opener, allow_insecure=self.cfg.allow_insecure,
                           progress=lambda got, total, s=step: self.events.progress(s, 100.0 * got / total))
            self.events.step(step, "ok")
            files[b.sha256] = dest
        return files

    def _rauc_install(self, path: str, bundle: Bundle) -> None:
        step = "install-%s" % bundle.kind
        self.events.step(step, "start", msg="installing the %s bundle" % bundle.kind)
        try:
            self.rauc.install(path, lambda pct, msg: self.events.progress(step, pct, msg=msg))
        except FlashError:
            self.events.step(step, "failed")
            self.state.update(pending=None)
            raise
        self.events.step(step, "ok")

    # ---- after the reboot ---------------------------------------------------------------------
    # Two systemd units, because the work straddles Klipper's start:
    #   eitri-prepare.service  (Before=klipper)  install queued data bundles, put matching firmware on the MCUs
    #   eitri-verify.service   (After=klipper)   health check, then commit the slot or roll back
    def _install_env(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            with open(os.path.join(self.cfg.eitri_root, "state", "install.env")) as fh:
                for line in fh:
                    if "=" in line:
                        k, _, v = line.rstrip("\n").partition("=")
                        out[k] = v
        except OSError:
            pass
        return out

    def prepare(self) -> Dict[str, Any]:
        doc = self.state.load()
        pending = doc.get("pending")
        slot = self.bootctl.current_slot()
        if pending:
            if pending.get("phase") == "installing":
                self.events.warn("an earlier update was interrupted before it was staged; discarding it")
                self.state.update(pending=None)
                return {"pending": False}
            if pending.get("slot_before") == slot:
                # The boot manager did not move to the new system (it fell back, or the staging never took effect).
                self.events.warn("the update to %s did not take effect (still on slot %s); discarding it" % (pending.get("to"), slot))
                self.state.update(pending=None)
                self.state.add_history("update-not-applied", to=pending.get("to"))
                return {"pending": False}
            try:
                for path in list(pending.get("remaining", [])):
                    self.events.step("install-data", "start", msg=os.path.basename(path))
                    self.rauc.install(path, lambda pct, msg: self.events.progress("install-data", pct, msg=msg))
                    pending["remaining"].remove(path)
                    self.state.update(pending=pending)
                    self.events.step("install-data", "ok")
                self._mcu_sync(pending)
            except FlashError as exc:
                self.events.step("post-boot", "failed", msg=exc.message)
                return self._roll_back(pending, slot, "post-boot step failed: %s" % exc.message)
            return {"pending": True}
        # First boot after the in-place conversion: the MCUs still run the stock firmware.
        env = self._install_env()
        if env.get("mcu_flash") == "auto" and not doc.get("firstboot_mcu_done") and self.bootctl.get_state(slot) != "good":
            first: Dict[str, Any] = {}
            try:
                self._mcu_sync(first)
                self.state.update(firstboot_mcu_done=True)
            except FlashError as exc:
                self.events.step("mcu-sync", "failed", msg=exc.message)
                return self._roll_back(first, slot, "MCU flashing failed on first boot: %s" % exc.message)
        return {"pending": False}

    def verify(self, start_services: bool = False) -> Dict[str, Any]:
        doc = self.state.load()
        pending = doc.get("pending")
        slot = self.bootctl.current_slot()
        if not pending and self.bootctl.get_state(slot) == "good":
            self.events.result(True, msg="nothing to verify")
            return {"pending": False}
        return self._verify_and_commit(pending, slot, start_services)

    def post_boot(self) -> Dict[str, Any]:
        """Both halves in order (manual runs and tests; systemd uses the two units)."""
        res = self.prepare()
        if res.get("rolled_back"):
            return res
        return self.verify(start_services=True)

    def mcu_sync(self) -> Dict[str, Any]:
        """Flash whichever MCU does not run the firmware that belongs to this host."""
        pending = self.state.load().get("pending") or {}
        self._mcu_sync(pending)
        return {"mcu_flashed": pending.get("mcu_flashed", [])}

    def _mcu_sync(self, pending: Dict[str, Any]) -> None:
        pin = ["--commit", pending["commit"]] if pending.get("commit") else []
        plan = self._mcuflash(["plan"] + pin)
        boards = (plan or {}).get("boards", {})
        need = [name for name, b in boards.items() if b.get("needs_flash")]
        if not need:
            self.events.info("MCU firmware already matches the host")
            return
        self.events.step("mcu-sync", "start", msg="flashing MCU firmware for: %s" % ", ".join(need))
        try:
            self._mcuflash(["flash", "--apply", "--yes"] + pin + sum([["--only", n] for n in need], []), want_ok=True)
        except FlashError:
            # Boards flashed before the failing one now run the NEW firmware: remember them so a rollback puts
            # every board back, instead of leaving the two MCUs on different versions.
            done = [b for b in self._mcu_partial if b in need]
            if done:
                pending.setdefault("mcu_flashed", []).extend(b for b in done if b not in pending.get("mcu_flashed", []))
                if self.state.load().get("pending"):
                    self.state.update(pending=pending)
            raise
        pending.setdefault("mcu_flashed", []).extend(need)
        if self.state.load().get("pending"):
            self.state.update(pending=pending)
        self.events.step("mcu-sync", "ok")

    def _mcuflash(self, args: List[str], want_ok: bool = False) -> Optional[Dict[str, Any]]:
        argv = [sys.executable, "-m", "eitri_mcuflash", args[0], "--json", "--stop-services",
                "--flavor", self.state.flavor, "--firmware-root", self.cfg.mcu_firmware_root, "--state-dir", self.cfg.mcu_state_dir] + args[1:]
        result: Dict[str, Any] = {}
        self._mcu_partial: List[str] = []

        def on_line(line: str) -> None:
            try:
                ev = json.loads(line)
            except ValueError:
                return
            if ev.get("type") == "result":
                result.update(ev)
            elif self.events._json_out is not None or self.events.events is not None:
                ev["component"] = "mcuflash"
                self.events.emit(ev.pop("type"), **{k: v for k, v in ev.items() if k not in ("schema", "ts", "run", "component")})

        # `-m eitri_mcuflash` must find the same copy we imported: matters when running from a checkout (bin/eitri-update).
        import eitri_mcuflash
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(eitri_mcuflash.__file__)))
        pythonpath = os.pathsep.join(p for p in (pkg_parent, os.environ.get("PYTHONPATH", "")) if p)
        res = self.runner(argv, on_line=on_line, env={"PYTHONPATH": pythonpath})
        if want_ok and (res.returncode != 0 or not result.get("ok")):
            self._mcu_partial = sorted(((result.get("data") or {}).get("results") or {}).keys())
            raise FlashError("MCU flashing failed: %s" % (result.get("msg") or res.output.strip().splitlines()[-1:] or "no result"),
                             code="mcu_flash", step="mcu-sync", recovery="The previous MCU firmware was restored by eitri-mcuflash where possible.")
        return result.get("data")

    def _verify_and_commit(self, pending: Optional[Dict[str, Any]], slot: str, start_services: bool = False) -> Dict[str, Any]:
        self.events.step("health", "start", msg="waiting for Klipper and Moonraker")
        if start_services:
            for unit in ("moonraker", "klipper"):
                self.runner(["systemctl", "start", unit])
        h = self.health_check(self.cfg.moonraker_url, timeout=self.cfg.health_timeout)
        if not h.ok:
            self.events.step("health", "failed", msg=h.detail)
            return self._roll_back(pending, slot, "health check failed: %s" % h.detail)
        self.events.step("health", "ok", msg=h.detail)
        self.rauc.mark("good")
        self.bootctl.set_state(slot, "good")
        if pending:
            self.state.update(pending=None, installed_version=pending["to"])
            self.state.set_text("release", pending["to"])
            self.state.add_history("upgraded", **{"from": pending.get("from"), "to": pending["to"]})
            if self.state.load().get("needs_pin_reset") and self.state.channel == "eitri":
                self.state.update(needs_pin_reset=False)
        self.events.result(True, msg="slot %s confirmed good%s" % (slot, (", now on %s" % pending["to"]) if pending else ""))
        return {"pending": False, "confirmed": slot}

    # ---- rollback -------------------------------------------------------------------------------
    def _roll_back(self, pending: Optional[Dict[str, Any]], slot: str, why: str) -> Dict[str, Any]:
        other = self.bootctl.other(slot)
        self.events.step("rollback", "start", msg=why)
        for board in list((pending or {}).get("mcu_flashed", [])):
            # The MCUs were flashed for the new host; put them back before the old host starts.
            try:
                self._mcuflash(["rollback", board, "--apply", "--yes"], want_ok=True)
            except FlashError as exc:
                self.events.warn("could not restore the %s MCU firmware: %s" % (board, exc.message))
        if self.bootctl.get_state(other) == "good":
            self.bootctl.set_state(slot, "bad")
            self.bootctl.set_primary(other)
            target = "slot %s (previous system)" % other
        else:
            # No earlier Eitri system exists (first boot after conversion).  Reboot and let the
            # boot manager spend the remaining attempts; when they are gone it boots the stock system.
            target = "a retry; if it keeps failing the boot manager returns to the original QIDI system"
        self.state.update(pending=None)
        self.state.add_history("rolled-back", reason=why)
        self.events.step("rollback", "ok", msg="next boot: %s" % target)
        self.events.result(False, msg=why + " - rolling back")
        self._reboot()
        return {"rolled_back": True, "to_slot": other if self.bootctl.get_state(other) == "good" else None}

    def rollback(self) -> Dict[str, Any]:
        """Operator-requested: switch back to the other (previous) slot."""
        slot = self.bootctl.current_slot()
        other = self.bootctl.other(slot)
        status = self.rauc.status()
        info = status.slot(other)
        if info is None or self.bootctl.get_state(other) != "good":
            raise FlashError("slot %s does not hold a known-good system, so there is nothing safe to roll back to" % other,
                             code="no_rollback_target", recovery="To return to the original QIDI system use revert.sh.")
        self.events.step("rollback", "start", msg="returning to slot %s%s" % (other, (" (%s)" % info.version) if info.version else ""))
        self.bootctl.set_primary(other)
        self.state.add_history("rollback-requested", to_slot=other)
        self.events.step("rollback", "ok", msg="slot %s is selected; reboot to apply" % other)
        return {"to_slot": other}
