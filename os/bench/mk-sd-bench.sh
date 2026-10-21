#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the bench microSD image: an Eitri system that the printer's STOCK eMMC U-Boot
# (2021.07-armbian) chain-boots from the internal microSD slot, leaving the eMMC untouched.
#
#   mk-sd-bench.sh [--images DIR] [--rootfs FILE] [--seed ARCHIVE | --no-seed] [--flavor klipper|kalico]
#                  [--output FILE] [--root-mib N] [--boot-mib N] [--headroom-mib N] [--disk-id HEX8]
#                  [--stock-backup FILE.img.gz]
#
# Why the stock U-Boot boots it: its bootcmd is distro_bootcmd with boot_targets="mmc1 mmc0 ...", and on the
# RK3328 mmc1 is the microSD slot, so the card is scanned first. scan_dev_for_boot_part picks the partition with
# the bootable flag (partition 1 here), finds no extlinux.conf and sources /boot.scr: the in-place boot manager
# (installer/boot/boot.cmd), built with installer/boot/mkscr.py. It reads eitri/layout.env and eitri/boot.env from
# the card's own FAT partition and loads /boot/Image and /boot/dtb/${fdtfile} from the card's rootfs slot. The stock
# environment sets fdtfile=rockchip/rk3328-roc-cc.dtb; the image's rootfs carries that name as a link to
# rk3328-eitri-plus4.dtb, so nothing about the stock environment has to change.
#
# When Eitri does not boot (slot out of attempts, nothing loadable) boot.scr ends with a sysboot of
# /extlinux/extlinux.conf.stock, which does not exist on the card: the script fails, distro_bootcmd moves on to mmc0
# and the untouched stock system on the eMMC boots. Pulling the card does the same.
#
# Layout (DOS, the in-place backend's partition numbers; nothing below sector 32768 is written, so there is no
# Rockchip loader on the card and the boot ROM keeps starting the eMMC's):
#
#   p1  16 MiB      vfat "EITRISDBOOT", bootable: boot.scr, eitri/layout.env, eitri/boot.env, README.txt
#   p2  -           unused (on a converted eMMC it is the shrunk stock root; a bench card has none)
#   p3              extended container
#   p5  rootfs A    the built rootfs.ext4, byte for byte
#   p6  rootfs B    empty until the first OS update
#   p7  data        /data built from the data seed by os/board/eitri-plus4/mkdata.sh (label "eitri-sd-data"),
#                   plus the RAUC slot override; grown to the end of the card on the first boot (eitri.growdata=1)
#
# Everything the running system needs is named by this card's own PARTUUIDs (label-id --disk-id, default
# e17a5d01): root=, eitri.data=, eitri.bootfs= and RAUC's slots (/dev/disk/by-partuuid/...). The label-id and the
# file-system identities are checked against the stock eMMC's (from --stock-backup, or the values recorded from
# the backup of the printer this was written for) and the build refuses any collision.
#
# This script only writes the output file and its .sha256. It never opens a block device; write the image to
# a card yourself, e.g.  dd if=eitri-sd-bench.img of=/dev/sdX bs=4M conv=fsync status=progress
#
# Environment: TMPDIR (scratch space, about the size of the data partition).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
BUILD=$(cd "$REPO/.." && pwd)/build
BOARD=$REPO/os/board/eitri-plus4
OVERLAY=$BOARD/rootfs-overlay

IMAGES=$BUILD/br-out/images
ROOTFS=""
SEED=""
NO_SEED=0
FLAVOR=klipper
OUTPUT=$BUILD/bench/eitri-sd-bench.img
ROOT_MIB=768
BOOT_MIB=64
HEADROOM_MIB=128
DISK_ID=e17a5d01
STOCK_BACKUP=""

# The stock eMMC of the printer this was written for (from its backup, mmcblk1.img.gz): DOS label-id, the FAT
# boot partition's volume id and label, the ext4 root's UUID. --stock-backup re-reads them from a backup.
STOCK_LABEL_ID=6a783df4
STOCK_IDS="C085-07AC 5dcae443-0796-4029-ae6c-bf5bd2a37067"
STOCK_LABELS="BOOT"

