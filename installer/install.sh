#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Eitri in-place installer for a stock QIDI X-Plus 4 (stage 1, runs on the stock system over SSH).
#
#   sudo ./install.sh                         dry run: checks everything, changes nothing
#   sudo ./install.sh --flavor klipper --i-have-a-backup --yes [--backup-to-usb | --backup-to-host U@H:DIR]
#
# What a real run does, in order - and where each step can still be undone:
#   1  preflight          read-only checks (printer model, eMMC layout, space, not printing)
#   2  backup             full eMMC image to a USB stick or your PC, read back and verified.   REQUIRED.
#   3  stage              download the signed bundle, verify signature + checksums
#   4  MCU recovery       save a way back for both MCU boards (Katapult read-back + round trip).  This WRITES to the
#                         toolhead (erase + same image back) before step 5, so it needs --accept-mcu-roundtrip.
#   5  --- you type CONVERT ---
#   6  arm stage 2        install the boot manager, one-shot RAM installer; reboot
#      (stage 2, in RAM: shrink the stock root, add rootfs A/B + /data, write + verify, switch)
#   7  first boot of Eitri flashes the MCUs and confirms the system (eitri-firstboot)
# The stock system stays bootable in its own partition; revert.sh switches back in one reboot.
#
# Exit codes: 0 ok | 2 usage | 3 not confirmed | 10 preflight failed | 11 backup problem
#             12 download/verification failed | 13 MCU recovery not proven | 20 missing tools

set -Eeuo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/eitri-common.sh
source "$INSTALLER_DIR/lib/eitri-common.sh"
# shellcheck source=lib/layout.sh
source "$INSTALLER_DIR/lib/layout.sh"
source "$INSTALLER_DIR/lib/stage2-build.sh"

EITRI_COMPONENT=install
R="${EITRI_ROOT:-}"                                     # test hook: prefix for every system path we read
STATE_DIR="${EITRI_STATE_DIR:-$R/var/lib/eitri-install}"
DISK="${EITRI_DISK:-/dev/mmcblk1}"
PART_PREFIX="p"
DEFAULT_BUNDLE_URL="${EITRI_BUNDLE_URL:-https://github.com/eitri-fw/eitri/releases/latest/download/}"
CONFIRM_PHRASE="CONVERT"
STOCK_SERVICES=(klipper.service moonraker.service makerbase-client.service QIDILink-client.service webcamd.service)

# ---- options ---------------------------------------------------------------
I_HAVE_BACKUP=0; ASSUME_YES=0; FORCE_DRY=0
FLAVOR=""; BUNDLE_URL=""; BUNDLE_DIR=""; PUBKEYS=()
STAGE_DIR=""; STAGE_USB=""
BACKUP_MODE=""; BACKUP_TARGET=""; BACKUP_FILE=""; BACKUP_FILE_SHA=""; BACKUP_MAX_AGE_H=72
MCU_MODE="auto"; PROBE_MCU=0; ACCEPT_NO_MCU_BACKUP=0; STOCK_MCU_BINS=()
STATUS_ONLY=0; BACKUP_STREAM=0; STAGE_ONLY=0; MCU_EXTRA=(); ACCEPT_MCU_ROUNDTRIP=0

usage() {
  awk 'NR >= 3 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
  cat <<'USAGE'

Options
  --flavor klipper|kalico        firmware flavor to install (asked interactively if omitted on a real run)
  --i-have-a-backup --yes        BOTH are required to make real changes; without them this is a dry run
  --dry-run                      force a dry run even if the two flags above are given
  --confirm CONVERT              supply the typed confirmation (for the web configurator); never use blindly

  --bundle-url URL               directory URL holding install.json (+ .minisig) and the payload files
  --bundle-dir DIR               same, from a local directory (USB stick, offline install)
  --pubkey FILE|DIR              trusted minisign public key(s); default: keys/ next to this script
  --stage-dir DIR                where the payload is staged (default: /var/lib/eitri-install/payload on the stock disk)
  --stage-usb [DEVICE]           stage on a USB stick instead (needs less space on the printer)

  Backup (one is required unless a verified backup is already recorded):
  --backup-to-usb [DEVICE]       stream a compressed full-disk image to a USB stick (FAT32 is split automatically)
  --backup-to-host USER@HOST:DIR stream it over ssh to your PC (needs sshd on the PC)
  --backup-file FILE --backup-sha256 HASH
                                 use an image you already made (e.g. mmcblk1.img.gz + the sha256 of that file)
  --backup-stream                write the compressed image to stdout, for pulling from the PC:
                                   ssh root@PRINTER 'eitri-install/install.sh --backup-stream' > printer-backup.img.gz
                                 then record it: install.sh --backup-file printer-backup.img.gz --backup-sha256 <sha256 of that file>

  --stock-mcu-bin BOARD=FILE     stock firmware image for a board whose flash cannot be read back (mainboard: qd_mcu.bin)
  --accept-no-mcu-backup         proceed without such an image (you can re-download it from QIDI if needed)
  --mcu auto|manual|skip         auto (default): flash both MCUs on the first Eitri boot; manual: leave it to the wizard
  --accept-mcu-roundtrip         consent to the MCU recovery proof, which WRITES to the toolhead board before the CONVERT
                                 prompt: its application area is erased and the same image written back (a few seconds;
                                 a power cut in that window needs a Katapult re-flash from the saved image, see
                                 docs/recovery.md). Asked interactively if omitted; required when there is no terminal.
  --probe-mcu                    in a dry run, also stop Klipper briefly to probe the MCUs
  --mcu-arg ARG                  pass ARG to eitri-mcuflash (repeatable), e.g. --mcu-arg --assign --mcu-arg toolhead=/dev/ttyS2
  --status                       show what an earlier run did (and any stage-2 error)
  --json / --events-file FILE    machine-readable progress (see docs/EVENTS.md)
USAGE
}

