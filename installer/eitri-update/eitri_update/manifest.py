# SPDX-License-Identifier: Apache-2.0
"""The signed channel manifest and the rules for choosing a release from it."""
from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from eitri_mcuflash.errors import FlashError

SCHEMA = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d{4}\.\d{2}\.\d+(?:[-+][0-9A-Za-z.]+)?$")
KINDS = ("os", "data")


def version_key(version: str) -> Tuple[int, ...]:
    """2026.10.1 -> (2026, 10, 1); pre-release suffixes sort before the release."""
    base = re.split(r"[-+]", version, maxsplit=1)[0]
    parts = tuple(int(p) for p in base.split("."))
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

    @property
    def filename(self) -> str:
        return "%s-%s.raucb" % (self.kind, self.sha256[:16])


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
    expires: Optional[str]
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
        if not self.expires:
            return False
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return now > datetime.datetime.strptime(self.expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


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
    releases: List[Release] = []
    for r in raw["releases"]:
        _need(_VERSION.match(str(r.get("version", ""))) is not None, "bad version %r" % r.get("version"))
        bundles: List[Bundle] = []
        for b in r.get("bundles", []):
            _need(b.get("kind") in KINDS, "bad bundle kind %r" % b.get("kind"))
            _need(_SHA256.match(str(b.get("sha256", ""))) is not None, "bundle sha256 must be 64 lowercase hex digits")
            _need(isinstance(b.get("size"), int) and 0 < b["size"] < 2 ** 33, "bundle size missing")
            _need(str(b.get("url", "")).startswith(("https://", "file://")), "bundle url must be https")
            _need(b.get("flavor") in (None, "klipper", "kalico"), "bad bundle flavor")
            bundles.append(Bundle(b["kind"], b["url"], b["sha256"], b["size"], b.get("flavor"), bool(b.get("reboot", b["kind"] == "os")),
                                  list(b.get("restart_services", []))))
        _need(any(b.kind == "os" for b in bundles), "release %s has no os bundle" % r["version"])
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
    if not any(b.kind == "data" for b in bundles):
        raise FlashError("release %s has no data bundle for flavor %s" % (release.version, flavor), code="no_flavor")
    return release, bundles
