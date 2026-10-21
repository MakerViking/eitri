#!/usr/bin/env bash
# Eitri klipper-layer installer: overlay the qd_* extras and the config set
# onto a pristine Klipper or Kalico clone WITHOUT modifying any tracked file.
#
#   * Each extras/*.py is symlinked into <clone>/klippy/extras/ and listed in
#     <clone>/.git/info/exclude (per-clone, never pushed). A name that is
#     already tracked upstream is never shadowed; it is skipped instead (this
#     is how gcode_shell_command coexists with Kalico's own copy).
#   * config/ is linked as <printer_data>/config/eitri, and
#     <printer_data>/config/eitri-flavor.cfg points at the flavor file
#     (config/flavors/klipper.cfg or kalico.cfg) matching the clone, so
#     printer.cfg can [include eitri-flavor.cfg] unconditionally.
#   * Idempotent. --uninstall removes exactly what this script created.
#   * The visible `git status` of the clone is compared before and after; any
#     change is a hard error (exit 4).
#
# Exit codes: 0 ok, 1 usage/invalid tree, 3 conflict (something that is not
# ours is in the way), 4 clean-tree violation.
#
# Copyright 2026 The Eitri Authors. SPDX-License-Identifier: Apache-2.0
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LAYER_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

KLIPPER_DIR="/data/firmware/klipper"
EXTRAS_DIR="$LAYER_DIR/extras"
CONFIG_DIR="$LAYER_DIR/config"
PRINTER_DATA="/data/printer_data"
FLAVOR="auto"
FLAVOR_FILE="/data/eitri/flavor"
INIT_CONFIG=0
UNINSTALL=0
DRY_RUN=0
QUIET=0
RENDER_VARS=()
CONFLICTS=0
OWNER=""
CREATED=()

MARK_BEGIN="# >>> eitri klipper-layer (managed, do not edit) >>>"
MARK_END="# <<< eitri klipper-layer <<<"

usage() {
    cat <<'EOF'
Usage: overlay-install.sh [options]

  --klipper-dir DIR    Klipper/Kalico clone (default /data/firmware/klipper)
  --extras-dir DIR     directory holding the extras (default: ../extras)
  --config-dir DIR     directory holding the config set (default: ../config)
  --printer-data DIR   printer_data root (default /data/printer_data)
  --flavor F           auto (default), klipper or kalico
  --flavor-file FILE   /data/eitri/flavor; only cross-checked, never written
  --init-config        render printer.cfg from the template if none exists
  --vars FILE          extra variables TOML for --init-config (repeatable)
  --owner USER[:GROUP] when run as root, give the files this script creates to
                       this owner (default: owner of printer_data, else of the
                       clone; Klipper must be able to write printer_data/config)
  --uninstall          remove everything this script installed
  --dry-run            print what would be done
  -q, --quiet          only print warnings and errors
  -h, --help           this text
EOF
}

log()  { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { local code=$1; shift; printf 'ERROR: %s\n' "$*" >&2; exit "$code"; }

# Run a mutating command, or just show it under --dry-run.
act() {
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[dry-run] $*"
    else
        "$@"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --klipper-dir)  KLIPPER_DIR="${2:?missing value for $1}"; shift 2 ;;
        --extras-dir)   EXTRAS_DIR="${2:?missing value for $1}"; shift 2 ;;
        --config-dir)   CONFIG_DIR="${2:?missing value for $1}"; shift 2 ;;
        --printer-data) PRINTER_DATA="${2:?missing value for $1}"; shift 2 ;;
        --flavor)       FLAVOR="${2:?missing value for $1}"; shift 2 ;;
        --flavor-file)  FLAVOR_FILE="${2:?missing value for $1}"; shift 2 ;;
        --init-config)  INIT_CONFIG=1; shift ;;
        --vars)         RENDER_VARS+=("${2:?missing value for $1}"); shift 2 ;;
        --owner)        OWNER="${2:?missing value for $1}"; shift 2 ;;
        --uninstall)    UNINSTALL=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -q|--quiet)     QUIET=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) usage >&2; die 1 "unknown option: $1" ;;
    esac