parse_args() {
  while (($#)); do
    case "$1" in
      -h | --help) usage; exit 0 ;;
      --dry-run) FORCE_DRY=1 ;;
      --i-have-a-backup) I_HAVE_BACKUP=1 ;;
      --yes | -y) ASSUME_YES=1 ;;
      --confirm) EITRI_CONFIRM="${2:?--confirm needs a value}"; shift ;;
      --flavor) FLAVOR="${2:?}"; shift ;;
      --bundle-url) BUNDLE_URL="${2:?}"; shift ;;
      --bundle-dir) BUNDLE_DIR="${2:?}"; shift ;;
      --pubkey) PUBKEYS+=("${2:?}"); shift ;;
      --stage-dir) STAGE_DIR="${2:?}"; shift ;;
      --stage-usb) STAGE_USB="auto"; if [[ "${2:-}" == /dev/* ]]; then STAGE_USB="$2"; shift; fi ;;
      --backup-to-usb) BACKUP_MODE=usb; BACKUP_TARGET=auto; if [[ "${2:-}" == /dev/* ]]; then BACKUP_TARGET="$2"; shift; fi ;;
      --backup-to-host) BACKUP_MODE=host; BACKUP_TARGET="${2:?}"; shift ;;
      --backup-file) BACKUP_MODE=file; BACKUP_FILE="${2:?}"; shift ;;
      --backup-sha256) BACKUP_FILE_SHA="${2:?}"; shift ;;
      --backup-stream) BACKUP_STREAM=1 ;;
      --backup-max-age-hours) BACKUP_MAX_AGE_H="${2:?}"; shift ;;
      --stock-mcu-bin) STOCK_MCU_BINS+=("${2:?}"); shift ;;
      --accept-no-mcu-backup) ACCEPT_NO_MCU_BACKUP=1 ;;
      --accept-mcu-roundtrip) ACCEPT_MCU_ROUNDTRIP=1 ;;
      --mcu) MCU_MODE="${2:?}"; shift ;;
      --probe-mcu) PROBE_MCU=1 ;;
      --mcu-arg) MCU_EXTRA+=("${2:?}"); shift ;;
      --stage-only) STAGE_ONLY=1 ;;
      --status) STATUS_ONLY=1 ;;
      --json) EITRI_JSON=1 ;;
      --events-file) EITRI_EVENTS_FILE="${2:?}"; shift ;;
      *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
  done
  [[ -z "$FLAVOR" || "$FLAVOR" == klipper || "$FLAVOR" == kalico ]] || { echo "--flavor must be klipper or kalico" >&2; exit 2; }
  case "$MCU_MODE" in auto | manual | skip) ;; *) echo "--mcu must be auto, manual or skip" >&2; exit 2 ;; esac
  if [[ "$BACKUP_MODE" == file && -z "$BACKUP_FILE_SHA" ]]; then echo "--backup-file needs --backup-sha256" >&2; exit 2; fi

  # Real changes need both flags, and never in the same breath as --dry-run.
  if ((I_HAVE_BACKUP && ASSUME_YES && !FORCE_DRY)); then
    DRY_RUN=0
  else
    DRY_RUN=1
    if ((I_HAVE_BACKUP != ASSUME_YES)) && ((!FORCE_DRY)); then
      echo "Both --i-have-a-backup and --yes are needed for a real run; continuing as a dry run." >&2
    fi
  fi
  [[ -n "$STAGE_DIR" ]] || STAGE_DIR="$STATE_DIR/payload"
}

# ---- small helpers -----------------------------------------------------------------

hostname_now() { cat "$(P /etc/hostname)" 2>/dev/null || hostname 2>/dev/null || echo unknown; }
# The same name, reduced to characters that are safe in a file name and in a (remote) shell command.
safe_hostname() { local h; h="$(hostname_now | tr -cd 'A-Za-z0-9._-')"; printf '%s' "${h:-unknown}"; }
# sh_quote STR -> STR single-quoted for a POSIX shell, whatever it contains
sh_quote() { local q="'" s="$1"; printf "'%s'" "${s//$q/$q\\$q$q}"; }

disk_sectors() { blockdev --getsz "$DISK"; }
part_dev() { printf '%s%s%s' "$DISK" "$PART_PREFIX" "$1"; }

sfdisk_dump() { sfdisk -d "$DISK" 2>/dev/null; }

dump_field() {
  # dump_field DUMP PART FIELD
  awk -v n="$2" -v f="$3" '
    $1 ~ /[0-9]+$/ && match($1, /[0-9]+$/) && substr($1, RSTART) + 0 == n {
      for (i = 1; i <= NF; i++) if ($i ~ ("^" f "=")) { v = $i; if (v == f "=") v = $(i + 1); sub(/^[a-z]+=/, "", v); sub(/,$/, "", v); print v; exit }
    }' <<<"$1"
}

fetch_to() {
  # fetch_to URL FILE   (resumes; https and file:// only)
  local url="$1" out="$2"
  case "$url" in https://* | file://*) ;; *) die 12 "refusing to download from $url (https:// only)" ;; esac
  if have_cmd curl; then
    curl -fsSL --retry 5 --retry-delay 3 --connect-timeout 15 -C - --proto '=https,file' --proto-redir '=https' -o "$out" "$url"
  elif have_cmd wget; then
    wget -q -c -O "$out" "$url"
  else
    die 20 "neither curl nor wget is installed"
  fi
}

verify_signature() {
  # verify_signature FILE SIGFILE
  local keys=("${PUBKEYS[@]}")
  ((${#keys[@]})) || keys=("$INSTALLER_DIR/keys")
  local args=() k
  for k in "${keys[@]}"; do args+=(--pubkey "$k"); done
  py -c 'import sys; sys.path.insert(0, sys.argv[1]); from eitri_update.minisign import main; sys.exit(main(sys.argv[2:]))' \
    "$INSTALLER_DIR/eitri-update" verify "$1" --sig "$2" "${args[@]}"
}

# ---- status ------------------------------------------------------------------------------
show_status() {
  local boot; boot="$(P /boot)/eitri"
  echo "Eitri installer status"
  if [[ -r "$boot/boot.env" ]]; then echo "  boot selection:"; sed 's/^/    /' "$boot/boot.env" | tr -d '\000'; else echo "  not installed (no $boot/boot.env)"; fi
  [[ -r "$boot/stage2.state" ]] && { echo "  stage 2:"; sed 's/^/    /' "$boot/stage2.state"; }
  if [[ -r "$boot/stage2.state" ]] && ! grep -q '^phase=committed' "$boot/stage2.state"; then
    echo "  The conversion has not finished (see the phase above). If the printer is stuck or powered off during it:"
    echo "    - an interrupted conversion is resumed automatically: boot.scr starts the installer again (two starts are allowed);"
    echo "    - when the starts are used up the STOCK system boots; re-running install.sh is refused from then on because the"
    echo "      partition table has changed. Use 'revert.sh --full' (stock system still boots) to restore the backup image, or the"
    echo "      maskrom procedure in docs/recovery.md (rkdeveloptool) if nothing boots."
  fi
  [[ -r "$boot/LAST_ERROR" ]] && { echo "  LAST ERROR from stage 2:"; sed 's/^/    /' "$boot/LAST_ERROR"; echo "  full log: $boot/stage2.log"; }
  [[ -r "$STATE_DIR/backup.env" ]] && { echo "  recorded backup:"; sed 's/^/    /' "$STATE_DIR/backup.env"; }
  return 0
}

# ===========================================================================================
# 1. preflight
# ===========================================================================================
PF_FAILS=0
pf_ok()   { _say "    ${_GRN}ok${_OFF}    $*"; emit log level=info "msg=ok: $*"; }
pf_warn() { _say "    ${_YEL}warn${_OFF}  $*"; emit log level=warn "msg=$*"; }
pf_fail() { _say "    ${_RED}FAIL${_OFF}  $*"; emit log level=error "msg=FAILED: $*"; PF_FAILS=$((PF_FAILS + 1)); }

preflight() {
  step_start preflight "Checking that this is an untouched QIDI X-Plus 4"
  need_cmd awk sed tr sha256sum gzip dd tar python3 findmnt lsblk blockdev sfdisk
  if [[ "${EUID:-$(id -u)}" != 0 && "${EITRI_ALLOW_NONROOT:-0}" != 1 ]]; then pf_fail "run as root (sudo ./install.sh ...)"; fi

  [[ "$(uname -m)" == aarch64 ]] && pf_ok "architecture aarch64" || pf_fail "architecture is $(uname -m), expected aarch64"

  local compat model
  compat="$(tr '\0' '\n' <"$(P /proc/device-tree/compatible)" 2>/dev/null || true)"
  model="$(tr -d '\0' <"$(P /proc/device-tree/model)" 2>/dev/null || true)"
  if grep -qx 'firefly,roc-rk3328-cc' <<<"$compat"; then pf_ok "board: ${model:-Firefly ROC-RK3328-CC}"; else pf_fail "not a Firefly ROC-RK3328-CC (device tree says: ${model:-unknown})"; fi

  local hn; hn="$(hostname_now)"
  [[ "$hn" == mkspi ]] && pf_ok "hostname mkspi (stock QIDI image)" ||
    pf_warn "hostname is '$hn'; a stock QIDI image uses 'mkspi' - continuing because the board checks passed"

  # Stock firmware version
  STOCK_SOC="unknown"
  local vfile; vfile="$(P /root/xindi/version)"
  if [[ -r "$vfile" ]]; then
    STOCK_SOC="$(awk -F'[= ]+' '$1 == "soc" {print $2}' "$vfile" | tr -d 'V')"
    pf_ok "stock firmware: $(tr '\n' ' ' <"$vfile" | sed 's/\[version\] *//') "
  else
    pf_warn "cannot read the stock firmware version (/root/xindi/version missing)"
  fi

  # Already converted?
  if [[ -e "$(P /boot)/eitri/boot.env" || -d "$(P /data)/eitri" ]]; then
    if [[ -r "$(P /boot)/eitri/stage2.state" ]] && ! grep -q '^phase=committed' "$(P /boot)/eitri/stage2.state"; then
      pf_fail "an earlier conversion did not finish (stage 2 phase: $(kv_get "$(P /boot)/eitri/stage2.state" phase)). Run './install.sh --status' for what to do; it can only be undone with revert.sh --full or the maskrom procedure (docs/recovery.md)"
    else
      pf_fail "this printer already runs (or has run) Eitri; use eitri-update, or revert.sh to go back to stock"
    fi
  fi

  # Booted from the eMMC we are about to change?
  local rootsrc bootsrc
  rootsrc="$(findmnt -no SOURCE "${R:-/}" 2>/dev/null || true)"; bootsrc="$(findmnt -no SOURCE "$(P /boot)" 2>/dev/null || true)"
  if [[ "$rootsrc" == "$(part_dev 2)" && "$bootsrc" == "$(part_dev 1)" ]]; then
    pf_ok "running from the eMMC ($rootsrc, $bootsrc)"
  else
    pf_fail "root is on '${rootsrc:-?}' and /boot on '${bootsrc:-?}'; expected $(part_dev 2) and $(part_dev 1). Booted from an SD card? This installer only converts the internal eMMC."
  fi

  # Partition table must look exactly like the stock one.
  DUMP="$(sfdisk_dump || true)"
  if [[ -z "$DUMP" ]]; then
    pf_fail "cannot read the partition table of $DISK"
  else
    LABEL_ID="$(awk -F': *' '/^label-id/ {print $2}' <<<"$DUMP")"
    P1_START="$(dump_field "$DUMP" 1 start)"; P1_SIZE="$(dump_field "$DUMP" 1 size)"
    P2_START="$(dump_field "$DUMP" 2 start)"; P2_SIZE="$(dump_field "$DUMP" 2 size)"
    local nparts; nparts="$(grep -c "^${DISK}${PART_PREFIX}[0-9]* :" <<<"$DUMP" || true)"
    if grep -q '^label: dos' <<<"$DUMP" && [[ "$nparts" == 2 && -n "$P1_START" && -n "$P2_START" ]] && ((P1_START == 32768)); then
      pf_ok "partition table: dos, /boot ($((P1_SIZE / 2048)) MiB) + root ($((P2_SIZE / 2048)) MiB), label-id $LABEL_ID"
    else
      pf_fail "unexpected partition table (not the stock 2-partition dos layout); refusing to touch it"
    fi
  fi
  [[ "$(blkid -s TYPE -o value "$(part_dev 1)" 2>/dev/null || echo vfat)" == vfat ]] || pf_fail "$(part_dev 1) is not vfat"

  # Not in the middle of a print
  local ps
  ps="$(curl -fsS --max-time 4 'http://127.0.0.1:7125/printer/objects/query?print_stats' 2>/dev/null | tr -d ' \n' | sed -n 's/.*"state":"\([a-z]*\)".*/\1/p' || true)"
  case "$ps" in
    printing | paused) pf_fail "a print is ${ps}; wait for it to finish (a restart would ruin it)" ;;
    "") pf_warn "could not ask Moonraker whether a print is running; make sure the printer is idle" ;;
    *) pf_ok "printer is idle ($ps)" ;;
  esac

  # Space: stock root usage + payload; /boot for the RAM installer.
  local avail_stage
  avail_stage="$(df -B1 --output=avail "$(P /var/lib)" 2>/dev/null | tail -1 | tr -d ' ' || echo 0)"
  AVAIL_STAGE="${avail_stage:-0}"
  BOOT_FREE="$(df -B1 --output=avail "$(P /boot)" 2>/dev/null | tail -1 | tr -d ' ' || echo 0)"
  ROOT_USED="$(df -B1 --output=used "${R:-/}" 2>/dev/null | tail -1 | tr -d ' ' || echo 0)"
  pf_ok "free: $(human_bytes "${AVAIL_STAGE:-0}") on the stock root, $(human_bytes "${BOOT_FREE:-0}") on /boot; root uses $(human_bytes "${ROOT_USED:-0}")"
  ((${BOOT_FREE:-0} > 64 * 1024 * 1024)) || pf_fail "less than 64 MiB free on /boot (the RAM installer needs about that much)"

  if ((PF_FAILS)); then
    step_fail preflight "$PF_FAILS check(s) failed"
    die 10 "preflight failed - nothing was changed"
  fi
  step_ok preflight "all checks passed"
}

