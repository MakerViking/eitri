#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Eitri stage 2: the RAM-resident half of the in-place conversion.
#
# Runs as /init-launched job inside the initramfs that stage 1 assembled, on the
# printer's own stock kernel, with the eMMC partitions NOT mounted - the only
# moment the stock root filesystem can be shrunk.  Operations (eitri.op= on the
# kernel command line):
#
#   convert   shrink the stock root, add rootfs A/B + /data, write the images,
#             then flip the boot switch to Eitri
#   restore   write a full-disk backup image back to the eMMC (revert --full)
#
# Invariants (tested in tests/test_stage2.py with tool shims):
#   * nothing destructive happens before the payload has been re-verified and the
#     layout validated, and the stock partition table has been saved to /boot;
#   * until the very last step (eitri_mode=eitri) every HANDLED failure leaves a stock
#     system that boots: a failure sets eitri_mode=stock itself, the shrunk stock
#     filesystem is intact and fsck-clean;
#   * an UNHANDLED stop (power cut, hang) keeps boot.env at eitri_mode=stage2 with a
#     start counter (eitri_s2_tries, spent by boot.scr): the next boot re-enters this
#     script, which resumes from stage2.state / the partition table, instead of
#     booting a half-converted stock disk;
#   * progress is visible at http://<printer>:8099/ (status.json) and in
#     /boot/eitri/stage2.log; a failure is also left in /boot/eitri/LAST_ERROR.
#
# The /data layer (Klipper or Kalico, Moonraker, the Python environments, Klarity, the extras) comes from the data
# seed (seed/SPEC.md) and is installed by eitri-seed-install, which stage 1 appends to this initramfs: it verifies the
# archive digest, refuses anything outside the seed layout and places each component with one rename.
#
# Needs in the initramfs: bash, busybox applets (mount, dd, sync, awk, sed, tr,
# tee, mktemp, udhcpc, httpd, find, grep, date, df, mv, rm, rmdir, mkdir, cat), util-linux (sfdisk, blockdev, blkid),
# e2fsprogs (e2fsck, resize2fs, dumpe2fs, mke2fs, debugfs), sha256sum, zstd, tar, gzip.

set -Eeuo pipefail

S2_LIB="${S2_LIB:-/usr/lib/eitri}"
S2_ROOT="${S2_ROOT:-}"
S2_CONF="${S2_CONF:-$S2_ROOT/etc/eitri/stage2.conf}"
# shellcheck source=../lib/eitri-common.sh
source "$S2_LIB/eitri-common.sh"
# shellcheck source=../lib/layout.sh
source "$S2_LIB/layout.sh"

EITRI_COMPONENT=stage2
DRY_RUN=0
EITRI_EVENTS_FILE=""

S2_MAIN_PID=$$
S2_LOG="$S2_ROOT/run/eitri-stage2.log"
S2_WWW="$S2_ROOT/www"
M_BOOT="$S2_ROOT/mnt/boot"
M_STOCK="$S2_ROOT/mnt/stock"
M_DATA="$S2_ROOT/mnt/data"
M_USB="$S2_ROOT/mnt/usb"
WORK="$S2_ROOT/run/eitri-stage2"
BY_UUID="${S2_BY_UUID_DIR:-/dev/disk/by-uuid}"       # tests point this at a directory of stand-in devices

