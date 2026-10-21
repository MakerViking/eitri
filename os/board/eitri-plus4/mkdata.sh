#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the ext4 image of the /data partition (data.ext4) for the flash-once disk image.
#
#   mkdata.sh --output FILE [--seed ARCHIVE --manifest FILE [--flavor klipper|kalico]]
#             [--installer PATH] [--owner UID:GID] [--headroom-mib N] [--min-mib N]
#
# With a data seed the partition leaves the factory populated: the seed is installed into a scratch
# directory with eitri-seed-install (seed/SPEC.md, "Installing a seed"), then mke2fs -d turns that
# directory into the file system. The first boot therefore does not unpack or download anything and
# the services start as soon as the configurator has rendered their configuration. Without --seed the
# result is an empty file system of --min-mib (a bench image for OS work only).
#
# Why at image-build time and not on the first boot (seed/SPEC.md 11.1 offers both): the 55 MiB
# archive would otherwise sit in BOTH RAUC root slots (110 MiB of a 640 MiB file system that already
# holds 325 MiB), every flash would carry it twice, and the first boot would spend its first minute
# unpacking 380 MiB on a Cortex-A53 before Klipper could start. Here it costs only the larger data
# partition of the flash-once image. A RAUC bundle never contains the seed (the root file system is
# the only thing it carries; seeds follow Klipper through data bundles, decision 2).
#
# Ownership: every file of the seed must belong to 1000:1000 (the eitri user), so this has to run as
# root or under fakeroot, as post-image.sh does; the whole tree is created and turned into the file
# system inside one fakeroot session. The script checks the result and refuses a mis-owned tree. The
# unit tests run it unprivileged with --owner none.
#
# Environment: SOURCE_DATE_EPOCH (pins the file system's timestamps), TMPDIR.
set -euo pipefail

OUTPUT="" SEED="" MANIFEST="" FLAVOR=klipper INSTALLER="" OWNER=1000:1000 HEADROOM_MIB=128 MIN_MIB=128

# The same ids as before, so that the image differs between builds only by its content.
FS_UUID=7b1f9f0e-3a5c-4f6e-9c41-0e17a1d20004
HASH_SEED=7b1f9f0e-3a5c-4f6e-9c41-0e17a1d30004

die() { echo "mkdata: $*" >&2; exit 1; }
log() { echo "mkdata: $*"; }

while [ $# -gt 0 ]; do
	case "$1" in
	--output) OUTPUT=${2:?}; shift 2 ;;
	--seed) SEED=${2:?}; shift 2 ;;
	--manifest) MANIFEST=${2:?}; shift 2 ;;
	--flavor) FLAVOR=${2:?}; shift 2 ;;
	--installer) INSTALLER=${2:?}; shift 2 ;;
	--owner) OWNER=${2:?}; shift 2 ;;
	--headroom-mib) HEADROOM_MIB=${2:?}; shift 2 ;;
	--min-mib) MIN_MIB=${2:?}; shift 2 ;;
	-h | --help) sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
	*) die "unknown option: $1" ;;
	esac
done
[ -n "$OUTPUT" ] || die "--output FILE is required"
case "$FLAVOR" in klipper | kalico) ;; *) die "--flavor must be klipper or kalico" ;; esac
case "$HEADROOM_MIB$MIN_MIB" in *[!0-9]* | "") die "--headroom-mib and --min-mib must be numbers" ;; esac
command -v mke2fs >/dev/null 2>&1 || die "mke2fs not found (host-e2fsprogs)"

if [ -z "$INSTALLER" ]; then
	INSTALLER=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/seed/bin/eitri-seed-install
fi

root=$(mktemp -d "${TMPDIR:-/tmp}/eitri-data.XXXXXX")
trap 'rm -rf "$root"' EXIT
chmod 0755 "$root"

