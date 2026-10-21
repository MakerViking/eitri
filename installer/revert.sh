#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Eitri revert: go back to the original QIDI system.
#
#   sudo ./revert.sh                  dry run of --hard: shows what would happen, changes nothing
#   sudo ./revert.sh --hard --yes     back to stock (the default mode)
#
# Modes (exactly one):
#   --soft        boot the stock system on the next start, keep Eitri installed (undo with --to-eitri)
#   --hard        restore QIDI's own boot setup and put the stock firmware back on the MCUs.        [default]
#                 Eitri's partitions stay on the disk, unused, until you run --reclaim.
#   --to-eitri    boot Eitri again after --soft / --hard
#   --reclaim     (run on the stock system) delete the Eitri partitions and give the space back to stock
#   --full        restore the COMPLETE eMMC from the backup image taken by install.sh (stage 2, in RAM).
#                 Last resort: it overwrites everything, including anything stored in Eitri's /data.
#
# Options
#   --yes                      required for real changes (without it every mode is a dry run)
#   --confirm PHRASE           supply the typed confirmation (REVERT, RESTORE or "DELETE EITRI")
#   --skip-mcu                 do not touch the MCU boards (they must be flashed back by hand, or the stock Klipper will refuse to start)
#   --no-reboot                make the change but do not reboot
#   --from-usb [DEVICE]        --full: read the image from the USB stick it was saved to (default: where backup.env says)
#   --from-host HOST:PORT      --full: stream the image from your PC:   cat mmcblk1.img.gz | nc -l -p PORT
#   --image PATH               --full: path of the image on the stick (default: the recorded one)
#   --raw-sha256 HASH          --full: SHA-256 of the UNCOMPRESSED image (zcat file | sha256sum); needed with --from-host when
#                              no backup record survives, so the image is checked before the partition table is replaced
#   --json / --events-file F   machine-readable progress
#
# Exit codes: 0 ok | 2 usage | 3 not confirmed | 10 precondition failed | 13 MCU restore failed | 20 missing tools

set -Eeuo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/eitri-common.sh
source "$INSTALLER_DIR/lib/eitri-common.sh"
# shellcheck source=lib/stage2-build.sh
source "$INSTALLER_DIR/lib/stage2-build.sh"

EITRI_COMPONENT=revert
R="${EITRI_ROOT:-}"
DISK="${EITRI_DISK:-/dev/mmcblk1}"
PART_PREFIX="p"
BOOT="$(P /boot)"
# On Eitri the FAT boot partition is mounted at /boot/firmware (the OS image's mount generator); on stock at /boot.
[[ -e "$(P /boot/firmware)/eitri/boot.env" ]] && BOOT="$(P /boot/firmware)"
DATA="${EITRI_DATA:-$(P /data)}"
STATE_DIR="${EITRI_STATE_DIR:-$(P /var/lib/eitri-install)}"
MODE=""
ASSUME_YES=0; FORCE_DRY=0; SKIP_MCU=0; NO_REBOOT=0
FROM_USB=""; FROM_HOST=""; IMAGE_PATH=""; RAW_SHA=""