# Does the layout we would create fit, given the payload size?  (stage 2 recomputes this with exact numbers)
plan_layout() {
  step_start layout "Planning the new partition layout"
  local payload="${PAYLOAD_BYTES:-0}" fs_min
  fs_min=$((ROOT_USED + payload + 256 * 1024 * 1024))
  [[ -n "$STAGE_USB" ]] && fs_min=$((ROOT_USED + 256 * 1024 * 1024))
  local plan="$STATE_DIR/layout.plan" msg
  mkdir -p "$STATE_DIR"
  if ! msg="$(layout_compute "$(disk_sectors)" "$P2_START" "$P2_SIZE" "$fs_min" "${ROOT_MIB:-2048}" "${DATA_MIN_MIB:-4096}")"; then
    step_fail layout "$(kv_get <(printf '%s\n' "$msg") error)"
    die 10 "$(kv_get <(printf '%s\n' "$msg") error)"
  fi
  printf '%s\n' "$msg" >"$plan"
  layout_validate "$plan" "$(disk_sectors)" "$P1_START" "$P1_SIZE" >/dev/null || die 10 "internal error: the planned layout is invalid"
  _say "  New layout (the stock system stays bootable in p2):"
  layout_summary "$plan" >&2
  step_ok layout "layout fits"
}

# ===========================================================================================
# 2. backup
# ===========================================================================================
BK_FILE="$STATE_DIR/backup.env"

backup_is_valid() {
  # A recorded, verified backup of THIS disk that is recent enough.
  [[ -r "$BK_FILE" ]] || return 1
  [[ "$(kv_get "$BK_FILE" verified)" == 1 ]] || return 1
  [[ "$(kv_get "$BK_FILE" label_id)" == "${LABEL_ID:-}" ]] || return 1
  local created age
  created="$(kv_get "$BK_FILE" created_epoch)"; [[ "$created" =~ ^[0-9]+$ ]] || return 1
  age=$(($(date +%s) - created))
  ((age >= 0 && age <= BACKUP_MAX_AGE_H * 3600)) || return 1
  return 0
}

