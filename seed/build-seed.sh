#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the Eitri data seed: the initial, offline-ready /data application layer for one or both
# firmware flavors (Klipper, Kalico), for aarch64, on an x86_64 (or any) build host.
#
#   seed/build-seed.sh [--flavor klipper|kalico|all] [--version V] [--out DIR] [--work DIR]
#                      [--offline] [--keep-work] [--no-bytecode]
#
# Output (per flavor), in --out:
#   data-seed-<version>-<flavor>.tar.zst
#   data-seed-<version>-<flavor>.manifest.json
# See seed/SPEC.md for the archive layout, manifest schema and how the seed is consumed.
#
# Inputs, all pinned: release.lock (Klipper/Kalico/Moonraker commits, Python version), seed/requirements/*.txt
# (hash-pinned wheels), seed/requirements/sdists.toml (the few sources built here), ../klarity, ../klipper-layer.
#
# Tools needed on the host: bash, git, uv, python3 (>= 3.11, 3.14 for pre-compiled bytecode), zstd, tar,
# readelf, and an aarch64 cross toolchain (default: the Buildroot one in $EITRI_BR_OUT/host). Network is needed
# unless everything is already cached under --work/src and the uv cache (--offline).
#
# Environment (all optional):
#   EITRI_BUILD_ROOT     scratch root (default: <repo>/../build)
#   EITRI_BR_OUT         Buildroot output dir (default: $EITRI_BUILD_ROOT/br-out)
#   EITRI_CROSS_CC       cross C compiler (default: $EITRI_BR_OUT/host/bin/aarch64-buildroot-linux-gnu-gcc)
#   EITRI_CROSS_STRIP    matching strip
#   EITRI_PY_INCLUDE     the image's Python headers (default: $EITRI_BR_OUT/staging/usr/include/python3.14)
#   EITRI_SYSCONFIGDATA  the image's _sysconfigdata__linux_aarch64-linux-gnu.py
#   EITRI_SYSROOT        image root for the ELF dependency check (default: $EITRI_BR_OUT/target; optional)
#   EITRI_GLIBC_MAX      newest glibc symbol version allowed (default: read from the sysroot, else 2.42)
#   EITRI_SEED_EPOCH     override the reproducible timestamp (default: newest pinned commit time)
#   EITRI_SEED_VERSION   same as --version
#   EITRI_ZSTD_LEVEL     default 19
set -euo pipefail
umask 022

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$(cd -- "$HERE/.." && pwd -P)"
TOOL=(python3 -I "$HERE/tools/seedtool.py")

FLAVORS=all
VERSION="${EITRI_SEED_VERSION:-0.0.0.dev0}"
BUILD_ROOT="${EITRI_BUILD_ROOT:-$(cd -- "$REPO/.." && pwd -P)/build}"
OUT=""
WORK=""
OFFLINE=0
KEEP_WORK=0
BYTECODE=1
ZSTD_LEVEL="${EITRI_ZSTD_LEVEL:-19}"

usage() { sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "build-seed: $*" >&2; exit 1; }
log() { echo "==> $*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --flavor)      FLAVORS="${2:?}"; shift 2 ;;
        --version)     VERSION="${2:?}"; shift 2 ;;
        --out)         OUT="${2:?}"; shift 2 ;;
        --work)        WORK="${2:?}"; shift 2 ;;
        --offline)     OFFLINE=1; shift ;;
        --keep-work)   KEEP_WORK=1; shift ;;
        --no-bytecode) BYTECODE=0; shift ;;
        -h|--help)     usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done