usage() { awk 'NR >= 3 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; }

set_mode() { [[ -z "$MODE" ]] || { echo "choose only one of --soft/--hard/--to-eitri/--reclaim/--full" >&2; exit 2; }; MODE="$1"; }

parse_args() {
  while (($#)); do
    case "$1" in
      -h | --help) usage; exit 0 ;;
      --soft) set_mode soft ;;
      --hard) set_mode hard ;;
      --to-eitri) set_mode to-eitri ;;
      --reclaim) set_mode reclaim ;;
      --full) set_mode full ;;
      --yes | -y) ASSUME_YES=1 ;;
      --dry-run) FORCE_DRY=1 ;;
      --confirm) EITRI_CONFIRM="${2:?}"; shift ;;
      --skip-mcu) SKIP_MCU=1 ;;
      --no-reboot) NO_REBOOT=1 ;;
      --from-usb) FROM_USB=auto; if [[ "${2:-}" == /dev/* ]]; then FROM_USB="$2"; shift; fi ;;
      --from-host) FROM_HOST="${2:?}"; shift ;;
      --image) IMAGE_PATH="${2:?}"; shift ;;
      --raw-sha256) RAW_SHA="${2:?}"; shift ;;
      --json) EITRI_JSON=1 ;;
      --events-file) EITRI_EVENTS_FILE="${2:?}"; shift ;;
      *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
  done
  [[ -n "$MODE" ]] || MODE=hard
  if ((ASSUME_YES && !FORCE_DRY)); then DRY_RUN=0; else DRY_RUN=1; fi
}

disk_dump() { sfdisk -d "$DISK" 2>/dev/null; }
part_dev() { printf '%s%s%s' "$DISK" "$PART_PREFIX" "$1"; }
on_eitri() { grep -q 'eitri\.slot=' "$(P /proc/cmdline)" 2>/dev/null; }
# Is /boot/boot.scr ours (the Eitri boot manager) and not a boot script QIDI shipped?
eitri_scr() { [[ -r "$BOOT/boot.scr" ]] && grep -aq eitri_mode "$BOOT/boot.scr"; }
boot_rw() {
  # make /boot writable for this script; undone by the EXIT trap
  if [[ ! -w "$BOOT/eitri" ]] && mountpoint -q "$BOOT" 2>/dev/null; then
    mutate "remount /boot read-write" mount -o remount,rw "$BOOT" && BOOT_REMOUNTED=1
  fi
}
cleanup() {
  if [[ "${BOOT_REMOUNTED:-0}" == 1 ]]; then sync; mount -o remount,ro "$BOOT" 2>/dev/null || true; fi
  if [[ "${USB_MOUNTED_BY_US:-0}" == 1 ]]; then umount "$USB_MNT" 2>/dev/null || true; fi
}

# ---------------------------------------------------------------------------------------------
# What do we have, and is the stock system still intact?
# ---------------------------------------------------------------------------------------------
STOCK_UUID=""; FAILS=0
chk_ok()   { _say "    ${_GRN}ok${_OFF}    $*"; emit log level=info "msg=ok: $*"; }
chk_warn() { _say "    ${_YEL}warn${_OFF}  $*"; emit log level=warn "msg=$*"; }
chk_fail() { _say "    ${_RED}FAIL${_OFF}  $*"; emit log level=error "msg=FAILED: $*"; FAILS=$((FAILS + 1)); }

check_installation() {
  step_start check "Looking at the current boot setup"
  if [[ ! -r "$BOOT/eitri/boot.env" ]]; then
    die 10 "$BOOT/eitri/boot.env not found: this printer is not (or no longer) set up for Eitri."
  fi
  chk_ok "Eitri boot selection: $(kv_get "$BOOT/eitri/boot.env" eitri_mode)"
  if [[ -r "$BOOT/boot.scr" ]]; then chk_ok "boot manager (boot.scr) installed"
  elif [[ "$MODE" != to-eitri ]]; then chk_warn "no boot.scr: the stock boot entry seems to be active already"; fi
  if [[ -r "$BOOT/extlinux/extlinux.conf.stock" || -r "$BOOT/extlinux/extlinux.conf" ]]; then chk_ok "stock boot entry present"
  else chk_fail "no stock boot entry (extlinux.conf[.stock]) found on /boot"; fi

  local orig="$BOOT/eitri/extlinux.conf.orig"
  [[ -r "$orig" ]] || orig="$BOOT/extlinux/extlinux.conf.stock"
  STOCK_UUID="$(sed -n 's/.*root=UUID=\([^ ]*\).*/\1/p' "$orig" 2>/dev/null | head -1)"
  local dev; dev="$(part_dev 2)"
  if [[ -n "$STOCK_UUID" && "$(blkid -s UUID -o value "$dev" 2>/dev/null || echo "$STOCK_UUID")" == "$STOCK_UUID" ]]; then
    chk_ok "the stock system is still in $dev (UUID ${STOCK_UUID:0:8}...)"
  else
    chk_fail "$dev no longer holds the stock system (expected UUID ${STOCK_UUID:-unknown}); use --full to restore from the backup image"
  fi
  if ((FAILS)); then step_fail check "$FAILS problem(s)"; return 1; fi
  step_ok check "stock system intact"
}

# ---------------------------------------------------------------------------------------------
# MCUs back to their original firmware (needs the Eitri side: that is where the backups live)
# ---------------------------------------------------------------------------------------------
mcuflash() { "${EITRI_MCUFLASH:-$INSTALLER_DIR/bin/eitri-mcuflash}" "$@"; }

# A one-line reason out of eitri-mcuflash's JSON event stream (its last "msg").
mcu_reason() { sed -n 's/.*"msg": *"\([^"]*\)".*/\1/p' <<<"$1" | tail -1; }

restore_mcus() {
  ((SKIP_MCU)) && { step_skip mcu "--skip-mcu: flash the MCUs back by hand before starting the stock system"; return 0; }
  if ! on_eitri; then step_skip mcu "running on the stock system: the MCU firmware is whatever is flashed now"; return 0; fi
  step_start mcu "Putting the original firmware back on the MCU boards"
  # The mainboard goes first: it is the board that can fail for lack of tooling (it is flashed through its SD-card
  # bootloader), so a failure there leaves the toolhead untouched.
  local board rc out reason problems=() restored=()
  local boards=(mainboard toolhead)
  local args=(--json --stop-services --state-dir "$DATA/eitri/mcu" --firmware-root "$DATA/eitri/mcu/firmware" --no-version-lock)
  # Pass 1 changes nothing: can EVERY board really be put back (image present and intact, flashing method usable)?
  # Flashing one board and then discovering the other cannot follow would leave the two MCUs on different firmware.
  for board in "${boards[@]}"; do
    rc=0
    out="$(mcuflash rollback "$board" --original "${args[@]}" 2>&1)" || rc=$?
    if ((rc != 0)); then
      reason="$(mcu_reason "$out")"
      problems+=("$board: ${reason:-$(printf '%s\n' "$out" | tail -1)}")
    elif ((DRY_RUN)); then
      _say "    would: restore the $board firmware"
    fi
  done
  if ((${#problems[@]})); then
    local p; for p in "${problems[@]}"; do ((DRY_RUN)) && chk_warn "would fail: $p" || err "$p"; done
    if ((DRY_RUN)); then return 0; fi
    step_fail mcu "${problems[*]}"
    die 13 "The original firmware cannot be put back automatically (${problems[*]}). NOTHING was changed: both boards still run Eitri's firmware and the boot selection is unchanged. Provide what is missing, or flash the boards by hand (docs/recovery.md) and re-run with --skip-mcu."
  fi
  ((DRY_RUN)) && return 0
  # Pass 2: do it.
  for board in "${boards[@]}"; do
    rc=0
    mcuflash rollback "$board" --original --apply --yes "${args[@]}" >"$STATE_DIR/mcu-rollback-$board.jsonl" 2>&1 || rc=$?
    if ((rc != 0)); then
      step_fail mcu "$board"
      die 13 "Could not restore the $board firmware ($(tail -1 "$STATE_DIR/mcu-rollback-$board.jsonl")). Restored so far: ${restored[*]:-none}. The boot selection was NOT changed (it still says Eitri), so run revert.sh --hard again once that is fixed, or flash the boards by hand (docs/recovery.md) and re-run with --skip-mcu. Do not boot the original system until both boards are back to the original firmware."
    fi
    restored+=("$board")
    ok "$board firmware restored"
  done
  step_ok mcu "MCU boards restored"
}

# ---------------------------------------------------------------------------------------------
do_soft() {
  step_start switch "Selecting the stock system for the next boot"
  boot_rw
  mutate "set eitri_mode=stock in $BOOT/eitri/boot.env" kv_set "$BOOT/eitri/boot.env" eitri_mode stock
  step_ok switch "next boot: stock system (Eitri stays installed; undo with --to-eitri)"
}

do_hard() {
  restore_mcus
  step_start switch "Restoring the original QIDI boot setup"
  boot_rw
  mutate "set eitri_mode=stock" kv_set "$BOOT/eitri/boot.env" eitri_mode stock
  if [[ -r "$BOOT/extlinux/extlinux.conf.stock" ]]; then
    grep -q '^[[:space:]]*kernel ' "$BOOT/extlinux/extlinux.conf.stock" || die 10 "extlinux.conf.stock looks damaged; refusing to continue"
    mutate "copy extlinux.conf.stock back to extlinux.conf" cp -f "$BOOT/extlinux/extlinux.conf.stock" "$BOOT/extlinux/extlinux.conf"
    mutate "sync" sync
    if eitri_scr; then
      mutate "retire boot.scr (kept as boot.scr.eitri)" mv -f "$BOOT/boot.scr" "$BOOT/boot.scr.eitri"
      # a boot script QIDI shipped is put back exactly as it was
      if [[ -r "$BOOT/eitri/boot.scr.stock" ]]; then mutate "put QIDI's own boot.scr back" cp -p "$BOOT/eitri/boot.scr.stock" "$BOOT/boot.scr"; fi
    fi
    mutate "remove the now redundant extlinux.conf.stock" mv -f "$BOOT/extlinux/extlinux.conf.stock" "$BOOT/extlinux/extlinux.conf.eitri-saved"
  fi
  mutate "sync" sync
  step_ok switch "stock boot setup restored. The Armbian disk-resize service stays masked while Eitri's partitions exist."
}

do_to_eitri() {
  step_start switch "Switching back to Eitri"
  local dev; dev="$(part_dev 5)"
  [[ "$(blkid -s TYPE -o value "$dev" 2>/dev/null || echo ext4)" == ext4 ]] || die 10 "$dev does not hold the Eitri system (was it reclaimed?)"
  boot_rw
  if ! eitri_scr; then
    [[ -r "$BOOT/boot.scr.eitri" ]] && mutate "restore boot.scr" cp -f "$BOOT/boot.scr.eitri" "$BOOT/boot.scr" ||
      { py "$INSTALLER_DIR/boot/mkscr.py" "$INSTALLER_DIR/boot/boot.cmd" "$STATE_DIR/boot.scr" && mutate "install boot.scr" cp -f "$STATE_DIR/boot.scr" "$BOOT/boot.scr"; }
  fi
  if [[ -r "$BOOT/extlinux/extlinux.conf" && ! -e "$BOOT/extlinux/extlinux.conf.stock" ]]; then
    mutate "keep the stock entry as extlinux.conf.stock" cp -f "$BOOT/extlinux/extlinux.conf" "$BOOT/extlinux/extlinux.conf.stock"
    mutate "sync" sync
    mutate "hand over to the boot manager" mv -f "$BOOT/extlinux/extlinux.conf" "$BOOT/extlinux/extlinux.conf.eitri-active"
  fi
  mutate "select Eitri slot A with fresh boot attempts" kv_write "$BOOT/eitri/boot.env" eitri_mode=eitri eitri_slot=A eitri_tries=3 \
    "eitri_state_A=$(kv_get "$BOOT/eitri/boot.env" eitri_state_A || echo unknown)" "eitri_state_B=$(kv_get "$BOOT/eitri/boot.env" eitri_state_B || echo unknown)"
  step_ok switch "next boot: Eitri"
}

do_reclaim() {
  on_eitri && die 10 "run --reclaim from the stock system (revert first with --hard and reboot)"
  ! eitri_scr || die 10 "the Eitri boot manager is still active; run --hard first"
  step_start reclaim "Deleting the Eitri partitions and growing the stock system"
  local n
  for n in 5 6 7; do
    if findmnt -no TARGET "$(part_dev "$n")" >/dev/null 2>&1; then die 10 "$(part_dev "$n") is mounted; unmount it first"; fi
  done
  need_cmd sfdisk partx resize2fs
  if ((! DRY_RUN)); then
    confirm_typed "DELETE EITRI" "This permanently deletes Eitri, including everything in its /data partition (printer configuration made under Eitri, gcode files, timelapses)."
  fi
  mutate "delete partitions 7 6 5 3" sfdisk --delete "$DISK" 7 6 5 3
  mutate "update the kernel's view of the deleted partitions" partx -d --nr 3:7 "$DISK"
  mutate "grow partition 2 to the end of the disk" bash -c "echo ', +' | sfdisk -N 2 --no-reread '$DISK'"
  mutate "tell the kernel the new size of partition 2" partx -u --nr 2 "$DISK"
  mutate "grow the stock filesystem (online)" resize2fs "$(part_dev 2)"
  mutate "give the disk-resize service back to the stock system" systemctl unmask armbian-resize-filesystem.service
  # the staged payload (a few GB) lives on the stock root; the backup record and the MCU backups stay
  mutate "delete the staged Eitri payload from the stock system" bash -c "rm -rf '${STATE_DIR:?}/payload' '${STATE_DIR:?}/bundle' '${STATE_DIR:?}/mcu-fw'"
  mutate "remove /boot/eitri" bash -c "rm -rf '${BOOT:?}/eitri' '${BOOT:?}/boot.scr.eitri' '${BOOT:?}/extlinux/extlinux.conf.eitri-saved' '${BOOT:?}/extlinux/extlinux.conf.eitri-active'"
  step_ok reclaim "done: the printer is a plain stock system again with the whole eMMC"
}

# ---------------------------------------------------------------------------------------------
# --full: the stage-2 restore
# ---------------------------------------------------------------------------------------------
find_record() {
  local f
  for f in "$DATA/eitri/state/backup.env" "$STATE_DIR/backup.env"; do [[ -r "$f" ]] && { printf '%s' "$f"; return 0; }; done
  return 1
}

do_full() {
  step_start restore "Preparing a full restore from the backup image"
  local rec src gz raw
  rec="$(find_record || true)"
  if [[ -n "$FROM_HOST" ]]; then
    src="tcp:$FROM_HOST"
  else
    local dest uuid path; dest=""
    if [[ -n "$rec" ]]; then dest="$(kv_get "$rec" dest)"
    elif [[ -n "$FROM_USB" && "$FROM_USB" != auto && -n "$IMAGE_PATH" ]]; then dest="usb:-:$IMAGE_PATH"
    else die 10 "no backup record found; give --from-usb /dev/sdX1 --image /path/on/the/stick, or use --from-host HOST:PORT"; fi
    if [[ "$dest" == usb:* ]]; then
      uuid="${dest#usb:}"; path="${uuid#*:}"; uuid="${uuid%%:*}"
    else
      uuid="$(kv_get "$rec" usb_uuid)"; path="$dest"
    fi
    [[ -n "$IMAGE_PATH" ]] && path="$IMAGE_PATH"
    # --from-usb /dev/sdX1: the stick the image is on right now, whatever the record says
    if [[ -n "$FROM_USB" && "$FROM_USB" != auto ]]; then
      uuid="$(blkid -s UUID -o value "$FROM_USB" 2>/dev/null || true)"
      [[ -n "$uuid" ]] || die 10 "cannot read the filesystem UUID of $FROM_USB"
      if [[ "$dest" != usb:* && -z "$IMAGE_PATH" ]]; then die 10 "$FROM_USB given, but the image's path on it is not known: add --image /path/on/the/stick/mmcblk1.img.gz"; fi
    fi
    [[ -n "$uuid" ]] || die 10 "the backup was not saved to a USB stick (see $rec); use --from-host HOST:PORT or copy it to a stick"
    if ((! DRY_RUN)) && [[ -z "$(blkid -U "$uuid" 2>/dev/null || true)" ]]; then die 10 "plug in the USB stick with the backup (filesystem UUID $uuid)"; fi
    src="usb:$uuid:$path"
  fi
  gz="$( [[ -n "$rec" ]] && kv_get "$rec" gz_sha256 || true)"; raw="$( [[ -n "$rec" ]] && kv_get "$rec" raw_sha256 || true)"
  if [[ -n "$RAW_SHA" ]]; then
    [[ "${RAW_SHA,,}" =~ ^[0-9a-f]{64}$ ]] || die 2 "--raw-sha256 expects 64 hexadecimal digits"
    raw="${RAW_SHA,,}"
  fi
  # A stream cannot be read twice, so the only protection against a damaged one is a known checksum that stage 2 verifies
  # BEFORE it replaces the partition table.  Without one, do not start.
  if [[ -n "$FROM_HOST" && -z "$raw" ]]; then
    die 10 "--from-host needs the SHA-256 of the uncompressed image: no backup record was found, so pass --raw-sha256 HASH (on your PC: zcat mmcblk1.img.gz | sha256sum)"
  fi
  [[ -r "$BOOT/eitri/stage2/initramfs.cpio.gz" ]] || die 10 "$BOOT/eitri/stage2/initramfs.cpio.gz is missing; re-run install.sh's staging or restore by maskrom (docs/recovery.md)"
  read_stock_boot
  _say "    source: ${src%%:*}   expected checksum: ${gz:-(unknown; stream is verified after writing)}"
  if ((DRY_RUN)); then
    _say "    would: reboot into the RAM installer with eitri.op=restore; it checks the image, writes it back (skipping the bootloader area) and reboots into stock"
    step_ok restore "dry run"
    return 0
  fi
  eitri_scr || die 10 "the Eitri boot manager is not installed, so the restore cannot be started from here; use --to-eitri first or restore via maskrom (docs/recovery.md)"
  confirm_typed RESTORE "This overwrites the WHOLE eMMC with the backup image. Everything done since the backup - including all of Eitri - is lost. Do not switch the printer off until it has rebooted by itself (about 15 minutes)."
  restore_mcus
  mkdir -p "$STATE_DIR"
  local ptuuid; ptuuid="$(printf '%08x' "$(( $(disk_dump | awk -F': *' '/^label-id/ {print $2}') ))")"
  kv_write "$STATE_DIR/stage2.conf" "operation=restore" "disk=$DISK" "part_prefix=$PART_PREFIX" "restore_src=$src" \
    "restore_gz_sha256=$gz" "restore_raw_sha256=$raw"
  build_stage2_initramfs "$BOOT/eitri/stage2/initramfs.cpio.gz.new" "$BOOT/eitri/stage2/initramfs.cpio.gz" 0
  mv -f "$BOOT/eitri/stage2/initramfs.cpio.gz.new" "$BOOT/eitri/stage2/initramfs.cpio.gz"
  boot_rw
  kv_set "$BOOT/eitri/layout.env" eitri_s2_args "console=tty1 rdinit=/init eitri.op=restore panic=60 loglevel=4 net.ifnames=0"
  # The stock U-Boot entry stays reachable if the RAM installer does not start: boot.scr spends one of these
  # starts each time (three, so a power cut mid-restore re-enters the installer instead of a half-written disk).
  kv_set "$BOOT/eitri/boot.env" eitri_s2_tries 3
  kv_set "$BOOT/eitri/boot.env" eitri_mode stage2
  sync
  step_ok restore "restore armed"
}

# ---------------------------------------------------------------------------------------------
main() {
  parse_args "$@"
  trap cleanup EXIT
  need_cmd awk sed tr sha256sum
  if [[ "${EUID:-$(id -u)}" != 0 && "${EITRI_ALLOW_NONROOT:-0}" != 1 ]]; then die 10 "run as root (sudo ./revert.sh ...)"; fi
  if ((DRY_RUN)); then _say "${_BLD}DRY RUN${_OFF} ($MODE) - nothing will be changed. Add --yes to do it."; else _say "${_BLD}REVERT: $MODE${_OFF}"; fi
  emit plan "msg=revert $MODE" "data={\"dry_run\":$([[ $DRY_RUN == 1 ]] && echo true || echo false),\"mode\":\"$MODE\"}"
  mkdir -p "$STATE_DIR" 2>/dev/null || true

  check_installation || die 10 "preconditions not met - nothing was changed"
  case "$MODE" in
    soft | hard | to-eitri)
      if ((! DRY_RUN)); then
        confirm_typed REVERT "The printer will boot $([[ $MODE == to-eitri ]] && echo Eitri || echo "the original QIDI system") on the next start."
      fi ;;
  esac
  case "$MODE" in
    soft) do_soft ;;
    hard) do_hard ;;
    to-eitri) do_to_eitri ;;
    reclaim) do_reclaim ;;
    full) do_full ;;
  esac
  if ((DRY_RUN)); then ok "Dry run finished: nothing was changed."; emit result ok=true "msg=dry run complete"; return 0; fi
  emit result ok=true "msg=$MODE complete"
  case "$MODE" in
    soft | hard | to-eitri | full)
      if ((NO_REBOOT)); then ok "Done. Reboot when you are ready."; else ok "Rebooting now."; sleep "${EITRI_REBOOT_DELAY:-5}"; sync; systemctl reboot || reboot; fi ;;
    *) ok "Done." ;;
  esac
}

if [[ "${EITRI_REVERT_NO_MAIN:-0}" != 1 ]]; then main "$@"; fi