s2_log() { mkdir -p "$(dirname "$S2_LOG")"; printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$S2_LOG" >&2; }

s2_status() {
  # s2_status STATE PCT STEP MESSAGE  - atomic write for the status page
  mkdir -p "$S2_WWW"
  printf '{"state":"%s","pct":%s,"step":"%s","msg":"%s","ts":"%s"}\n' \
    "$1" "$2" "$(json_escape "$3")" "$(json_escape "$4")" "$(_now)" >"$S2_WWW/status.json.tmp" &&
    mv -f "$S2_WWW/status.json.tmp" "$S2_WWW/status.json"
  s2_log "[$1 ${2}%] $3: $4"
}

cfg() { kv_get "$S2_CONF" "$1"; }
cfg_req() { local v; v="$(cfg "$1")" || true; [[ -n "$v" ]] || s2_fail_early "missing $1 in $S2_CONF"; printf '%s' "$v"; }

part_dev() {
  # part_dev N -> device node of partition N (S2_PART_FMT lets tests point at plain files)
  if [[ -n "${S2_PART_FMT:-}" ]]; then printf "$S2_PART_FMT" "$1"; else printf '%s%s%s' "$DISK" "${PART_PREFIX:-}" "$1"; fi
}

# ---------------------------------------------------------------------------
s2_fail_early() { s2_status failed 0 "setup" "$1"; s2_finish_failed "$1"; }

s2_in_subshell() { [[ "${BASHPID:-$$}" != "$S2_MAIN_PID" ]]; }

s2_finish_failed() {
  local why="$1"
  # Only the main shell ends stage 2.  A failure inside a subshell ($(...), a pipeline, the background network job) hands
  # its reason to the main shell and exits: the main shell then fails at that command and reports it.  (With errtrace the
  # ERR trap runs in subshells too; before this guard a failing udhcpc in the background job rebooted the printer 90 s
  # into the conversion - found by the rehearsal on a copy of the real eMMC, which has no network.)
  if s2_in_subshell; then
    printf '%s\n' "$why" >"$WORK/.subshell-failure" 2>/dev/null || true
    exit 1
  fi
  # Leave a note where stock userspace will find it, make sure the next boot is stock.
  if mountpoint -q "$M_BOOT" 2>/dev/null || [[ -d "$M_BOOT/eitri" ]]; then
    kv_set "$M_BOOT/eitri/boot.env" eitri_mode stock || true
    kv_set "$M_BOOT/eitri/boot.env" eitri_s2_tries 0 || true
    printf '%s\n%s\n' "$(_now)" "$why" >"$M_BOOT/eitri/LAST_ERROR" || true
    cp -f "$S2_LOG" "$M_BOOT/eitri/stage2.log" 2>/dev/null || true
  fi
  s2_status failed 0 "failed" "$why"
  sync || true
  s2_reboot_after "${S2_FAIL_DELAY:-90}"
  exit 1
}

s2_reboot_after() {
  # Keep the status page up long enough to be read, then reboot.
  [[ "${S2_NO_REBOOT:-0}" == 1 ]] && return 0
  sleep "$1"
  umount -a 2>/dev/null || true
  reboot -f
}

trap 's2_on_error $LINENO' ERR
s2_on_error() {
  if s2_in_subshell; then exit 1; fi
  trap - ERR
  local why="unexpected error at stage2.sh line $1 (see $S2_LOG)"
  if [[ -s "$WORK/.subshell-failure" ]]; then why="$(cat "$WORK/.subshell-failure") (stage2.sh line $1)"; fi
  s2_finish_failed "$why"
}

# ---------------------------------------------------------------------------
s2_init() {
  mkdir -p "$S2_ROOT/proc" "$S2_ROOT/sys" "$S2_ROOT/dev" "$S2_ROOT/run" "$S2_ROOT/tmp" "$M_BOOT" "$M_STOCK" "$M_DATA" "$M_USB" "$WORK" "$S2_WWW"
  if [[ "${S2_NO_SYSMOUNT:-0}" != 1 ]]; then
    mountpoint -q /proc || mount -t proc proc /proc
    mountpoint -q /sys || mount -t sysfs sysfs /sys
    mountpoint -q /dev || mount -t devtmpfs devtmpfs /dev
    mountpoint -q /run || mount -t tmpfs tmpfs /run
    mkdir -p /run/eitri-stage2
  fi
  : >"$S2_LOG"
  [[ -f "$S2_LIB/www/index.html" ]] && cp -f "$S2_LIB/www/index.html" "$S2_WWW/index.html"
  s2_status running 0 "init" "stage 2 started"
  if [[ "${S2_NO_NET:-0}" != 1 ]]; then
    # Networking is only for the status page and (restore) streaming; never fatal.
    if command -v udhcpc >/dev/null; then ( trap - ERR; set +eE; ip link set eth0 up 2>/dev/null; udhcpc -b -q -i eth0 -t 5 >/dev/null 2>&1 ) & fi
    if command -v httpd >/dev/null; then httpd -p 8099 -h "$S2_WWW" 2>/dev/null || true; fi
  fi
}

# Kernel modules of the STOCK kernel that stage 1 collected (vfat, usb-storage, ...).
s2_load_modules() {
  local list="$S2_ROOT/etc/eitri/modules.list" ko
  [[ -r "$list" ]] || return 0
  while IFS= read -r ko; do
    [[ -n "$ko" ]] || continue
    if insmod "$S2_ROOT/$ko" 2>>"$S2_LOG"; then s2_log "module loaded: $ko"; else s2_log "module not loaded: $ko"; fi
  done <"$list"
}

s2_require_tools() {
  local t missing=""
  for t in ${S2_REQUIRED_TOOLS:-sfdisk blockdev e2fsck resize2fs dumpe2fs mke2fs sha256sum zstd tar dd awk sed mount umount sync find grep date df mv rm rmdir mkdir cat tr}; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
  done
  [[ -z "$missing" ]] || s2_fail_early "the installer environment lacks:$missing"
}

# FAT mounts on the stock kernel (5.16.20-rockchip64): its default iocharset, iso8859-1, is a module that the kernel cannot
# load by itself (xz-compressed .ko.xz, no in-kernel decompression before 5.17); stage 1 appends it and s2_load_modules
# loads it, but should that fail, ascii (built in) reads and writes the plain names this installer uses just as well.
s2_mount_fat() {   # s2_mount_fat OPTS DEV DIR
  mount -t vfat -o "$1" "$2" "$3" 2>>"$S2_LOG" || mount -t vfat -o "$1,iocharset=ascii" "$2" "$3" 2>>"$S2_LOG"
}
s2_mount_ro_any() {   # s2_mount_ro_any DEV DIR   (a USB stick: FAT32, exFAT or ext4)
  mount -o ro "$1" "$2" 2>>"$S2_LOG" || mount -t vfat -o ro,iocharset=ascii "$1" "$2" 2>>"$S2_LOG"
}

s2_wait_device() {
  local dev="$1" i
  for i in $(seq 1 "${S2_DEVICE_WAIT:-60}"); do [[ -b "$dev" || -e "$dev" ]] && return 0; sleep 0.5; done
  return 1
}

# ---------------------------------------------------------------------------
# Payload: stock-side staging directory, or a USB stick.  Read-only mount.
s2_mount_payload() {
  local loc; loc="$(cfg_req payload_loc)"
  case "$loc" in
    stock:*) PAYLOAD="$M_STOCK${loc#stock:}" ;;
    usb:*)
      local spec="${loc#usb:}" uuid path
      uuid="${spec%%:*}"; path="${spec#*:}"
      s2_wait_device "$BY_UUID/$uuid" || s2_fail_early "payload USB stick (UUID $uuid) not found"
      s2_mount_ro_any "$BY_UUID/$uuid" "$M_USB" || s2_fail_early "cannot mount the payload USB stick"
      PAYLOAD="$M_USB$path"
      ;;
    dir:*) PAYLOAD="${loc#dir:}" ;;           # tests
    *) s2_fail_early "unknown payload location '$loc'" ;;
  esac
  [[ -d "$PAYLOAD" ]] || s2_fail_early "payload directory $PAYLOAD not found"
}

s2_release_payload() {
  umount "$M_STOCK" 2>/dev/null || true
  umount "$M_USB" 2>/dev/null || true
}