if [ -n "$SEED" ]; then
	[ -f "$SEED" ] || die "seed archive $SEED not found"
	[ -f "$MANIFEST" ] || die "--manifest FILE is required with --seed"
	[ -x "$INSTALLER" ] || die "installer $INSTALLER is not executable"
	for tool in zstd tar sha256sum python3; do
		command -v "$tool" >/dev/null 2>&1 || die "$tool not found (needed to unpack the seed)"
	done
	# The digest comes from the manifest that sits next to the archive in the same build; the installer
	# checks it against the archive before reading anything else, and cross-checks name and flavor.
	sha=$(python3 -I -c '
import json, re, sys
sha = json.load(open(sys.argv[1]))["archive"]["sha256"]
if not re.fullmatch(r"[0-9a-f]{64}", sha):
    raise SystemExit("archive.sha256 is not a SHA-256")
print(sha)
' "$MANIFEST") || die "cannot read archive.sha256 from $MANIFEST"
	owner_args=()
	[ "$OWNER" = none ] || owner_args=(--owner "$OWNER")
	log "installing $(basename "$SEED") into the data tree"
	"$INSTALLER" --archive "$SEED" --sha256 "$sha" --manifest "$MANIFEST" --flavor "$FLAVOR" \
		--data "$root" --mode install -q "${owner_args[@]}"

	# The installer creates the parents of the components as the running user; they belong to eitri
	# like the ones eitri-data-init makes on a blank partition ("eitri" itself stays root's).
	if [ "$OWNER" != none ]; then
		for parent in firmware venv eitri/klarity; do
			chown "$OWNER" "$root/$parent" || die "cannot chown to $OWNER: run under fakeroot or as root (or --owner none)"
		done
		got=$(stat -c %u:%g "$root/firmware/klipper" "$root/venv/klippy" "$root/eitri/klarity/venv" "$root/eitri/extras")
		for line in $got; do
			[ "$line" = "$OWNER" ] || die "files are owned by $line, not $OWNER: run under fakeroot or as root (or --owner none)"
		done
	fi
	[ -f "$root/eitri/state/seed-installed.env" ] || die "the installer left no seed-installed.env"
	[ -f "$root/eitri/flavor" ] || die "the seed has no eitri/flavor"
fi

# Size: the tree as the build host's file system counts it, plus 20 % for ext4 metadata, plus working
# room. The partition is enlarged to the whole eMMC on the first boot (eitri-grow-data), so this is
# the size of the flash-once image, not of the printer's /data.
used_kib=$(du -sk "$root" | cut -f1)
size_mib=$(( (used_kib * 12 / 10 + 1023) / 1024 + HEADROOM_MIB ))
[ -n "$SEED" ] || size_mib=$MIN_MIB
[ "$size_mib" -ge "$MIN_MIB" ] || size_mib=$MIN_MIB

tmp="$OUTPUT.tmp"
rm -f "$tmp"
mkdir -p "$(dirname "$OUTPUT")"
# mke2fs clamps every timestamp to SOURCE_DATE_EPOCH (Buildroot exports it) and E2FSPROGS_FAKE_TIME overrides
# them all. The seed's mtimes are data, not noise: klippy rebuilds c_helper.so (and the printer has no
# compiler) unless the library is newer than the sources, and the seed makes it exactly one second newer.
# So mke2fs runs without either variable; the price is that the file system's creation time is not reproducible.
# -e remount-ro: a filesystem error must stop the writes (the mount unit says the same). root_owner:
# sshd's StrictModes wants the directories above the access keys (/data/eitri/state/access) to be root's.
# -i 8192: a Python venv and two git clones are many small files.
env -u SOURCE_DATE_EPOCH -u E2FSPROGS_FAKE_TIME mke2fs -q -F -t ext4 -L data -m 1 -i 8192 -e remount-ro -U "$FS_UUID" \
	-E "hash_seed=$HASH_SEED,root_owner=0:0" -d "$root" "$tmp" "${size_mib}M"
mv "$tmp" "$OUTPUT"
log "wrote $OUTPUT: ${size_mib} MiB, $([ -n "$SEED" ] && echo "seeded ($FLAVOR)" || echo "empty")"