quiesce() {
  QUIESCED=()
  local s
  for s in "${STOCK_SERVICES[@]}"; do
    if systemctl is-active --quiet "$s" 2>/dev/null; then
      mutate "stop $s while the disk is read" systemctl stop "$s" && QUIESCED+=("$s")
    fi
  done
  mutate "sync" sync
}
unquiesce() {
  local s
  for s in "${QUIESCED[@]:-}"; do [[ -n "$s" ]] && systemctl start "$s" 2>/dev/null || true; done
  QUIESCED=()
}

find_usb_partition() {
  # find_usb_partition [DEVICE] -> prints one partition (or bare filesystem) on a USB disk, or dies with guidance
  local want="${1:-auto}" d name type fs cands=()
  if [[ "$want" != auto ]]; then printf '%s\n' "$want"; return 0; fi
  while read -r d; do
    while read -r name type fs; do
      [[ -n "$fs" && ( "$type" == part || "$type" == disk ) ]] && cands+=("$name")
    done < <(lsblk -rpno NAME,TYPE,FSTYPE "$d" 2>/dev/null)
  done < <(lsblk -dpno NAME,TRAN 2>/dev/null | awk '$2 == "usb" {print $1}')
  case "${#cands[@]}" in
    0) die 11 "no USB stick found. Plug one in (8 GB or more, FAT32/exFAT/ext4) - the same kind of stick you use for QIDI firmware updates - and try again." ;;
    1) printf '%s\n' "${cands[0]}" ;;
    *) die 11 "several USB partitions found (${cands[*]}); choose one with --backup-to-usb /dev/sdX1" ;;
  esac
}

mount_usb() {
  # mount_usb DEVICE -> sets USB_MNT, USB_FS, USB_UUID
  local dev="$1"
  USB_FS="$(blkid -s TYPE -o value "$dev" 2>/dev/null || true)"; USB_UUID="$(blkid -s UUID -o value "$dev" 2>/dev/null || true)"
  [[ -n "$USB_FS" && -n "$USB_UUID" ]] || die 11 "$dev has no recognisable filesystem"
  USB_MNT="$(findmnt -no TARGET "$dev" 2>/dev/null | head -1 || true)"
  if [[ -z "$USB_MNT" ]]; then
    USB_MNT="$(P /mnt/eitri-usb)"; mkdir -p "$USB_MNT"
    mount "$dev" "$USB_MNT" || die 11 "cannot mount $dev"
    USB_MOUNTED_BY_US=1
  fi
  touch "$USB_MNT/.eitri-write-test" 2>/dev/null && rm -f "$USB_MNT/.eitri-write-test" || die 11 "$dev is read-only or not writable"
}
umount_usb() { if [[ "${USB_MOUNTED_BY_US:-0}" == 1 ]]; then sync; umount "$USB_MNT" 2>/dev/null || true; USB_MOUNTED_BY_US=0; fi; }

# Stream the whole eMMC as gzip to stdout.  The checksums of the raw and of the compressed stream land in
# $STATE_DIR/raw.sum and gz.sum (this runs inside a pipeline, so variables would be lost).
backup_pipeline() {
  local fifo_raw="$STATE_DIR/raw.fifo" fifo_gz="$STATE_DIR/gz.fifo" total hr hg prog st
  total="$(blockdev --getsize64 "$DISK")"
  mkdir -p "$STATE_DIR"; rm -f "$fifo_raw" "$fifo_gz" "$STATE_DIR/raw.sum" "$STATE_DIR/gz.sum"
  mkfifo "$fifo_raw" "$fifo_gz"
  { sha256sum <"$fifo_raw" | awk '{print $1}' >"$STATE_DIR/raw.sum"; } & hr=$!
  { sha256sum <"$fifo_gz" | awk '{print $1}' >"$STATE_DIR/gz.sum"; } & hg=$!
  (
    while sleep 5; do
      pid="$(pgrep -f "^dd if=$DISK " | head -1 || true)"
      [[ -n "$pid" && -r "/proc/$pid/io" ]] || continue
      progress backup "$(awk -v t="$total" '/^rchar/ {printf "%d", $2 * 100 / t}' "/proc/$pid/io" 2>/dev/null || echo 0)" "reading the eMMC"
    done
  ) >&2 &
  prog=$!
  dd if="$DISK" bs=4M status=none | tee "$fifo_raw" | gzip -1 | tee "$fifo_gz"
  st=("${PIPESTATUS[@]}")
  kill "$prog" 2>/dev/null || true
  wait "$hr" "$hg"
  rm -f "$fifo_raw" "$fifo_gz"
  local c
  for c in "${st[@]}"; do ((c == 0)) || return 1; done
}

read_backup_sums() {
  BK_RAW_SUM="$(cat "$STATE_DIR/raw.sum")"; BK_GZ_SUM="$(cat "$STATE_DIR/gz.sum")"
  rm -f "$STATE_DIR/raw.sum" "$STATE_DIR/gz.sum"
  [[ "$BK_RAW_SUM" =~ ^[0-9a-f]{64}$ && "$BK_GZ_SUM" =~ ^[0-9a-f]{64}$ ]] || die 11 "could not compute the backup checksums"
}

record_backup() {
  # record_backup METHOD DEST RAW GZ BYTES [extra key=value...]
  local method="$1" dest="$2" raw="$3" gz="$4" bytes="$5"; shift 5
  mkdir -p "$STATE_DIR"
  kv_write "$BK_FILE" "verified=1" "method=$method" "dest=$dest" "raw_sha256=$raw" "gz_sha256=$gz" "bytes_gz=$bytes" \
    "label_id=$LABEL_ID" "hostname=$(hostname_now)" "created=$(_now)" "created_epoch=$(date +%s)" "$@"
}

backup_to_usb() {
  local dev; dev="$(find_usb_partition "$BACKUP_TARGET")"
  mount_usb "$dev"
  local need=$((ROOT_USED / 2 + 2 * 1024 * 1024 * 1024))
  local free; free="$(df -B1 --output=avail "$USB_MNT" | tail -1 | tr -d ' ')"
  ((free > need)) || die 11 "$dev has $(human_bytes "$free") free; the compressed image needs about $(human_bytes "$need")"
  local dir="$USB_MNT/EITRI-BACKUP/$(safe_hostname)-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$dir"
  local file="$dir/mmcblk1.img.gz" split=0
  [[ "$USB_FS" == vfat || "$USB_FS" == msdos ]] && split=1
  step_start backup "Backing up the whole eMMC to $dev ($USB_FS) - this takes 5-10 minutes"
  _say "  (This stick may also hold your QIDI QD_Update folder; the backup goes into EITRI-BACKUP/ and leaves it alone.)"
  quiesce
  if ((split)); then
    backup_pipeline | split -b 1900M -d -a 3 - "$file.part"
  else
    backup_pipeline >"$file"
  fi
  read_backup_sums
  unquiesce
  sync
  drop_caches                   # the read-back below must come from the stick, not from RAM
  local files=("$file"*) bytes
  bytes="$(cat "${files[@]}" | wc -c)"
  step_start backup-verify "Reading the backup back to prove it is intact"
  local back_gz back_raw
  back_gz="$(cat "${files[@]}" | sha256sum | awk '{print $1}')"
  back_raw="$(cat "${files[@]}" | gzip -dc | sha256sum | awk '{print $1}')"
  [[ "$back_gz" == "$BK_GZ_SUM" && "$back_raw" == "$BK_RAW_SUM" ]] || { step_fail backup-verify "mismatch"; die 11 "the backup on $dev does not read back correctly (bad stick?) - nothing was changed"; }
  step_ok backup-verify "verified ($(human_bytes "$bytes"))"
  record_backup usb "usb:$USB_UUID:${file#"$USB_MNT"}" "$BK_RAW_SUM" "$BK_GZ_SUM" "$bytes" "usb_uuid=$USB_UUID" "split=$split"
  step_ok backup "backup stored in ${file#"$USB_MNT"/} on the stick"
}

