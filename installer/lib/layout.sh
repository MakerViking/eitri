# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
#
# Partition layout planning for the in-place conversion.  Pure integer maths on
# 512-byte sectors, no device access - so it can be unit-tested and so stage 1
# (dry-run) and stage 2 (the real thing) compute exactly the same plan.
#
# Stock eMMC (dos label):   p1 /boot vfat 256 MiB @ 16 MiB,  p2 / ext4 (rest)
# Converted:
#   p1  /boot       unchanged (shared; holds boot.scr, eitri/boot.env, stage2)
#   p2  stock root  shrunk, kept bootable = instant fallback + revert path
#   p3  extended
#   p5  eitri-rootA   read-only rootfs slot A (ext4)
#   p6  eitri-rootB   read-only rootfs slot B (empty until the first OS update)
#   p7  eitri-data    /data (ext4, rest of the disk)

SECTOR=512
ALIGN=2048            # 1 MiB
ONE_GIB=$((1024 * 1024 * 1024))

_align_up()   { echo $((($1 + ALIGN - 1) / ALIGN * ALIGN)); }
_align_down() { echo $(($1 / ALIGN * ALIGN)); }

# layout_compute DISK_SECTORS P2_START P2_OLD_SIZE FS_MIN_BYTES ROOT_MIB DATA_MIN_MIB
# FS_MIN_BYTES is the smallest size the stock filesystem can shrink to INCLUDING
# anything staged on it.  Prints key=value lines; returns 1 (with a reason on
# stdout as "error=...") when the plan does not fit.
layout_compute() {
  local disk="$1" p2_start="$2" p2_old="$3" fs_min="$4" root_mib="$5" data_min_mib="$6"
  local margin=$((fs_min / 8))
  ((margin < ONE_GIB)) && margin=$ONE_GIB
  local p2_size p2_end ext_start usable
  p2_size="$(_align_up $(((fs_min + margin + SECTOR - 1) / SECTOR)))"
  if ((p2_size >= p2_old)); then
    echo "error=the stock root partition cannot shrink: it needs $((p2_size / 2048)) MiB but is only $((p2_old / 2048)) MiB"
    return 1
  fi
  p2_end=$((p2_start + p2_size))
  ext_start="$(_align_up "$p2_end")"
  usable="$(_align_down $((disk - ALIGN)))"
  local root_sectors=$((root_mib * 2048))
  local p5_start=$((ext_start + ALIGN))
  local p6_start=$((p5_start + root_sectors + ALIGN))
  local p7_start=$((p6_start + root_sectors + ALIGN))
  local p7_size=$((usable - p7_start))
  if ((p7_size < data_min_mib * 2048)); then
    echo "error=not enough room: /data would only get $((p7_size / 2048)) MiB (need $data_min_mib MiB) after keeping the stock system ($((p2_size / 2048)) MiB) and two $root_mib MiB rootfs slots"
    return 1
  fi
  cat <<PLAN
p2_start=$p2_start
p2_size=$p2_size
ext_start=$ext_start
ext_size=$((usable - ext_start))
p5_start=$p5_start
p5_size=$root_sectors
p6_start=$p6_start
p6_size=$root_sectors
p7_start=$p7_start
p7_size=$p7_size
PLAN
}