die() { echo "mk-sd-bench: $*" >&2; exit 1; }
log() { echo "mk-sd-bench: $*"; }

while [ $# -gt 0 ]; do
	case "$1" in
	--images) IMAGES=${2:?}; shift 2 ;;
	--rootfs) ROOTFS=${2:?}; shift 2 ;;
	--seed) SEED=${2:?}; shift 2 ;;
	--no-seed) NO_SEED=1; shift ;;
	--flavor) FLAVOR=${2:?}; shift 2 ;;
	--output) OUTPUT=${2:?}; shift 2 ;;
	--root-mib) ROOT_MIB=${2:?}; shift 2 ;;
	--boot-mib) BOOT_MIB=${2:?}; shift 2 ;;
	--headroom-mib) HEADROOM_MIB=${2:?}; shift 2 ;;
	--disk-id) DISK_ID=${2:?}; shift 2 ;;
	--stock-backup) STOCK_BACKUP=${2:?}; shift 2 ;;
	-h | --help) sed -n '3,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
	*) die "unknown option: $1" ;;
	esac
done

case "$FLAVOR" in klipper | kalico) ;; *) die "--flavor must be klipper or kalico" ;; esac
case "$ROOT_MIB$BOOT_MIB$HEADROOM_MIB" in *[!0-9]* | "") die "--root-mib, --boot-mib and --headroom-mib must be numbers" ;; esac
[ "$BOOT_MIB" -ge 16 ] || die "--boot-mib must be at least 16"
DISK_ID=$(printf '%s' "$DISK_ID" | tr 'A-F' 'a-f')
printf '%s\n' "$DISK_ID" | grep -Eqx '[0-9a-f]{8}' || die "--disk-id must be 8 hex digits"
[ "$DISK_ID" != 00000000 ] || die "--disk-id must not be 0"

for tool in sfdisk mkfs.vfat mcopy mmd mtype mdir mke2fs debugfs dumpe2fs blkid fakeroot python3 sha256sum truncate dd awk cmp; do
	command -v "$tool" >/dev/null 2>&1 || die "$tool not found"
done
export MTOOLS_SKIP_CHECK=1

[ -n "$ROOTFS" ] || ROOTFS=$IMAGES/rootfs.ext4
ROOTFS=$(readlink -f "$ROOTFS")
[ -f "$ROOTFS" ] || die "rootfs image $ROOTFS not found (build it with 'make -C os image')"

work=$(mktemp -d "${TMPDIR:-/tmp}/eitri-sd-bench.XXXXXX")
trap 'rm -rf "$work"' EXIT

# --- stock identities -------------------------------------------------------------------------------
if [ -n "$STOCK_BACKUP" ]; then
	[ -f "$STOCK_BACKUP" ] || die "--stock-backup $STOCK_BACKUP not found"
	log "reading the stock eMMC's identities from $STOCK_BACKUP"
	# The first 280 MiB hold the partition table, the FAT partition and the start of the root partition.
	case "$STOCK_BACKUP" in
	*.gz) { zcat "$STOCK_BACKUP" 2>/dev/null || true; } | head -c $((280 << 20)) >"$work/stock.bin" ;;
	*) head -c $((280 << 20)) "$STOCK_BACKUP" >"$work/stock.bin" ;;
	esac
	STOCK_LABEL_ID=$(sfdisk -d "$work/stock.bin" 2>/dev/null | sed -n 's/^label-id: 0x//p')
	[ -n "$STOCK_LABEL_ID" ] || die "no DOS partition table in $STOCK_BACKUP"
	STOCK_IDS="" STOCK_LABELS=""
	while read -r start; do
		STOCK_IDS="$STOCK_IDS $(blkid -p -O $((start * 512)) -s UUID -o value "$work/stock.bin" 2>/dev/null || true)"
		STOCK_LABELS="$STOCK_LABELS $(blkid -p -O $((start * 512)) -s LABEL -o value "$work/stock.bin" 2>/dev/null || true)"
	done < <(sfdisk -d "$work/stock.bin" | sed -nE 's#.* : start= *([0-9]+),.*#\1#p')
	rm -f "$work/stock.bin"
