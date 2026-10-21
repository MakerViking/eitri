#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the Eitri MCU firmware bundles: one mcu-firmware-<version>-<flavor>.tar.zst (+ .manifest.json) per flavor,
# holding klipper.bin for the mainboard (STM32F401) and the toolhead (STM32F103). Layout and consumer:
# installer/docs/BUNDLE.md ("mcu-firmware.tar.zst"), eitri-mcuflash and eitri-update.
#
# The firmware is built from EXACTLY the host commit pinned in release.lock (the same one the data seed ships), with
# core.abbrev=9, so the MCU reports the same version string as the host's `git describe`; the build refuses otherwise.
#
#   mcu/build-mcu.sh --check-only --flavor both --work W            configs vs params.json vs the pinned trees; no
#                                                                  toolchain, writes nothing outside W. What CI runs
#                                                                  while a board is unresolved.
#   mcu/build-mcu.sh --flavor both --version V --out O --work W \
#                    --toolchain DIR [--seed-manifest M]...            the real build (needs every board RESOLVED)
#
# SAFETY: the real build runs `mcutool.py gate` first and stops (exit 3) while any board's flash parameters are
# unresolved (mcu/params.json). A firmware linked for the wrong application offset or the wrong crystal does not boot.
# Nothing here touches a device, and no bootloader is ever produced or written.
set -euo pipefail
export LC_ALL=C TZ=UTC

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$(cd -- "$HERE/.." && pwd -P)"
TOOL=(python3 -I "$HERE/mcutool.py")

log() { printf 'build-mcu: %s\n' "$*" >&2; }
die() { printf 'build-mcu: error: %s\n' "$*" >&2; exit "${2:-1}"; }
usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

FLAVOR_ARG=both; VERSION=""; OUT=""; WORK=""; TOOLCHAIN=""; SEED_MANIFESTS=(); CHECK_ONLY=0; JOBS=""
TC_VERSION=""; TC_SHA256=""; TC_NAME="Arm GNU Toolchain arm-none-eabi"
declare -A SOURCES=() DESCRIBES=()
while [ $# -gt 0 ]; do
    case "$1" in
        --flavor) FLAVOR_ARG="$2"; shift 2;;
        --version) VERSION="$2"; shift 2;;
        --out) OUT="$2"; shift 2;;
        --work) WORK="$2"; shift 2;;
        --toolchain) TOOLCHAIN="$2"; shift 2;;
        --toolchain-version) TC_VERSION="$2"; shift 2;;
        --toolchain-sha256) TC_SHA256="$2"; shift 2;;
        --seed-manifest) SEED_MANIFESTS+=("$2"); shift 2;;     # data-seed-*.manifest.json (repeatable): its sources.firmware.describe is the version to match
        --source) SOURCES["${2%%=*}"]="${2#*=}"; shift 2;;          # FLAVOR=DIR: a clone that has the pinned commit and tags
        --describe) DESCRIBES["${2%%=*}"]="${2#*=}"; shift 2;;      # FLAVOR=STRING: expected `git describe`
        --jobs) JOBS="$2"; shift 2;;
        --check-only) CHECK_ONLY=1; shift;;
        -h|--help) usage;;
        *) die "unknown argument $1" 2;;
    esac
done
case "$FLAVOR_ARG" in both) FLAVORS=(klipper kalico);; klipper|kalico) FLAVORS=("$FLAVOR_ARG");; *) die "--flavor must be klipper, kalico or both" 2;; esac
[ -n "$WORK" ] || die "--work DIR is required" 2
mkdir -p "$WORK"; WORK="$(cd -- "$WORK" && pwd -P)"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

# --- the gate -------------------------------------------------------------------------------------------------
if [ "$CHECK_ONLY" -eq 0 ]; then
    "${TOOL[@]}" gate || die "not building: a board's flash parameters are unresolved (see above)" 3
    [ -n "$VERSION" ] && [ -n "$OUT" ] && [ -n "$TOOLCHAIN" ] || die "--version, --out and --toolchain are required for a real build" 2
    [ -x "$TOOLCHAIN/bin/arm-none-eabi-gcc" ] || die "no arm-none-eabi-gcc in $TOOLCHAIN/bin (mcu/fetch-toolchain.sh installs the pinned one)"
    [ -n "$TC_VERSION" ] || TC_VERSION="$("${TOOL[@]}" lock mcu.toolchain version 2>/dev/null)" || die "no toolchain version: pass --toolchain-version or pin [mcu.toolchain] in release.lock"
    [ -n "$TC_SHA256" ] || TC_SHA256="$("${TOOL[@]}" lock mcu.toolchain sha256 2>/dev/null)" || die "no toolchain sha256: pass --toolchain-sha256 or pin [mcu.toolchain] in release.lock"
    mkdir -p "$OUT"; OUT="$(cd -- "$OUT" && pwd -P)"
fi

git_q() { git -c advice.detachedHead=false -c gc.auto=0 "$@"; }

