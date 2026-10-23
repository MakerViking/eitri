#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Assemble a signed DEVELOPMENT update channel that `eitri-update` can install from, so a dev build reaches the printer
# the normal way (RAUC to the spare slot, then post-boot applies the seed + MCU firmware, health-checks, commits or
# rolls back) instead of a hand-run `rauc install`.  Nothing is downloaded; everything comes from this PC's build outputs.
#
#   installer/tools/mk-dev-channel.sh --version 2026.10.23-dev.<sha> --key ../build/devkeys/eitri-dev.key \
#       [--out ../build/dev-channel] [--base-url file:///data/eitri/home/devchan]
#
# The channel version MUST be CalVer (YYYY.MM.PATCH[-pre]); eitri-update and seed.py reject 0.0.0.dev0.  Build the seeds
# and MCU firmware with the SAME version first:
#
#   make -C .. seed      VERSION=2026.10.23-dev.<sha> FLAVOR=all
#   make -C .. mcu-build MCU_VERSION=2026.10.23-dev.<sha> MCU_TOOLCHAIN=<dir> MCU_BOARDS=mainboard   # toolhead: gated until Katapult
#   make -C ../os image                                   # the OS .raucb (its own version is independent)
#
# Deploy (the channel dir is copied to the printer, then eitri-update is run AS REAL ROOT so the sudo hardening, which
# forbids --manifest-url/--key-dir for the web-facing eitri account, does not apply -- see etc/sudoers.d/eitri):
#
#   scp -r ../build/dev-channel eitri@PRINTER:/data/eitri/home/devchan
#   ssh eitri@PRINTER "sudo env -i PATH=/usr/bin:/bin /usr/bin/eitri-update \
#       --manifest-url file:///data/eitri/home/devchan/manifest.json \
#       --key-dir /data/eitri/home/devchan/eitri-dev.pub upgrade --yes"
#   # then a cold power-cycle (the RK3328 warm-reboot hang, until the syscon-reboot DTB fix is verified).
#
# Inputs (defaults relative to the sibling build directory):
#   --version V       CalVer release version; must equal the version the seeds and MCU firmware were built with
#   --br-out DIR      Buildroot output (images/*.raucb)                           default ../build/br-out
#   --seed-dir DIR    data seeds + .manifest.json (make seed)                     default ../build/seed/out
#   --mcu-dir DIR     MCU firmware bundles (make mcu-build)                        default ../build/mcu/out
#   --raucb FILE      the OS bundle; default: the newest *.raucb in --br-out/images
#   --base-url U      file:// or https:// prefix the printer will serve from      default file:///data/eitri/home/devchan
#   --key FILE        minisign SECRET key (kept outside the repo)
#   --out DIR         channel output directory                                    default ../build/dev-channel
#   --flavor F        klipper | kalico | both                                     default both
#   --channel NAME    manifest channel name                                       default dev
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$(cd "$HERE/.." && pwd)"; REPO="$(cd "$INSTALLER/.." && pwd)"; BUILD="$(cd "$REPO/.." && pwd)/build"
BR_OUT="$BUILD/br-out"; SEED_DIR="$BUILD/seed/out"; MCU_DIR="$BUILD/mcu/out"; OUT="$BUILD/dev-channel"
VERSION=""; KEY=""; PUB=""; RAUCB=""; BASE_URL="file:///data/eitri/home/devchan"; FLAVOR=both; CHANNEL=dev
while (($#)); do
  case "$1" in
    --version) VERSION="${2:?}"; shift ;;
    --br-out) BR_OUT="${2:?}"; shift ;;
    --seed-dir) SEED_DIR="${2:?}"; shift ;;
    --mcu-dir) MCU_DIR="${2:?}"; shift ;;
    --raucb) RAUCB="${2:?}"; shift ;;
    --base-url) BASE_URL="${2:?}"; shift ;;
    --key) KEY="${2:?}"; shift ;;
    --pubkey) PUB="${2:?}"; shift ;;
    --out) OUT="${2:?}"; shift ;;
    --flavor) FLAVOR="${2:?}"; shift ;;
    --channel) CHANNEL="${2:?}"; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "mk-dev-channel: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