backup_to_host() {
  need_cmd ssh
  local target="$BACKUP_TARGET" host dir name
  host="${target%%:*}"; dir="${target#*:}"
  [[ "$host" != "$target" && -n "$dir" ]] || die 2 "--backup-to-host expects USER@HOST:DIR"
  # HOST is an ssh destination, not an option (-oProxyCommand=... would run a command here), and DIR is quoted for the
  # remote shell: neither may smuggle anything in.
  [[ "$host" =~ ^([A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die 2 "--backup-to-host: '$host' is not a plain USER@HOST"
  [[ "$dir" != *$'\n'* ]] || die 2 "--backup-to-host: DIR must not contain a line break"
  name="eitri-backup-$(safe_hostname)-$(date +%Y%m%d-%H%M%S).img.gz"
  local qdir qname
  qdir="$(sh_quote "$dir")"; qname="$(sh_quote "$name")"
  step_start backup "Backing up the whole eMMC to $host:$dir/$name"
  quiesce
  backup_pipeline | ssh -- "$host" "mkdir -p -- $qdir && cat > $qdir/$qname"
  read_backup_sums
  unquiesce
  step_start backup-verify "Asking $host for the checksum of what it received"
  local remote
  remote="$(ssh -- "$host" "cd -- $qdir && (sha256sum -- $qname 2>/dev/null || shasum -a 256 -- $qname) | cut -d' ' -f1" 2>/dev/null || true)"
  if [[ "$remote" == "$BK_GZ_SUM" ]]; then
    step_ok backup-verify "checksum matches"
  elif [[ -z "$remote" ]]; then
    warn "the PC has neither sha256sum nor shasum. Compute the SHA-256 of the file there and compare it with: $BK_GZ_SUM"
    ask_yes_no "Do the two checksums match?" n || die 11 "backup not confirmed"
  else
    step_fail backup-verify mismatch; die 11 "the copy on $host has a different checksum - the transfer was corrupted. Nothing was changed."
  fi
  record_backup host "$target/$name" "$BK_RAW_SUM" "$BK_GZ_SUM" 0
  step_ok backup "backup stored as $host:$dir/$name"
}

backup_from_file() {
  [[ -r "$BACKUP_FILE" ]] || die 11 "cannot read $BACKUP_FILE"
  case "$(df --output=source "$BACKUP_FILE" 2>/dev/null | tail -1 | tr -d ' ')" in
    "$DISK"*) die 11 "$BACKUP_FILE is stored on the disk that is about to be converted; a backup there is not a backup. Copy it to a USB stick or your PC first." ;;
  esac
  step_start backup-verify "Verifying the existing backup $BACKUP_FILE"
  local gz; gz="$(sha256_of "$BACKUP_FILE")"
  [[ "$gz" == "${BACKUP_FILE_SHA,,}" ]] || die 11 "checksum of $BACKUP_FILE is $gz, not $BACKUP_FILE_SHA"
  gzip -t "$BACKUP_FILE" || die 11 "$BACKUP_FILE is not a valid gzip file"
  # It must be THIS printer: the MBR disk signature (offset 440) has to match the live disk.
  local live img
  live="$(dd if="$DISK" bs=1 skip=440 count=4 status=none | od -An -tx1 | tr -d ' \n')"
  # gzip is cut off by dd after 444 bytes (SIGPIPE) - expected, hence the `|| true`
  img="$({ gzip -dc "$BACKUP_FILE" 2>/dev/null || true; } | dd bs=1 skip=440 count=4 status=none | od -An -tx1 | tr -d ' \n')"
  [[ "$live" == "$img" ]] || die 11 "that image comes from a different eMMC (disk signature $img, this printer has $live)"
  local raw; raw="$(gzip -dc "$BACKUP_FILE" | sha256sum | awk '{print $1}')"
  local mnt uuid=""
  mnt="$(df --output=target "$BACKUP_FILE" | tail -1)"
  uuid="$(blkid -s UUID -o value "$(df --output=source "$BACKUP_FILE" | tail -1)" 2>/dev/null || true)"
  record_backup file "$BACKUP_FILE" "$raw" "$gz" "$(stat -c %s "$BACKUP_FILE")" "usb_uuid=$uuid" "split=0"
  step_ok backup-verify "existing backup verified"
}

# Pull model: the PC runs `ssh printer install.sh --backup-stream > file`
backup_stream_mode() {
  need_cmd dd gzip sha256sum
  EITRI_JSON=0; DRY_RUN=0                       # stdout carries the image; stopping services is the point here
  [[ "${EUID:-$(id -u)}" == 0 || "${EITRI_ALLOW_NONROOT:-0}" == 1 ]] || { echo "run as root" >&2; exit 10; }
  mkdir -p "$STATE_DIR"
  DUMP="$(sfdisk_dump || true)"; LABEL_ID="$(awk -F': *' '/^label-id/ {print $2}' <<<"$DUMP")"
  quiesce
  backup_pipeline
  read_backup_sums
  kv_write "$STATE_DIR/backup-stream.env" "raw_sha256=$BK_RAW_SUM" "gz_sha256=$BK_GZ_SUM" "label_id=$LABEL_ID" "created=$(_now)" "created_epoch=$(date +%s)"
  _say "Stream finished. Verify on your PC that the SHA-256 of the saved file is: $BK_GZ_SUM"
  _say "Then record it with: install.sh --backup-file <that file> --backup-sha256 $BK_GZ_SUM  (run where the file is readable, e.g. from a USB stick)"
}

ensure_backup() {
  step_start backup-check "A verified backup of the eMMC is required before anything is changed"
  if backup_is_valid; then
    step_ok backup-check "using the verified backup recorded $(kv_get "$BK_FILE" created) ($(kv_get "$BK_FILE" method): $(kv_get "$BK_FILE" dest))"
    return 0
  fi
  if ((DRY_RUN)); then
    if [[ -n "$BACKUP_MODE" ]]; then
      step_skip backup-check "dry run: would create the backup now ($BACKUP_MODE)"
    else
      pf_warn "no verified backup yet. A real run needs one of --backup-to-usb, --backup-to-host USER@HOST:DIR or --backup-file FILE --backup-sha256 HASH."
      step_skip backup-check "dry run: no backup recorded"
    fi
    return 0
  fi
  [[ -n "$BACKUP_MODE" ]] || die 11 "no verified backup recorded. Choose one: --backup-to-usb (plug in a USB stick), --backup-to-host USER@HOST:DIR, or --backup-file FILE --backup-sha256 HASH. The installer will not continue without it."
  _say "  Note: the image is a copy of the whole disk, so it holds your Wi-Fi password and QIDI account tokens. Keep it private."
  case "$BACKUP_MODE" in
    usb) backup_to_usb ;;
    host) backup_to_host ;;
    file) backup_from_file ;;
  esac
  backup_is_valid || die 11 "backup verification did not complete"
}