s2_verify_payload() {
  s2_status running 5 "verify" "re-checking the staged files"
  # (BusyBox sha256sum has no --quiet; the OK lines go to the log)
  ( cd "$PAYLOAD" && sha256sum -c payload.sha256 ) >>"$S2_LOG" 2>&1 || s2_fail_early "staged payload does not match its checksums - nothing was changed"
  [[ -r "$PAYLOAD/payload.env" ]] || s2_fail_early "payload.env missing"
  ROOTFS_FILE="$(kv_get "$PAYLOAD/payload.env" rootfs_file)"
  ROOTFS_RAW_SIZE="$(kv_get "$PAYLOAD/payload.env" rootfs_raw_size)"
  ROOTFS_RAW_SHA="$(kv_get "$PAYLOAD/payload.env" rootfs_raw_sha256)"
  SEED_FILE="$(kv_get "$PAYLOAD/payload.env" data_seed_file)"
  SEED_SHA="$(kv_get "$PAYLOAD/payload.env" data_seed_sha256)"
  MCU_FW_FILE="$(kv_get "$PAYLOAD/payload.env" mcu_firmware_file)"
  ROOT_MIB="$(kv_get "$PAYLOAD/payload.env" root_mib)"
  DATA_MIN_MIB="$(kv_get "$PAYLOAD/payload.env" data_min_mib)"
  local v
  # MCU_FW_FILE may be empty: a release with mcu_policy keep-stock carries no MCU firmware (both boards stay stock).
  for v in ROOTFS_FILE ROOTFS_RAW_SIZE ROOTFS_RAW_SHA SEED_FILE SEED_SHA ROOT_MIB DATA_MIN_MIB; do
    [[ -n "${!v}" ]] || s2_fail_early "payload.env lacks $v"
  done
  [[ "$ROOTFS_RAW_SHA" =~ ^[0-9a-f]{64}$ ]] || s2_fail_early "bad rootfs checksum in payload.env"
  [[ "$SEED_SHA" =~ ^[0-9a-f]{64}$ ]] || s2_fail_early "bad data seed checksum in payload.env"
  [[ "$SEED_FILE" =~ ^data-seed-[0-9A-Za-z.+_~-]+-(klipper|kalico)\.tar\.zst$ ]] || s2_fail_early "bad data seed file name in payload.env"
  ((ROOTFS_RAW_SIZE <= ROOT_MIB * 1024 * 1024)) || s2_fail_early "rootfs image ($ROOTFS_RAW_SIZE B) does not fit a $ROOT_MIB MiB slot"
}

# The backup is the way back; refuse to shrink anything when it has disappeared since stage 1 recorded it.
# (Only a USB-stick backup can be looked at from here: a copy on the PC or in the stock root was checked by stage 1
# right before it armed this installer.)
s2_check_backup() {
  local method; method="$(cfg backup_method)" || method=""
  case "$method" in
    usb)
      local uuid path bytes f total=0 files=()
      uuid="$(cfg_req backup_usb_uuid)"; path="$(cfg_req backup_path)"; bytes="$(cfg backup_bytes)" || bytes=""
      s2_wait_device "$BY_UUID/$uuid" || s2_fail_early "the USB stick with the backup (UUID $uuid) is not plugged in. Nothing was changed and the original system will start; plug the stick back in, then run revert.sh --hard --yes followed by install.sh again"
      s2_mount_ro_any "$BY_UUID/$uuid" "$M_USB" || s2_fail_early "cannot mount the backup USB stick - nothing was changed"
      if [[ -f "$M_USB$path" ]]; then files=("$M_USB$path"); else files=("$M_USB$path".part*); fi
      for f in "${files[@]}"; do
        [[ -f "$f" ]] || { umount "$M_USB" 2>/dev/null || true; s2_fail_early "the backup file $path is no longer on the USB stick - nothing was changed"; }
        total=$((total + $(wc -c <"$f")))
      done
      umount "$M_USB" 2>/dev/null || true
      [[ -z "$bytes" || "$total" == "$bytes" ]] || s2_fail_early "the backup on the USB stick is $total bytes, $bytes were recorded - nothing was changed"
      s2_log "backup re-checked on the USB stick ($total bytes)"
      ;;
    "") ;;
    *) s2_log "the backup ($method) cannot be re-checked from the installer environment; stage 1 checked it right before arming" ;;
  esac
}

# ---------------------------------------------------------------------------
# Partition table helpers
sfdisk_field() {
  # sfdisk_field DUMP PARTNO FIELD  (start|size|type)
  awk -v n="$2" -v f="$3" '
    $1 ~ ("[0-9]+$") && match($1, /[0-9]+$/) && substr($1, RSTART) + 0 == n {
      for (i = 1; i <= NF; i++) if ($i ~ ("^" f "=")) { v = $i; if (v == f "=") v = $(i + 1); sub(/^[a-z]+=/, "", v); sub(/,$/, "", v); print v; exit }
    }' <<<"$1"
}

s2_read_table() {
  OLD_DUMP="$(sfdisk -d "$DISK")" || s2_fail_early "cannot read the partition table of $DISK"
  LABEL_ID="$(awk -F': *' '/^label-id/ {print $2}' <<<"$OLD_DUMP")"
  grep -q '^label: dos' <<<"$OLD_DUMP" || s2_fail_early "the disk does not have a dos partition table"
  P1_START="$(sfdisk_field "$OLD_DUMP" 1 start)"; P1_SIZE="$(sfdisk_field "$OLD_DUMP" 1 size)"
  P2_START="$(sfdisk_field "$OLD_DUMP" 2 start)"; P2_SIZE="$(sfdisk_field "$OLD_DUMP" 2 size)"
  DISK_SECTORS="$(blockdev --getsz "$DISK")"
  local v
  for v in LABEL_ID P1_START P1_SIZE P2_START P2_SIZE DISK_SECTORS; do
    [[ -n "${!v}" ]] || s2_fail_early "could not parse $v from the partition table"
  done
}