done

case "$FLAVOR" in auto|klipper|kalico) ;; *) die 1 "--flavor must be auto, klipper or kalico" ;; esac

command -v git >/dev/null 2>&1 || die 1 "git is required"
[ -d "$KLIPPER_DIR/klippy/extras" ] || die 1 "$KLIPPER_DIR is not a Klipper/Kalico clone (no klippy/extras)"
[ -d "$EXTRAS_DIR" ] || die 1 "extras directory not found: $EXTRAS_DIR"
KLIPPER_DIR="$(cd -- "$KLIPPER_DIR" && pwd -P)"
EXTRAS_DIR="$(cd -- "$EXTRAS_DIR" && pwd -P)"
EXTRAS_TARGET="$KLIPPER_DIR/klippy/extras"

# safe.directory: the installer may run as root over a clone owned by the
# service user; git would otherwise refuse with "dubious ownership".
git_k() { git -c "safe.directory=$KLIPPER_DIR" -C "$KLIPPER_DIR" "$@"; }

[ "$(git_k rev-parse --is-inside-work-tree 2>/dev/null)" = "true" ] \
    || die 1 "$KLIPPER_DIR is not a git work tree"
[ "$(git_k rev-parse --show-toplevel)" = "$KLIPPER_DIR" ] \
    || die 1 "$KLIPPER_DIR is inside another repository; point --klipper-dir at the clone root"

