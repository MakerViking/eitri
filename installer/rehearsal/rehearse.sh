#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Rehearse the in-place install against a COPY of a real printer's eMMC, on this PC, without root and without the printer.
#
#   installer/rehearsal/rehearse.sh --scratch build/rehearsal/scratch.img --bundle build/dev-bundle \
#        --backup downloads/emmc-backup/mmcblk1.img.gz --work build/rehearsal  PHASE...
#
# The copy is a raw image (decompressed backup, sparse).  Everything runs on it, never on a device:
#   * QEMU (machine virt, 4 x Cortex-A53, 1 GiB) boots the printer's OWN stock kernel (5.16.20-rockchip64, from the copy's
#     /boot) with the copy as /dev/vda.  The device tree is QEMU's own with the board's compatible string, so install.sh's
#     board check sees a Firefly ROC-RK3328-CC.  The copy is /dev/vda instead of /dev/mmcblk1 (EITRI_DISK=/dev/vda).
#   * A real U-Boot 2021.07 (sandbox build; the printer's U-Boot is 2021.07-armbian) runs the boot.scr that stage 1 wrote,
#     against the copy's partition 1, with booti replaced by an echo (the sandbox cannot start an arm64 kernel): it decides,
#     counts and loads exactly as on the printer, and prints the kernel arguments it would boot with.
#
# Phases (in order; each appends to $WORK/logs):
#   baseline    check VM: e2fsck -n and a SHA-256 manifest of the untouched copy (stock root + /boot)
#   usb         build the "USB stick" image: the bundle, the installer and the backup file (FAT32)
#   stage1      boot the copy's stock system; run install.sh --dry-run, then the real install.sh (offline, --bundle-dir,
#               --backup-file, --mcu skip, --confirm CONVERT); it arms stage 2 and reboots
#   uboot       one U-Boot run of the copy's boot.scr (prints what it boots)
#   stage2      boot exactly what U-Boot chose for stage 2 (stock kernel + /eitri/stage2/initramfs.cpio.gz from the copy)
#   check-pre   the same check after stage 1, before stage 2 (what stage 2 must preserve)
#   check       check VM after the conversion: e2fsck -n of p2/p5/p7, manifests to compare with the baseline
#   stockboot   boot the stock system of the copy as U-Boot's stock entry would (kernel/initrd/append of extlinux.conf.stock
#               or extlinux.conf), to multi-user; RUN='cmd' runs one command instead (systemd.run) and powers off
#   eitriboot   boot slot A as U-Boot chose it (root=PARTUUID=...), with the QEMU build of the same kernel (the printer's
#               kernel has no virtio drivers), to multi-user, and run checks in a root debug shell on the serial port
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$INSTALLER/.." && pwd)"
BUILD="$(cd "$REPO/.." && pwd)/build"
SCRATCH=""; BUNDLE="$BUILD/dev-bundle"; BACKUP=""; WORK="$BUILD/rehearsal"
UBOOT="${EITRI_UBOOT_SANDBOX:-$BUILD/uboot-sandbox/u-boot-2021.07/u-boot}"
QEMU_IMAGES="${QEMU_IMAGES:-$BUILD/br-qemu/images}"
PHASES=()
while (($#)); do
  case "$1" in
    --scratch) SCRATCH="${2:?}"; shift ;;
    --bundle) BUNDLE="${2:?}"; shift ;;
    --backup) BACKUP="${2:?}"; shift ;;
    --work) WORK="${2:?}"; shift ;;
    -h | --help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    -*) echo "rehearse: unknown option $1" >&2; exit 2 ;;
    *) PHASES+=("$1") ;;
  esac
  shift
