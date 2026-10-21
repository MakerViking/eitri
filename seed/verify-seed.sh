#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Verify a built data seed and prove that its aarch64 environments run.
#
#   seed/verify-seed.sh MANIFEST.json [--no-qemu] [--keep] [--verbose] [--sysroot DIR]
#
# 1. archive sha256/size vs the manifest, member policy, tree digest of the extracted tree, chelper timestamps;
# 2. unless --no-qemu: extract into a copy of the image's root file system (hard links, ~free) and run, under
#    qemu-aarch64 user-mode emulation with the image's own Python and C library:
#      - the klippy venv: imports, the prebuilt c_helper.so (klippy must not try to compile), every qd_* extra;
#      - the moonraker venv: its dependencies, the cross-compiled streaming_form_data, `import moonraker`;
#      - the Klarity venv.
#
# Environment: EITRI_BR_OUT (default <repo>/../build/br-out), EITRI_BUILD_ROOT, QEMU_AARCH64 (default qemu-aarch64),
# EITRI_VERIFY_SYSROOT (root to run in instead of the Buildroot image; see tools/ci-toolchain.sh).
# qemu-user cannot follow absolute symlinks or chdir() into the emulated root; the throw-away copy gets
# equivalent relative links, and the smoke scripts tolerate the chdir limitation.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$(cd -- "$HERE/.." && pwd -P)"
TOOL=(python3 -I "$HERE/tools/seedtool.py")
BUILD_ROOT="${EITRI_BUILD_ROOT:-$(cd -- "$REPO/.." && pwd -P)/build}"
BR_OUT="${EITRI_BR_OUT:-$BUILD_ROOT/br-out}"
QEMU="${QEMU_AARCH64:-qemu-aarch64}"
# Root to run the seed in: the Buildroot image root, or the one tools/ci-toolchain.sh assembles.
ROOT_TEMPLATE="${EITRI_VERIFY_SYSROOT:-$BR_OUT/target}"

MANIFEST="" ; QEMU_RUN=1 ; KEEP=0 ; VERBOSE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-qemu) QEMU_RUN=0; shift ;;
        --keep)    KEEP=1; shift ;;
        -v|--verbose) VERBOSE=1; shift ;;
        --sysroot) ROOT_TEMPLATE="${2:?}"; shift 2 ;;
        -h|--help) sed -n '3,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) MANIFEST="$1"; shift ;;
    esac
done
[ -f "$MANIFEST" ] || { echo "usage: verify-seed.sh MANIFEST.json [--no-qemu] [--keep] [--verbose] [--sysroot DIR]" >&2; exit 2; }
MANIFEST="$(cd -- "$(dirname -- "$MANIFEST")" && pwd -P)/$(basename -- "$MANIFEST")"

if [ "$QEMU_RUN" -eq 0 ]; then
    "${TOOL[@]}" verify "$MANIFEST"
    exit 0
fi

command -v "$QEMU" >/dev/null 2>&1 || { echo "verify-seed: $QEMU not found (use --no-qemu)" >&2; exit 2; }
[ -x "$ROOT_TEMPLATE/usr/bin/python3.14" ] || { echo "verify-seed: no aarch64 root with /usr/bin/python3.14 at $ROOT_TEMPLATE (set EITRI_BR_OUT, or --sysroot)" >&2; exit 2; }

SCRATCH="$(mktemp -d "$BUILD_ROOT/seed/verify.XXXXXX" 2>/dev/null || mktemp -d)"
cleanup() { [ "$KEEP" -eq 1 ] || rm -rf "$SCRATCH"; }
trap cleanup EXIT
SR="$SCRATCH/sysroot"
# Hard-link copy of the image root (same file system) so that /data can be a real directory in it.
if ! cp -al "$ROOT_TEMPLATE" "$SR" 2>/dev/null; then rm -rf "$SR"; cp -a "$ROOT_TEMPLATE" "$SR"; fi
rm -rf "$SR/data"; mkdir "$SR/data"

"${TOOL[@]}" verify "$MANIFEST" --extract-to "$SR/data"
"${TOOL[@]}" emulation-prep --data "$SR/data"

flavor="$(tr -d '[:space:]' <"$SR/data/eitri/flavor")"
echo "--- running the $flavor seed under $("$QEMU" --version | head -1) with the image's Python ($("$QEMU" -L "$SR" "$SR/usr/bin/python3.14" -c 'import sys; print(sys.version.split()[0])'))"

fail=0
run_guest() { # name venv-path
    local name="$1" venv="$2" t0 t1
    echo "=== $name  (/data/$venv)"
    t0=$(date +%s)
    # -0 makes argv[0] the venv's python, as when systemd starts it; python then finds pyvenv.cfg there.
    local filter='^(FAIL|note|imported|PASSED|FAILED|Traceback)|Error'
    [ "$VERBOSE" -eq 0 ] || filter='.'
    PYTHONDONTWRITEBYTECODE=1 "$QEMU" -L "$SR" -0 "/data/$venv/bin/python" "$SR/usr/bin/python3.14" \
        "$HERE/tools/guest/smoke_$name.py" >"$SCRATCH/$name.log" 2>&1 || true
    grep -E "$filter" "$SCRATCH/$name.log" || true
    t1=$(date +%s)
    grep -q '^PASSED' "$SCRATCH/$name.log" || fail=1
    echo "    $(grep -c '^ok' "$SCRATCH/$name.log") checks ok, $((t1 - t0)) s"
}
run_guest klippy venv/klippy
run_guest moonraker venv/moonraker
run_guest klarity eitri/klarity/venv

if [ "$ROOT_TEMPLATE" = "$BR_OUT/target" ] && ! ls "$SR"/usr/lib/libsodium.so* >/dev/null 2>&1; then
    echo "WARNING: the image has no libsodium; Moonraker imports libnacl at start-up and will not run without it (BR2_PACKAGE_LIBSODIUM)."
fi

echo "--- sizes"
du -sh "$SR/data/firmware/klipper" "$SR/data/firmware/moonraker" "$SR/data/venv/klippy" "$SR/data/venv/moonraker" \
    "$SR/data/eitri/klarity" "$SR/data/eitri/extras" "$SR/data/eitri/klipper-layer" | sed "s#$SR##"
du -sh --apparent-size "$SR/data" | sed "s#$SR##"
ls -l "$(dirname -- "$MANIFEST")/$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive"]["name"])' "$MANIFEST")" | awk '{print $5, $9}'

if [ "$fail" -ne 0 ]; then echo "verify-seed: FAILED"; [ "$KEEP" -eq 1 ] && echo "kept $SCRATCH"; exit 1; fi
echo "verify-seed: PASSED"