# ===========================================================================================
# 3. stage the payload
# ===========================================================================================
stage_manifest() {
  step_start manifest "Fetching and verifying the Eitri release description"
  local work="$STATE_DIR/bundle" base="" ij
  mkdir -p "$work"
  ij="$work/install.json"
  if [[ -n "$BUNDLE_DIR" ]]; then
    cp -f "$BUNDLE_DIR/install.json" "$ij" && cp -f "$BUNDLE_DIR/install.json.minisig" "$ij.minisig" || die 12 "$BUNDLE_DIR has no install.json(.minisig)"
    base="file://$(cd "$BUNDLE_DIR" && pwd)/"
  else
    base="${BUNDLE_URL:-$DEFAULT_BUNDLE_URL}"; [[ "$base" == */ ]] || base="$base/"
    fetch_to "${base}install.json" "$ij" || die 12 "cannot download ${base}install.json"
    fetch_to "${base}install.json.minisig" "$ij.minisig" || die 12 "cannot download the signature"
  fi
  # Nothing in the bundle is looked at until its signature checks out.
  verify_signature "$ij" "$ij.minisig" >/dev/null || die 12 "install.json is not signed by a trusted Eitri key - refusing to continue (nothing was changed)"
  ok "install.json: signature OK"
  py "$INSTALLER_DIR/tools/prepare_payload.py" plan --install-json "$ij" --flavor "$FLAVOR" --base-url "$base" --out-dir "$work" ||
    die 12 "this release cannot be installed on this printer (see the message above)"
  BUNDLE_VERSION="$(kv_get "$work/install.env" version)"
  PAYLOAD_BYTES="$(kv_get "$work/install.env" payload_bytes)"
  ROOT_MIB="$(kv_get "$work/install.env" root_mib)"; DATA_MIN_MIB="$(kv_get "$work/install.env" data_min_mib)"
  KERNEL_CMDLINE="$(inplace_cmdline "$(kv_get "$work/install.env" kernel_cmdline)" "$LABEL_ID")"
  local known; known="$(kv_get "$work/install.env" stock_soc_known)"
  if [[ -n "$known" && "$STOCK_SOC" != unknown && ",$known," != *",$STOCK_SOC,"* ]]; then
    warn "stock firmware V$STOCK_SOC has not been tested with this release (tested: $known). The checks above still apply."
  fi
  _say "  Eitri $BUNDLE_VERSION ($FLAVOR $(kv_get "$work/install.env" flavor_describe)), $(human_bytes "$PAYLOAD_BYTES") to download"
  STAGE_BASE_URL="$base"
  step_ok manifest "Eitri $BUNDLE_VERSION: signature verified"
}

stage_download() {
  local work="$STATE_DIR/bundle" ij="$STATE_DIR/bundle/install.json"
  step_start stage "Downloading the Eitri bundle"
  if ((DRY_RUN)); then step_ok stage "dry run: nothing downloaded"; return 0; fi
  if [[ -n "$STAGE_USB" ]]; then
    local dev; dev="$(find_usb_partition "$STAGE_USB")"
    mount_usb "$dev"
    STAGE_USB_UUID="$USB_UUID"; STAGE_USB_MNT="$USB_MNT"; STAGE_DIR="$USB_MNT/EITRI-PAYLOAD"
    local free; free="$(df -B1 --output=avail "$USB_MNT" | tail -1 | tr -d ' ')"
    ((free > PAYLOAD_BYTES + 512 * 1024 * 1024)) || die 10 "$dev has only $(human_bytes "$free") free"
  fi
  local avail; avail="${AVAIL_STAGE:-0}"
  if [[ -z "$STAGE_USB" ]] && ((avail < PAYLOAD_BYTES + 1024 * 1024 * 1024)); then
    die 10 "only $(human_bytes "$avail") free on the printer; the payload needs $(human_bytes "$PAYLOAD_BYTES") plus working room. Use --stage-usb with a USB stick."
  fi
  mkdir -p "$STAGE_DIR"
  local name url sha size
  while IFS=$'\t' read -r name url sha size; do
    [[ -n "$name" ]] || continue
    if [[ -f "$STAGE_DIR/$name" ]] && verify_sha256 "$STAGE_DIR/$name" "$sha"; then ok "$name already staged"; continue; fi
    _say "  downloading $name ($(human_bytes "$size"))"
    fetch_to "$url" "$STAGE_DIR/$name.part" || die 12 "download of $name failed"
    if ! verify_sha256 "$STAGE_DIR/$name.part" "$sha"; then
      rm -f "${STAGE_DIR:?}/${name:?}.part"
      die 12 "$name does not match the signed checksum - the download was corrupted or tampered with"
    fi
    mv -f "$STAGE_DIR/$name.part" "$STAGE_DIR/$name"
    progress stage 100 "$name"
  done <"$work/downloads.tsv"
  py "$INSTALLER_DIR/tools/prepare_payload.py" finalize --install-json "$ij" --flavor "$FLAVOR" --dir "$STAGE_DIR" || die 12 "staged payload failed its final check"
  # The MCU firmware is also needed right now, for the recovery proof.
  rm -rf "${STATE_DIR:?}/mcu-fw"; mkdir -p "$STATE_DIR/mcu-fw"
  have_cmd zstd || die 20 "zstd is needed to unpack the MCU firmware (apt install zstd)"
  zstd -dc "$STAGE_DIR/mcu-firmware.tar.zst" | tar -x -C "$STATE_DIR/mcu-fw" --no-same-owner
  step_ok stage "payload staged in $STAGE_DIR and verified"
}

# ===========================================================================================
# 4. MCU boards: can we always get back?
# ===========================================================================================
mcuflash() { "${EITRI_MCUFLASH:-$INSTALLER_DIR/bin/eitri-mcuflash}" "$@"; }