EXCLUDE_FILE="$(git_k rev-parse --git-path info/exclude)"
case "$EXCLUDE_FILE" in /*) ;; *) EXCLUDE_FILE="$KLIPPER_DIR/$EXCLUDE_FILE" ;; esac

detect_flavor() {
    # Kalico (a Klipper fork) carries danger_options and the compat/plugin
    # machinery; mainline Klipper has none of them.
    if [ -f "$KLIPPER_DIR/klippy/extras/danger_options.py" ] \
        || [ -f "$KLIPPER_DIR/klippy/compat.py" ]; then
        echo kalico
    else
        echo klipper
    fi
}

DETECTED="$(detect_flavor)"
if [ "$FLAVOR" = "auto" ]; then
    FLAVOR="$DETECTED"
elif [ "$FLAVOR" != "$DETECTED" ]; then
    warn "--flavor $FLAVOR given but the clone looks like $DETECTED"
fi
if [ -r "$FLAVOR_FILE" ]; then
    RECORDED="$(tr -d '[:space:]' < "$FLAVOR_FILE")"
    [ "$RECORDED" = "$FLAVOR" ] \
        || warn "$FLAVOR_FILE says '$RECORDED' but the clone is $FLAVOR"
fi

status_snapshot() { git_k status --porcelain=v1 --untracked-files=all; }
BEFORE="$(status_snapshot)"

# --- extras -------------------------------------------------------------

is_tracked() {
    git_k ls-files --error-unmatch -- "klippy/extras/$1" >/dev/null 2>&1
}

# Names of the extras this layer provides (private helpers start with _)
extras_list() {
    local f
    for f in "$EXTRAS_DIR"/*.py; do
        [ -e "$f" ] || continue
        case "$(basename "$f")" in _*|test_*) continue ;; esac
        basename "$f"
    done
}

# Does this symlink target look like one of our extras directories? Besides
# the current one, earlier installs may have used the repository checkout or
# a relocated layer/release directory under /data/eitri.
is_ours_target() {
    case "$1" in
        "$EXTRAS_DIR"/*|*/klipper-layer/extras/*) return 0 ;;
        /data/eitri/extras/*|/data/eitri/*/extras/*) return 0 ;;
        *) return 1 ;;
    esac
}

# Symlinks in the clone that point into one of our extras directories
owned_links() {
    local l
    for l in "$EXTRAS_TARGET"/*; do
        [ -L "$l" ] || continue
        if is_ours_target "$(readlink "$l")"; then basename "$l"; fi
    done
}

LINKED=()

install_extras() {
    local name src dest current
    while IFS= read -r name; do
        src="$EXTRAS_DIR/$name"
        dest="$EXTRAS_TARGET/$name"
        if is_tracked "$name"; then
            log "skip   $name (tracked in this clone; not shadowing)"
            continue
        fi
        if [ -L "$dest" ]; then
            current="$(readlink "$dest")"
            if [ "$current" = "$src" ]; then
                log "ok     $name (already linked)"
                LINKED+=("$name")
                continue
            fi
            if ! is_ours_target "$current"; then
                warn "$dest points at $current, not ours; leaving it"
                CONFLICTS=1; continue
            fi
            act rm -f -- "$dest"
        elif [ -e "$dest" ]; then
            warn "$dest exists and is not a symlink; leaving it"
            CONFLICTS=1
            continue
        fi
        act ln -s -- "$src" "$dest"
        CREATED+=("$dest")
        log "link   $name"
        LINKED+=("$name")
    done < <(extras_list)

    # Drop links to extras that no longer exist in the layer
    local stale
    while IFS= read -r stale; do
        [ -n "$stale" ] || continue
        if ! printf '%s\n' "$(extras_list)" | grep -Fxq -- "$stale"; then
            act rm -f -- "$EXTRAS_TARGET/$stale"
            log "unlink $stale (no longer provided)"
        fi
    done < <(owned_links)
}

write_exclude_block() {
    # $@ = names to hide; none removes the block.
    local tmp
    [ "$DRY_RUN" -eq 1 ] && { log "[dry-run] update $EXCLUDE_FILE"; return; }
    mkdir -p -- "$(dirname -- "$EXCLUDE_FILE")"
    [ -e "$EXCLUDE_FILE" ] || CREATED+=("$EXCLUDE_FILE")
    touch -- "$EXCLUDE_FILE"
    tmp="$(mktemp)"
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        $0 == b { skip = 1; next }
        $0 == e { skip = 0; next }
        !skip   { print }' "$EXCLUDE_FILE" > "$tmp"
    if [ $# -gt 0 ]; then
        [ -s "$tmp" ] && [ -n "$(tail -c1 "$tmp")" ] && echo >> "$tmp"
        {
            echo "$MARK_BEGIN"
            local n
            for n in "$@"; do echo "/klippy/extras/$n"; done
            echo "$MARK_END"
        } >> "$tmp"
    fi
    cat -- "$tmp" > "$EXCLUDE_FILE"
    rm -f -- "$tmp"
}

uninstall_extras() {
    local name
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        act rm -f -- "$EXTRAS_TARGET/$name"
        log "unlink $name"
    done < <(owned_links)
    write_exclude_block
}

# --- config -------------------------------------------------------------

link_config() {
    local cfg="$PRINTER_DATA/config" flavor_src="$CONFIG_DIR/flavors/$FLAVOR.cfg"
    [ -d "$CONFIG_DIR" ] || die 1 "config directory not found: $CONFIG_DIR"
    CONFIG_DIR="$(cd -- "$CONFIG_DIR" && pwd -P)"
    [ -f "$flavor_src" ] || die 1 "missing flavor file $flavor_src"
    [ -d "$PRINTER_DATA" ] || CREATED+=("$PRINTER_DATA")
    [ -d "$cfg" ] || CREATED+=("$cfg")
    act mkdir -p -- "$cfg"
    local eitri="$cfg/eitri"
    CREATED+=("$eitri" "$cfg/eitri-flavor.cfg")
    if [ -L "$eitri" ] && [ "$(readlink "$eitri")" = "$CONFIG_DIR" ]; then
        log "ok     $eitri (already linked)"
    elif [ -e "$eitri" ] || [ -L "$eitri" ]; then
        if [ -L "$eitri" ]; then
            act rm -f -- "$eitri"
            act ln -s -- "$CONFIG_DIR" "$eitri"
            log "relink $eitri -> $CONFIG_DIR"
        else
            warn "$eitri exists and is not a symlink; leaving it"
            CONFLICTS=1
        fi
    else
        act ln -s -- "$CONFIG_DIR" "$eitri"
        log "link   $eitri -> $CONFIG_DIR"
    fi
    # Relative target so the link survives printer_data being relocated
    local fl="$cfg/eitri-flavor.cfg" want="eitri/flavors/$FLAVOR.cfg"
    if [ -L "$fl" ] && [ "$(readlink "$fl")" = "$want" ]; then
        log "ok     $fl (already $FLAVOR)"
    elif [ -e "$fl" ] && [ ! -L "$fl" ]; then
        warn "$fl exists and is not a symlink; leaving it"
        CONFLICTS=1
    else
        act ln -sfn -- "$want" "$fl"
        log "link   $fl -> $want"
    fi
}

unlink_config() {
    local cfg="$PRINTER_DATA/config" l
    l="$cfg/eitri"
    if [ -L "$l" ]; then
        act rm -f -- "$l"; log "unlink $l"
    fi
    l="$cfg/eitri-flavor.cfg"
    if [ -L "$l" ]; then
        case "$(readlink "$l")" in eitri/flavors/*) act rm -f -- "$l"; log "unlink $l" ;; esac
    fi
}

init_config() {
    local out="$PRINTER_DATA/config/printer.cfg" py="${EITRI_PYTHON:-python3}"
    if [ -e "$out" ]; then
        log "keep   $out (user-owned; not re-rendered)"
        return
    fi
    local args=(--output "$out")
    local v
    for v in ${RENDER_VARS[@]+"${RENDER_VARS[@]}"}; do args+=(--vars "$v"); done
    act "$py" "$SCRIPT_DIR/render-config.py" "${args[@]}"
    CREATED+=("$out")
    log "render $out"
}

# --- main ---------------------------------------------------------------

if [ "$UNINSTALL" -eq 1 ]; then
    log "Uninstalling Eitri overlay from $KLIPPER_DIR"
    uninstall_extras
    unlink_config
else
    log "Installing Eitri overlay ($FLAVOR clone at $KLIPPER_DIR)"
    install_extras
    write_exclude_block ${LINKED[@]+"${LINKED[@]}"}
    link_config
    [ "$INIT_CONFIG" -eq 1 ] && init_config
fi

# Running as root: hand what we created to the service user.
fix_ownership() {
    [ "$(id -u)" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] || return 0
    local owner="$OWNER" p
    if [ -z "$owner" ]; then
        if [ -d "$PRINTER_DATA" ] && [ "$(stat -c %u "$PRINTER_DATA")" != 0 ]; then
            owner="$(stat -c %u:%g "$PRINTER_DATA")"
        else
            owner="$(stat -c %u:%g "$KLIPPER_DIR")"
        fi
    fi
    [ "${owner%%:*}" != 0 ] || return 0      # everything is root's: nothing to do
    for p in ${CREATED[@]+"${CREATED[@]}"}; do
        if [ -e "$p" ] || [ -L "$p" ]; then chown -h -- "$owner" "$p"; fi
    done
}
fix_ownership

if [ "$DRY_RUN" -eq 0 ]; then
    AFTER="$(status_snapshot)"
    if [ "$BEFORE" != "$AFTER" ]; then
        printf 'ERROR: git status of %s changed:\n--- before\n%s\n--- after\n%s\n' \
            "$KLIPPER_DIR" "$BEFORE" "$AFTER" >&2
        exit 4
    fi
fi

[ "$CONFLICTS" -eq 0 ] || die 3 "finished with conflicts (see warnings above)"
log "Done."