s2_already_converted() { [[ -n "$(sfdisk_field "$OLD_DUMP" 5 start)" && -n "$(sfdisk_field "$OLD_DUMP" 7 start)" ]]; }

# ---------------------------------------------------------------------------
s2_convert() {
  DISK="$(cfg_req disk)"; PART_PREFIX="$(cfg part_prefix)" || PART_PREFIX=""
  local stock_n boot_n
  boot_n="$(cfg_req boot_part)"; stock_n="$(cfg_req stock_part)"
  BOOT_DEV="$(part_dev "$boot_n")"; STOCK_DEV="$(part_dev "$stock_n")"
  s2_wait_device "$STOCK_DEV" || s2_fail_early "stock root device $STOCK_DEV did not appear"

  s2_mount_fat rw,noatime "$BOOT_DEV" "$M_BOOT" || s2_fail_early "cannot mount $BOOT_DEV"
  mkdir -p "$M_BOOT/eitri"
  cp -f "$S2_LOG" "$M_BOOT/eitri/stage2.log" 2>/dev/null || true

  s2_read_table
  local pre_converted=0
  s2_already_converted && pre_converted=1

  # --- stock filesystem: check, and measure how small it can get (unmounted) ---
  s2_status running 8 "check" "checking the stock filesystem"
  local rc=0
  e2fsck -fp "$STOCK_DEV" >>"$S2_LOG" 2>&1 || rc=$?
  ((rc <= 1)) || s2_fail_early "the stock filesystem has errors that could not be fixed automatically (e2fsck exit $rc) - nothing was changed"

  # The payload is verified while the disk is still untouched, and once more after the shrink.
  mount -o ro "$STOCK_DEV" "$M_STOCK" || s2_fail_early "cannot mount the stock system read-only"
  s2_mount_payload
  s2_verify_payload
  s2_release_payload

  local plan="$WORK/plan.env"
  if ((pre_converted)); then
    s2_log "partition table already converted by an earlier run - reusing it"
    {
      echo "p2_start=$P2_START"; echo "p2_size=$P2_SIZE"
      echo "ext_start=$(sfdisk_field "$OLD_DUMP" 3 start)"; echo "ext_size=$(sfdisk_field "$OLD_DUMP" 3 size)"
      local n; for n in 5 6 7; do echo "p${n}_start=$(sfdisk_field "$OLD_DUMP" "$n" start)"; echo "p${n}_size=$(sfdisk_field "$OLD_DUMP" "$n" size)"; done
    } >"$plan"
  else
    s2_check_backup
    local minblocks bs fs_min msg
    minblocks="$(resize2fs -P "$STOCK_DEV" 2>/dev/null | awk -F': *' '/[Mm]inimum size/ {print $2}')"
    bs="$(dumpe2fs -h "$STOCK_DEV" 2>/dev/null | awk -F': *' '/^Block size/ {print $2}')"
    [[ "$minblocks" =~ ^[0-9]+$ && "$bs" =~ ^[0-9]+$ ]] || s2_fail_early "cannot determine how far the stock filesystem can shrink"
    fs_min=$((minblocks * bs))
    s2_log "stock filesystem minimum: $((fs_min / 1048576)) MiB"
    msg="$(layout_compute "$DISK_SECTORS" "$P2_START" "$P2_SIZE" "$fs_min" "$ROOT_MIB" "$DATA_MIN_MIB")" ||
      s2_fail_early "$(kv_get <(printf '%s\n' "$msg") error) - nothing was changed"
    printf '%s\n' "$msg" >"$plan"
    msg="$(layout_validate "$plan" "$DISK_SECTORS" "$P1_START" "$P1_SIZE")" || s2_fail_early "internal layout error: $msg - nothing was changed"
  fi
  s2_log "layout:"; layout_summary "$plan" | while IFS= read -r l; do s2_log "$l"; done
  [[ "$(kv_get "$plan" p2_start)" == "$P2_START" ]] || s2_fail_early "layout moved the stock partition start - refusing"

  # --- point of no return for the partition table: save everything needed to undo it ---
  printf '%s\n' "$OLD_DUMP" >"$M_BOOT/eitri/ptable.stock.sfdisk"
  cp -f "$plan" "$M_BOOT/eitri/layout.plan"
  kv_set "$M_BOOT/eitri/stage2.state" phase planned
  sync

  if ((! pre_converted)); then
    s2_status running 12 "shrink" "shrinking the stock system (this takes several minutes)"
    umount "$M_BOOT"                                    # nothing may be mounted when the table is re-read
    resize2fs "$STOCK_DEV" "$(kv_get "$plan" p2_size)s" >>"$S2_LOG" 2>&1 || s2_fail_early_boot "resize2fs failed - the stock system was not repartitioned"
    e2fsck -fn "$STOCK_DEV" >>"$S2_LOG" 2>&1 || s2_fail_early_boot "stock filesystem failed its check after shrinking"
    s2_status running 30 "partition" "creating the Eitri partitions"
    layout_sfdisk_script "$DISK" "$LABEL_ID" "$P1_START" "$P1_SIZE" "$plan" "${PART_PREFIX:-}" | sfdisk --no-reread "$DISK" >>"$S2_LOG" 2>&1 ||
      s2_fail_early_boot "sfdisk failed"
    blockdev --rereadpt "$DISK" >>"$S2_LOG" 2>&1 || partx -u "$DISK" >>"$S2_LOG" 2>&1 || s2_fail_early_boot "the kernel did not accept the new partition table"
    s2_mount_fat rw,noatime "$BOOT_DEV" "$M_BOOT" || s2_fail_early "cannot remount $BOOT_DEV"
    kv_set "$M_BOOT/eitri/stage2.state" phase partitioned
  fi

  ROOT_A_DEV="$(part_dev 5)"; ROOT_B_DEV="$(part_dev 6)"; DATA_DEV="$(part_dev 7)"
  s2_wait_device "$ROOT_A_DEV" && s2_wait_device "$DATA_DEV" || s2_fail_early_boot "new partitions did not appear"
  mount -o ro "$STOCK_DEV" "$M_STOCK" || s2_fail_early_boot "cannot mount the stock system read-only"
  s2_mount_payload
  s2_verify_payload          # again: the data was moved by resize2fs

  s2_write_rootfs
  s2_make_data
  s2_mask_resize_service
  s2_commit
}

