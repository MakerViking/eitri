#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Runs INSIDE the rehearsal VM on the copy's own stock system (as root, from systemd.run, offline: QEMU gives it no
# network).  The "USB stick" is mounted at /mnt/usb.  Does what the user does on the printer: unpack the installer from
# the stick onto the stock system, a dry run, then the real run.  $1 = SHA-256 of the backup file.
GZ="$1"
U=/mnt/usb
exec > >(tee -a "$U/logs/stage1.log" /dev/console) 2>&1
echo "STAGE1: $(date -u +%FT%TZ) kernel $(uname -r) on $(tr -d '\0' </proc/device-tree/model), root $(findmnt -no SOURCE /), boot $(findmnt -no SOURCE /boot)"
echo "STAGE1: python $(python3 -V 2>&1), zstd $(command -v zstd || echo none), curl $(command -v curl || echo none)"
mkdir -p /root/eitri && tar -xzf "$U/dev-bundle/eitri-installer.tar.gz" -C /root/eitri && cd /root/eitri/installer || exit 1
export EITRI_DISK=/dev/vda NO_COLOR=1
before="$(find /var/lib /boot -xdev 2>/dev/null | sort | sha256sum)"
echo "STAGE1: ===== dry run"
t0=$(date +%s)
./install.sh --dry-run --flavor klipper --bundle-dir "$U/dev-bundle" --pubkey "$U/dev-bundle/eitri-dev.pub" \
  --backup-file "$U/mmcblk1.img.gz" --backup-sha256 "$GZ"
echo "STAGE1: dry run exit $? in $(( $(date +%s) - t0 )) s"
after="$(find /var/lib /boot -xdev 2>/dev/null | sort | sha256sum)"
[ "$before" = "$after" ] && echo "STAGE1: dry run left /var/lib and /boot unchanged (no file or directory added or removed)" || echo "STAGE1: DRY RUN CHANGED /var/lib or /boot"
ls -ld /var/lib/eitri-install 2>&1 | sed 's/^/STAGE1: /'
echo "STAGE1: ===== real run"
t0=$(date +%s)
EITRI_REBOOT_DELAY=3 ./install.sh --flavor klipper --i-have-a-backup --yes --bundle-dir "$U/dev-bundle" --pubkey "$U/dev-bundle/eitri-dev.pub" \
  --backup-file "$U/mmcblk1.img.gz" --backup-sha256 "$GZ" --mcu skip --confirm CONVERT --events-file "$U/logs/stage1-events.jsonl"
rc=$?
echo "STAGE1: real run exit $rc after $(( $(date +%s) - t0 )) s"
sync
exit $rc      # success: install.sh has already asked for the reboot into stage 2; failure: systemd.run_failure_action=poweroff