case "$FLAVORS" in all) FLAVORS="klipper kalico" ;; klipper|kalico) ;; *) die "--flavor must be klipper, kalico or all" ;; esac
[[ "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+_~-]*$ ]] || die "invalid --version '$VERSION'"

SEED_BUILD="$BUILD_ROOT/seed"
OUT="${OUT:-$SEED_BUILD/out}"
WORK="${WORK:-$SEED_BUILD/work}"
BR_OUT="${EITRI_BR_OUT:-$BUILD_ROOT/br-out}"
CROSS_CC="${EITRI_CROSS_CC:-$BR_OUT/host/bin/aarch64-buildroot-linux-gnu-gcc}"
CROSS_STRIP="${EITRI_CROSS_STRIP:-$(dirname -- "$CROSS_CC")/$(basename -- "$CROSS_CC" | sed 's/gcc$/strip/')}"
READELF="${EITRI_READELF:-}"
if [ -z "$READELF" ]; then
    if [ -x "$BR_OUT/host/bin/aarch64-buildroot-linux-gnu-readelf" ]; then READELF="$BR_OUT/host/bin/aarch64-buildroot-linux-gnu-readelf"; else READELF=readelf; fi
fi
PY_INCLUDE="${EITRI_PY_INCLUDE:-$BR_OUT/staging/usr/include/python3.14}"
SYSCONFIGDATA="${EITRI_SYSCONFIGDATA:-$BR_OUT/target/usr/lib/python3.14/_sysconfigdata__linux_aarch64-linux-gnu.py}"
SYSROOT="${EITRI_SYSROOT:-$BR_OUT/target}"
export UV="${UV:-uv}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$SEED_BUILD/cache}"
[ "$OFFLINE" -eq 0 ] || export UV_OFFLINE=1

for t in git "$UV" python3 zstd tar "$READELF"; do
    command -v "$t" >/dev/null 2>&1 || die "required tool not found: $t"
done
[ -x "$CROSS_CC" ] || command -v "$CROSS_CC" >/dev/null 2>&1 || die "cross compiler not found: $CROSS_CC (set EITRI_CROSS_CC)"
[ -d "$PY_INCLUDE" ] || die "Python headers not found: $PY_INCLUDE (set EITRI_PY_INCLUDE)"
[ -f "$SYSCONFIGDATA" ] || die "sysconfigdata not found: $SYSCONFIGDATA (set EITRI_SYSCONFIGDATA)"
[ -f "$PY_INCLUDE/Python.h" ] || die "$PY_INCLUDE has no Python.h"

if [ -n "${EITRI_GLIBC_MAX:-}" ]; then
    GLIBC_MAX="$EITRI_GLIBC_MAX"
elif [ -e "$SYSROOT/lib/libc.so.6" ]; then
    GLIBC_MAX="$(strings -a "$SYSROOT/lib/libc.so.6" 2>/dev/null | sed -n 's/.*GNU C Library .*release version \([0-9]*\.[0-9]*\).*/\1/p' | head -1 || true)"
    GLIBC_MAX="${GLIBC_MAX:-2.42}"
else
    GLIBC_MAX=2.42
fi

mkdir -p "$OUT" "$WORK" "$UV_CACHE_DIR"
OUT="$(cd -- "$OUT" && pwd -P)"
WORK="$(cd -- "$WORK" && pwd -P)"

git_q() { git -c advice.detachedHead=false -c gc.auto=0 "$@"; }

# --- sources ---------------------------------------------------------------------------------------------

# fetch_repo NAME URL COMMIT TAG TAG_COMMIT -> $WORK/src/NAME
# Exactly the pinned commit, with just enough history for `git describe` to work: depth 1 when the commit is the
# tag, otherwise everything since the day before the tag (Klipper: ~800 commits instead of 25 000+).
fetch_repo() {
    local name="$1" url="$2" commit="$3" tag="$4" tag_commit="$5" d="$WORK/src/$1"
    if [ -d "$d/.git" ] && git -C "$d" cat-file -e "$commit^{commit}" 2>/dev/null \
        && [ "$(git -C "$d" rev-parse "refs/tags/$tag^{commit}" 2>/dev/null)" = "$tag_commit" ]; then
        log "using cached $name @ ${commit:0:12}"
    else
        [ "$OFFLINE" -eq 0 ] || die "$name @ $commit is not cached and --offline was given"
        log "fetching $name @ ${commit:0:12} from $url"
        rm -rf "$d"; mkdir -p "$d"
        git init -q --template= "$d"
        git -C "$d" remote add origin "$url"
        git_q -C "$d" fetch -q --no-tags --depth=1 origin "refs/tags/$tag:refs/tags/$tag"
        if [ "$commit" = "$tag_commit" ]; then
            git_q -C "$d" fetch -q --no-tags --depth=1 origin "$commit"
        else
            local since
            since="$(git -C "$d" log -1 --format=%ct "refs/tags/$tag^{commit}")"
            since="$((since - 86400))"
            git_q -C "$d" fetch -q --no-tags --shallow-since="@$since" origin "$commit" "refs/tags/$tag:refs/tags/$tag"
        fi
        git_q -C "$d" checkout -q --detach "$commit"
    fi
    [ "$(git -C "$d" rev-parse HEAD)" = "$commit" ] || git_q -C "$d" checkout -q --detach "$commit"
    [ "$(git -C "$d" rev-parse "refs/tags/$tag^{commit}")" = "$tag_commit" ] \
        || die "$name: tag $tag is $(git -C "$d" rev-parse "refs/tags/$tag^{commit}"), release.lock says $tag_commit"
    git -C "$d" merge-base --is-ancestor "$tag_commit" "$commit" || die "$name: $tag is not an ancestor of $commit"
    [ -z "$(git -C "$d" status --porcelain)" ] || die "$name: work tree is not clean"
}

# stage_repo NAME DEST ORIGIN BRANCH COMMIT: a pristine, attached clone of the pinned commit for the printer.
stage_repo() {
    local name="$1" dest="$2" origin="$3" branch="$4" commit="$5"
    rm -rf "$dest"; mkdir -p "$(dirname -- "$dest")"
    cp -a "$WORK/src/$name" "$dest"
    local g=(git -C "$dest")
    "${g[@]}" update-ref "refs/remotes/origin/$branch" "$commit"
    git_q -C "$dest" checkout -q -B "$branch" "$commit"
    "${g[@]}" config branch."$branch".remote origin
    "${g[@]}" config branch."$branch".merge "refs/heads/$branch"
    "${g[@]}" config remote.origin.url "$origin"
    "${g[@]}" config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"
    # core.abbrev: otherwise the length of `git describe`'s hash depends on the object count, and the version
    # string (and the MCU firmware version lock built from it) would differ between builds.
    "${g[@]}" config core.abbrev 9
    rm -rf "$dest/.git/logs" "$dest/.git/FETCH_HEAD" "$dest/.git/ORIG_HEAD"
    # One pack, packed the same way every time.
    git_q -C "$dest" -c pack.threads=1 -c pack.writeBitmaps=false -c repack.writeBitmaps=false \
        repack -a -d -f -q --window=10 --depth=50
    git -C "$dest" prune-packed
    # An index without stat data is byte-reproducible; the first `git status` on the printer refreshes it.
    rm -f "$dest/.git/index"
    "${g[@]}" read-tree HEAD
    [ "$(git -C "$dest" rev-parse HEAD)" = "$commit" ] || die "$name: staged HEAD is wrong"
    [ -z "$(git -C "$dest" status --porcelain)" ] || die "$name: staged work tree is not clean"
}

# --- tools shared by the flavors -----------------------------------------------------------------------

BUILDENV="$WORK/buildenv"
make_buildenv() {
    local want
    want="$(sha256sum "$HERE/requirements/build.txt" | cut -d' ' -f1)"
    if [ -f "$BUILDENV/.locksum" ] && [ "$(cat "$BUILDENV/.locksum")" = "$want" ]; then return; fi
    log "creating the host-side build environment"
    rm -rf "$BUILDENV"
    UV_PYTHON_DOWNLOADS=never "$UV" venv -q --no-config --python "${EITRI_BUILD_PYTHON:-3.14}" "$BUILDENV" 2>/dev/null \
        || UV_PYTHON_DOWNLOADS=never "$UV" venv -q --no-config --python python3 "$BUILDENV"
    "$UV" pip install -q --no-config --python "$BUILDENV/bin/python" --require-hashes --only-binary :all: -r "$HERE/requirements/build.txt"
    echo "$want" >"$BUILDENV/.locksum"
}

KLARITY_WHEEL=""
build_klarity_wheel() {
    local epoch="$1" src="$WORK/klarity-src" out="$WORK/klarity-wheel"
    rm -rf "$src" "$out"; mkdir -p "$src" "$out"
    cp "$REPO/klarity/pyproject.toml" "$REPO/klarity/README.md" "$src/"
    cp -a "$REPO/klarity/src" "$src/src"
    find "$src" \( -name __pycache__ -o -name '*.pyc' \) -prune -exec rm -rf {} +
    "${TOOL[@]}" build-wheel --src "$src" --out "$out" --buildenv "$BUILDENV" --epoch "$epoch" >/dev/null
    KLARITY_WHEEL="$(ls "$out"/klarity-*.whl)"
}

# --- one flavor ----------------------------------------------------------------------------------------

build_flavor() {
    local flavor="$1"
    log "=== flavor: $flavor, seed version $VERSION ==="
    eval "$("${TOOL[@]}" pins --repo "$REPO" --flavor "$flavor")"

    fetch_repo "$FW_NAME" "${FW_ORIGIN%.git}" "$FW_COMMIT" "$FW_TAG" "$FW_TAG_COMMIT"
    fetch_repo moonraker "${MR_ORIGIN%.git}" "$MR_COMMIT" "$MR_TAG" "$MR_COMMIT"

    local epoch="${EITRI_SEED_EPOCH:-}"
    if [ -z "$epoch" ]; then
        local a b
        a="$(git -C "$WORK/src/$FW_NAME" log -1 --format=%ct "$FW_COMMIT")"
        b="$(git -C "$WORK/src/moonraker" log -1 --format=%ct "$MR_COMMIT")"
        epoch=$(( a > b ? a : b ))
    fi
    local created="${SOURCE_DATE_EPOCH:-$(date -u +%s)}"

    local stage="$WORK/stage-$flavor" data
    rm -rf "$stage"; mkdir -p "$stage/data"
    data="$(cd -- "$stage/data" && pwd -P)"

    log "staging the firmware clones"
    stage_repo "$FW_NAME" "$data/firmware/klipper" "$FW_ORIGIN" "$FW_BRANCH" "$FW_COMMIT"
    stage_repo moonraker "$data/firmware/moonraker" "$MR_ORIGIN" "$MR_BRANCH" "$MR_COMMIT"

    log "klipper-layer: extras and config set"
    install -d -m 0755 "$data/eitri/extras" "$data/eitri/klipper-layer"
    local f
    for f in "$REPO"/klipper-layer/extras/*.py "$REPO"/klipper-layer/extras/LICENSE; do
        [ -f "$f" ] && install -m 0644 "$f" "$data/eitri/extras/"
    done
    cp -a "$REPO/klipper-layer/config" "$REPO/klipper-layer/scripts" "$data/eitri/klipper-layer/"
    [ ! -f "$REPO/klipper-layer/README.md" ] || install -m 0644 "$REPO/klipper-layer/README.md" "$data/eitri/klipper-layer/"
    ln -s ../extras "$data/eitri/klipper-layer/extras"
    printf '%s\n' "$LAYER_VERSION" >"$data/eitri/klipper-layer/VERSION"
    find "$data/eitri" \( -name __pycache__ -o -name '*.pyc' \) -prune -exec rm -rf {} +
    chmod 0755 "$data"/eitri/klipper-layer/scripts/* 2>/dev/null || true

    log "overlay: link the extras into klippy/extras (git status stays clean)"
    local pd="$WORK/overlay-printer-data"
    rm -rf "$pd"; mkdir -p "$pd"
    bash "$data/eitri/klipper-layer/scripts/overlay-install.sh" -q \
        --klipper-dir "$data/firmware/klipper" \
        --extras-dir "$data/eitri/extras" \
        --config-dir "$data/eitri/klipper-layer/config" \
        --printer-data "$pd" --flavor "$flavor" --flavor-file /nonexistent
    # The links must point at the printer's /data, not at this build tree.
    local link n=0
    for link in "$data"/firmware/klipper/klippy/extras/*; do
        [ -L "$link" ] || continue
        case "$(readlink "$link")" in
            "$data"/eitri/extras/*) ln -sfn "/data/eitri/extras/$(basename -- "$link")" "$link"; n=$((n + 1)) ;;
            *) die "unexpected symlink $link -> $(readlink "$link")" ;;
        esac
    done
    [ "$n" -gt 0 ] || die "the overlay linked no extras"
    rm -rf "$pd"
    [ -z "$(git -C "$data/firmware/klipper" status --porcelain)" ] || die "overlay left the clone dirty"

    log "chelper: cross-compile c_helper.so"
    "${TOOL[@]}" chelper --klipper "$data/firmware/klipper" --cc "$CROSS_CC" --strip "$CROSS_STRIP" \
        --epoch "$epoch" --report "$WORK/chelper-$flavor.json"

    make_buildenv
    build_klarity_wheel "$epoch"

    local rep="$WORK/report-$flavor"; rm -rf "$rep"; mkdir -p "$rep"

    log "venv: klippy ($flavor)"
    "${TOOL[@]}" make-venv --stage "$data" --dest venv/klippy --lock "$HERE/requirements/klippy-$flavor.txt" \
        --python-version "$PY_FULL_VERSION" --report "$rep/klippy-packages.json"

    log "venv: moonraker (cross-compiling the sdist-only dependencies)"
    local offline_arg=()
    [ "$OFFLINE" -eq 0 ] || offline_arg=(--offline)
    "${TOOL[@]}" build-sdists --sdists "$HERE/requirements/sdists.toml" --venv moonraker --work "$WORK/sdists-$flavor" \
        --buildenv "$BUILDENV" --epoch "$epoch" --cross-cc "$CROSS_CC" --sysconfigdata "$SYSCONFIGDATA" \
        --py-include "$PY_INCLUDE" "${offline_arg[@]}" --report "$rep/moonraker-sdists.json"
    local mr_wheels=()
    while IFS= read -r f; do mr_wheels+=(--wheel "$f"); done < <(python3 -I -c 'import json,sys; [print(w["wheel"]) for w in json.load(open(sys.argv[1]))]' "$rep/moonraker-sdists.json")
    "${TOOL[@]}" make-venv --stage "$data" --dest venv/moonraker --lock "$HERE/requirements/moonraker.txt" \
        --python-version "$PY_FULL_VERSION" --report "$rep/moonraker-packages.json" "${mr_wheels[@]}"

    log "venv: klarity"
    "${TOOL[@]}" make-venv --stage "$data" --dest eitri/klarity/venv --lock "$HERE/requirements/klarity.txt" \
        --python-version "$PY_FULL_VERSION" --report "$rep/klarity-packages.json" --wheel "$KLARITY_WHEEL"

    log "checking every compiled object (architecture, glibc symbol versions, shared libraries)"
    local sysroot_arg=()
    [ -d "$SYSROOT/usr/lib" ] && sysroot_arg=(--sysroot "$SYSROOT")
    "${TOOL[@]}" check-elf --root "$data" --glibc-max "$GLIBC_MAX" --readelf "$READELF" "${sysroot_arg[@]}" \
        --allow-file "$HERE/requirements/image-libs.txt" --report "$rep/elf.json"

    if [ "$BYTECODE" -eq 1 ]; then
        log "byte-compiling (timestamps are the build epoch, so the bytecode stays valid after extraction)"
        "${TOOL[@]}" normalize --stage "$data" --epoch "$epoch"
        "${TOOL[@]}" compile-bytecode --stage "$data" venv eitri/klarity/venv firmware/klipper/klippy firmware/moonraker/moonraker
    fi

    # --- facts for the manifest ---
    local describe_fw describe_mr
    describe_fw="$(git -C "$data/firmware/klipper" describe --always --tags --long --dirty)"
    describe_mr="$(git -C "$data/firmware/moonraker" describe --always --tags --long --dirty)"
    local kl_ver kl_name
    kl_name="$(basename -- "$KLARITY_WHEEL")"
    kl_ver="$(echo "$kl_name" | cut -d- -f2)"
    export F_FLAVOR="$flavor" F_VERSION="$VERSION" F_EPOCH="$epoch" F_CREATED="$created" F_PYFULL="$PY_FULL_VERSION" \
        F_GLIBC="$GLIBC_MAX" F_FW_NAME="$FW_NAME" F_FW_VERSION="$FW_VERSION" F_FW_COMMIT="$FW_COMMIT" F_FW_TAG="$FW_TAG" \
        F_FW_ORIGIN="$FW_ORIGIN" F_FW_BRANCH="$FW_BRANCH" F_FW_DESCRIBE="$describe_fw" \
        F_MR_VERSION="$MR_VERSION" F_MR_COMMIT="$MR_COMMIT" F_MR_TAG="$MR_TAG" F_MR_ORIGIN="$MR_ORIGIN" \
        F_MR_BRANCH="$MR_BRANCH" F_MR_DESCRIBE="$describe_mr" F_KL_VERSION="$kl_ver" F_KL_WHEEL="$kl_name" \
        F_KL_SHA="$(sha256sum "$KLARITY_WHEEL" | cut -d' ' -f1)" F_LAYER_VERSION="$LAYER_VERSION" \
        F_CC="$("$CROSS_CC" --version | head -1)" F_UV="$("$UV" --version)" F_ZSTD="$(zstd --version | head -1)" \
        F_GIT="$(git --version)" F_PYINC="$(sed -n 's/^#define PY_VERSION[[:space:]]*"\(.*\)"/\1/p' "$PY_INCLUDE/patchlevel.h" | head -1)" F_REP="$rep" F_CHELPER="$WORK/chelper-$flavor.json" F_ELF="$rep/elf.json"
    python3 -I - >"$rep/facts.json" <<'PY'
import json, os, sys
e = os.environ
rep = e["F_REP"]
def built(name):
    p = f"{rep}/{name}-sdists.json"
    if not os.path.exists(p):
        return []
    return [{"name": x["name"], "version": x["version"], "sdist_sha256": x["sdist_sha256"],
             "wheel": os.path.basename(x["wheel"]), "wheel_sha256": x["wheel_sha256"]} for x in json.load(open(p))]
facts = {
    "flavor": e["F_FLAVOR"], "version": e["F_VERSION"], "source_date_epoch": int(e["F_EPOCH"]),
    "created_epoch": int(e["F_CREATED"]), "python_full_version": e["F_PYFULL"], "glibc_max": e["F_GLIBC"],
    "chelper_report": e["F_CHELPER"], "elf_report": e["F_ELF"], "chelper_compiler": e["F_CC"],
    "sources": {
        "firmware": {"name": e["F_FW_NAME"], "version": e["F_FW_VERSION"], "commit": e["F_FW_COMMIT"], "tag": e["F_FW_TAG"],
                     "describe": e["F_FW_DESCRIBE"], "origin": e["F_FW_ORIGIN"], "branch": e["F_FW_BRANCH"], "shallow": True},
        "moonraker": {"name": "moonraker", "version": e["F_MR_VERSION"], "commit": e["F_MR_COMMIT"], "tag": e["F_MR_TAG"],
                      "describe": e["F_MR_DESCRIBE"], "origin": e["F_MR_ORIGIN"], "branch": e["F_MR_BRANCH"], "shallow": True},
        "klarity": {"version": e["F_KL_VERSION"], "wheel": e["F_KL_WHEEL"], "wheel_sha256": e["F_KL_SHA"]},
        "klipper_layer": {"version": e["F_LAYER_VERSION"]},
    },
    "venvs": {
        "klippy": {"packages_json": f"{rep}/klippy-packages.json", "built_from_source": built("klippy")},
        "moonraker": {"packages_json": f"{rep}/moonraker-packages.json", "built_from_source": built("moonraker")},
        "klarity": {"packages_json": f"{rep}/klarity-packages.json", "built_from_source": []},
    },
    "builder": {"script": "seed/build-seed.sh", "uv": e["F_UV"], "zstd": e["F_ZSTD"], "git": e["F_GIT"],
                "host_python": sys.version.split()[0], "cross_compiler": e["F_CC"], "python_headers": e["F_PYINC"]},
}
json.dump(facts, sys.stdout, indent=2, sort_keys=True)
PY

    # Last git invocation done: leave the indexes without stat data (byte-reproducible). The first `git status`
    # on the printer refreshes them.
    local repo_dir
    for repo_dir in "$data/firmware/klipper" "$data/firmware/moonraker"; do
        rm -f "$repo_dir/.git/index"
        git -C "$repo_dir" read-tree HEAD
    done

    log "packing"
    "${TOOL[@]}" finalize --stage "$data" --out "$OUT" --facts "$rep/facts.json" --requirements "$HERE/requirements" \
        --level "$ZSTD_LEVEL"

    log "verifying the result"
    "${TOOL[@]}" verify "$OUT/data-seed-$VERSION-$flavor.manifest.json"

    [ "$KEEP_WORK" -eq 1 ] || rm -rf "$stage" "$rep"
}

for fl in $FLAVORS; do build_flavor "$fl"; done
log "done: $OUT"
ls -l "$OUT" >&2