# A failure after the table changed is still "stock boots": record why and stop.
s2_fail_early_boot() {
  mountpoint -q "$M_BOOT" 2>/dev/null || s2_mount_fat rw,noatime "$BOOT_DEV" "$M_BOOT" || true
  s2_fail_early "$1"
}

s2_write_rootfs() {
  s2_status running 40 "rootfs" "writing the Eitri system to slot A"
  zstd -dc "$PAYLOAD/$ROOTFS_FILE" | dd of="$ROOT_A_DEV" bs=4M conv=fsync,notrunc iflag=fullblock 2>>"$S2_LOG" ||
    s2_fail_early_boot "writing the rootfs failed"
  sync; [[ -w /proc/sys/vm/drop_caches ]] && echo 3 >/proc/sys/vm/drop_caches || true
  s2_status running 60 "verify-rootfs" "reading the system back from the eMMC"
  local back
  # head closes the pipe once it has the image bytes, which kills dd with SIGPIPE: expected, hence `|| true`
  back="$({ dd if="$ROOT_A_DEV" bs=4M count=$(((ROOTFS_RAW_SIZE + 4194303) / 4194304)) iflag=fullblock 2>>"$S2_LOG" || true; } |
    head -c "$ROOTFS_RAW_SIZE" | sha256sum | awk '{print $1}')"
  [[ "$back" == "$ROOTFS_RAW_SHA" ]] || s2_fail_early_boot "read-back of slot A does not match - the eMMC write failed"
  # Slot B stays empty until the first OS update; clear any stale filesystem signature.
  dd if=/dev/zero of="$ROOT_B_DEV" bs=1M count=4 conv=fsync,notrunc 2>>"$S2_LOG" || s2_fail_early_boot "cannot clear slot B"
  kv_set "$M_BOOT/eitri/stage2.state" phase rootfs_written
}

s2_make_data() {
  s2_status running 72 "data" "creating /data"
  mke2fs -F -q -t ext4 -L eitri-data -m 1 -E lazy_itable_init=1,lazy_journal_init=1 "$DATA_DEV" >>"$S2_LOG" 2>&1 || s2_fail_early_boot "mke2fs failed"
  mount "$DATA_DEV" "$M_DATA" || s2_fail_early_boot "cannot mount the new /data"
  s2_install_seed
  mkdir -p "$M_DATA/eitri/state" "$M_DATA/eitri/migrate" "$M_DATA/eitri/mcu/firmware"
  # Where the pre-install backup lives, for revert.sh --full
  [[ -r "$M_STOCK/var/lib/eitri-install/backup.env" ]] && cp -f "$M_STOCK/var/lib/eitri-install/backup.env" "$M_DATA/eitri/state/backup.env"
  # The release this /data belongs to: eitri-update compares it with the channel (also read by the configurator).
  printf '%s\n' "$(kv_get "$PAYLOAD/payload.env" version)" >"$M_DATA/eitri/release"
  # MCU firmware that matches this release (if it carries any), and the firmware backups taken from the stock boards (stage 1).
  if [[ -n "$MCU_FW_FILE" ]]; then
    zstd -dc "$PAYLOAD/$MCU_FW_FILE" | tar -x -C "$M_DATA/eitri/mcu/firmware" --no-same-owner ||
      s2_fail_early_boot "unpacking the MCU firmware failed"
  else
    s2_log "this release carries no MCU firmware: both MCU boards keep their stock firmware"
  fi
  if [[ -d "$M_STOCK/var/lib/eitri-install/mcu" ]]; then
    mkdir -p "$M_DATA/eitri/mcu/state"
    cp -a "$M_STOCK/var/lib/eitri-install/mcu/." "$M_DATA/eitri/mcu/state/" || s2_log "warning: could not copy the stock MCU backups"
  fi
  s2_migrate_from_stock
  s2_rauc_override
  # What the first boot should do and where the way back is.  toolhead_firmware=stock: the boards were not flashed (--mcu
  # skip/manual); the configurator then leaves the ADXL345 out of printer.cfg (the stock v0.12 toolhead firmware speaks an
  # older accelerometer protocol).  Whoever flashes the toolhead later rewrites the key.
  local th=stock; [[ "$(cfg mcu_mode)" == auto ]] && th=pending
  kv_write "$M_DATA/eitri/state/install.env" \
    "installed_at=$(_now)" "flavor=$(cfg flavor)" "stock_partuuid=$(cfg stock_partuuid)" "stock_part=$(cfg stock_part)" \
    "stock_uuid=$(cfg stock_fs_uuid)" "stock_soc=$(cfg stock_soc)" "bundle_version=$(kv_get "$PAYLOAD/payload.env" version)" \
    "seed_version=$(kv_get "$M_DATA/eitri/state/seed-installed.env" SEED_VERSION)" "seed_sha256=$SEED_SHA" \
    "mcu_flash=$(cfg mcu_mode)" "toolhead_firmware=$th" "backup_sha256=$(cfg backup_sha256)"
  # No secrets in it, and the configurator (user eitri) reads toolhead_firmware: world-readable, root-owned.
  chmod 0644 "$M_DATA/eitri/state/install.env"
  sync
  umount "$M_DATA"
  kv_set "$M_BOOT/eitri/stage2.state" phase data_written
}

