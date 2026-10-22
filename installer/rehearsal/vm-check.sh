#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Runs INSIDE the rehearsal check VM (the stage-2 userland booted on the printer's own stock kernel under QEMU, the
# rehearsal copy of the eMMC as /dev/vda).  Installed in place of stage2.sh, so the initramfs' /init starts it.
# Read-only on the disk: e2fsck -n, ro/noload mounts.  Results go out as a tar stream on /dev/vdb.
#
#   /etc/eitri/rig.conf: mode=baseline|after
export PATH=/usr/bin
OUT=/run/rig; mkdir -p "$OUT"
mode="$(sed -n 's/^mode=//p' /etc/eitri/rig.conf 2>/dev/null)"
say() { echo "RIG: $*"; echo "$*" >>"$OUT/summary.txt"; }
say "mode=$mode kernel=$(uname -r) date=$(date -u +%FT%TZ)"
for i in $(seq 1 50); do [ -b /dev/vda ] && break; sleep 0.2; done
sfdisk -d /dev/vda >"$OUT/sfdisk.txt" 2>&1; cat "$OUT/sfdisk.txt"
blkid >"$OUT/blkid.txt" 2>&1; cat "$OUT/blkid.txt"

fsck_part() {   # fsck_part N   (read-only)
  local d=/dev/vda$1
  [ -b "$d" ] || { say "vda$1: absent"; return 0; }
  dumpe2fs -h "$d" >"$OUT/dumpe2fs-vda$1.txt" 2>/dev/null || { say "vda$1: not ext4"; return 0; }
  local t0=$(date +%s) rc=0
  e2fsck -fn "$d" >"$OUT/e2fsck-vda$1.txt" 2>&1 || rc=$?
  say "vda$1: e2fsck -fn exit $rc ($(( $(date +%s) - t0 )) s): $(tail -1 "$OUT/e2fsck-vda$1.txt")"
  say "vda$1: state $(sed -n 's/^Filesystem state: *//p' "$OUT/dumpe2fs-vda$1.txt"), features $(sed -n 's/^Filesystem features: *//p' "$OUT/dumpe2fs-vda$1.txt")"
  say "vda$1: blocks $(sed -n 's/^Block count: *//p' "$OUT/dumpe2fs-vda$1.txt") x $(sed -n 's/^Block size: *//p' "$OUT/dumpe2fs-vda$1.txt"), free $(sed -n 's/^Free blocks: *//p' "$OUT/dumpe2fs-vda$1.txt")"
}

manifest() {    # manifest DIR NAME   sha256 of every regular file, plus symlinks
  local t0=$(date +%s)
  ( cd "$1" && find . -xdev -type f -exec sha256sum {} + ) 2>/dev/null | sort -k2 >"$OUT/$2.sha256"
  ( cd "$1" && find . -xdev -type l -exec stat -c '%N' {} + ) 2>/dev/null | sort >"$OUT/$2.links"
  say "$2: $(wc -l <"$OUT/$2.sha256") files, $(wc -l <"$OUT/$2.links") symlinks hashed in $(( $(date +%s) - t0 )) s"
}

mkdir -p /mnt/p1 /mnt/p2 /mnt/p5 /mnt/p7
fsck_part 2
if [ "$mode" = after ]; then fsck_part 5; fsck_part 7; fi
{ mount -t vfat -o ro /dev/vda1 /mnt/p1 2>/dev/null || mount -t vfat -o ro,iocharset=ascii /dev/vda1 /mnt/p1; } && { manifest /mnt/p1 boot; ls -la /mnt/p1 /mnt/p1/extlinux /mnt/p1/eitri >"$OUT/boot-ls.txt" 2>&1
  for f in eitri/boot.env eitri/layout.env eitri/stage2.state eitri/LAST_ERROR eitri/stage2.log eitri/ptable.stock.sfdisk eitri/layout.plan eitri/stock-changes.env; do
    [ -r "/mnt/p1/$f" ] && { echo "== $f"; cat "/mnt/p1/$f"; } ; done >"$OUT/boot-eitri.txt" 2>&1; umount /mnt/p1; }
# noload: never replay a journal on the copy from here (the stock system / stage 2 do that themselves)
if mount -t ext4 -o ro,noload /dev/vda2 /mnt/p2; then
  df -k /mnt/p2 >"$OUT/df-vda2.txt"
  manifest /mnt/p2 stock
  ls -la /mnt/p2/var/lib/eitri-install /mnt/p2/var/lib/eitri-install/payload >"$OUT/stock-eitri-install.txt" 2>&1
  ls -la /mnt/p2/etc/systemd/system/armbian-resize-filesystem.service >>"$OUT/stock-eitri-install.txt" 2>&1
  umount /mnt/p2
else
  say "vda2: cannot be mounted read-only"
fi
if [ "$mode" = after ]; then
  if mount -t ext4 -o ro,noload /dev/vda7 /mnt/p7; then
    df -k /mnt/p7 >"$OUT/df-vda7.txt"
    { echo "== install.env"; cat /mnt/p7/eitri/state/install.env; echo "== seed-installed.env"; cat /mnt/p7/eitri/state/seed-installed.env
      echo "== release"; cat /mnt/p7/eitri/release; echo "== flavor"; cat /mnt/p7/eitri/flavor; echo "== mcu"; find /mnt/p7/eitri/mcu | head -20
      echo "== migrate"; find /mnt/p7/eitri/migrate | head -60; echo "== rauc override"; cat /mnt/p7/eitri/state/etc/upper/rauc/system.conf
      echo "== top"; ls -la /mnt/p7 /mnt/p7/eitri /mnt/p7/firmware; du -sk /mnt/p7/firmware /mnt/p7/venv /mnt/p7/eitri 2>/dev/null
      echo "== klipper git HEAD"; cat /mnt/p7/firmware/klipper/.git/HEAD 2>/dev/null; } >"$OUT/data.txt" 2>&1
    umount /mnt/p7
  fi
  mount -t ext4 -o ro,noload /dev/vda5 /mnt/p5 && { ls -la /mnt/p5/boot /mnt/p5/boot/dtb/rockchip >"$OUT/slotA-boot.txt" 2>&1; umount /mnt/p5; }
fi
say "done"
tar -cf /dev/vdb -C /run rig 2>/dev/null && sync
echo "RIG-DONE"
poweroff -f