# stage_tree FLAVOR -> $WORK/src/FLAVOR checked out at the pinned commit, abbrev 9, clean; sets COMMIT, DESCRIBE, EPOCH
stage_tree() {
    local flavor="$1" dest="$WORK/src/$1" url src
    COMMIT="$("${TOOL[@]}" lock "firmware.$flavor" commit)"
    url="$("${TOOL[@]}" lock "firmware.$flavor" source)"
    src="${SOURCES[$flavor]:-}"
    rm -rf "$dest"; mkdir -p "$WORK/src"
    if [ -n "$src" ]; then
        git -C "$src" cat-file -e "$COMMIT^{commit}" 2>/dev/null || die "$src does not contain $flavor commit ${COMMIT:0:12} (release.lock)"
        git_q clone -q --no-checkout --no-hardlinks "$src" "$dest"
    else
        log "cloning $flavor from $url"
        git_q clone -q --no-checkout --filter=blob:none "$url" "$dest"
    fi
    git_q -C "$dest" checkout -q --detach "$COMMIT"
    git -C "$dest" config core.abbrev 9           # the seed does the same; see seed/SPEC.md
    [ "$(git -C "$dest" rev-parse HEAD)" = "$COMMIT" ] || die "$flavor: checked out HEAD is not the pinned commit"
    [ -z "$(git -C "$dest" status --porcelain)" ] || die "$flavor: work tree is not clean"
    DESCRIBE="$(git -C "$dest" describe --always --tags --long --dirty)"
    case "$DESCRIBE" in *-dirty) die "$flavor: describe is dirty: $DESCRIBE";; esac
    local want="${DESCRIBES[$flavor]:-}"
    if [ -z "$want" ] && [ "${#SEED_MANIFESTS[@]}" -gt 0 ]; then
        want="$(python3 -I - "$flavor" "${SEED_MANIFESTS[@]}" <<'PY'
import json, sys
for path in sys.argv[2:]:
    m = json.load(open(path))
    if m.get("flavor") == sys.argv[1]:
        print(m["sources"]["firmware"]["describe"])
PY
)"
    fi
    if [ -n "$want" ]; then
        [ "$want" = "$DESCRIBE" ] || die "$flavor: git describe here is $DESCRIBE but the host/seed says $want"
        log "$flavor: version $DESCRIBE matches the host's"
    else
        log "$flavor: version $DESCRIBE (no seed manifest given, so not cross-checked against the host)"
    fi
    EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$dest" log -1 --format=%ct)}"
}

for flavor in "${FLAVORS[@]}"; do
    stage_tree "$flavor"
    tree="$WORK/src/$flavor"

    # Configs against params.json, expanded by this tree's own Kconfig (no toolchain needed).
    "${TOOL[@]}" check-config --tree "$tree" || die "$flavor: mcu/configs do not match mcu/params.json"

    if [ "$CHECK_ONLY" -eq 1 ]; then
        log "$flavor: --check-only, nothing built"
        continue
    fi

    export PATH="$TOOLCHAIN/bin:$PATH" SOURCE_DATE_EPOCH="$EPOCH"
    GCC_VERSION="$(arm-none-eabi-gcc --version | head -n1)"
    build_args=()
    for board in mainboard toolhead; do
        case "$board" in mainboard) cfg=mainboard-stm32f401.config;; toolhead) cfg=toolhead-stm32f103.config;; esac
        bdir="$WORK/build/$flavor/$board"
        rm -rf "$bdir" "$bdir.config"; mkdir -p "$bdir"
        cp "$HERE/configs/$cfg" "$bdir.config"
        # Out of tree: OUT and KCONFIG_CONFIG on the command line (Klipper's Makefile assigns KCONFIG_CONFIG itself).
        mk=(make -C "$tree" "OUT=$bdir/" "KCONFIG_CONFIG=$bdir.config")
        log "$flavor/$board: olddefconfig, make -j$JOBS"
        "${mk[@]}" olddefconfig > "$bdir.log" 2>&1 || { tail -n 20 "$bdir.log" >&2; die "$flavor/$board: olddefconfig failed"; }
        "${mk[@]}" "-j$JOBS" >> "$bdir.log" 2>&1 || { tail -n 30 "$bdir.log" >&2; die "$flavor/$board: build failed"; }
        [ -f "$bdir/klipper.bin" ] && [ -f "$bdir/klipper.elf" ] || die "$flavor/$board: build produced no klipper.bin / klipper.elf"
        [ -z "$(git -C "$tree" status --porcelain)" ] || die "$flavor/$board: the build left the source tree dirty"
        build_args+=(--build "$board=$bdir")
    done

    "${TOOL[@]}" package --flavor "$flavor" --version "$VERSION" --describe "$DESCRIBE" --commit "$COMMIT" --epoch "$EPOCH" \
        --origin "$("${TOOL[@]}" lock "firmware.$flavor" source)" --out "$OUT" "${build_args[@]}" \
        --toolchain-name "$TC_NAME" --toolchain-version "$TC_VERSION" --toolchain-sha256 "$TC_SHA256" --toolchain-gcc "$GCC_VERSION"
    log "$flavor: wrote $OUT/mcu-firmware-$VERSION-$flavor.tar.zst"
done