# The data seed: Klipper/Kalico, Moonraker, venvs, Klarity, extras and the layer config, ready to run, no network.  The tool
# checks the archive digest first (the value comes from the signed install.json, via payload.env), applies the member policy
# before extracting anything and places each component with one rename.  It also ships eitri/flavor; the flavor chosen at
# stage 1 must agree with it.
s2_install_seed() {
  local tool="${S2_SEED_INSTALL:-/usr/libexec/eitri/eitri-seed-install}" flavor
  flavor="$(cfg_req flavor)"
  s2_status running 74 "data-seed" "installing the Klipper, Moonraker and Klarity environments (a few minutes)"
  [[ -x "$tool" ]] || s2_fail_early_boot "eitri-seed-install is missing from the installer environment"
  "$tool" --archive "$PAYLOAD/$SEED_FILE" --sha256 "$SEED_SHA" --flavor "$flavor" --data "$M_DATA" --mode install --owner 1000:1000 -q >>"$S2_LOG" 2>&1 ||
    s2_fail_early_boot "installing the data seed failed (see $S2_LOG)"
  [[ "$(tr -d ' \t\r\n' <"$M_DATA/eitri/flavor" 2>/dev/null)" == "$flavor" ]] || s2_fail_early_boot "the data seed is not a $flavor seed"
  [[ -f "$M_DATA/eitri/state/seed-installed.env" ]] || s2_fail_early_boot "the data seed was not recorded as installed"
  s2_log "data seed $(kv_get "$M_DATA/eitri/state/seed-installed.env" SEED_VERSION) installed"
}

