# SPDX-License-Identifier: Apache-2.0
"""The release-channel client: check, upgrade, post-boot verification, rollback."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.events import EventSink
from eitri_mcuflash.proc import Runner, run
from eitri_mcuflash.registry import load_registry

from . import fetch, health as health_mod, mcufw, minisign, seed as seedlib
from .bootenv import BootControl
from .manifest import Bundle, Manifest, parse_manifest, plan_flavor_switch, plan_upgrade, version_key
from .rauc import RaucClient
from .state import State

DEFAULT_MANIFEST_URL = "https://github.com/eitri-fw/eitri/releases/latest/download/manifest.json"
# Only the keys that ship in the (RAUC-verified) OS image are trusted by default.  /data is writable by every service
# on the printer, so a key directory there is opt-in: name it with `key_dir=` in /etc/eitri/update.conf.
DEFAULT_KEY_DIRS = ["/usr/share/eitri/keys"]
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
    min_sequence_file: str = "/usr/share/eitri/min-manifest-sequence"
    seed_tool: Optional[str] = None                   # eitri-seed-install; None = /usr/libexec/eitri/eitri-seed-install
    overlay_command: Optional[List[str]] = None       # None = the layer's own overlay-install.sh on /data
    expect_abi: Optional[str] = None                  # interpreter ABI a seed must have; None = this Python's (cp314)
    services: Tuple[str, ...] = ("klarity", "moonraker", "klipper")    # stopped in this order, started in reverse

    @property
    def eitri_root(self) -> str:
        return os.path.join(self.data_root, "eitri")

    @property
    def application_paths(self) -> List[str]:
        """What klipper.service and moonraker.service need before they will start (their ConditionPathExists=)."""
        d = self.data_root
        return [os.path.join(d, p) for p in (
            "firmware/klipper/klippy/klippy.py", "venv/klippy/bin/python", "printer_data/config/printer.cfg",
            "firmware/moonraker/moonraker/moonraker.py", "venv/moonraker/bin/python", "printer_data/config/moonraker.conf")]

    def application_configured(self) -> bool:
        return all(os.path.exists(p) for p in self.application_paths)

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
        print_state: Callable[..., Optional[str]] = health_mod.print_state,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg, self.events, self.runner, self.opener = config, events, runner, opener
        self.rauc = rauc or RaucClient(runner)
        self.bootctl = bootctl or BootControl(
            runner=runner, confirmed_path=os.path.join(config.eitri_root, "state", "slot-confirmed.json"))
        self.state = state or State(config.eitri_root)
        self._reboot = reboot or (lambda: self.runner(["systemctl", "reboot"]))
        self.health_check = health_check
        self.print_state = print_state
        self.seedtool = seedlib.SeedTool(runner, config.seed_tool, config.data_root)
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
        stored = max(self.state.load().get("manifest_sequence", -1), self._min_sequence())
        if manifest.sequence < stored and not allow_older:
            raise FlashError(
                "the manifest (sequence %d) is older than one already seen (%d): refusing a possible rollback attack" % (manifest.sequence, stored),
                code="stale_manifest", step="manifest")
        if manifest.expired():
            raise FlashError("the signed manifest has expired (%s); the mirror may be stale" % manifest.expires, code="expired_manifest", step="manifest")
        self.events.step("manifest", "ok", msg="signed by key %s: %s" % (signer.key_id_hex, sig.trusted_comment))
        return manifest

    def _min_sequence(self) -> int:
        """The lowest sequence this OS image will accept: shipped inside the (RAUC-verified) image, so it survives a
        reset of /data, which would otherwise forget how far the channel had got."""
        try:
            with open(self.cfg.min_sequence_file) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return -1

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
        self._require_settled_system()
        seed_b = next((b for b in bundles if b.kind == "seed"), None)
        mcu_b = next((b for b in bundles if b.kind == "mcu"), None)
        # The seed's manifest is small, signed through the channel manifest and tells how much room the unpacked seed needs:
        # check it (and refuse a seed that does not belong to this release) before any big download.
        seed_info = self._fetch_seed_manifest(release, flavor, seed_b) if seed_b else None
        need = int(total * 1.1) + (64 << 20) + (int(seed_info["uncompressed_size"] * 1.1) if seed_info else 0)
        free = fetch.free_bytes(self.cfg.updates_dir)
        if free < need:
            raise FlashError("not enough space for the update: need %d MiB, have %d MiB on /data" % (need >> 20, free >> 20), code="no_space",
                             recovery="Delete old gcode/timelapse files and try again.")
        status = self.rauc.status()
        if status.booted is None:
            raise FlashError("rauc reports no booted slot; this does not look like an Eitri A/B system", code="rauc")
        self._check_slot_layout(status)
        files = self._download(bundles)
        if seed_b:
            seedlib.check_archive_file(files[seed_b.sha256], seed_b.sha256, seed_b.size)
            seedlib.check_archive_contents(files[seed_b.sha256], flavor, seed_info)
        # The slot about to be overwritten loses whatever was proven about the system in it.
        self.bootctl.forget_confirmation(self.bootctl.other(status.booted))
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
        pending["restart_services"] = sorted({s for b in bundles if b.kind != "os" for s in b.restart_services})
        if mcu_b:
            pending["mcu_fw"] = {"archive": files[mcu_b.sha256], "flavor": flavor, "sha256": mcu_b.sha256}
        if seed_b:
            pending["seed"] = {"archive": files[seed_b.sha256], "manifest": os.path.join(self.cfg.updates_dir, seed_b.manifest.filename),
                               "sha256": seed_b.sha256, "flavor": flavor, "version": release.version}
        pending["phase"] = "await-reboot"
        self.state.update(pending=pending)
        self.events.step("install", "ok", msg="%s is installed in the inactive slot; it becomes active after a reboot" % release.version)
        if reboot:
            self.events.info("rebooting into %s" % release.version)
            self._reboot()
        self.events.result(True, msg="update staged", data=plan)
        return plan

    def _require_settled_system(self) -> None:
        """An update overwrites the OTHER slot, which is the only fallback.  That is only safe while the running
        system is itself proven good and no earlier update is still waiting for its verdict."""
        pending = self.state.load().get("pending")
        if pending:
            raise FlashError(
                "an earlier update (to %s, %s) has not been verified yet; installing another would overwrite the only known-good system"
                % (pending.get("to", "?"), pending.get("phase", "?")),
                code="update_pending", step="install",
                recovery="Reboot and let eitri-verify finish (it commits or rolls back), then try again.")
        slot = self.bootctl.current_slot()           # raises when this is not an Eitri A/B system
        if self.bootctl.get_state(slot) != "good":
            raise FlashError(
                "the running system (slot %s) has not been confirmed healthy yet; installing now would overwrite the only known-good system" % slot,
                code="unconfirmed_slot", step="install",
                recovery="Wait for eitri-verify (about two minutes after boot), or check the printer and run `eitri-update confirm`.")

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

    def _expect_abi(self) -> str:
        return self.cfg.expect_abi or "cp%d%d" % (sys.version_info[0], sys.version_info[1])

    def _fetch_seed_manifest(self, release, flavor: str, b: Bundle) -> Dict[str, Any]:
        """Download the seed's own manifest (its digest is in the signed channel manifest) and check it against the release:
        same version, same flavor, the archive that was announced, the firmware commit the release pins, this image's
        interpreter and glibc.  Raises before anything is installed."""
        assert b.manifest is not None
        dest = os.path.join(self.cfg.updates_dir, b.manifest.filename)
        self.events.step("seed-manifest", "start", msg="checking the %s data seed of %s" % (flavor, release.version))
        fetch.download(b.manifest.url, dest, sha256=b.manifest.sha256, size=b.manifest.size, opener=self.opener,
                       allow_insecure=self.cfg.allow_insecure)
        try:
            info = seedlib.check_manifest(
                seedlib.load_manifest(dest), flavor=flavor, version=release.version,
                archive={"name": b.filename, "sha256": b.sha256, "size": b.size},
                release_flavor=release.flavors.get(flavor), expect_abi=self._expect_abi(), check_host_libc=True)
        except FlashError as exc:
            self.events.step("seed-manifest", "failed", msg=exc.message)
            raise
        self.events.step("seed-manifest", "ok", msg="%s %s, %d MiB unpacked" % (flavor, info["describe"], info["uncompressed_size"] >> 20))
        return info

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
        if doc.get("switch"):
            # A flavor switch runs in one go without a reboot; finding it unfinished at boot means power was lost or the
            # process died in the middle.  The previous flavor is the only state known to be whole: go back to it.
            self.events.warn("an earlier flavor switch (%s -> %s) was interrupted; undoing it" % (doc["switch"].get("from"), doc["switch"].get("to")))
            self._undo_switch(doc["switch"], restart=False)
            self.state.update(switch=None)
            self.state.add_history("flavor-switch-undone", to=doc["switch"].get("to"))
            doc = self.state.load()
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
                self._install_mcu_firmware(pending, "pending")
                self._install_seed(pending, "pending")
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

    def _mcu_sync(self, pending: Dict[str, Any], key: str = "pending") -> None:
        pin = ["--commit", pending["commit"]] if pending.get("commit") else []
        ok, plan = self._mcuflash(["plan"] + pin, want_ok=False, with_status=True)
        self._check_plan(ok, plan)
        boards = plan.get("boards", {})
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
                self._save(pending, key)
            raise
        pending.setdefault("mcu_flashed", []).extend(need)
        self._save(pending, key)
        self.events.step("mcu-sync", "ok")

    @staticmethod
    def _check_plan(ok: bool, plan: Optional[Dict[str, Any]]) -> None:
        """"Nothing needs flashing" is only a conclusion when the plan itself succeeded and described every board.
        A blocked or failed plan lists no boards at all (or boards without firmware), which must never read as
        "already matches"."""
        blocked = [c for c in (plan or {}).get("checks", []) if isinstance(c, dict) and c.get("status") == "block"]
        boards = (plan or {}).get("boards") or {}
        expected = sorted(load_registry().boards)
        problems = []
        if not ok or not plan:
            problems.append("eitri-mcuflash plan failed")
        if plan and plan.get("blocked"):
            problems.append("the plan is blocked")
        missing = [b for b in expected if b not in boards]
        if plan and missing:
            problems.append("no plan for: %s" % ", ".join(missing))
        no_fw = [n for n, b in boards.items() if "firmware" in b and not b["firmware"]]
        if no_fw:
            problems.append("no firmware for: %s" % ", ".join(sorted(no_fw)))
        if problems:
            detail = "; ".join("%s: %s" % (c.get("id", "check"), c.get("detail") or "blocked") for c in blocked)
            raise FlashError(
                "cannot tell whether the MCU firmware matches this host (%s)%s" % ("; ".join(problems), (" - " + detail) if detail else ""),
                code="mcu_plan_blocked", step="mcu-sync",
                recovery="Run `eitri-mcuflash plan` for the preflight table. The MCUs were not touched.")

    def _mcuflash(self, args: List[str], want_ok: bool = False, with_status: bool = False):
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
        if with_status:
            return bool(res.returncode == 0 and result.get("ok")), result.get("data")
        return result.get("data")

    def _verify_and_commit(self, pending: Optional[Dict[str, Any]], slot: str, start_services: bool = False) -> Dict[str, Any]:
        if not self.cfg.application_configured():
            # No printer.cfg / moonraker.conf yet (before the first-run wizard, or a deliberately Klipper-less
            # printer): both services are held back by ConditionPathExists=, so there is nothing to wait for and
            # waiting would roll back a perfectly good OS.  The OS-level health gate (eitri-verify's
            # `mark-good --check`) has already passed by the time we run.
            self.events.step("health", "ok", msg="application layer not configured; committing on OS health alone")
            return self._commit(pending, slot, "application layer not configured")
        self.events.step("health", "start", msg="waiting for Klipper and Moonraker")
        if start_services:
            for unit in ("moonraker", "klipper"):
                self.runner(["systemctl", "start", unit])
        h = self.health_check(self.cfg.moonraker_url, timeout=self.cfg.health_timeout)
        if not h.ok:
            self.events.step("health", "failed", msg=h.detail)
            return self._roll_back(pending, slot, "health check failed: %s" % h.detail)
        self.events.step("health", "ok", msg=h.detail)
        return self._commit(pending, slot, h.detail)

    def _commit(self, pending: Optional[Dict[str, Any]], slot: str, detail: str) -> Dict[str, Any]:
        self.rauc.mark("good")
        self.bootctl.set_state(slot, "good")
        if pending:
            self._finish_data_layer(pending)
            self.state.update(pending=None, installed_version=pending["to"])
            self.state.set_text("release", pending["to"])
            self.state.add_history("upgraded", **{"from": pending.get("from"), "to": pending["to"]})
            if self.state.load().get("needs_pin_reset") and self.state.channel == "eitri":
                self.state.update(needs_pin_reset=False)
        self.events.result(True, msg="slot %s confirmed good%s" % (slot, (", now on %s" % pending["to"]) if pending else ""))
        return {"pending": False, "confirmed": slot}

    # ---- the data layer: MCU firmware + data seed ----------------------------------------------------------------
    # A "job" is the dict update.json keeps for it: update.json["pending"] for a release update (applied by prepare after
    # the reboot) or update.json["switch"] for a flavor switch.  Both hold: seed {archive, sha256, flavor, version, installed},
    # mcu_fw {archive, flavor, installed, had_old}, commit, mcu_flashed, stopped (services this run stopped).
    def _save(self, job: Dict[str, Any], key: str) -> None:
        """Persist *job* if its slot in update.json is still in use (a finished or cancelled job must not reappear)."""
        if self.state.load().get(key) is not None:
            self.state.update(**{key: job})

    def _active(self, unit: str) -> bool:
        return self.runner(["systemctl", "is-active", "--quiet", unit]).returncode == 0

    def _stop_services(self, job: Dict[str, Any], key: str) -> None:
        """Stop the services that read the trees about to be swapped.  Only units that are running are stopped: during
        boot eitri-prepare runs *before* them and a `stop` job queued then would cancel their pending start."""
        stopped = list(job.get("stopped", []))
        for unit in self.cfg.services:
            if self._active(unit):
                self.runner(["systemctl", "stop", unit])
                stopped.append(unit)
        job["stopped"] = stopped
        self._save(job, key)

    def _start_services(self, job: Dict[str, Any]) -> None:
        for unit in reversed(self.cfg.services):
            if unit in job.get("stopped", []):
                self.runner(["systemctl", "start", unit])

    def _install_mcu_firmware(self, job: Dict[str, Any], key: str) -> None:
        fw = job.get("mcu_fw")
        if not fw or fw.get("installed"):
            return
        self.events.step("mcu-firmware", "start", msg="unpacking the %s MCU firmware" % fw["flavor"])
        res = mcufw.install(fw["archive"], fw["flavor"], self.cfg.mcu_firmware_root)
        fw["installed"], fw["had_old"] = True, res["had_old"]
        self._save(job, key)
        self.events.step("mcu-firmware", "ok")

    def _install_seed(self, job: Dict[str, Any], key: str) -> None:
        sd = job.get("seed")
        if not sd or sd.get("installed"):
            return
        root = self.cfg.data_root
        rec = seedlib.read_record(root)
        step = "seed"
        self.events.step(step, "start", msg="installing the %s data seed %s" % (sd["flavor"], sd.get("version", "")))
        if rec.get("SEED_ARCHIVE_SHA256") == sd["sha256"] and rec.get("SEED_FLAVOR") == sd["flavor"]:
            self.events.info("this seed is already installed (an earlier run got that far)")
        else:
            if seedlib.leftovers(root):
                # An update of ours died half-way: the replaced trees are still next to the new ones.  Re-running the
                # installer on top would overwrite the only copy of the originals, so go back to a whole state first.
                self.events.warn("an earlier seed update did not finish; restoring the previous trees first")
                self.seedtool.rollback()
            self._stop_services(job, key)
            if sd.get("switch") and sd.get("from") and self.state.flavor != sd["from"]:
                # A front end may already have written the wanted flavor into the flag file.  The installer keeps the file's
                # content for its own rollback, so it must say what is really installed until the swap is done.
                self.state.set_text("flavor", sd["from"])
            self.seedtool.update(sd["archive"], sd["sha256"], sd["flavor"], switch_flavor=bool(sd.get("switch")), keep_old=True)
        sd["installed"] = True
        self._save(job, key)
        self._overlay()
        self.events.step(step, "ok")

    def _overlay(self) -> None:
        """Link the layer's qd_* extras into the (new or kept) Klipper/Kalico clone and point eitri-flavor.cfg at its flavor."""
        cmd = self.cfg.overlay_command or seedlib.default_overlay_command(self.cfg.data_root)
        if not os.path.exists(cmd[0]):
            self.events.warn("%s not found; the extras are not re-linked" % cmd[0])
            return
        res = self.runner(cmd)
        if res.returncode != 0:
            raise FlashError("linking the Klipper extras failed:\n" + "\n".join(res.output.strip().splitlines()[-6:]), code="overlay", step="seed",
                             recovery="The previous Klipper tree is restored automatically.")

    def _undo_data_layer(self, job: Dict[str, Any]) -> None:
        """Put back what the job replaced: the seed's trees (and the flavor flag) and the MCU firmware tree."""
        root = self.cfg.data_root
        for unit in self.cfg.services:
            if self._active(unit):
                self.runner(["systemctl", "stop", unit])
        sd, fw = job.get("seed"), job.get("mcu_fw")
        if (sd and sd.get("installed")) or seedlib.leftovers(root):
            try:
                self.seedtool.rollback()
                self.events.info("the previous Klipper/Moonraker/Klarity trees are back")
            except FlashError as exc:
                self.events.warn("could not restore the previous data trees: %s" % exc.message)
        if fw and fw.get("installed"):
            try:
                mcufw.restore(self.cfg.mcu_firmware_root, fw["flavor"], bool(fw.get("had_old")))
            except (OSError, FlashError) as exc:
                self.events.warn("could not restore the previous MCU firmware files: %s" % exc)

    def _finish_data_layer(self, job: Dict[str, Any]) -> None:
        """The new system proved healthy: drop the replaced trees and the downloaded archives."""
        sd, fw = job.get("seed"), job.get("mcu_fw")
        if sd:
            try:
                self.seedtool.finish()
            except FlashError as exc:
                self.events.warn("could not remove the replaced data trees: %s" % exc.message)
            for path in (sd.get("archive"), sd.get("manifest")):
                if path and os.path.isfile(path):
                    os.unlink(path)
        if fw:
            mcufw.finish(self.cfg.mcu_firmware_root, fw["flavor"])
            if fw.get("archive") and os.path.isfile(fw["archive"]):
                os.unlink(fw["archive"])

    # ---- flavor switch (klipper <-> kalico) -----------------------------------------------------------------------------
    def switch_flavor(self, target: str, *, dry_run: bool = False) -> Dict[str, Any]:
        """Run the other flavor: install its data seed and its MCU firmware (from the installed release, so the OS, the
        seed and the MCU images stay one matching set), flash both MCUs, restart the services and check health.  The OS and
        the boot slot are not touched, so there is no reboot; anything that fails restores the previous flavor, files and
        MCU firmware, and restarts the services."""
        if target not in seedlib.FLAVORS:
            raise FlashError("the flavor must be klipper or kalico", code="usage")
        if self.state.channel == "upstream":
            raise FlashError("this printer follows upstream; switching flavor replaces the Klipper/Kalico clone with the release's seed",
                             code="upstream_mode", recovery="Run `eitri-update upstream disable` first.")
        root = self.cfg.data_root
        if self.state.load().get("switch"):
            raise FlashError("an earlier flavor switch was interrupted and has not been undone yet", code="switch_pending",
                             recovery="Reboot; eitri-prepare restores the previous flavor, then try again.")
        current = seedlib.installed_flavor(root) or self.state.flavor
        plan = {"from": current, "to": target, "release": self.state.installed_version}
        if current == target:
            if self.state.flavor != target:
                self.state.set_text("flavor", target)           # only the flag file disagreed
            self.events.result(True, msg="already running %s" % target, data=plan)
            return plan
        installed = self.state.installed_version
        if not installed:
            raise FlashError("this system does not know which release it runs, so it cannot pick the matching %s seed" % target, code="no_release")
        self._require_settled_system()
        state = self.print_state(self.cfg.moonraker_url)
        if state in ("printing", "paused"):
            raise FlashError("a print is %s; switching flavor restarts Klipper and re-flashes both MCUs" % state, code="printing",
                             recovery="Wait for the print to finish, then try again.")
        manifest = self.fetch_manifest()
        release, bundles = plan_flavor_switch(manifest, installed, target)
        plan["bundles"] = [{"kind": b.kind, "size": b.size, "url": b.url} for b in bundles]
        self.events.emit("plan", msg="%s -> %s on release %s (%d MiB)" % (current, target, installed, sum(b.size for b in bundles) >> 20), data=plan)
        if dry_run:
            self.events.result(True, msg="dry run: nothing was downloaded or changed", data=plan)
            return plan
        seed_b = next(b for b in bundles if b.kind == "seed")
        mcu_b = next(b for b in bundles if b.kind == "mcu")
        info = self._fetch_seed_manifest(release, target, seed_b)
        need = int(sum(b.size for b in bundles) * 1.1) + int(info["uncompressed_size"] * 1.1) + (64 << 20)
        free = fetch.free_bytes(self.cfg.updates_dir)
        if free < need:
            raise FlashError("not enough space to switch flavor: need %d MiB, have %d MiB on /data" % (need >> 20, free >> 20), code="no_space",
                             recovery="Delete old gcode/timelapse files and try again.")
        files = self._download(bundles)
        seedlib.check_archive_file(files[seed_b.sha256], seed_b.sha256, seed_b.size)
        seedlib.check_archive_contents(files[seed_b.sha256], target, info)
        job: Dict[str, Any] = {
            "from": current, "to": target, "phase": "applying", "commit": (release.flavors.get(target) or {}).get("commit"),
            "mcu_fw": {"archive": files[mcu_b.sha256], "flavor": target, "sha256": mcu_b.sha256},
            "seed": {"archive": files[seed_b.sha256], "manifest": os.path.join(self.cfg.updates_dir, seed_b.manifest.filename),
                     "sha256": seed_b.sha256, "flavor": target, "version": release.version, "switch": True, "from": current},
            "stopped": [], "mcu_flashed": []}
        self.state.update(switch=job)
        try:
            self._install_mcu_firmware(job, "switch")
            self._install_seed(job, "switch")
            job["phase"] = "mcu"                                # from here on a board may be half-flashed
            self._save(job, "switch")
            self._mcu_sync(job, "switch")
            job["phase"] = "health"
            self._save(job, "switch")
            self._start_services(job)
            self._save(job, "switch")
            if self.cfg.application_configured():
                self.events.step("health", "start", msg="waiting for Klipper and Moonraker on %s" % target)
                h = self.health_check(self.cfg.moonraker_url, timeout=self.cfg.health_timeout)
                if not h.ok:
                    self.events.step("health", "failed", msg=h.detail)
                    raise FlashError("%s did not come up healthy: %s" % (target, h.detail), code="health", step="health")
                self.events.step("health", "ok", msg=h.detail)
        except FlashError as exc:
            self.events.step("flavor-switch", "failed", msg=exc.message)
            self._undo_switch(job, restart=True)
            self.state.update(switch=None)
            self.state.add_history("flavor-switch-failed", to=target, reason=exc.message)
            raise
        self._finish_data_layer(job)
        self.state.update(switch=None)
        self.state.add_history("flavor-switched", **{"from": current, "to": target})
        self.events.result(True, msg="now running %s" % target, data=plan)
        return plan

    def _undo_switch(self, job: Dict[str, Any], *, restart: bool) -> None:
        self._undo_data_layer(job)
        boards = list(job.get("mcu_flashed", []))
        if job.get("phase") == "mcu":
            # Interrupted while flashing: which board was written is not known, and restoring a board to the image it
            # already runs is harmless.
            boards = sorted(set(boards) | set(load_registry().boards))
        for board in boards:
            try:
                self._mcuflash(["rollback", board, "--apply", "--yes"], want_ok=True)
            except FlashError as exc:
                self.events.warn("could not restore the %s MCU firmware: %s" % (board, exc.message))
        if restart:
            self._start_services(job)

    # ---- rollback -------------------------------------------------------------------------------
    def _roll_back(self, pending: Optional[Dict[str, Any]], slot: str, why: str) -> Dict[str, Any]:
        other = self.bootctl.other(slot)
        self.events.step("rollback", "start", msg=why)
        if pending:
            # The previous system needs the previous /data layer (Klipper, Moonraker, venvs, MCU firmware): put it back first.
            self._undo_data_layer(pending)
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
