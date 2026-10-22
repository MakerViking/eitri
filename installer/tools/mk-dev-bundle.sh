#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Assemble an offline DEVELOPMENT bundle for the in-place installer (install.sh --bundle-dir DIR) and sign it with a
# local development key.  Everything comes from this PC's build outputs; nothing is downloaded and nothing touches a printer.
#
#   installer/tools/mk-dev-bundle.sh --key ../build/devkeys/eitri-dev.key [--out ../build/dev-bundle] [--flavor klipper]
#
# Inputs (defaults relative to the repository's sibling build directory, as the other builders use):
#   --br-out DIR      Buildroot output of `make -C os image` (rootfs.ext4 for slot A, and the trees the stage-2 userland and
#                     the static zstd are built from)                                          default ../build/br-out
#   --seed-dir DIR    data seeds from `make seed` (data-seed-<version>-<flavor>.tar.zst + .manifest.json)
#                                                                                              default ../build/seed/out
#   --flavor F        klipper (default) or kalico: the one flavor this bundle offers
#   --key FILE        minisign SECRET key (passwordless, minisign -G -W); keep it outside the repository.  The matching
#                     public key (FILE with .key replaced by .pub, or --pubkey) is copied into the bundle so the user can
#                     pass it to install.sh --pubkey.
#   --stage2-dir DIR  reuse stage2-initramfs.cpio.gz + zstd-aarch64 from mk-stage2-initramfs.sh instead of building them
#   --uboot FILE      U-Boot 2021.07 sandbox binary; when present, slot A's kernel and device tree are read back through
#                     U-Boot's own ext4 driver (the stock U-Boot is 2021.07) and compared byte for byte
#
# The bundle (DIR):
#   install.json + install.json.minisig   channel "dev", "dev": true, mcu_policy keep-stock (no MCU firmware: both boards
#                                         keep their stock firmware), exact SHA-256 and size of every file
#   rootfs.img.zst                        slot A: the image's rootfs.ext4 (conservative ext4 features, checked here)
#   stage2-initramfs.cpio.gz              the RAM installer's userland (mk-stage2-initramfs.sh)
#   data-seed-<v>-<flavor>.tar.zst/.manifest.json
#   zstd-aarch64                          static zstd for the stock system (Debian 10 there has none)
#   eitri-dev.pub                         the public half of the development key
#   eitri-installer.tar.gz                install.sh, revert.sh and what they need (not covered by install.json: it is the
#                                         verifier itself; it comes from this checkout)
#   SHA256SUMS, BUILD-INFO.txt            for humans
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$INSTALLER/.." && pwd)"
BUILD="$(cd "$REPO/.." && pwd)/build"
BR_OUT="$BUILD/br-out"; SEED_DIR="$BUILD/seed/out"; OUT="$BUILD/dev-bundle"; FLAVOR=klipper
KEY=""; PUB=""; STAGE2_DIR=""; UBOOT="${EITRI_UBOOT_SANDBOX:-$BUILD/uboot-sandbox/u-boot-2021.07/u-boot}"
ROOT_MIB=2048; DATA_MIN_MIB=4096
while (($#)); do
  case "$1" in
    --br-out) BR_OUT="${2:?}"; shift ;;
    --seed-dir) SEED_DIR="${2:?}"; shift ;;
    --out) OUT="${2:?}"; shift ;;
    --flavor) FLAVOR="${2:?}"; shift ;;
    --key) KEY="${2:?}"; shift ;;
    --pubkey) PUB="${2:?}"; shift ;;
    --stage2-dir) STAGE2_DIR="${2:?}"; shift ;;
    --uboot) UBOOT="${2:?}"; shift ;;
    -h | --help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "mk-dev-bundle: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