# layout_sfdisk_script DISK LABEL_ID P1_START P1_SIZE PLAN_FILE [PART_PREFIX]
# PLAN_FILE holds the output of layout_compute.  Keeps p1 and the label-id (so
# PARTUUID=<id>-01/02 stay valid for the stock system).  PART_PREFIX: "p" for /dev/mmcblk1p1 (default), "" for /dev/vda1.
layout_sfdisk_script() {
  local disk="$1" id="$2" p1s="$3" p1z="$4" plan="$5" pp="${6-p}"
  local p2s p2z es ez p5s p5z p6s p6z p7s p7z
  p2s="$(kv_get "$plan" p2_start)"; p2z="$(kv_get "$plan" p2_size)"
  es="$(kv_get "$plan" ext_start)"; ez="$(kv_get "$plan" ext_size)"
  p5s="$(kv_get "$plan" p5_start)"; p5z="$(kv_get "$plan" p5_size)"
  p6s="$(kv_get "$plan" p6_start)"; p6z="$(kv_get "$plan" p6_size)"
  p7s="$(kv_get "$plan" p7_start)"; p7z="$(kv_get "$plan" p7_size)"
  cat <<SCRIPT
label: dos
label-id: $id
device: $disk
unit: sectors

${disk}${pp}1 : start=$p1s, size=$p1z, type=e
${disk}${pp}2 : start=$p2s, size=$p2z, type=83
${disk}${pp}3 : start=$es, size=$ez, type=5
${disk}${pp}5 : start=$p5s, size=$p5z, type=83
${disk}${pp}6 : start=$p6s, size=$p6z, type=83
${disk}${pp}7 : start=$p7s, size=$p7z, type=83
SCRIPT
}

# layout_validate PLAN_FILE DISK_SECTORS P1_START P1_SIZE
# Independent re-check of a plan (also run on the plan read back from disk
# before anything is written).  Prints the first problem and returns 1.
layout_validate() {
  local plan="$1" disk="$2" p1s="$3" p1z="$4"
  local -a name start size
  local k
  for k in p2 ext p5 p6 p7; do
    name+=("$k"); start+=("$(kv_get "$plan" "${k}_start")"); size+=("$(kv_get "$plan" "${k}_size")")
  done
  local i
  for i in "${!name[@]}"; do
    [[ "${start[i]}" =~ ^[0-9]+$ && "${size[i]}" =~ ^[0-9]+$ ]] || { echo "${name[i]}: missing start/size"; return 1; }
    ((size[i] > 0)) || { echo "${name[i]}: empty"; return 1; }
    ((start[i] % ALIGN == 0 && size[i] % ALIGN == 0)) || { echo "${name[i]}: not 1 MiB aligned"; return 1; }
    ((start[i] + size[i] <= disk)) || { echo "${name[i]}: beyond end of disk"; return 1; }
  done
  ((start[0] >= p1s + p1z)) || { echo "p2 overlaps p1"; return 1; }
  ((start[1] >= start[0] + size[0])) || { echo "extended partition overlaps p2"; return 1; }
  # logical partitions need one aligned gap (the EBR) before each and must sit inside the extended partition
  local ext_end=$((start[1] + size[1]))
  local prev_end=${start[1]}
  for i in 2 3 4; do
    ((start[i] >= prev_end + ALIGN)) || { echo "${name[i]}: no room for its EBR"; return 1; }
    ((start[i] + size[i] <= ext_end)) || { echo "${name[i]}: outside the extended partition"; return 1; }
    prev_end=$((start[i] + size[i]))
  done
  return 0
}

# layout_summary PLAN_FILE   human readable, for logs and the dry-run report
layout_summary() {
  local plan="$1" k
  printf '  %-4s %-14s %10s MiB\n' p1 "/boot (kept)" 256
  for k in p2:stock-root p5:eitri-rootA p6:eitri-rootB p7:eitri-data; do
    printf '  %-4s %-14s %10s MiB\n' "${k%%:*}" "${k##*:}" "$(($(kv_get "$plan" "${k%%:*}_size") / 2048))"
  done
}

# inplace_cmdline CMDLINE LABEL_ID
# The OS image mounts /data and the FAT boot partition from kernel arguments (eitri-generator); a GPT flash-once
# disk derives them from eitri.disk=, this dos disk has no partition names, so name them explicitly.
# /data is found by its filesystem label (stage 2 creates it), /boot by the PARTUUID of partition 1.
inplace_cmdline() {
  local c="$1" ptuuid
  ptuuid="$(printf '%08x' "$(($2))")"
  [[ " $c " == *" eitri.data="* ]] || c="$c eitri.data=LABEL=eitri-data"
  [[ " $c " == *" eitri.bootfs="* ]] || c="$c eitri.bootfs=PARTUUID=$ptuuid-01"
  printf '%s' "$c"
}
