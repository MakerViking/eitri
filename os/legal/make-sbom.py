#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate an SBOM (CycloneDX 1.5 and SPDX 2.3, JSON) from Buildroot's legal-info output.

Usage:
    make-sbom.py --legal-info <br-out>/legal-info --release-lock release.lock \
                 --version 0.1.0 --out <dir> [--include-host] [--hash-sources]

Inputs
  <legal-info>/manifest.csv       target packages (what is in the image)
  <legal-info>/host-manifest.csv  build tools; only with --include-host
  release.lock                    adds runtime inputs that are not in the image
                                  (Klipper, Kalico, Moonraker are cloned at install time)

Outputs (in --out)
  eitri-<version>.cdx.json        CycloneDX 1.5
  eitri-<version>.spdx.json       SPDX 2.3
  sbom-summary.txt                one line per package: license, redistribution class

Licence strings in Buildroot's manifest are free text. They are normalised to SPDX
expressions only when every part is a known identifier; otherwise the declared licence is
NOASSERTION and the original text is kept verbatim in a comment/property. Nothing is guessed.

Standard library only. Exit status 1 if the manifest is missing or empty, or when
--fail-on-unknown is set and a package licence is "unknown" or "Proprietary" without a
reviewed entry in PROPRIETARY_REVIEWED.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
import tomllib
import uuid
from pathlib import Path

# Proprietary / non-GPL blobs that were reviewed. Key = Buildroot package name.
PROPRIETARY_REVIEWED = {
    "linux-firmware": (
        "Realtek rtlwifi firmware (LICENCE.rtlwifi_firmware.txt): binary redistribution allowed "
        "unmodified, with copyright notice and disclaimer; no reverse engineering; patent licence "
        "only with OSI-approved OS. Only rtlwifi/* is installed in the image."
    ),
    "rockchip-rkbin": (
        "Rockchip rkbin LICENSE: use/copy/distribute allowed, no decompiling, notices must be kept. "
        "Not in the default image (open TPL)."
    ),
}

KNOWN_SPDX = {
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-2.0-or-later", "LGPL-3.0-or-later",
    "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "BSD-4-Clause", "BSD-5-Clause",
    "0BSD", "ISC", "Apache-2.0", "MPL-2.0", "Zlib", "PSF-2.0", "Python-2.0", "curl",
    "AFL-2.1", "CC0-1.0", "BSL-1.0", "OFL-1.1", "blessing", "bzip2-1.0.6", "Unlicense",
    "GPL-3.0-with-GCC-exception", "LicenseRef-public-domain",
}
ALIASES = {
    "GPL-2.0": "GPL-2.0-only", "GPL-2.0+": "GPL-2.0-or-later",
    "GPL-3.0": "GPL-3.0-only", "GPL-3.0+": "GPL-3.0-or-later",
    "LGPL-2.1": "LGPL-2.1-only", "LGPL-2.1+": "LGPL-2.1-or-later",
    "LGPL-2.0+": "LGPL-2.0-or-later", "LGPL-3.0+": "LGPL-3.0-or-later",
    "BSD-0-Clause": "0BSD", "Public Domain": "LicenseRef-public-domain",
    "Public domain": "LicenseRef-public-domain", "bzip2 license": "bzip2-1.0.6",
    "GPL-2.0+ (programs)": "GPL-2.0-or-later",
}


def norm_license(raw: str) -> tuple[str | None, str]:
    """Return (SPDX expression or None, class). Class is one of copyleft-strong,
    copyleft-weak, permissive, proprietary, unknown."""
    text = raw.strip()
    if not text or text.lower() == "unknown":
        return None, "unknown"
    if text.lower() in ("proprietary",):
        return None, "proprietary"
    parts = []
    text = re.sub(r"\s*\([^)]*\)", "", text)  # drop descriptions such as "(extras, tools)"
    # Buildroot: ", " (or " / ") joins the licences of different parts of a package (AND);
    # " or " is a choice (OR). Parenthetical notes are descriptions, not part of the id.
    for chunk in re.split(r",\s+|\s+/\s+", text):
        alts = []
        for alt in re.split(r"\s+or\s+", chunk):
            alt = re.sub(r"\s*\([^)]*\)", "", alt).strip()
            alt = re.sub(r"\s+with\s+Linux-syscall-note$", "", alt, flags=re.I).strip()
            alt = ALIASES.get(alt, alt)
            if alt not in KNOWN_SPDX:
                return None, classify(text)
            alts.append(alt)
        alts = sorted(set(alts))
        parts.append(alts[0] if len(alts) == 1 else "(" + " OR ".join(alts) + ")")
    parts = sorted(set(parts))
    return " AND ".join(parts), classify(text)


