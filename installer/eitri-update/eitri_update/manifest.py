# SPDX-License-Identifier: Apache-2.0
"""The signed channel manifest and the rules for choosing a release from it."""
from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from eitri_mcuflash.errors import FlashError

from . import seed

SCHEMA = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d{4}\.\d{2}\.\d+(?:[-+][0-9A-Za-z.]+)?$")
# os: RAUC bundle for the inactive root slot.  seed / mcu: the release's data seed and MCU firmware for one flavor, applied by
# eitri-update itself after the reboot (seed.py, mcufw.py).  data: a RAUC bundle whose hooks update /data (older releases).
# The tuple is also the order in which they are applied.
KINDS = ("os", "mcu", "seed", "data")
FLAVORS = ("klipper", "kalico")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# The printer has no battery-backed clock: until time sync it can read 1970 or the last saved time.  A manifest can
# therefore never be judged "not expired yet" against a date earlier than this floor, which is moved forward with
# releases (it only ever needs to be earlier than the real present, so a stale value weakens nothing it protects).
CLOCK_FLOOR = "2026-10-01T00:00:00Z"


def parse_timestamp(text: str) -> datetime.datetime:
    if not isinstance(text, str) or _TIMESTAMP.match(text) is None:
        raise FlashError("invalid channel manifest: bad timestamp %r (expected YYYY-MM-DDTHH:MM:SSZ)" % (text,), code="manifest")
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError as exc:
        raise FlashError("invalid channel manifest: bad timestamp %r (%s)" % (text, exc), code="manifest") from exc


def version_key(version: str) -> Tuple[int, ...]:
    """2026.10.1 -> (2026, 10, 1); pre-release suffixes sort before the release.

    A version string that is not CalVer -- e.g. the ``<sha>-dirty`` that ``git
    describe`` leaves on a hand-flashed bench build -- is not a channel release.
    Rather than crash the comparison (int('6888c58') raises), it sorts before
    every real release, so ``check``/``plan_upgrade`` simply offer the upgrade
    onto it. Manifest versions are validated against _VERSION before they reach
    here, so only the installed side can be unparseable."""
    base = re.split(r"[-+]", version, maxsplit=1)[0]
    try:
        parts = tuple(int(p) for p in base.split("."))
    except ValueError:
        return (-1,)
    return parts + ((0,) if "-" in version else (1,))


@dataclass
class Bundle:
    kind: str
    url: str
    sha256: str
    size: int
    flavor: Optional[str] = None
    reboot: bool = False
    restart_services: List[str] = field(default_factory=list)
    version: str = ""                            # the release this bundle belongs to
    manifest: Optional["Companion"] = None       # seed bundles: the seed's own manifest (also signed through this document)

    @property
    def filename(self) -> str:
        if self.kind == "seed":
            return seed.archive_name(self.version, self.flavor or "")
        if self.kind == "mcu":
            return "mcu-firmware-%s-%s.tar.zst" % (self.version, self.flavor)
        return "%s-%s.raucb" % (self.kind, self.sha256[:16])


@dataclass
class Companion:
    url: str
    sha256: str
    size: int
    filename: str


@dataclass
class Release:
    version: str
    bundles: List[Bundle]
    flavors: Dict[str, Dict[str, Any]]
    min_installed: Optional[str] = None
    rollback_floor: Optional[str] = None
    notes_url: str = ""
    released: str = ""

    def bundles_for(self, flavor: str) -> List[Bundle]:
        """OS bundles are flavor independent; data bundles are chosen by flavor. OS comes first."""
        chosen = [b for b in self.bundles if b.kind == "os" or b.flavor in (None, flavor)]
        return sorted(chosen, key=lambda b: KINDS.index(b.kind))


@dataclass
class Addon:
    version: str
    url: str
    sha256: str
    size: int
    installed_size: int