fi
log "stock eMMC: label-id 0x$STOCK_LABEL_ID, file systems $STOCK_IDS, labels $STOCK_LABELS"
[ "$DISK_ID" != "$(printf '%s' "$STOCK_LABEL_ID" | tr 'A-F' 'a-f')" ] || die "--disk-id $DISK_ID is the stock eMMC's label-id"

# --- the root file system ---------------------------------------------------------------------------
rcat() { debugfs -R "cat $1" "$ROOTFS" 2>/dev/null; }
rtype() { debugfs -R "stat $1" "$ROOTFS" 2>/dev/null | sed -nE 's/.*Type: ([a-z]+).*/\1/p' | head -1; }

[ "$(rtype /boot/Image)" = regular ] || die "$ROOTFS has no /boot/Image"
# boot.cmd loads /boot/dtb/${fdtfile}; the stock environment's fdtfile is rockchip/rk3328-roc-cc.dtb.
FDTFILE=rockchip/rk3328-roc-cc.dtb
case "$(rtype "/boot/dtb/$FDTFILE")" in
regular) ;;
symlink)
	target=$(debugfs -R "stat /boot/dtb/$FDTFILE" "$ROOTFS" 2>/dev/null | sed -nE 's/^Fast link dest: "(.*)"$/\1/p')
	resolved=$(python3 -I -c 'import os, sys; print(os.path.normpath(os.path.join(os.path.dirname(sys.argv[1]), sys.argv[2])))' "/boot/dtb/$FDTFILE" "$target")
	[ "$(rtype "$resolved")" = regular ] || die "/boot/dtb/$FDTFILE in $ROOTFS points at $target, which is not a file"
	log "/boot/dtb/$FDTFILE -> $target ($resolved)"
	;;
*) die "$ROOTFS has no /boot/dtb/$FDTFILE (the stock U-Boot's fdtfile)" ;;
esac

# The image must carry the device-safety fixes of this tree: these are the files that decide which disk the
# running system writes to. An older rootfs.ext4 could repartition or write the wrong disk.
site=$(debugfs -R "ls -p /usr/lib" "$ROOTFS" 2>/dev/null | awk -F/ '$6 ~ /^python3\./ { print "/usr/lib/" $6; exit }')
checks="/usr/libexec/eitri/grow-data=$OVERLAY/usr/libexec/eitri/grow-data
/usr/libexec/eitri/rauc-bootloader=$OVERLAY/usr/libexec/eitri/rauc-bootloader
/usr/libexec/eitri/rauc-pre-install=$OVERLAY/usr/libexec/eitri/rauc-pre-install
/usr/libexec/eitri/mark-good=$OVERLAY/usr/libexec/eitri/mark-good
/usr/libexec/eitri/data-init=$OVERLAY/usr/libexec/eitri/data-init
/usr/lib/systemd/system-generators/eitri-generator=$OVERLAY/usr/lib/systemd/system-generators/eitri-generator
/etc/rauc/system.conf=$OVERLAY/etc/rauc/system.conf"
[ -z "$site" ] || checks="$checks
$site/site-packages/eitri_update/bootenv.py=$REPO/installer/eitri-update/eitri_update/bootenv.py
$site/site-packages/eitri_update/client.py=$REPO/installer/eitri-update/eitri_update/client.py"
stale=""
while IFS== read -r inimage repo; do
	rcat "$inimage" >"$work/check" || true
	cmp -s "$work/check" "$repo" || stale="$stale $inimage"
done <<<"$checks"
[ -z "$stale" ] || die "$ROOTFS was built from an older tree (differs from the repository in:$stale); rebuild it with 'make -C os image'"
[ "$(rtype /usr/libexec/eitri/eitri-bootctl)" = regular ] || die "$ROOTFS has no /usr/libexec/eitri/eitri-bootctl"

# --- partition plan (512-byte sectors, 1 MiB aligned) ----------------------------------------------
ALIGN=2048
mib() { echo $(($1 * ALIGN)); }
up() { echo $((($1 + ALIGN - 1) / ALIGN * ALIGN)); }