mcu_common_args() {
  MCU_ARGS=(--json --flavor "$FLAVOR" --firmware-root "$STATE_DIR/mcu-fw" --state-dir "$STATE_DIR/mcu" --no-version-lock
            --klipper-dir "$(P /home/mks/klipper)" --klippy-python "$(P /home/mks/klippy-env/bin/python)")
  local b; for b in "${STOCK_MCU_BINS[@]:-}"; do [[ -n "$b" ]] && MCU_ARGS+=(--stock-bin "$b"); done
  ((ACCEPT_NO_MCU_BACKUP)) && MCU_ARGS+=(--accept-no-mcu-backup)
  if ((${#MCU_EXTRA[@]})); then MCU_ARGS+=("${MCU_EXTRA[@]}"); fi
  return 0
}

mcu_recovery() {
  [[ "$MCU_MODE" == skip ]] && { step_skip mcu "MCU handling skipped by --mcu skip"; return 0; }
  step_start mcu "Checking that both MCU boards can be recovered"
  mcu_common_args
  if ((DRY_RUN)); then
    if ! ((PROBE_MCU)); then
      step_skip mcu "dry run: Klipper is running and holds the MCU ports; use --probe-mcu to stop it briefly and probe"
      return 0
    fi
    # Read-only: identify what answers on each UART.  The full plan needs the firmware, which a dry run does not download.
    local out rc=0
    out="$(mcuflash probe "${MCU_ARGS[@]}" --stop-services 2>/dev/null)" || rc=$?
    printf '%s\n' "$out" >"$STATE_DIR/mcu-probe.jsonl"
    if ((rc == 0)); then step_ok mcu "MCU ports probed (details: $STATE_DIR/mcu-probe.jsonl)"
    else warn "could not probe the MCUs (see $STATE_DIR/mcu-probe.jsonl); run 'eitri-mcuflash probe' for details"; step_fail mcu "probe failed"; fi
    return 0
  fi
  # Real: reset each board into its bootloader, read its firmware back and write it back (round trip), keep the copy.
  local rc=0
  mcuflash backup --apply --yes --origin stock "${MCU_ARGS[@]}" --stop-services >"$STATE_DIR/mcu-backup.jsonl" 2>"$STATE_DIR/mcu-backup.err" || rc=$?
  if ((rc != 0)); then
    err "MCU recovery check failed: $(tail -1 "$STATE_DIR/mcu-backup.jsonl")"
    step_fail mcu "recovery not proven"
    die 13 "At least one MCU board could not be proven recoverable, so nothing was changed. Details: $STATE_DIR/mcu-backup.jsonl (and run 'eitri-mcuflash plan' for the preflight table)."
  fi
  step_ok mcu "both boards have a saved way back"
}

# The recovery proof is the one device write that happens BEFORE the CONVERT prompt, so it gets its own, named consent.
confirm_mcu_roundtrip() {
  [[ "$MCU_MODE" == skip ]] && return 0
  ((ACCEPT_MCU_ROUNDTRIP)) && return 0
  _say ""
  _say "${_BLD}Before anything is converted, the installer proves it can put the MCU boards back.${_OFF}"
  _say "  It resets the toolhead board into its bootloader, reads the application back to a file, ERASES the application"
  _say "  area and writes the same image back (a few seconds), then waits for the toolhead to answer again."
  _say "  If power is lost in those seconds the toolhead has no application until it is re-flashed through Katapult from"
  _say "  the saved image (docs/recovery.md); the saved copy is made first and is synced to disk before the erase."
  _say "  The mainboard cannot be read back; it is not written."
  local a=""
  if [[ -t 0 ]]; then
    printf 'Allow this write to the toolhead board? [y/N] ' >&2; read -r a || true
  elif { true </dev/tty; } 2>/dev/null; then
    printf 'Allow this write to the toolhead board? [y/N] ' >&2; read -r a </dev/tty || true
  fi
  [[ "${a,,}" == y* ]] || die 3 "the MCU recovery proof writes to the toolhead board and needs your consent: pass --accept-mcu-roundtrip (or --mcu skip to leave the boards alone). Nothing was changed."
}

# The recorded backup is only a promise until it is looked at again: the stick may have been pulled, the file deleted or
# moved during the (long) download.  Checked right before the point of no return, and again by stage 2 before it shrinks.
revalidate_backup() {
  step_start backup-recheck "Making sure the backup is still there"
  local method dest gz bytes uuid path dev mnt here=0 f total=0 files=() remote host rest
  method="$(kv_get "$BK_FILE" method)"; dest="$(kv_get "$BK_FILE" dest)"; gz="$(kv_get "$BK_FILE" gz_sha256)"; bytes="$(kv_get "$BK_FILE" bytes_gz)"
  case "$method" in
    usb)
      uuid="$(kv_get "$BK_FILE" usb_uuid)"; path="${dest#usb:*:}"
      dev="$(blkid -U "$uuid" 2>/dev/null || true)"
      [[ -n "$dev" ]] || die 11 "the USB stick that holds the backup (filesystem UUID $uuid) is not plugged in any more. Plug it back in and run the installer again - nothing was changed."
      mnt="$(findmnt -no TARGET "$dev" 2>/dev/null | head -1 || true)"
      if [[ -z "$mnt" ]]; then
        mnt="$(P /mnt/eitri-usb-check)"; mkdir -p "$mnt"
        mount -o ro "$dev" "$mnt" || die 11 "cannot mount $dev to re-check the backup - nothing was changed"
        here=1
      fi
      if [[ -f "$mnt$path" ]]; then files=("$mnt$path"); else files=("$mnt$path".part*); fi
      for f in "${files[@]}"; do
        if [[ ! -f "$f" ]]; then ((here)) && umount "$mnt" 2>/dev/null; die 11 "the backup file ${path} is no longer on the USB stick - nothing was changed. Run the installer again to make a new backup."; fi
        total=$((total + $(wc -c <"$f")))
      done
      ((here)) && umount "$mnt" 2>/dev/null
      [[ -z "$bytes" || "$total" == "$bytes" ]] || die 11 "the backup on the USB stick is $total bytes but $bytes were recorded - nothing was changed. Run the installer again to make a new backup."
      ;;
    host)
      need_cmd ssh
      host="${dest%%:*}"; rest="${dest#*:}"
      remote="$(ssh -- "$host" "sha256sum -- $(sh_quote "$rest") 2>/dev/null || shasum -a 256 -- $(sh_quote "$rest")" 2>/dev/null | cut -d' ' -f1 || true)"
      [[ "$remote" == "$gz" ]] || die 11 "the backup copy on $host cannot be found or no longer matches its checksum - nothing was changed. Run the installer again to make a new backup."
      ;;
    file)
      [[ -r "$dest" ]] || die 11 "the backup file $dest is gone - nothing was changed. Run the installer again."
      [[ "$(sha256_of "$dest")" == "$gz" ]] || die 11 "the backup file $dest has changed since it was verified - nothing was changed."
      ;;
    *) die 11 "the backup record has an unknown method '$method' - nothing was changed" ;;
  esac
  step_ok backup-recheck "backup is still in place"
}

# ===========================================================================================
# 5/6. arm stage 2
# ===========================================================================================
write_stage2_conf() {
  local payload_rel="${STAGE_DIR#"$R"}"
  local loc="stock:$payload_rel"
  if [[ -n "$STAGE_USB" ]]; then loc="usb:$STAGE_USB_UUID:${STAGE_DIR#"$STAGE_USB_MNT"}"; fi
  kv_write "$STATE_DIR/stage2.conf" \
    "operation=convert" "disk=$DISK" "part_prefix=$PART_PREFIX" "boot_part=1" "stock_part=2" \
    "stock_partuuid=${LABEL_ID#0x}-02" "stock_fs_uuid=$STOCK_FS_UUID" "stock_soc=$STOCK_SOC" \
    "payload_loc=$loc" "flavor=$FLAVOR" "mcu_mode=$MCU_MODE" "backup_sha256=$(kv_get "$BK_FILE" gz_sha256)" \
    "kernel_cmdline=$KERNEL_CMDLINE" \
    "backup_method=$(kv_get "$BK_FILE" method)" "backup_usb_uuid=$(kv_get "$BK_FILE" usb_uuid)" \
    "backup_path=$([[ "$(kv_get "$BK_FILE" method)" == usb ]] && kv_get "$BK_FILE" dest | sed 's/^usb:[^:]*://')" \
    "backup_bytes=$(kv_get "$BK_FILE" bytes_gz)" \
    "s2_kernel=$STOCK_KERNEL" "s2_fdt=$STOCK_FDT" "s2_initrd=/eitri/stage2/initramfs.cpio.gz" "s2_args=$S2_ARGS"
}