log() { printf 'mk-dev-bundle: %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }
[[ "$FLAVOR" == klipper || "$FLAVOR" == kalico ]] || die "--flavor must be klipper or kalico"
[[ -n "$KEY" && -r "$KEY" ]] || die "--key FILE (a passwordless minisign secret key) is required"
[[ -n "$PUB" ]] || PUB="${KEY%.key}.pub"
[[ -r "$PUB" ]] || die "public key $PUB not found (--pubkey)"
case "$(cd "$(dirname "$KEY")" && pwd)/" in "$REPO"/*) die "the secret key must not live inside the repository ($KEY)" ;; esac
for t in minisign zstd debugfs dumpe2fs e2fsck python3 tar; do command -v "$t" >/dev/null || die "$t is needed"; done
ROOTFS="$BR_OUT/images/rootfs.ext4"
[[ -r "$ROOTFS" ]] || die "$ROOTFS not found: run make -C os image first"

mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d "$BUILD/.mk-dev-bundle.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
rm -f "$OUT"/install.json "$OUT"/install.json.minisig "$OUT"/rootfs.img.zst "$OUT"/stage2-initramfs.cpio.gz "$OUT"/zstd-aarch64 \
      "$OUT"/data-seed-* "$OUT"/eitri-dev.pub "$OUT"/eitri-installer.tar.gz "$OUT"/SHA256SUMS "$OUT"/BUILD-INFO.txt "$OUT"/rootfs.check.txt

# --- the data seed: the release version is the seed's -------------------------------------------------------
seeds=("$SEED_DIR"/data-seed-*-"$FLAVOR".tar.zst)
[[ ${#seeds[@]} == 1 && -r "${seeds[0]}" ]] || die "need exactly one data-seed-*-$FLAVOR.tar.zst in $SEED_DIR (found ${#seeds[@]})"
SEED="${seeds[0]}"; SEED_MAN="${SEED%.tar.zst}.manifest.json"
[[ -r "$SEED_MAN" ]] || die "$SEED_MAN is missing"
VERSION="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$SEED_MAN")"
log "version $VERSION ($FLAVOR seed $(basename "$SEED"))"

# --- slot A: the conservative-feature rootfs ----------------------------------------------------------------
# U-Boot 2021.07 (the stock boot loader) loads /boot/Image and the device tree from this file system with ext4load.
check_rootfs() {
  local img="$1" feats bad="" f
  feats="$(dumpe2fs -h "$img" 2>/dev/null | sed -n 's/^Filesystem features: *//p')"
  [[ -n "$feats" ]] || die "$img is not an ext4 file system"
  for f in 64bit metadata_csum_seed orphan_file huge_file inline_data encrypt casefold verity needs_recovery; do
    [[ " $feats " == *" $f "* ]] && bad="$bad $f"
  done
  [[ -z "$bad" ]] || die "$img uses ext4 features the stock U-Boot 2021.07 must not meet:$bad (os/configs/eitri_plus4_defconfig BR2_TARGET_ROOTFS_EXT2_MKFS_OPTIONS)"
  e2fsck -fn "$img" >"$WORK/e2fsck.log" 2>&1 || { cat "$WORK/e2fsck.log" >&2; die "$img does not pass e2fsck -fn"; }
  printf 'features: %s\n' "$feats"
}
cp --sparse=always "$ROOTFS" "$WORK/rootfs.img"
check_rootfs "$WORK/rootfs.img" >"$WORK/rootfs.check.txt"
mkdir -p "$WORK/rootfs-files"
debugfs -R "dump -p /boot/Image $WORK/rootfs-files/Image" "$WORK/rootfs.img" 2>/dev/null
dtb_link="$(debugfs -R "stat /boot/dtb/rockchip/rk3328-roc-cc.dtb" "$WORK/rootfs.img" 2>/dev/null | sed -n 's/^Fast link dest: "\(.*\)"/\1/p')"
dtb_path="/boot/dtb/rockchip/rk3328-roc-cc.dtb"
[[ -n "$dtb_link" ]] && dtb_path="$(python3 -I -c 'import os,sys; print(os.path.normpath(os.path.join("/boot/dtb/rockchip", sys.argv[1])))' "$dtb_link")"
debugfs -R "dump -p $dtb_path $WORK/rootfs-files/dtb" "$WORK/rootfs.img" 2>/dev/null
[[ -s "$WORK/rootfs-files/Image" && -s "$WORK/rootfs-files/dtb" ]] || die "slot A lacks /boot/Image or /boot/dtb/rockchip/rk3328-roc-cc.dtb (boot.scr loads both)"
echo "kernel /boot/Image $(stat -c %s "$WORK/rootfs-files/Image") B sha256 $(sha256sum <"$WORK/rootfs-files/Image" | cut -d' ' -f1)" >>"$WORK/rootfs.check.txt"
echo "dtb /boot/dtb/rockchip/rk3328-roc-cc.dtb -> $dtb_path $(stat -c %s "$WORK/rootfs-files/dtb") B" >>"$WORK/rootfs.check.txt"
if [[ -x "$UBOOT" ]]; then
  crc() { python3 -I -c 'import sys,zlib; print("%08x" % zlib.crc32(open(sys.argv[1],"rb").read()))' "$1"; }
  out="$(timeout 120 "$UBOOT" -c "host bind 0 $WORK/rootfs.img; ext4load host 0 0x2080000 /boot/Image && crc32 0x2080000 \${filesize}; ext4load host 0 0x1f00000 /boot/dtb/rockchip/rk3328-roc-cc.dtb && crc32 0x1f00000 \${filesize}" 2>&1 || true)"
  got="$(tr -d '\r' <<<"$out" | sed -n 's/^crc32 for .* ==> \([0-9a-f]*\)$/\1/p' | tr '\n' ' ')"
  want="$(crc "$WORK/rootfs-files/Image") $(crc "$WORK/rootfs-files/dtb") "
  [[ "$got" == "$want" ]] || { printf '%s\n' "$out" >&2; die "U-Boot 2021.07 (sandbox) could not read slot A's kernel/device tree back correctly (crc32 '$got', expected '$want')"; }
  echo "u-boot 2021.07 sandbox ext4load: kernel and dtb read back, crc32 $want" >>"$WORK/rootfs.check.txt"