rootfs_bytes=$(stat -c %s "$ROOTFS")
[ "$rootfs_bytes" -le $((ROOT_MIB << 20)) ] || die "rootfs.ext4 ($((rootfs_bytes >> 20)) MiB) does not fit a $ROOT_MIB MiB slot"

P1_START=$(mib 16)
P1_SIZE=$(mib "$BOOT_MIB")
EXT_START=$((P1_START + P1_SIZE))
P5_START=$((EXT_START + ALIGN))
P5_SIZE=$(mib "$ROOT_MIB")
P6_START=$((P5_START + P5_SIZE + ALIGN))
P6_SIZE=$P5_SIZE
P7_START=$((P6_START + P6_SIZE + ALIGN))

PU() { printf '%s-%02x' "$DISK_ID" "$1"; }
CMDLINE="$(python3 -I "$REPO/os/scripts/lockget.py" release.install.kernel_cmdline) eitri.data=PARTUUID=$(PU 7) eitri.bootfs=PARTUUID=$(PU 1) eitri.growdata=1"
case " $CMDLINE " in *" console=ttyS0"* | *" console=ttyS2"*) die "the kernel command line puts a console on an MCU UART" ;; esac

# --- data partition: the seed, plus RAUC's slots for this card --------------------------------------
# The image's own system.conf describes the flash-once GPT eMMC (/dev/mmcblk1p2, p3, raw-sector backend). Like the
# in-place conversion (installer/stage2/stage2.sh s2_rauc_override) the card overrides it through the /etc overlay's
# upper layer on /data, with only the slot devices and the boot-state backend changed. The slots are this card's
# PARTUUIDs, so RAUC can never resolve them to the eMMC; rauc-pre-install refuses any other disk anyway.
rcat /etc/rauc/system.conf >"$work/system.conf.image"
grep -q '^\[slot\.rootfs\.' "$work/system.conf.image" || die "$ROOTFS has no RAUC slots in /etc/rauc/system.conf"
mkdir -p "$work/overlay/eitri/state/etc/upper/rauc" "$work/overlay/eitri/state/etc/work"
awk -v a="/dev/disk/by-partuuid/$(PU 5)" -v b="/dev/disk/by-partuuid/$(PU 6)" -v handler=/usr/libexec/eitri/eitri-bootctl '
	FNR == NR { if ($0 ~ /^\[/) s = $0; else if (s ~ /^\[slot\.rootfs\./ && $0 ~ /^bootname=/) { v = $0; sub(/^bootname=[ ]*/, "", v); bn[s] = v } next }
	/^\[/ { s = $0 }
	s ~ /^\[slot\.rootfs\./ && /^device=/ { if (bn[s] == "A") { print "device=" a; next } if (bn[s] == "B") { print "device=" b; next } }
	/^bootloader-custom-backend=/ { print "bootloader-custom-backend=" handler; next }
	{ print }' "$work/system.conf.image" "$work/system.conf.image" >"$work/overlay/eitri/state/etc/upper/rauc/system.conf"
conf=$work/overlay/eitri/state/etc/upper/rauc/system.conf
grep -qx "device=/dev/disk/by-partuuid/$(PU 5)" "$conf" && grep -qx "device=/dev/disk/by-partuuid/$(PU 6)" "$conf" ||
	die "could not map the RAUC slots of $ROOTFS to this card"
grep -qx 'bootloader-custom-backend=/usr/libexec/eitri/eitri-bootctl' "$conf" || die "could not set the RAUC boot-state backend"
grep -qx 'pre-install=/usr/libexec/eitri/rauc-pre-install' "$conf" || die "the RAUC configuration lost its pre-install guard"
! grep -v '^[[:space:]]*#' "$conf" | grep -q 'mmcblk' || die "the card's RAUC configuration still names a fixed mmcblk device"
chmod 0644 "$conf"

mkdata_args=(--output "$work/data.ext4" --label eitri-sd-data --uuid "$DISK_ID-5d01-4b3e-9c41-0e17a1d00007"
	--headroom-mib "$HEADROOM_MIB" --overlay "$work/overlay")
if [ "$NO_SEED" = 1 ]; then
	log "WARNING: --no-seed: /data has no Klipper, Moonraker or Klarity"
	mkdata_args+=(--min-mib 64)
else
	if [ -z "$SEED" ]; then
		seeds=("$BUILD"/seed/out/data-seed-*-"$FLAVOR".tar.zst)
		[ -f "${seeds[0]}" ] || die "no $FLAVOR data seed in $BUILD/seed/out (make seed), or pass --seed / --no-seed"
		[ ${#seeds[@]} -eq 1 ] || die "several $FLAVOR seeds in $BUILD/seed/out; pass --seed"
		SEED=${seeds[0]}
	fi
	manifest=${SEED%.tar.zst}.manifest.json
	[ -f "$SEED" ] && [ -f "$manifest" ] || die "seed $SEED or its manifest $manifest not found"
	mkdata_args+=(--seed "$SEED" --manifest "$manifest" --flavor "$FLAVOR" --installer "$REPO/seed/bin/eitri-seed-install")
	log "seeding /data from $(basename "$SEED")"
fi
# The seed's files belong to uid 1000; fakeroot records that without root (the same as post-image.sh).
fakeroot -- "$BOARD/mkdata.sh" "${mkdata_args[@]}"
data_bytes=$(stat -c %s "$work/data.ext4")
P7_SIZE=$(up $((data_bytes / 512)))
END=$((P7_START + P7_SIZE))
EXT_SIZE=$((END - EXT_START))

# --- boot partition ---------------------------------------------------------------------------------
python3 -I "$REPO/installer/boot/mkscr.py" "$REPO/installer/boot/boot.cmd" "$work/boot.scr"
python3 -I "$REPO/installer/boot/mkscr.py" --check "$work/boot.scr" | cmp -s - "$REPO/installer/boot/boot.cmd" ||
	die "boot.scr does not hold boot.cmd byte for byte"
printf '%s\n' "eitri_part_A=5" "eitri_part_B=6" "eitri_partuuid_A=$(PU 5)" "eitri_partuuid_B=$(PU 6)" \
	"eitri_cmdline=$CMDLINE" >"$work/layout.env"
printf '%s\n' "eitri_mode=eitri" "eitri_slot=A" "eitri_tries=3" "eitri_state_A=unknown" "eitri_state_B=unknown" >"$work/boot.env"
cat >"$work/README.txt" <<EOF
Eitri bench microSD card (os/bench/mk-sd-bench.sh). Developers only.

The printer's stock U-Boot scans the microSD slot first and runs boot.scr from this
partition. It boots Eitri from this card (rootfs slot A = partition 5) and never writes
the eMMC. Pull the card to return to the stock system.

eitri/boot.env   boot state (eitri_mode, eitri_slot, eitri_tries, eitri_state_A/B).
                 Three failed boots of a slot and it is skipped: the card's boot.scr then
                 gives up and the stock system on the eMMC boots. To try again, set
                 eitri_tries=3 and eitri_state_A=unknown. eitri_mode=stock makes the card
                 step aside without removing it.
eitri/layout.env this card's partitions (PARTUUID $DISK_ID-NN) and the kernel arguments.
EOF

truncate -s $((P1_SIZE * 512)) "$work/boot.vfat"
mkfs.vfat -n EITRISDBOOT -i "$DISK_ID" "$work/boot.vfat" >/dev/null
mmd -i "$work/boot.vfat" ::/eitri
mcopy -i "$work/boot.vfat" "$work/boot.scr" ::/boot.scr
mcopy -i "$work/boot.vfat" "$work/layout.env" ::/eitri/layout.env
mcopy -i "$work/boot.vfat" "$work/boot.env" ::/eitri/boot.env
mcopy -i "$work/boot.vfat" "$work/README.txt" ::/README.txt

# --- assemble ---------------------------------------------------------------------------------------
mkdir -p "$(dirname "$OUTPUT")"
img=$OUTPUT.tmp
rm -f "$img" "$OUTPUT" "$OUTPUT.sha256"
truncate -s $((END * 512)) "$img"
sfdisk --quiet "$img" <<EOF
label: dos
label-id: 0x$DISK_ID
unit: sectors

1 : start=$P1_START, size=$P1_SIZE, type=e, bootable
3 : start=$EXT_START, size=$EXT_SIZE, type=5
5 : start=$P5_START, size=$P5_SIZE, type=83
6 : start=$P6_START, size=$P6_SIZE, type=83
7 : start=$P7_START, size=$P7_SIZE, type=83
EOF
put() { dd if="$1" of="$img" bs=1M seek=$(($2 / ALIGN)) conv=notrunc,sparse status=none; }
put "$work/boot.vfat" "$P1_START"
put "$ROOTFS" "$P5_START"
put "$work/data.ext4" "$P7_START"

# --- verify -----------------------------------------------------------------------------------------
dump=$(sfdisk -d "$img")
printf '%s\n' "$dump" | grep -qx "label-id: 0x$DISK_ID" || die "label-id was not written"
for nr in 1 3 5 6 7; do printf '%s\n' "$dump" | grep -Eq "^[^ ]*[^0-9]$nr : " || die "partition $nr is missing"; done
printf '%s\n' "$dump" | grep -E "^[^ ]*[^0-9]1 : " | grep -q bootable || die "partition 1 is not marked bootable"
# Nothing between the MBR and partition 1: no Rockchip loader on the card.
[ "$(dd if="$img" bs=512 skip=1 count=$((P1_START - 1)) status=none | tr -d '\000' | wc -c)" = 0 ] || die "data before partition 1"
probe() { blkid -p -O $(($2 * 512)) -s "$1" -o value "$img" 2>/dev/null || true; }
[ "$(probe TYPE "$P1_START")" = vfat ] && [ "$(probe TYPE "$P5_START")" = ext4 ] && [ "$(probe TYPE "$P7_START")" = ext4 ] ||
	die "a file system is not where the partition table says"
[ "$(probe LABEL "$P7_START")" = eitri-sd-data ] || die "the data partition has the wrong label"
ids="$(probe UUID "$P1_START") $(probe UUID "$P5_START") $(probe UUID "$P7_START")"
labels="$(probe LABEL "$P1_START") $(probe LABEL "$P5_START") $(probe LABEL "$P7_START")"
[ "$(echo "$ids" | wc -w)" = 3 ] || die "could not read the file-system ids of the new image ($ids)"
for id in $ids; do
	for s in $STOCK_IDS; do [ "$(printf '%s' "$id" | tr 'a-f' 'A-F')" != "$(printf '%s' "$s" | tr 'a-f' 'A-F')" ] || die "file system id $id collides with the stock eMMC"; done
done
for l in $labels; do
	for s in $STOCK_LABELS; do [ "$l" != "$s" ] || die "label $l collides with the stock eMMC"; done
done
dd if="$img" bs=512 skip="$P5_START" count=$((rootfs_bytes / 512)) status=none | cmp -s - "$ROOTFS" || die "rootfs slot A differs from $ROOTFS"
for f in boot.scr eitri/layout.env eitri/boot.env; do
	mtype -i "$img@@$((P1_START * 512))" "::/$f" | cmp -s - "$work/$(basename "$f")" || die "$f on partition 1 is not what was written"
done
if mdir -i "$img@@$((P1_START * 512))" ::/extlinux >/dev/null 2>&1; then die "partition 1 must not have an extlinux directory"; fi

mv "$img" "$OUTPUT"
img=$OUTPUT
(cd "$(dirname "$OUTPUT")" && sha256sum "$(basename "$OUTPUT")" >"$(basename "$OUTPUT").sha256")

log "wrote $OUTPUT ($(($(stat -c %s "$OUTPUT") >> 20)) MiB), sha256 $(cut -d' ' -f1 "$OUTPUT.sha256")"
log "label-id 0x$DISK_ID; file systems: boot $(probe UUID "$P1_START") root-A $(probe UUID "$P5_START") data $(probe UUID "$P7_START")"
sfdisk -l "$OUTPUT" | sed 's/^/  /'
log "kernel arguments: root=PARTUUID=$(PU 5) ro rootwait rootfstype=ext4 eitri.slot=A $CMDLINE"