arm_stage2() {
  step_start arm "Installing the boot manager and the one-shot installer"
  read_stock_boot
  local boot; boot="$(P /boot)"
  # Kernel arguments of the RAM installer: no serial console (ttyS2 is an MCU port), no root device.
  S2_ARGS="console=tty1 rdinit=/init eitri.op=convert panic=60 loglevel=4 net.ifnames=0"
  local ptuuid; ptuuid="$(printf '%08x' "$((LABEL_ID))")"

  if ((DRY_RUN)); then
    _say "    would: build the RAM installer (stock kernel $STOCK_KERNEL + $FLAVOR payload) into /boot/eitri/stage2/"
    _say "    would: install boot.scr (from boot/boot.cmd), write /boot/eitri/{layout,boot}.env"
    [[ -e "$boot/boot.scr" ]] && _say "    would: keep the existing /boot/boot.scr as /boot/eitri/boot.scr.stock (revert.sh restores it)"
    _say "    would: rename /boot/extlinux/extlinux.conf to extlinux.conf.stock (the stock entry stays reachable)"
    _say "    would: mask armbian-resize-filesystem.service (it would grow the stock root over the new partitions)"
    _say "    would: reboot into the one-shot installer; the stock kernel $STOCK_KERNEL boots it"
    step_ok arm "dry run: nothing installed"
    return 0
  fi

  mkdir -p "$boot/eitri/stage2"
  cp -f "$boot/extlinux/extlinux.conf" "$boot/eitri/extlinux.conf.orig"
  write_stage2_conf
  build_stage2_initramfs "$boot/eitri/stage2/initramfs.cpio.gz.new" "$STAGE_DIR/stage2-initramfs.cpio.gz" 1
  mv -f "$boot/eitri/stage2/initramfs.cpio.gz.new" "$boot/eitri/stage2/initramfs.cpio.gz"

  py "$INSTALLER_DIR/boot/mkscr.py" "$INSTALLER_DIR/boot/boot.cmd" "$STATE_DIR/boot.scr"
  py "$INSTALLER_DIR/boot/mkscr.py" --check "$STATE_DIR/boot.scr" >/dev/null || die 10 "internal error: generated boot.scr is invalid"
  cp -f "$STATE_DIR/boot.scr" "$boot/boot.scr.new"
  cmp -s "$STATE_DIR/boot.scr" "$boot/boot.scr.new" || die 10 "boot.scr did not copy cleanly"
  # A boot.scr that QIDI shipped must survive: keep it, revert.sh puts it back.
  if [[ -e "$boot/boot.scr" ]] && ! grep -aq eitri_mode "$boot/boot.scr"; then
    cp -p "$boot/boot.scr" "$boot/eitri/boot.scr.stock" || die 10 "cannot keep a copy of the existing boot.scr"
  fi

  kv_write "$boot/eitri/layout.env" \
    "eitri_part_A=5" "eitri_part_B=6" "eitri_partuuid_A=$ptuuid-05" "eitri_partuuid_B=$ptuuid-06" \
    "eitri_cmdline=$KERNEL_CMDLINE" \
    "eitri_s2_kernel=$STOCK_KERNEL" "eitri_s2_fdt=$STOCK_FDT" "eitri_s2_initrd=/eitri/stage2/initramfs.cpio.gz" "eitri_s2_args=$S2_ARGS"
  # eitri_s2_tries: how many times U-Boot may start the RAM installer. Two, so that a power cut during the conversion
  # re-enters it (stage 2 resumes from its phase markers) instead of leaving a half-converted disk to boot stock.
  kv_write "$boot/eitri/boot.env" "eitri_mode=stage2" "eitri_slot=A" "eitri_tries=3" "eitri_state_A=unknown" "eitri_state_B=unknown" "eitri_s2_tries=2"
  kv_write "$boot/eitri/stock-changes.env" "extlinux_renamed=1" "boot_scr_installed=1" "installed_by=install.sh" "when=$(_now)"

  if systemctl mask armbian-resize-filesystem.service >/dev/null 2>&1; then
    kv_set "$boot/eitri/stock-changes.env" masked_armbian_resize_live 1
  fi
  sync
  # --- the switch: from here on U-Boot runs boot.scr instead of the stock extlinux entry ---
  mv -f "$boot/boot.scr.new" "$boot/boot.scr"
  mv -f "$boot/extlinux/extlinux.conf" "$boot/extlinux/extlinux.conf.stock"
  sync
  step_ok arm "boot manager installed; stage 2 is armed"
}

reboot_into_stage2() {
  ((DRY_RUN)) && return 0
  ok "Rebooting into the Eitri installer in 10 seconds. It restarts again by itself when done."
  _say "   Progress page:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):8099/   (your SSH session ends now)"
  emit result ok=true "msg=stage 1 complete, rebooting into stage 2"
  sleep "${EITRI_REBOOT_DELAY:-10}"
  sync
  systemctl reboot || reboot
}

# ===========================================================================================
cleanup() { unquiesce 2>/dev/null || true; umount_usb 2>/dev/null || true; }

choose_flavor() {
  [[ -n "$FLAVOR" ]] && return 0
  if ((DRY_RUN)); then FLAVOR=klipper; info "no --flavor given; checking with klipper (the default)"; return 0; fi
  if [[ -t 0 ]]; then
    _say ""
    _say "Which firmware flavor do you want?"
    _say "  1) klipper  - mainline Klipper, stable (recommended)"
    _say "  2) kalico   - the Kalico fork with extra features; fewer people test it"
    _say "  (Switching later means re-flashing both MCUs.)"
    local a; read -r -p "Choice [1]: " a
    case "${a:-1}" in 1 | klipper) FLAVOR=klipper ;; 2 | kalico) FLAVOR=kalico ;; *) die 2 "no such choice" ;; esac
  else
    die 2 "--flavor klipper|kalico is required for a real run"
  fi
}

main() {
  parse_args "$@"
  if ((BACKUP_STREAM)); then backup_stream_mode; return; fi
  if ((STATUS_ONLY)); then show_status; return; fi
  trap cleanup EXIT
  trap 'err "unexpected failure at install.sh line $LINENO; nothing irreversible has been done unless stage 2 was armed (see --status)"' ERR
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  if ((DRY_RUN)); then _say "${_BLD}DRY RUN${_OFF} - nothing will be changed. (Real run: add --i-have-a-backup --yes)"; else _say "${_BLD}REAL RUN${_OFF}"; fi
  emit plan "msg=Eitri in-place install" "data={\"dry_run\":$([[ $DRY_RUN == 1 ]] && echo true || echo false)}"

  choose_flavor
  preflight
  stage_manifest          # small, signed: gives the layout numbers
  plan_layout
  ensure_backup           # nothing big is downloaded and nothing is armed before a verified backup exists
  if ((! DRY_RUN)); then confirm_mcu_roundtrip; fi     # consent for the one device write that precedes the CONVERT prompt
  stage_download
  mcu_recovery

  if ((DRY_RUN)); then
    arm_stage2
    _say ""
    ok "Dry run finished: nothing was changed."
    emit result ok=true "msg=dry run complete"
    return 0
  fi
  if ((STAGE_ONLY)); then ok "Payload staged and MCU recovery proven. Not arming (--stage-only)."; emit result ok=true "msg=staged"; return 0; fi

  revalidate_backup
  confirm_typed "$CONFIRM_PHRASE" "POINT OF NO RETURN.
  Next the printer reboots into the Eitri installer. It will shrink the stock system, create new partitions
  and write Eitri. Until it finishes, the stock system still boots; afterwards the original is kept in its
  own partition and revert.sh switches back. Do NOT switch the printer off while the installer runs
  (about 15-25 minutes). Your backup: $(kv_get "$BK_FILE" dest)"
  arm_stage2
  reboot_into_stage2
}

if [[ "${EITRI_INSTALL_NO_MAIN:-0}" != 1 ]]; then main "$@"; fi