# The OS image's RAUC configuration describes the flash-once disk: GPT, slots in partitions 2 and 3, its own
# boot-state backend.  Here the slots are logical partitions 5 and 6 (3 is the extended container: a bundle
# written to it would destroy the partition table) and the boot state lives in /boot/eitri/boot.env.  So the
# image's own system.conf is rewritten into the /etc overlay's upper layer on /data with just those two
# things changed.  eitri-update independently refuses to install while RAUC's slots disagree with layout.env.
s2_rauc_override() {
  local src="$WORK/rauc.system.conf" out="$WORK/rauc.system.conf.new"
  local upper="$M_DATA/eitri/state/etc/upper" handler="${S2_RAUC_HANDLER:-/usr/libexec/eitri/eitri-bootctl}"
  if ! command -v debugfs >/dev/null 2>&1; then s2_log "warning: debugfs missing, RAUC slot override not written"; return 0; fi
  debugfs -R 'cat /etc/rauc/system.conf' "$ROOT_A_DEV" >"$src" 2>>"$S2_LOG" || true
  if [[ ! -s "$src" ]] || ! grep -q '^\[slot\.rootfs\.' "$src"; then
    s2_log "warning: no /etc/rauc/system.conf in the new system, RAUC slot override not written"; return 0
  fi
  awk -v disk="${DISK}${PART_PREFIX:-}" -v pa=5 -v pb=6 -v handler="$handler" '
    FNR == NR { if ($0 ~ /^\[/) s = $0; else if (s ~ /^\[slot\.rootfs\./ && $0 ~ /^bootname=/) { v = $0; sub(/^bootname=[ ]*/, "", v); bn[s] = v } next }
    /^\[/ { s = $0 }
    s ~ /^\[slot\.rootfs\./ && /^device=/ { if (bn[s] == "A") { print "device=" disk pa; next } if (bn[s] == "B") { print "device=" disk pb; next } }
    /^bootloader-custom-backend=/ { print "bootloader-custom-backend=" handler; next }
    { print }' "$src" "$src" >"$out"
  if ! grep -q "^device=${DISK}${PART_PREFIX:-}5\$" "$out"; then
    s2_log "warning: could not map the RAUC slots in the new system's system.conf, override not written"; return 0
  fi
  mkdir -p "$upper/rauc" "$M_DATA/eitri/state/etc/work"
  cp -f "$out" "$upper/rauc/system.conf" && chmod 0644 "$upper/rauc/system.conf" && s2_log "RAUC slots mapped to partitions 5/6"
}

# Carry the user's settings across without ever copying Qidi's screen config or tokens.
s2_migrate_from_stock() {
  local dest="$M_DATA/eitri/migrate" home="" h
  for h in "$M_STOCK"/home/*; do [[ -d "$h/printer_data/config" ]] && home="$h" && break; done
  if [[ -n "$home" ]]; then
    mkdir -p "$dest/stock-config"
    # config.mksini* is QIDI's screen/cloud configuration (contains account tokens): never migrated.
    ( cd "$home/printer_data/config" && tar -c --exclude='config.mksini*' --exclude='*.bak' . ) | tar -x -C "$dest/stock-config" ||
      s2_log "warning: could not copy the stock printer configuration"
  fi
  if [[ -r "$M_BOOT/wpa_supplicant-wlan0.conf" ]]; then
    mkdir -p "$dest/network"; install -m 0600 "$M_BOOT/wpa_supplicant-wlan0.conf" "$dest/network/wpa_supplicant-wlan0.conf" || true
  fi
  local keys="" f
  for f in "$M_STOCK"/root/.ssh/authorized_keys "$M_STOCK"/home/*/.ssh/authorized_keys; do
    [[ -r "$f" ]] && keys="$keys$(cat "$f")"$'\n'
  done
  if [[ -n "$keys" ]]; then printf '%s' "$keys" | awk 'NF && !seen[$0]++' >"$dest/authorized_keys"; chmod 0600 "$dest/authorized_keys"; fi
}

# Armbian's resize service would grow the (now shrunk) stock root over our partitions if the
# stock system is ever booted again.  Mask it offline; revert.sh removes the mask.
s2_mask_resize_service() {
  mount -o remount,rw "$M_STOCK" 2>/dev/null || { umount "$M_STOCK"; mount "$STOCK_DEV" "$M_STOCK" || s2_fail_early_boot "cannot remount the stock system read-write"; }
  local unit="$M_STOCK/etc/systemd/system/armbian-resize-filesystem.service"
  if [[ ! -e "$unit" ]]; then
    ln -s /dev/null "$unit" && kv_set "$M_BOOT/eitri/stock-changes.env" masked_armbian_resize 1
  fi
  sync
}

s2_commit() {
  s2_status running 90 "commit" "switching the boot selection to Eitri"
  local ptuuid pu_a pu_b
  ptuuid="$(printf '%08x' "$((LABEL_ID))")"             # dos label-id 0x6a783df4 -> PARTUUID=6a783df4-NN
  pu_a="$ptuuid-05"; pu_b="$ptuuid-06"
  kv_write "$M_BOOT/eitri/layout.env" \
    "eitri_part_A=5" "eitri_part_B=6" "eitri_partuuid_A=$pu_a" "eitri_partuuid_B=$pu_b" \
    "eitri_cmdline=$(cfg kernel_cmdline)" \
    "eitri_s2_kernel=$(cfg s2_kernel)" "eitri_s2_fdt=$(cfg s2_fdt)" "eitri_s2_initrd=$(cfg s2_initrd)" "eitri_s2_args=$(cfg s2_args)"
  [[ "$(kv_get "$M_BOOT/eitri/layout.env" eitri_part_A)" == 5 ]] || s2_fail_early_boot "could not write layout.env"
  umount "$M_STOCK" 2>/dev/null || true
  sync
  # The single switch: everything before this left a bootable stock system.
  kv_write "$M_BOOT/eitri/boot.env" "eitri_mode=eitri" "eitri_slot=A" "eitri_tries=3" "eitri_state_A=unknown" "eitri_state_B=unknown"
  kv_set "$M_BOOT/eitri/stage2.state" phase committed
  rm -f "$M_BOOT/eitri/LAST_ERROR"
  cp -f "$S2_LOG" "$M_BOOT/eitri/stage2.log" 2>/dev/null || true
  sync
  s2_status done 100 "done" "Eitri installed - rebooting"
  umount "$M_BOOT" 2>/dev/null || true
  s2_reboot_after "${S2_DONE_DELAY:-8}"
}

# ---------------------------------------------------------------------------
# Restore helpers
s2_mbr_p1() {
  # s2_mbr_p1 FILE -> "START_LBA SECTORS" of partition entry 1 in a 512-byte MBR; fails unless it is a plausible one
  local f="$1" b lba n
  [[ "$(od -An -v -tx1 -j510 -N2 "$f" 2>/dev/null | tr -d ' \n')" == 55aa ]] || return 1
  read -r -a b < <(od -An -v -tu1 -j454 -N8 "$f" 2>/dev/null) || return 1
  ((${#b[@]} == 8)) || return 1
  lba=$((b[0] | b[1] << 8 | b[2] << 16 | b[3] << 24)); n=$((b[4] | b[5] << 8 | b[6] << 16 | b[7] << 24))
  ((lba > 0 && n > 0)) || return 1
  printf '%s %s\n' "$lba" "$n"
}

s2_pick_bs() {
  # largest power of two (up to 4 MiB) that divides BYTES, so dd's seek= lands exactly on the offset
  local bytes="$1" bs
  for bs in 4194304 1048576 524288 262144 131072 65536 32768 16384 8192 4096 2048 1024 512; do
    if ((bytes % bs == 0)); then printf '%s\n' "$bs"; return 0; fi
  done
  printf '512\n'
}

s2_ram_fits() {
  # s2_ram_fits BYTES: may that much be held in RAM (tmpfs is half of RAM; stay under 40% of what is free)
  local kib; kib="$(awk '/^MemAvailable:/ {print $2}' "${S2_MEMINFO:-/proc/meminfo}" 2>/dev/null)" || kib=0
  [[ "$kib" =~ ^[0-9]+$ ]] || kib=0
  (($1 / 1024 * 5 <= kib * 2))
}

s2_restore() {
  DISK="$(cfg_req disk)"
  local src; src="$(cfg_req restore_src)"
  local want_gz; want_gz="$(cfg restore_gz_sha256)" || true
  local want_raw; want_raw="$(cfg restore_raw_sha256)" || true
  local skip_sectors=32768                            # sectors 1..32767 hold idbloader/u-boot, which the installer never touched
  s2_status running 5 "restore" "preparing to restore the original eMMC image"
  IMAGE_FILES=()
  local stream=""
  case "$src" in
    usb:*)
      local spec="${src#usb:}" uuid path
      uuid="${spec%%:*}"; path="${spec#*:}"
      s2_wait_device "$BY_UUID/$uuid" || s2_fail_early "backup USB stick (UUID $uuid) not found"
      s2_mount_ro_any "$BY_UUID/$uuid" "$M_USB" || s2_fail_early "cannot mount the backup USB stick"
      stream="$M_USB$path"
      # a FAT32 stick holds the image in numbered parts: <name>.part000, .part001, ...
      if [[ -r "$stream" ]]; then IMAGE_FILES=("$stream")
      else IMAGE_FILES=("$stream".part*); fi
      [[ -r "${IMAGE_FILES[0]}" ]] || s2_fail_early "backup file $path not found on the stick"
      if [[ -n "$want_gz" ]]; then
        s2_status running 10 "verify-backup" "checking the backup file before touching the eMMC"
        [[ "$(cat "${IMAGE_FILES[@]}" | sha256sum | awk '{print $1}')" == "$want_gz" ]] || s2_fail_early "the backup file does not match its recorded checksum - nothing was written"
      fi
      cat "${IMAGE_FILES[@]}" | gzip -t || s2_fail_early "the backup file is not a valid gzip archive - nothing was written"
      ;;
    file:*) IMAGE_FILES=("${src#file:}") ;;               # tests
    tcp:*) stream="" ;;
    *) s2_fail_early "unknown restore source '$src'" ;;
  esac
  # A hash that can be checked BEFORE the commit point is what makes a streamed (tcp) source safe at all.
  if [[ "$src" == tcp:* && -z "$want_raw" ]]; then
    s2_fail_early "a streamed restore needs the recorded checksum of the backup (backup.env), and none was found - nothing was written"
  fi
  s2_status running 20 "restore" "writing the original image back (do not power off)"
  # Write order is the safety property.  The partition table (sector 0) and the boot partition (which holds this very
  # installer, the start counter and boot.scr) are the last things touched, and only after the whole stream has been
  # checked against its recorded hash.  Until then an interruption leaves a disk that still boots into this installer
  # (boot.scr re-arms it, three starts), which simply repeats the restore.  Sector 0 and p1 are held in RAM meanwhile.
  local tmp="$WORK/restore.sha" fifo="$WORK/restore.fifo" hasher mbr="$WORK/restore.mbr" p1img="$WORK/restore.p1" why="$WORK/restore.why"
  rm -f "$fifo" "$mbr" "$p1img" "$why"; mkfifo "$fifo"
  { sha256sum <"$fifo" | awk '{print $1}' >"$tmp"; } & hasher=$!
  {
    if [[ "$src" == tcp:* ]]; then
      local hp="${src#tcp:}"
      nc "${hp%%:*}" "${hp##*:}"
    else
      cat "${IMAGE_FILES[@]}"
    fi | gzip -dc | tee "$fifo" |
      { dd of="$mbr" bs=512 count=1 conv=fsync iflag=fullblock 2>>"$S2_LOG" || exit 1
        dd of=/dev/null bs=512 count=$((skip_sectors - 1)) iflag=fullblock 2>>"$S2_LOG" || exit 1      # the bootloader area is never touched
        local geo lba n bs
        geo="$(s2_mbr_p1 "$mbr")" || { echo "the image does not start with a valid partition table" >"$why"; exit 2; }
        read -r lba n <<<"$geo"
        if ((lba != skip_sectors)); then echo "the image's boot partition starts at sector $lba, not $skip_sectors" >"$why"; exit 2; fi
        if ! s2_ram_fits $((n * 512)); then echo "the boot partition ($((n / 2048)) MiB) does not fit in RAM" >"$why"; exit 2; fi
        bs="$(s2_pick_bs $((n * 512)))"
        dd of="$p1img" bs="$bs" count=$((n * 512 / bs)) iflag=fullblock 2>>"$S2_LOG" || exit 1          # p1: kept in RAM until the end
        bs="$(s2_pick_bs $(((lba + n) * 512)))"
        dd of="$DISK" bs="$bs" seek=$(((lba + n) * 512 / bs)) conv=fsync,notrunc iflag=fullblock 2>>"$S2_LOG" || exit 1
      }
  } || { wait "$hasher" 2>/dev/null || true
         if [[ -s "$why" ]]; then s2_fail_early "$(cat "$why") - nothing was written to the disk"; fi
         s2_fail_early "writing the image failed. The installer starts again by itself (up to three times) and repeats the restore; keep the printer powered and the backup connected"; }
  wait "$hasher"
  if [[ -n "$want_raw" && "$(cat "$tmp")" != "$want_raw" ]]; then
    s2_fail_early "the restored data does not match the recorded checksum, so the new partition table was NOT written. The installer starts again by itself and repeats the restore"
  fi
  # Commit: boot partition, then the partition table (each flushed).  Only now does the disk stop re-entering this installer.
  local geo lba n bs
  geo="$(s2_mbr_p1 "$mbr")" || s2_fail_early "internal error: lost the partition table"
  read -r lba n <<<"$geo"
  [[ -s "$p1img" && "$(stat -c %s "$p1img")" == "$((n * 512))" ]] || s2_fail_early "internal error: the boot partition image is incomplete"
  bs="$(s2_pick_bs $((lba * 512)))"
  dd if="$p1img" of="$DISK" bs="$bs" seek=$((lba * 512 / bs)) conv=fsync,notrunc 2>>"$S2_LOG" ||
    s2_fail_early "writing the boot partition failed. The installer starts again by itself and repeats the restore"
  dd if="$mbr" of="$DISK" bs=512 count=1 conv=fsync,notrunc 2>>"$S2_LOG" ||
    s2_fail_early "writing the partition table failed. The installer starts again by itself and repeats the restore"
  rm -f "$p1img" "$mbr"
  sync; blockdev --rereadpt "$DISK" 2>/dev/null || true
  s2_status done 100 "done" "stock system restored - rebooting"
  s2_reboot_after "${S2_DONE_DELAY:-8}"
}

# ---------------------------------------------------------------------------
main() {
  s2_init
  s2_load_modules
  s2_require_tools
  local op; op="$(cfg operation)" || op=""
  [[ -n "$op" ]] || op="$(tr ' ' '\n' <"$S2_ROOT/proc/cmdline" 2>/dev/null | awk -F= '$1 == "eitri.op" {print $2}')"
  case "$op" in
    convert) s2_convert ;;
    restore) s2_restore ;;
    *) s2_fail_early "unknown operation '$op'" ;;
  esac
}

if [[ "${EITRI_STAGE2_NO_MAIN:-0}" != 1 ]]; then main "$@"; fi
