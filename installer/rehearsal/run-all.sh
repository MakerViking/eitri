#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# The whole in-place rehearsal, in order, on a FRESH copy of a printer's eMMC backup (see rehearse.sh for each phase).
#
#   installer/rehearsal/run-all.sh BACKUP.img.gz [WORK_DIR] [BUNDLE_DIR]
#
# WORK_DIR (default ../build/rehearsal) receives the copy (scratch.img, sparse; about 12-17 GB on disk for a 29 GiB
# eMMC), the USB-stick image and logs/.  The backup itself is only read.  Nothing touches a printer or a block device.
# About 30 minutes on a 24-core PC (most of it stage 1 checking every byte of the backup under emulation).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BACKUP="${1:?usage: run-all.sh BACKUP.img.gz [WORK_DIR] [BUNDLE_DIR]}"
WORK="${2:-$(cd "$REPO/.." && pwd)/build/rehearsal}"
BUNDLE="${3:-$(cd "$REPO/.." && pwd)/build/dev-bundle}"
R=("$HERE/rehearse.sh" --scratch "$WORK/scratch.img" --backup "$BACKUP" --bundle "$BUNDLE" --work "$WORK")
mkdir -p "$WORK/logs"
log() { printf '[run-all %s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$WORK/logs/rehearse.log" >&2; }

[[ -r "$BACKUP.sha256" ]] && { log "checking $BACKUP against $(basename "$BACKUP").sha256"; (cd "$(dirname "$BACKUP")" && sha256sum -c --quiet "$(basename "$BACKUP").sha256" 2>/dev/null) ||
  [[ "$(sha256sum <"$BACKUP" | cut -d' ' -f1)" == "$(cut -d' ' -f1 "$BACKUP.sha256")" ]] || { log "the backup does not match its .sha256"; exit 1; }; }
log "fresh copy of the backup -> $WORK/scratch.img (sparse)"
rm -f "$WORK/scratch.img"
gzip -dc "$BACKUP" | dd of="$WORK/scratch.img" bs=1M conv=sparse iflag=fullblock status=none
rm -f "$WORK/usb.img"

[[ -d "$WORK/check-baseline" ]] || "${R[@]}" baseline
"${R[@]}" usb stage1 check-pre
UBOOT_TAG="1. after stage 1: U-Boot starts stage 2" "${R[@]}" uboot
"${R[@]}" stage2
UBOOT_TAG="2. first boot after stage 2 (attempt 1 of slot A)" "${R[@]}" uboot
UBOOT_TAG="3. slot A did not confirm (attempt 2)" "${R[@]}" uboot
UBOOT_TAG="4. slot A did not confirm (attempt 3)" "${R[@]}" uboot
UBOOT_TAG="5. attempts used up: fall back to stock" "${R[@]}" uboot
"${R[@]}" check
TAG=stockboot-fallback "${R[@]}" stockboot
TAG=revert-hard RUN='cd /root/eitri/installer && EITRI_DISK=/dev/vda NO_COLOR=1 ./revert.sh --hard --yes --confirm REVERT --no-reboot > /mnt/usb/logs/revert-hard.log 2>&1; echo REVERT_RC=$? >> /mnt/usb/logs/revert-hard.log; sync' "${R[@]}" stockboot
UBOOT_TAG="6. after revert.sh --hard" "${R[@]}" uboot
TAG=stockboot-after-revert "${R[@]}" stockboot
TAG=revert-to-eitri RUN='cd /root/eitri/installer && EITRI_DISK=/dev/vda NO_COLOR=1 ./revert.sh --to-eitri --yes --confirm REVERT --no-reboot > /mnt/usb/logs/revert-to-eitri.log 2>&1; echo REVERT_RC=$? >> /mnt/usb/logs/revert-to-eitri.log; sync' "${R[@]}" stockboot
UBOOT_TAG="7. after revert.sh --to-eitri" "${R[@]}" uboot
TAG=eitriboot "${R[@]}" eitriboot
UBOOT_TAG="8. second Eitri boot" "${R[@]}" uboot
TAG=eitriboot2 "${R[@]}" eitriboot
UBOOT_TAG="9. third Eitri boot (attempts must be back to 3 before it)" "${R[@]}" uboot

export MTOOLS_SKIP_CHECK=1
mkdir -p "$WORK/logs/usb"
for f in stage1.log stage1-events.jsonl revert-hard.log revert-to-eitri.log; do mcopy -o -n -i "$WORK/usb.img" "::/logs/$f" "$WORK/logs/usb/" 2>/dev/null || true; done
python3 -I "$HERE/compare-manifests.py" "$WORK/check-prestage2/rig" "$WORK/check-after/rig" >"$WORK/logs/compare-prestage2-vs-after.txt" || true
python3 -I "$HERE/compare-manifests.py" "$WORK/check-baseline/rig" "$WORK/check-after/rig" >"$WORK/logs/compare-baseline-vs-after.txt" || true
log "done; results in $WORK/logs"