else
  echo "u-boot sandbox not available: ext4load not tried" >>"$WORK/rootfs.check.txt"
  log "WARNING: no U-Boot sandbox ($UBOOT); slot A was not read back through U-Boot's ext4 driver"
fi
RAW_SIZE="$(stat -c %s "$WORK/rootfs.img")"; RAW_SHA="$(sha256sum <"$WORK/rootfs.img" | cut -d' ' -f1)"
((RAW_SIZE <= ROOT_MIB * 1024 * 1024)) || die "rootfs ($RAW_SIZE B) does not fit a $ROOT_MIB MiB slot"
log "compressing slot A ($((RAW_SIZE / 1048576)) MiB)"
zstd -q -19 -T0 --no-progress "$WORK/rootfs.img" -o "$OUT/rootfs.img.zst"
cp "$WORK/rootfs.check.txt" "$OUT/rootfs.check.txt"

# --- the stage-2 userland and the stock system's zstd --------------------------------------------------------
if [[ -z "$STAGE2_DIR" ]]; then
  STAGE2_DIR="$WORK/stage2"
  "$HERE/mk-stage2-initramfs.sh" --br-out "$BR_OUT" --out "$STAGE2_DIR" >&2
fi
cp "$STAGE2_DIR/stage2-initramfs.cpio.gz" "$STAGE2_DIR/zstd-aarch64" "$OUT/"
cp "$SEED" "$SEED_MAN" "$OUT/"
cp "$PUB" "$OUT/eitri-dev.pub"

# --- install.json ------------------------------------------------------------------------------------------
python3 -I - "$OUT" "$VERSION" "$FLAVOR" "$(basename "$SEED")" "$(basename "$SEED_MAN")" "$RAW_SIZE" "$RAW_SHA" "$ROOT_MIB" "$DATA_MIN_MIB" <<'PY'
import hashlib, json, os, sys
out, version, flavor, seed, seed_man, raw_size, raw_sha, root_mib, data_min_mib = sys.argv[1:]