done
[[ -n "$SCRATCH" && -f "$SCRATCH" ]] || { echo "rehearse: --scratch IMAGE (the decompressed copy) is required" >&2; exit 2; }
mkdir -p "$WORK/logs" "$WORK/stockboot"
LOGS="$WORK/logs"
export MTOOLS_SKIP_CHECK=1
P1="$SCRATCH@@$((32768 * 512))"
log() { printf '[rehearse %s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOGS/rehearse.log" >&2; }

# The board's compatible on QEMU's own device tree (the machine options must match the ones qemu_stock uses).
dtb() {
  local f="$WORK/virt-roc.dtb"
  if [[ ! -s "$f" ]]; then
    qemu-system-aarch64 -M virt,gic-version=2,dumpdtb="$WORK/virt.dtb" -cpu cortex-a53 -smp 4 -m 1024 -display none >/dev/null 2>&1
    dtc -q -I dtb -O dtb -o "$f" "$WORK/virt.dtb"
    fdtput -t s "$f" / compatible "firefly,roc-rk3328-cc" "rockchip,rk3328"
    fdtput -t s "$f" / model "QEMU virt as Firefly ROC-RK3328-CC (Eitri rehearsal)"
  fi
  printf '%s' "$f"
}

# Files the printer's U-Boot would load from the copy's /boot (read from the copy each time: stage 1 changes /boot).
p1_get() { mcopy -o -n -i "$P1" "::$1" "$2"; }

# qemu_stock LOGFILE KERNEL INITRD APPEND [extra -drive files before the copy...]
qemu_stock() {
  local logf="$1" kernel="$2" initrd="$3" append="$4"; shift 4
  local args=(-M virt,gic-version=2 -dtb "$(dtb)" -cpu cortex-a53 -smp 4 -m 1024 -accel tcg,thread=multi -display none -monitor none
              -kernel "$kernel" -append "$append" -no-reboot -serial "file:$logf")
  [[ -n "$initrd" ]] && args+=(-initrd "$initrd")
  local i=1 f
  for f in "$@"; do args+=(-drive "file=$f,if=none,id=x$i,format=raw" -device "virtio-blk-device,drive=x$i"); i=$((i + 1)); done
  # virtio-mmio devices are probed in reverse order: the copy, given last, is /dev/vda
  # The check VM attaches it read-only: a read-only *mount* is not enough, ext4 still runs its orphan cleanup on a writable
  # device (found the hard way: the first check of the copy freed 350 MB of orphaned blocks without replaying the journal).
  local ro=""; [[ "${QEMU_RO:-0}" == 1 ]] && ro=",readonly=on"
  args+=(-drive "file=$SCRATCH,if=none,id=emmc,format=raw,cache=writeback$ro" -device virtio-blk-device,drive=emmc)
  echo "qemu-system-aarch64 ${args[*]}" >>"$LOGS/qemu-commands.log"
  if [[ "${QEMU_BG:-0}" == 1 ]]; then qemu-system-aarch64 "${args[@]}" & echo $! >"$WORK/qemu.pid"; else qemu-system-aarch64 "${args[@]}"; fi
}

# wait_for_line LOG REGEX SECONDS  (then the caller stops QEMU)
wait_for_line() {
  local w=0
  until grep -aq -E "$2" "$1" 2>/dev/null; do
    kill -0 "$(cat "$WORK/qemu.pid")" 2>/dev/null || return 1
    ((w < $3)) || return 1
    sleep 5; w=$((w + 5))
  done
}
stop_qemu() { local pid; pid="$(cat "$WORK/qemu.pid")"; kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }

rig_initrd() {  # rig_initrd MODE -> path of an initramfs = the bundle's stage-2 userland + vm-check.sh as stage2.sh
  local out="$WORK/rig-$1.cpio.gz"
  printf 'mode=%s\n' "$1" >"$WORK/rig.conf"
  python3 -I "$INSTALLER/tools/mkcpio.py" "$WORK/rig-extra.cpio.gz" --file "usr/lib/eitri/stage2.sh=$HERE/vm-check.sh:0755" \
    --file "etc/eitri/rig.conf=$WORK/rig.conf:0644"
  cat "$BUNDLE/stage2-initramfs.cpio.gz" "$WORK/rig-extra.cpio.gz" >"$out"
  printf '%s' "$out"
}

stock_kernel() { p1_get /Image "$WORK/stockboot/Image"; printf '%s' "$WORK/stockboot/Image"; }

phase_check_vm() {  # phase_check_vm MODE
  local mode="$1" out="$WORK/rig-$1.out.img" t0
  rm -f "$out"; truncate -s 64M "$out"
  t0=$(date +%s)
  QEMU_RO=1 qemu_stock "$LOGS/check-$mode.serial.log" "$(stock_kernel)" "$(rig_initrd "$mode")" "console=ttyAMA0 rdinit=/init panic=10 loglevel=4" "$out"
  rm -rf "$WORK/check-$mode"; mkdir -p "$WORK/check-$mode"
  tar -xf "$out" -C "$WORK/check-$mode" && rm -f "$out"
  log "check VM ($mode) took $(( $(date +%s) - t0 )) s"
  sed 's/^/    /' "$WORK/check-$mode/rig/summary.txt" | tee -a "$LOGS/rehearse.log" >&2
}

phase_usb() {
  [[ -n "$BACKUP" && -r "$BACKUP" ]] || { echo "rehearse usb: --backup FILE is required" >&2; exit 2; }
  local img="$WORK/usb.img" need
  need=$(( ( $(stat -c %s "$BACKUP") + $(du -sb "$BUNDLE" | cut -f1) ) / 1048576 + 512 ))
  rm -f "$img"; truncate -s "${need}M" "$img"
  mkfs.vfat -F 32 -n EITRI-USB "$img" >/dev/null
  mmd -i "$img" ::/dev-bundle ::/logs
  mcopy -i "$img" "$BUNDLE"/* ::/dev-bundle/
  mcopy -i "$img" "$BACKUP" ::/mmcblk1.img.gz
  [[ -r "$BACKUP.sha256" ]] && mcopy -i "$img" "$BACKUP.sha256" ::/mmcblk1.img.gz.sha256
  mcopy -i "$img" "$HERE/stage1-in-vm.sh" ::/stage1-in-vm.sh
  log "USB stick image $img (${need} MiB, FAT32): bundle + installer + backup $(basename "$BACKUP")"
}

phase_stage1() {
  local img="$WORK/usb.img" t0 gz
  [[ -r "$img" ]] || phase_usb
  gz="$(cut -d' ' -f1 "$BACKUP.sha256")"
  p1_get /extlinux/extlinux.conf "$WORK/stockboot/extlinux.conf"
  local append; append="$(awk '$1 == "append" {$1 = ""; sub(/^ /, ""); print; exit}' "$WORK/stockboot/extlinux.conf")"
  p1_get /uInitrd "$WORK/stockboot/uInitrd"
  dd if="$WORK/stockboot/uInitrd" of="$WORK/stockboot/initrd" bs=64 skip=1 status=none        # what U-Boot hands the kernel
  t0=$(date +%s)
  qemu_stock "$LOGS/stage1.serial.log" "$(stock_kernel)" "$WORK/stockboot/initrd" \
    "$append console=ttyAMA0 systemd.run=\"/bin/sh -c 'mkdir -p /mnt/usb && mount /dev/vdb /mnt/usb && exec /bin/bash /mnt/usb/stage1-in-vm.sh $gz'\" systemd.run_success_action=none systemd.run_failure_action=poweroff" "$img"
  log "stage 1 VM ran $(( $(date +%s) - t0 )) s"
  mcopy -o -n -i "$img" ::/logs "$LOGS/stage1-usb" 2>/dev/null || true
  tail -5 "$LOGS/stage1.serial.log" | sed 's/^/    /' >&2
}

uboot_run() {  # uboot_run TAG [hang]  - one boot.scr run; prints U-Boot's console
  local tag="$1" hang="${2:-}" scr="$WORK/uboot/boot.scr" sim="$WORK/uboot/sim.img"
  mkdir -p "$WORK/uboot"
  local env="host bind 0 $SCRATCH; setenv devtype host; setenv devnum 0; setenv distro_bootpart 1; \
setenv pxefile_addr_r 0x00600000; setenv scriptaddr 0x00500000; setenv kernel_addr_r 0x02080000; setenv fdt_addr_r 0x01f00000; \
setenv ramdisk_addr_r 0x06000000; setenv fdtfile rockchip/rk3328-roc-cc.dtb"
  echo "== $tag" >>"$LOGS/uboot.log"
  # distro boot order of the stock U-Boot: /extlinux/extlinux.conf first, boot.scr only when there is none
  if mtype -i "$P1" ::/extlinux/extlinux.conf >/dev/null 2>&1; then
    echo "DISTRO extlinux.conf: present -> sysboot (boot.scr is not run)" | tee -a "$LOGS/uboot.log"
    timeout 120 "$UBOOT" -c "$env; echo SYSBOOT host 0:1 /extlinux/extlinux.conf; sysboot host 0:1 any \${scriptaddr} /extlinux/extlinux.conf" 2>&1 |
      tr -d '\r' | sed -n '/^SYSBOOT/,$p' | tee -a "$LOGS/uboot.log"
    return 0
  fi
  p1_get /boot.scr "$scr" 2>/dev/null || { log "neither extlinux.conf nor boot.scr on the copy: U-Boot would find nothing to boot"; return 1; }
  python3 -I "$INSTALLER/boot/mkscr.py" --check "$scr" >"$WORK/uboot/boot.cmd.from-copy"
  cmp -s "$WORK/uboot/boot.cmd.from-copy" "$INSTALLER/boot/boot.cmd" || log "WARNING: the copy's boot.scr is not this checkout's boot.cmd"
  # booti cannot run in the sandbox: echo what it would start, then reset right there - a booti that works never returns,
  # and every Eitri boot in this rehearsal is a kernel that does not confirm itself from U-Boot's point of view
  local after="; reset"
  sed -E "s/^(\s*)booti (.*)$/\1echo \"BOOTI bootargs=[\${bootargs}] ARGS: \2 filesize=\${filesize}\"$after/; s/^sysboot (.*)$/echo SYSBOOT \1; sysboot \1/" \
    "$WORK/uboot/boot.cmd.from-copy" >"$WORK/uboot/boot.cmd.sim"
  python3 -I "$INSTALLER/boot/mkscr.py" "$WORK/uboot/boot.cmd.sim" "$WORK/uboot/boot.scr.sim"
  rm -f "$sim"; truncate -s 8M "$sim"; mkfs.vfat -n SIM "$sim" >/dev/null; mcopy -i "$sim" "$WORK/uboot/boot.scr.sim" ::/boot.scr
  # The distro-boot environment the stock U-Boot hands boot.scr (devtype/devnum/distro_bootpart, the *_addr_r of rk3328)
  local cmd="$env; host bind 1 $sim; \
echo DISTRO extlinux.conf: ; if test -e host 0:1 /extlinux/extlinux.conf; then echo present; else echo absent; fi; \
echo DISTRO boot.scr: ; if test -e host 0:1 /boot.scr; then echo present; else echo absent; fi; \
load host 1 \${scriptaddr} /boot.scr; source \${scriptaddr}"
  { timeout 120 "$UBOOT" -c "$cmd" 2>&1 || true; } | tr -d '\r' | sed -n '/^DISTRO/,$p' | tee -a "$LOGS/uboot.log"
  echo "-- boot.env after:" >>"$LOGS/uboot.log"; mtype -i "$P1" ::/eitri/boot.env 2>/dev/null | tr -d '\0' | tee -a "$LOGS/uboot.log"
}

phase_uboot() { uboot_run "${UBOOT_TAG:-uboot}" "${UBOOT_HANG:-}"; }

phase_stage2() {
  local t0 args
  args="$(grep -a 'BOOTI bootargs=' "$LOGS/uboot.log" | tail -1 | sed 's/.*bootargs=\[\([^]]*\)\].*/\1/')"
  [[ "$args" == *eitri.op=convert* ]] || { log "the last U-Boot run did not start stage 2"; return 1; }
  p1_get /eitri/stage2/initramfs.cpio.gz "$WORK/stage2-initramfs.from-copy.cpio.gz"
  log "stage 2: stock kernel /Image + /eitri/stage2/initramfs.cpio.gz ($(stat -c %s "$WORK/stage2-initramfs.from-copy.cpio.gz") B) from the copy, args: $args"
  t0=$(date +%s)
  qemu_stock "$LOGS/stage2.serial.log" "$(stock_kernel)" "$WORK/stage2-initramfs.from-copy.cpio.gz" "$args console=ttyAMA0"
  log "stage 2 VM ran $(( $(date +%s) - t0 )) s (until its reboot)"
  mtype -i "$P1" ::/eitri/stage2.log 2>/dev/null | tr -d '\0' >"$LOGS/stage2.log" || true
  tail -3 "$LOGS/stage2.log" | sed 's/^/    /' >&2
}

phase_stockboot() {
  local conf=/extlinux/extlinux.conf t0 append run="${RUN:-}" tag="${TAG:-stockboot}"
  mtype -i "$P1" ::/extlinux/extlinux.conf >/dev/null 2>&1 || conf=/extlinux/extlinux.conf.stock
  p1_get "$conf" "$WORK/stockboot/extlinux.cur"
  append="$(awk '$1 == "append" {$1 = ""; sub(/^ /, ""); print; exit}' "$WORK/stockboot/extlinux.cur")"
  p1_get "$(awk '$1 == "initrd" {print $2; exit}' "$WORK/stockboot/extlinux.cur")" "$WORK/stockboot/uInitrd"
  dd if="$WORK/stockboot/uInitrd" of="$WORK/stockboot/initrd" bs=64 skip=1 status=none
  local extra=() a="$append console=ttyAMA0"
  if [[ -n "$run" ]]; then
    a="$a systemd.run=\"/bin/sh -c 'mkdir -p /mnt/usb && mount /dev/vdb /mnt/usb && $run'\" systemd.run_success_action=poweroff systemd.run_failure_action=poweroff"
    extra=("$WORK/usb.img")
  else
    a="$a systemd.show_status=1"
  fi
  log "$tag: stock entry from $conf: $append"
  t0=$(date +%s)
  if [[ -n "$run" ]]; then
    qemu_stock "$LOGS/$tag.serial.log" "$(stock_kernel)" "$WORK/stockboot/initrd" "$a" "${extra[@]}"
  else
    # boot to multi-user; stop QEMU once the login prompt shows (or after 15 minutes)
    rm -f "$LOGS/$tag.serial.log"
    QEMU_BG=1 qemu_stock "$LOGS/$tag.serial.log" "$(stock_kernel)" "$WORK/stockboot/initrd" "$a"
    wait_for_line "$LOGS/$tag.serial.log" "login:" 900 || log "$tag: no login prompt"
    sleep 15; stop_qemu
  fi
  log "$tag: $(( $(date +%s) - t0 )) s; $(grep -a -c -E 'Reached target.*Multi-User|login:' "$LOGS/$tag.serial.log") multi-user/login line(s)"
}

phase_eitriboot() {
  local args t0 sock="$WORK/eitri-serial.sock" tag="${TAG:-eitriboot}"
  args="$(grep -a 'BOOTI bootargs=\[root=PARTUUID' "$LOGS/uboot.log" | tail -1 | sed 's/.*bootargs=\[\([^]]*\)\].*/\1/')"
  [[ -n "$args" ]] || { log "the last U-Boot run did not boot a slot"; return 1; }
  [[ -r "$QEMU_IMAGES/Image" ]] || { log "no QEMU kernel in $QEMU_IMAGES (os/qemu/build.sh)"; return 1; }
  log "$tag: kernel arguments from U-Boot: $args"
  log "$tag: kernel = $QEMU_IMAGES/Image (the same 6.18 kernel + virtio/PL011, os/qemu); root, /data and /boot/firmware from the copy"
  log "$tag: the console stays on tty1 as on the printer; the serial port only carries a root debug shell (systemd.debug_shell)"
  rm -f "$sock" "$LOGS/$tag.serial.log"
  t0=$(date +%s)
  qemu-system-aarch64 -M virt,gic-version=3 -cpu cortex-a53 -smp 4 -m 1024 -accel tcg,thread=multi -display none -monitor none \
    -kernel "$QEMU_IMAGES/Image" -append "$args systemd.debug_shell=ttyAMA0" -no-reboot \
    -drive "file=$SCRATCH,if=none,id=emmc,format=raw,cache=writeback" -device virtio-blk-device,drive=emmc \
    -chardev "socket,id=ser0,path=$sock,server=on,wait=off,logfile=$LOGS/$tag.serial.log" -serial chardev:ser0 &
  local qpid=$!
  python3 -I "$HERE/serial-checks.py" "$sock" "$LOGS/$tag.shell.log" | tee "$LOGS/$tag.checks.txt" || true
  wait "$qpid" || true
  log "$tag: $(( $(date +%s) - t0 )) s"
}

for p in "${PHASES[@]}"; do
  log "=== phase $p"
  case "$p" in
    baseline) phase_check_vm baseline ;;
    check) phase_check_vm after ;;
    check-pre) phase_check_vm prestage2 ;;
    usb) phase_usb ;;
    stage1) phase_stage1 ;;
    uboot) phase_uboot ;;
    stage2) phase_stage2 ;;
    stockboot) phase_stockboot ;;
    eitriboot) phase_eitriboot ;;
    *) echo "rehearse: unknown phase $p" >&2; exit 2 ;;
  esac
done