def classify(text: str) -> str:
    t = text.lower()
    if t in ("unknown", ""):
        return "unknown"
    if "proprietary" in t:
        return "proprietary"
    if "lgpl" in t and "gpl-" not in t.replace("lgpl", ""):
        return "copyleft-weak"
    if "gpl" in t or "agpl" in t:
        return "copyleft-strong"
    if "mpl" in t:
        return "copyleft-weak"
    return "permissive"


def sha256_file(p: Path) -> str | None:
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_manifest(path: Path, scope: str, legal: Path, hash_sources: bool) -> list[dict]:
    out = []
    if not path.is_file():
        return out
    sources_dir = legal / ("host-sources" if scope == "build-tool" else "sources")
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            name, ver = r["PACKAGE"], r["VERSION"]
            archive = r["SOURCE ARCHIVE"]
            digest = None
            if hash_sources and archive:
                digest = sha256_file(sources_dir / f"{name}-{ver}" / archive)
            expr, cls = norm_license(r["LICENSE"])
            out.append({
                "name": name, "version": ver, "license_raw": r["LICENSE"], "spdx": expr,
                "class": cls, "license_files": r["LICENSE FILES"], "archive": archive,
                "site": r["SOURCE SITE"], "scope": scope, "sha256": digest,
                "deps": r.get("DEPENDENCIES WITH LICENSES", ""),
            })
    return out


def lock_components(lock: Path, present: set[str]) -> list[dict]:
    """Runtime inputs from release.lock that Buildroot does not ship in the image."""
    wanted = {
        ("firmware", "klipper"): ("klipper", "GPL-3.0-only"),
        ("firmware", "kalico"): ("kalico", "GPL-3.0-only"),
        ("moonraker",): ("moonraker", "GPL-3.0-only"),
    }
    try:
        data = tomllib.loads(lock.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"warning: cannot read {lock}: {e}", file=sys.stderr)
        return []
    out = []
    for path, (name, spdx) in wanted.items():
        node = data
        for k in path:
            node = node.get(k, {}) if isinstance(node, dict) else {}
        if not node or name in present:
            continue
        out.append({
            "name": name, "version": node.get("version", "NOASSERTION"), "license_raw": spdx,
            "spdx": spdx, "class": "copyleft-strong", "license_files": "", "archive": "",
            "site": node.get("source", ""), "scope": "runtime-fetched",
            "sha256": None, "deps": "", "commit": node.get("commit"),
        })
    return out


def spdx_id(name: str, ver: str) -> str:
    return "SPDXRef-Pkg-" + re.sub(r"[^A-Za-z0-9.\-]", "-", f"{name}-{ver}")[:100]


def purl(c: dict) -> str:
    v = re.sub(r"[^A-Za-z0-9._~+\-]", "-", c["version"])
    return f"pkg:generic/{c['name']}@{v}"


def download(c: dict) -> str:
    if not c["site"]:
        return "NOASSERTION"
    if c["site"].startswith("/"):
        return "NOASSERTION"  # local path of the build machine
    return c["site"].rstrip("/") + ("/" + c["archive"] if c["archive"] and "git" not in c["site"] else "")