@dataclass
class Manifest:
    channel: str
    sequence: int
    generated: str
    expires: str
    releases: List[Release]
    addons: Dict[str, Addon]
    upstream: Dict[str, Dict[str, str]]
    raw: Dict[str, Any]

    def latest(self) -> Release:
        return max(self.releases, key=lambda r: version_key(r.version))

    def find(self, version: str) -> Release:
        for r in self.releases:
            if r.version == version:
                return r
        raise FlashError("release %s is not in this channel" % version, code="no_release")

    def expired(self, now: Optional[datetime.datetime] = None) -> bool:
        """Judged against the later of the clock, the manifest's own signed `generated` time and CLOCK_FLOOR, so a
        printer whose clock has not been set yet still rejects a long-expired (replayed) manifest."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        now = max(now, parse_timestamp(CLOCK_FLOOR))
        if self.generated:
            now = max(now, parse_timestamp(self.generated))
        return now > parse_timestamp(self.expires)


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise FlashError("invalid channel manifest: " + msg, code="manifest")


def parse_manifest(data: bytes) -> Manifest:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FlashError("channel manifest is not valid JSON: %s" % exc, code="manifest") from exc
    _need(isinstance(raw, dict) and raw.get("schema") == SCHEMA, "unsupported schema %r" % (raw.get("schema") if isinstance(raw, dict) else None))
    _need(isinstance(raw.get("sequence"), int) and raw["sequence"] >= 0, "sequence missing")
    _need(isinstance(raw.get("releases"), list) and raw["releases"], "no releases")
    _need(isinstance(raw.get("expires"), str), "expires missing (every signed manifest must carry an expiry)")
    parse_timestamp(raw["expires"])
    if raw.get("generated"):
        parse_timestamp(raw["generated"])
    releases: List[Release] = []
    for r in raw["releases"]:
        _need(_VERSION.match(str(r.get("version", ""))) is not None, "bad version %r" % r.get("version"))
        bundles: List[Bundle] = []
        for b in r.get("bundles", []):
            _need(b.get("kind") in KINDS, "bad bundle kind %r" % b.get("kind"))
            _need(_SHA256.match(str(b.get("sha256", ""))) is not None, "bundle sha256 must be 64 lowercase hex digits")
            _need(isinstance(b.get("size"), int) and 0 < b["size"] < 2 ** 33, "bundle size missing")
            _need(str(b.get("url", "")).startswith(("https://", "file://")), "bundle url must be https")
            _need(b.get("flavor") in (None,) + FLAVORS, "bad bundle flavor")
            companion = None
            if b["kind"] in ("seed", "mcu"):
                _need(b.get("flavor") in FLAVORS, "a %s bundle must name its flavor" % b["kind"])
            if b["kind"] == "seed":
                m = b.get("manifest")
                _need(isinstance(m, dict) and str(m.get("url", "")).startswith(("https://", "file://")),
                      "the seed bundle needs its manifest (url, sha256, size)")
                _need(_SHA256.match(str(m.get("sha256", ""))) is not None, "seed manifest sha256 must be 64 lowercase hex digits")
                _need(isinstance(m.get("size"), int) and 0 < m["size"] < 2 ** 20, "seed manifest size missing")
                companion = Companion(m["url"], m["sha256"], m["size"], seed.manifest_name(r["version"], b["flavor"]))
            bundles.append(Bundle(b["kind"], b["url"], b["sha256"], b["size"], b.get("flavor"), bool(b.get("reboot", b["kind"] == "os")),
                                  list(b.get("restart_services", [])), r["version"], companion))
        _need(any(b.kind == "os" for b in bundles), "release %s has no os bundle" % r["version"])
        for fl in FLAVORS:
            _need(not any(b.kind == "seed" and b.flavor == fl for b in bundles) or any(b.kind == "mcu" and b.flavor == fl for b in bundles),
                  "release %s ships a %s data seed without the matching MCU firmware" % (r["version"], fl))
            _need(sum(1 for b in bundles if b.kind == "seed" and b.flavor == fl) <= 1 and sum(1 for b in bundles if b.kind == "mcu" and b.flavor == fl) <= 1,
                  "release %s lists a %s seed or MCU bundle twice" % (r["version"], fl))
        req = r.get("requires", {})
        releases.append(Release(r["version"], bundles, r.get("flavors", {}), req.get("min_installed"), r.get("rollback_floor"),
                                r.get("notes_url", ""), r.get("released", "")))
    addons: Dict[str, Addon] = {}
    for name, a in (raw.get("addons") or {}).items():
        _need(_SHA256.match(str(a.get("sha256", ""))) is not None, "addon %s sha256" % name)
        _need(str(a.get("url", "")).startswith(("https://", "file://")), "addon %s url" % name)
        addons[name] = Addon(a["version"], a["url"], a["sha256"], int(a["size"]), int(a.get("installed_size", a["size"] * 4)))
    return Manifest(raw.get("channel", "stable"), raw["sequence"], raw.get("generated", ""), raw.get("expires"), releases, addons,
                    raw.get("upstream", {}), raw)


def plan_upgrade(manifest: Manifest, installed: Optional[str], flavor: str, target: Optional[str] = None,
                 allow_downgrade: bool = False) -> Tuple[Release, List[Bundle]]:
    """Choose the release and the bundles to install, or raise with an actionable reason."""
    release = manifest.find(target) if target else manifest.latest()
    if installed:
        if version_key(release.version) == version_key(installed):
            raise FlashError("already on %s" % installed, code="up_to_date")
        if version_key(release.version) < version_key(installed):
            floor = release.rollback_floor
            if not allow_downgrade:
                raise FlashError("%s is older than the installed %s (pass --allow-downgrade)" % (release.version, installed), code="downgrade")
            if floor and version_key(installed) < version_key(floor):
                raise FlashError("%s cannot be installed over %s: its rollback floor is %s" % (release.version, installed, floor), code="downgrade")
        if release.min_installed and version_key(installed) < version_key(release.min_installed):
            raise FlashError(
                "%s needs at least %s installed first (you have %s); upgrade in two steps" % (release.version, release.min_installed, installed),
                code="needs_intermediate",
            )
    bundles = release.bundles_for(flavor)
    if not any(b.kind in ("seed", "data") for b in bundles):
        raise FlashError("release %s has no data seed (or data bundle) for flavor %s" % (release.version, flavor), code="no_flavor")
    return release, bundles


def plan_flavor_switch(manifest: Manifest, installed: str, target: str) -> Tuple[Release, List[Bundle]]:
    """The seed and MCU firmware of the *installed* release for the other flavor.  An OS from release X with a seed from
    release Y is never mixed: the seed's interpreter ABI, libraries and host/MCU pin belong together."""
    if target not in FLAVORS:
        raise FlashError("flavor must be klipper or kalico", code="usage")
    try:
        release = manifest.find(installed)
    except FlashError:
        raise FlashError(
            "the installed release %s is no longer listed in the channel, so its %s seed cannot be fetched" % (installed, target),
            code="no_release", recovery="Run `eitri-update upgrade` first (it keeps your flavor), then switch.")
    bundles = [b for b in release.bundles if b.flavor == target and b.kind in ("seed", "mcu")]
    if {b.kind for b in bundles} != {"seed", "mcu"}:
        raise FlashError("release %s does not ship both the %s data seed and its MCU firmware" % (release.version, target), code="no_flavor")
    return release, sorted(bundles, key=lambda b: KINDS.index(b.kind))
