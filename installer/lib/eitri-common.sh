# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
#
# Shared helpers for install.sh, revert.sh and firstboot.  Source, do not execute.
#
# Event stream: with EITRI_JSON=1 every step/progress/log line is also written to
# stdout as one JSON object (schema 1, same fields as eitri-mcuflash), so the
# configurator can follow an install the same way it follows an MCU flash.

[[ -n "${EITRI_COMMON_LOADED:-}" ]] && return 0
EITRI_COMMON_LOADED=1

EITRI_COMPONENT="${EITRI_COMPONENT:-install}"
EITRI_JSON="${EITRI_JSON:-0}"
EITRI_EVENTS_FILE="${EITRI_EVENTS_FILE:-}"
EITRI_RUN_ID="${EITRI_RUN_ID:-$(date +%s)-$$}"
DRY_RUN="${DRY_RUN:-1}"

if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  _RED=$'\033[31m'; _YEL=$'\033[33m'; _GRN=$'\033[32m'; _DIM=$'\033[2m'; _BLD=$'\033[1m'; _OFF=$'\033[0m'
else
  _RED=''; _YEL=''; _GRN=''; _DIM=''; _BLD=''; _OFF=''
fi

json_escape() {
  # Escape a string for embedding in JSON (no surrounding quotes).
  printf '%s' "$1" | tr -d '\000-\010\013\014\016-\037' |
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' -e 's/\r/\\r/g' |
    awk 'BEGIN { ORS = "" } { if (NR > 1) printf "\\n"; print }'
}

_now() { date -u +%Y-%m-%dT%H:%M:%S.000Z; }

# emit TYPE [key=value ...]    values are strings; data=<json> is passed through verbatim
emit() {
  local type="$1"; shift
  local line
  line="{\"schema\":1,\"ts\":\"$(_now)\",\"run\":\"$(json_escape "$EITRI_RUN_ID")\",\"component\":\"$EITRI_COMPONENT\",\"type\":\"$type\""
  local kv key val
  for kv in "$@"; do
    key="${kv%%=*}"; val="${kv#*=}"
    case "$key" in
      data | pct | ok) line+=",\"$key\":$val" ;;
      *) line+=",\"$key\":\"$(json_escape "$val")\"" ;;
    esac
  done
  line+='}'
  [[ "$EITRI_JSON" == 1 ]] && printf '%s\n' "$line"
  if [[ -n "$EITRI_EVENTS_FILE" ]]; then printf '%s\n' "$line" >>"$EITRI_EVENTS_FILE" 2>/dev/null || true; fi
  return 0
}

_say() { printf '%s\n' "$*" >&2; }

info() { _say "${_DIM}[eitri]${_OFF} $*"; emit log level=info "msg=$*"; }
warn() { _say "${_YEL}[eitri] WARNING:${_OFF} $*"; emit log level=warn "msg=$*"; }
err()  { _say "${_RED}[eitri] ERROR:${_OFF} $*"; emit log level=error "msg=$*"; }
ok()   { _say "${_GRN}[eitri]${_OFF} $*"; emit log level=info "msg=$*"; }

die() {
  # die CODE MESSAGE...    exits with CODE after emitting a failed result
  local code="$1"; shift
  err "$*"
  emit result ok=false "msg=$*"
  exit "$code"
}

step_start() { _say "${_BLD}==> $2${_OFF}"; emit step "step=$1" status=start "msg=$2"; }
step_ok()    { emit step "step=$1" status=ok "msg=${2:-}"; }
step_skip()  { _say "    skipped: ${2:-}"; emit step "step=$1" status=skipped "msg=${2:-}"; }
step_fail()  { emit step "step=$1" status=failed "msg=${2:-}"; }
progress()   { emit progress "step=$1" "pct=$2" "msg=${3:-}"; }

# mutate "what it does" cmd args...   -- runs only when DRY_RUN=0
mutate() {
  local what="$1"; shift
  if [[ "$DRY_RUN" == 1 ]]; then
    _say "    ${_DIM}[dry-run] would: ${what}${_OFF}"
    emit log level=info "msg=[dry-run] would: $what"
    return 0
  fi
  "$@"
}