def cyclonedx(comps: list[dict], version: str, ts: str, serial: str) -> dict:
    cc = []
    for c in comps:
        e = {
            "type": "firmware" if c["name"] in ("linux", "uboot", "linux-firmware",
                                                "arm-trusted-firmware") else "library",
            "bom-ref": spdx_id(c["name"], c["version"]),
            "name": c["name"], "version": c["version"], "purl": purl(c),
            "scope": "excluded" if c["scope"] == "build-tool" else "required",
            "properties": [
                {"name": "eitri:scope", "value": c["scope"]},
                {"name": "eitri:licenseText", "value": c["license_raw"]},
                {"name": "eitri:licenseClass", "value": c["class"]},
            ],
        }
        if c["spdx"]:
            e["licenses"] = [{"expression": c["spdx"]}]
        else:
            e["licenses"] = [{"license": {"name": c["license_raw"] or "NOASSERTION"}}]
        refs = []
        if c["site"]:
            refs.append({"type": "distribution", "url": c["site"]})
        if refs:
            e["externalReferences"] = refs
        if c["sha256"]:
            e["hashes"] = [{"alg": "SHA-256", "content": c["sha256"]}]
        if c["name"] in PROPRIETARY_REVIEWED:
            e["properties"].append({"name": "eitri:redistribution", "value": PROPRIETARY_REVIEWED[c["name"]]})
        cc.append(e)
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": ts,
            "tools": {"components": [{"type": "application", "name": "eitri make-sbom.py"}]},
            "component": {"type": "firmware", "name": "eitri-plus4", "version": version,
                          "licenses": [{"license": {"id": "Apache-2.0"}}]},
        },
        "components": cc,
    }


def spdx(comps: list[dict], version: str, ts: str, serial: str) -> dict:
    pk, rel = [], []
    for c in comps:
        sid = spdx_id(c["name"], c["version"])
        p = {
            "SPDXID": sid, "name": c["name"], "versionInfo": c["version"],
            "downloadLocation": download(c), "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": c["spdx"] or "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                              "referenceLocator": purl(c)}],
            "comment": f"scope={c['scope']}; licence text from Buildroot: {c['license_raw']!r}",
        }
        if c["sha256"]:
            p["checksums"] = [{"algorithm": "SHA256", "checksumValue": c["sha256"]}]
        if c["name"] in PROPRIETARY_REVIEWED:
            p["comment"] += "; " + PROPRIETARY_REVIEWED[c["name"]]
        pk.append(p)
        rel.append({"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": sid})
    return {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"eitri-plus4-{version}",
        "documentNamespace": f"https://github.com/eitri-fw/eitri/sbom/{version}/{serial}",
        "creationInfo": {"created": ts, "creators": ["Tool: eitri-make-sbom"]},
        "packages": pk, "relationships": rel,
        "hasExtractedLicensingInfos": [{
            "licenseId": "LicenseRef-public-domain", "extractedText": "Public domain, as declared by the package.",
        }],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--legal-info", required=True, type=Path)
    ap.add_argument("--release-lock", type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--include-host", action="store_true")
    ap.add_argument("--hash-sources", action="store_true", help="SHA-256 each source archive (slow, ~1 GB)")
    ap.add_argument("--fail-on-unknown", action="store_true")
    a = ap.parse_args()

    comps = read_manifest(a.legal_info / "manifest.csv", "image", a.legal_info, a.hash_sources)
    if not comps:
        print(f"error: no packages in {a.legal_info}/manifest.csv", file=sys.stderr)
        return 1
    if a.include_host:
        comps += read_manifest(a.legal_info / "host-manifest.csv", "build-tool", a.legal_info, a.hash_sources)
    if a.release_lock:
        comps += lock_components(a.release_lock, {c["name"] for c in comps})

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Deterministic serial for a given content set, so reruns diff cleanly.
    serial = str(uuid.uuid5(uuid.NAMESPACE_URL, a.version + "|" + "|".join(
        f"{c['name']}={c['version']}" for c in comps)))
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"eitri-{a.version}.cdx.json").write_text(
        json.dumps(cyclonedx(comps, a.version, ts, serial), indent=2) + "\n")
    (a.out / f"eitri-{a.version}.spdx.json").write_text(
        json.dumps(spdx(comps, a.version, ts, serial), indent=2) + "\n")

    bad = []
    lines = []
    for c in comps:
        flag = c["class"]
        if c["class"] in ("unknown", "proprietary") and c["name"] not in PROPRIETARY_REVIEWED:
            bad.append(c["name"])
            flag += " UNREVIEWED"
        lines.append(f"{c['scope']:15} {c['name']} {c['version']}  [{flag}]  {c['license_raw']}")
    (a.out / "sbom-summary.txt").write_text("\n".join(lines) + "\n")
    print(f"{len(comps)} components -> {a.out}")
    if bad and a.fail_on_unknown:
        print("error: unreviewed unknown/proprietary licence: " + ", ".join(bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