def entry(name, **extra):
    h = hashlib.sha256()
    with open(os.path.join(out, name), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    e = {"url": name, "sha256": h.hexdigest(), "size": os.path.getsize(os.path.join(out, name))}
    e.update(extra)
    return e

man = json.load(open(os.path.join(out, seed_man)))
fw = man["sources"]["firmware"]
doc = {
    "schema": 1,
    "version": version,
    "channel": "dev",
    "dev": True,
    "comment": "DEVELOPMENT bundle signed with a local development key; not an Eitri release",
    "target": {"compatible": "firefly,roc-rk3328-cc", "python_abi": man["target"]["python_abi"]},
    "stock": {"soc": ["1.7.1", "1.7.3"]},
    "layout": {"root_mib": int(root_mib), "data_min_mib": int(data_min_mib)},
    "kernel_cmdline": "console=tty1 loglevel=4 panic=10 net.ifnames=0",
    "mcu_policy": "keep-stock",
    "flavors": {flavor: {"commit": fw["commit"], "describe": fw["describe"]}},
    "files": {
        "rootfs": entry("rootfs.img.zst", raw_size=int(raw_size), raw_sha256=raw_sha),
        "stage2_initramfs": entry("stage2-initramfs.cpio.gz"),
        "data_seed": {flavor: dict(entry(seed), manifest=entry(seed_man))},
        "zstd": entry("zstd-aarch64"),
    },
}
with open(os.path.join(out, "install.json"), "w") as fh:
    json.dump(doc, fh, indent=1, sort_keys=True)
    fh.write("\n")
PY
minisign -S -s "$KEY" -m "$OUT/install.json" -x "$OUT/install.json.minisig" \
  -t "Eitri DEV bundle $VERSION ($FLAVOR), $(date -u +%Y-%m-%dT%H:%M:%SZ), development key - not a release" >/dev/null

# --- the installer itself (dereferenced: bin/eitri-seed-install is a link into seed/) ------------------------
tar -C "$REPO" -h --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='installer/tests' --exclude='installer/*/tests' \
  --owner=0 --group=0 --numeric-owner --sort=name -czf "$OUT/eitri-installer.tar.gz" installer

# --- verify the bundle exactly as install.sh will --------------------------------------------------------------
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from eitri_update.minisign import main; sys.exit(main(sys.argv[2:]))' \
  "$INSTALLER/eitri-update" verify "$OUT/install.json" --sig "$OUT/install.json.minisig" --pubkey "$OUT/eitri-dev.pub" >/dev/null ||
  die "the signature does not verify with $PUB"
python3 -I "$INSTALLER/tools/prepare_payload.py" plan --install-json "$OUT/install.json" --flavor "$FLAVOR" --base-url "file://$OUT/" --out-dir "$WORK/plan"
mkdir -p "$WORK/stage"
for f in rootfs.img.zst stage2-initramfs.cpio.gz "$(basename "$SEED")" "$(basename "$SEED_MAN")"; do ln "$OUT/$f" "$WORK/stage/$f" 2>/dev/null || cp "$OUT/$f" "$WORK/stage/$f"; done
python3 -I "$INSTALLER/tools/prepare_payload.py" finalize --install-json "$OUT/install.json" --flavor "$FLAVOR" --dir "$WORK/stage"
grep -q '^mcu_firmware_file=$' "$WORK/stage/payload.env" || die "payload.env should name no MCU firmware"

( cd "$OUT" && sha256sum install.json install.json.minisig rootfs.img.zst stage2-initramfs.cpio.gz data-seed-* zstd-aarch64 eitri-dev.pub \
    eitri-installer.tar.gz >SHA256SUMS )
{
  echo "Eitri DEVELOPMENT bundle - signed with a local development key, NOT an Eitri release."
  echo "version:   $VERSION   flavor: $FLAVOR   mcu_policy: keep-stock (no MCU firmware; both boards keep their stock firmware)"
  echo "built:     $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname) from $(git -C "$REPO" describe --always --dirty 2>/dev/null || echo unknown)"
  echo "rootfs:    $ROOTFS ($RAW_SIZE B, sha256 $RAW_SHA)"
  sed 's/^/           /' "$OUT/rootfs.check.txt"
  echo "key:       $(sed -n 2p "$OUT/eitri-dev.pub")  (install.sh --pubkey eitri-dev.pub)"
} >"$OUT/BUILD-INFO.txt"
log "bundle ready in $OUT"
ls -l "$OUT" >&2