# Flush and forget the page cache so that a read-back really reads the medium.
drop_caches() {
  sync
  if [[ -w /proc/sys/vm/drop_caches ]]; then echo 3 >/proc/sys/vm/drop_caches 2>/dev/null || true; fi
  return 0
}

need_cmd() {
  local c missing=()
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
  ((${#missing[@]} == 0)) || die 20 "missing required tools: ${missing[*]}"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

verify_sha256() {
  # verify_sha256 FILE EXPECTED_HEX
  local got
  got="$(sha256_of "$1")"
  [[ "$got" == "${2,,}" ]]
}

human_bytes() {
  awk -v b="$1" 'BEGIN { s = "B KiB MiB GiB TiB"; split(s, u, " "); i = 1; while (b >= 1024 && i < 5) { b /= 1024; i++ } printf "%.1f %s", b, u[i] }'
}

# confirm_typed PHRASE "explanation"
# The phrase must be typed on the terminal, or passed as --confirm / EITRI_CONFIRM
# by a caller that has already shown the explanation to a human (the configurator).
confirm_typed() {
  local phrase="$1" text="$2" answer=''
  _say ""
  _say "${_BLD}${text}${_OFF}"
  if [[ "${EITRI_CONFIRM:-}" == "$phrase" ]]; then
    info "confirmation phrase supplied on the command line"
    return 0
  fi
  if [[ -n "${EITRI_CONFIRM:-}" ]]; then
    die 3 "the phrase given with --confirm is not '$phrase' - nothing was changed"
  fi
  if [[ -t 0 ]]; then
    printf 'Type %s to continue, anything else aborts: ' "$phrase" >&2
    read -r answer || true
  elif { true </dev/tty; } 2>/dev/null; then
    printf 'Type %s to continue, anything else aborts: ' "$phrase" >&2
    read -r answer </dev/tty || true
  else
    die 3 "this step needs the confirmation phrase '$phrase' (pass --confirm '$phrase' once you have read the warning above)"
  fi
  [[ "$answer" == "$phrase" ]] || die 3 "not confirmed - nothing was changed"
}

ask_yes_no() {
  # ask_yes_no "question" default(y|n)   honours --yes
  local q="$1" def="${2:-n}" a=""
  if [[ "${ASSUME_YES:-0}" == 1 ]]; then return 0; fi
  if [[ -t 0 ]]; then
    printf '%s [%s] ' "$q" "$([[ $def == y ]] && echo Y/n || echo y/N)" >&2
    read -r a || true
  elif { true </dev/tty; } 2>/dev/null; then
    printf '%s [%s] ' "$q" "$([[ $def == y ]] && echo Y/n || echo y/N)" >&2
    read -r a </dev/tty || true
  fi
  a="${a:-$def}"
  [[ "${a,,}" == y* ]]
}

# Read "key=value" from a simple env-style file (no sourcing, no eval).
kv_get() {
  # kv_get FILE KEY
  [[ -r "$1" ]] || return 1
  tr -d '\000' <"$1" | awk -F= -v k="$2" '$1 == k { sub(/^[^=]*=/, ""); print; exit }'
}

# Atomically set KEY=VALUE in a state file, keeping every other line.  The file
# lives on the vfat /boot partition, where U-Boot reads it, so write a sibling
# temp file and rename it; a power cut leaves either the old or the new content.
kv_set() {
  # kv_set FILE KEY VALUE
  local f="$1" k="$2" v="$3" tmp
  tmp="$(mktemp "${f}.XXXXXX")" || return 1
  {
    if [[ -r "$f" ]]; then tr -d '\000' <"$f" | awk -F= -v k="$k" '$1 != k && NF'; fi
    printf '%s=%s\n' "$k" "$v"
  } >"$tmp" || { rm -f "$tmp"; return 1; }
  sync "$tmp" 2>/dev/null || sync
  mv -f "$tmp" "$f"
}

# Write a whole key=value file atomically from "key=value" arguments.
kv_write() {
  local f="$1" tmp; shift
  tmp="$(mktemp "${f}.XXXXXX")" || return 1
  printf '%s\n' "$@" >"$tmp" || { rm -f "$tmp"; return 1; }
  sync "$tmp" 2>/dev/null || sync
  mv -f "$tmp" "$f"
}