die() { echo "mk-dev-channel: $*" >&2; exit 1; }
[[ -n "$VERSION" ]] || die "--version is required and must be CalVer (e.g. 2026.10.23-dev.abc1234)"
[[ "$VERSION" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]+([-+][0-9A-Za-z.]+)?$ ]] || die "--version $VERSION is not CalVer YYYY.MM.PATCH[-pre]"
[[ "$FLAVOR" == klipper || "$FLAVOR" == kalico || "$FLAVOR" == both ]] || die "--flavor must be klipper, kalico or both"
case "$FLAVOR" in both) FLAVORS=(klipper kalico);; *) FLAVORS=("$FLAVOR");; esac
[[ -n "$KEY" && -r "$KEY" ]] || die "--key FILE (a passwordless minisign secret key) is required"
[[ -n "$PUB" ]] || PUB="${KEY%.key}.pub"; [[ -r "$PUB" ]] || die "public key $PUB not found (--pubkey)"
case "$(cd "$(dirname "$KEY")" && pwd)/" in "$REPO"/*) die "the secret key must not live inside the repository ($KEY)";; esac
for t in minisign python3 sha256sum; do command -v "$t" >/dev/null || die "$t is needed"; done
[[ -z "$RAUCB" ]] && RAUCB="$(ls -t "$BR_OUT"/images/*.raucb 2>/dev/null | head -1 || true)"
[[ -n "$RAUCB" && -r "$RAUCB" ]] || die "no OS .raucb found (looked in $BR_OUT/images; pass --raucb)"
for fl in "${FLAVORS[@]}"; do
  [[ -r "$SEED_DIR/data-seed-$VERSION-$fl.tar.zst" ]] || die "missing $SEED_DIR/data-seed-$VERSION-$fl.tar.zst (run: make seed VERSION=$VERSION FLAVOR=all)"
  [[ -r "$SEED_DIR/data-seed-$VERSION-$fl.manifest.json" ]] || die "missing the $fl seed .manifest.json for $VERSION"
  [[ -r "$MCU_DIR/mcu-firmware-$VERSION-$fl.tar.zst" ]] || die "missing $MCU_DIR/mcu-firmware-$VERSION-$fl.tar.zst (run: make mcu-build MCU_VERSION=$VERSION)"
done

mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
rm -f "$OUT"/manifest.json "$OUT"/manifest.json.minisig "$OUT"/*.raucb "$OUT"/data-seed-* "$OUT"/mcu-firmware-* "$OUT"/eitri-dev.pub "$OUT"/SHA256SUMS
os_sha="$(sha256sum "$RAUCB" | cut -d' ' -f1)"
cp -f "$RAUCB" "$OUT/os-${os_sha:0:16}.raucb"
for fl in "${FLAVORS[@]}"; do
  cp -f "$SEED_DIR/data-seed-$VERSION-$fl.tar.zst" "$SEED_DIR/data-seed-$VERSION-$fl.manifest.json" "$MCU_DIR/mcu-firmware-$VERSION-$fl.tar.zst" "$OUT/"
done
cp -f "$PUB" "$OUT/eitri-dev.pub"

VERSION="$VERSION" BASE_URL="$BASE_URL" CHANNEL="$CHANNEL" OS_RAUCB="$OUT/os-${os_sha:0:16}.raucb" OS_SHA="$os_sha" \
FLAVORS="${FLAVORS[*]}" SEQ="$(date +%s)" NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
EXPIRES="$(date -u -d '+365 days' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+365d +%Y-%m-%dT%H:%M:%SZ)" \
python3 -I - "$OUT" <<'PY'
import hashlib, json, os, sys
out = sys.argv[1]
ver, base, chan = os.environ["VERSION"], os.environ["BASE_URL"].rstrip("/"), os.environ["CHANNEL"]
flavors = os.environ["FLAVORS"].split()
def h(p): b=open(p,"rb").read(); return hashlib.sha256(b).hexdigest(), len(b)
os_sha, os_size = os.environ["OS_SHA"], os.path.getsize(os.environ["OS_RAUCB"])
os_name = os.path.basename(os.environ["OS_RAUCB"])
bundles = [{"kind":"os","url":f"{base}/{os_name}","sha256":os_sha,"size":os_size,"reboot":True}]
flav_meta = {}
for fl in flavors:
    seed = f"{out}/data-seed-{ver}-{fl}.tar.zst"; seedman = f"{out}/data-seed-{ver}-{fl}.manifest.json"
    mcu = f"{out}/mcu-firmware-{ver}-{fl}.tar.zst"
    s_sha, s_size = h(seed); m_sha, m_size = h(seedman); u_sha, u_size = h(mcu)
    doc = json.load(open(seedman))
    fw = doc.get("firmware") or doc.get("source") or {}
    flav_meta[fl] = {"commit": fw.get("commit",""), "describe": fw.get("describe","")}
    bundles.append({"kind":"seed","flavor":fl,"url":f"{base}/{os.path.basename(seed)}","sha256":s_sha,"size":s_size,
                    "manifest":{"url":f"{base}/{os.path.basename(seedman)}","sha256":m_sha,"size":m_size}})
    bundles.append({"kind":"mcu","flavor":fl,"url":f"{base}/{os.path.basename(mcu)}","sha256":u_sha,"size":u_size})
manifest = {"schema":1,"channel":chan,"sequence":int(os.environ["SEQ"]),
            "generated":os.environ["NOW"],"expires":os.environ["EXPIRES"],
            "releases":[{"version":ver,"released":os.environ["NOW"],"flavors":flav_meta,"bundles":bundles}],
            "addons":{},"upstream":{}}
open(f"{out}/manifest.json","w").write(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
print("wrote manifest.json: version %s, %d bundles, sequence %d" % (ver, len(bundles), manifest["sequence"]))
PY

minisign -S -s "$KEY" -m "$OUT/manifest.json" \
  -c "Eitri DEV channel $CHANNEL" \
  -t "Eitri DEV channel $CHANNEL $VERSION, $(date -u +%Y-%m-%dT%H:%M:%SZ), development key - not a release" >/dev/null
( cd "$OUT" && sha256sum manifest.json manifest.json.minisig os-*.raucb data-seed-* mcu-firmware-* eitri-dev.pub > SHA256SUMS )

# Self-check: the manifest parses under eitri-update's own rules, and the signature verifies with the dev pubkey.
PYTHONPATH="$INSTALLER/eitri-update:$INSTALLER/mcuflash" python3 -I - "$OUT" "$PUB" <<'PY'
import sys
sys.path[:0] = [p for p in __import__("os").environ.get("PYTHONPATH","").split(":") if p]
from eitri_update.manifest import parse_manifest
from eitri_update import minisign
out, pub = sys.argv[1], sys.argv[2]
data = open(f"{out}/manifest.json","rb").read()
m = parse_manifest(data)   # raises FlashError on any schema problem
sig = minisign.parse_signature(open(f"{out}/manifest.json.minisig").read())
signer = minisign.verify_bytes(data, sig, minisign.load_keys([pub]))
rel = m.latest()
print("self-check OK: channel %r seq %d, release %s, flavors %s, os+%d bundles, signed by %s"
      % (m.channel, m.sequence, rel.version, sorted(rel.flavors), len(rel.bundles), signer.key_id_hex))
PY
echo "mk-dev-channel: channel ready in $OUT (serve at $BASE_URL)" >&2
