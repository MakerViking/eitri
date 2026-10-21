#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Buildroot post-image hook, first of three (post-image.sh, genimage.sh,
# post-bundle.sh). Stages what genimage.cfg puts on the FAT boot partition
# next to the other images, and builds data.ext4, the /data partition.
#
# data.ext4 is populated with the data seed (seed/SPEC.md) when the image is built, so a printer
# flashed with it boots with Klipper, Moonraker and Klarity already in place and needs no network.
# The reasoning is in mkdata.sh. Environment (all optional):
#
#   EITRI_SEED_ARCHIVE   the seed to use (data-seed-<version>-<flavor>.tar.zst; its .manifest.json
#                        must sit next to it). Default: the only data-seed-*-<flavor>.tar.zst in
#                        EITRI_SEED_DIR.
#   EITRI_SEED_DIR       where seed/build-seed.sh wrote its output (default: <build root>/seed/out,
#                        next to Buildroot's output directory)
#   EITRI_SEED_FLAVOR    klipper (default) or kalico: the flavor the image starts with. The other one
#                        is installed later by the configurator or eitri-update.
#   EITRI_NO_SEED=1      build an image with an empty /data (bench work on the OS only; the services
#                        stay off). Not allowed with EITRI_RELEASE=1, and a missing seed is an error then.
#   EITRI_RELEASE=1      a seed is mandatory
set -euo pipefail

BOARD_DIR=${BR2_EXTERNAL_EITRI_PATH:?run through Buildroot with BR2_EXTERNAL=os/}/board/eitri-plus4
REPO_ROOT=$(cd "$BR2_EXTERNAL_EITRI_PATH/.." && pwd)
BINARIES_DIR=${BINARIES_DIR:?}
RELEASE=${EITRI_RELEASE:-0}
FLAVOR=${EITRI_SEED_FLAVOR:-klipper}

log() { echo "eitri post-image: $*"; }
warn() { echo "eitri post-image: WARNING: $*" >&2; }
fail() { echo "eitri post-image: ERROR: $*" >&2; exit 1; }

install -m 0644 "$BOARD_DIR/boot/eitri-boot.env" "$BOARD_DIR/boot/README.txt" "$BINARIES_DIR/"

# --- /data ------------------------------------------------------------------------------------------
case "$FLAVOR" in klipper | kalico) ;; *) fail "EITRI_SEED_FLAVOR must be klipper or kalico" ;; esac

seed=${EITRI_SEED_ARCHIVE:-}
if [ -z "$seed" ] && [ "${EITRI_NO_SEED:-0}" != 1 ]; then
	seed_dir=${EITRI_SEED_DIR:-$(cd "${BASE_DIR:-$BINARIES_DIR/..}/.." && pwd)/seed/out}
	candidates=()
	for f in "$seed_dir"/data-seed-*-"$FLAVOR".tar.zst; do
		[ -f "$f" ] && candidates+=("$f")
	done
	case ${#candidates[@]} in
	0) ;;
	1) seed=${candidates[0]} ;;
	*) fail "several $FLAVOR seeds in $seed_dir; set EITRI_SEED_ARCHIVE to the one to use" ;;
	esac
fi

args=(--output "$BINARIES_DIR/data.ext4")
if [ "${EITRI_NO_SEED:-0}" = 1 ]; then
	[ "$RELEASE" != 1 ] || fail "EITRI_NO_SEED must not be set for a release"
	warn "EITRI_NO_SEED=1: /data is empty, Klipper, Moonraker and Klarity will not start"
elif [ -n "$seed" ]; then
	[ -f "$seed" ] || fail "seed archive $seed not found"
	manifest="${seed%.tar.zst}.manifest.json"
	[ -f "$manifest" ] || fail "the manifest $manifest must sit next to the seed archive"
	args+=(--seed "$seed" --manifest "$manifest" --flavor "$FLAVOR" --installer "$REPO_ROOT/seed/bin/eitri-seed-install")
	log "seeding /data from $(basename "$seed")"
elif [ "$RELEASE" = 1 ]; then
	fail "no data seed: build one with 'make -C seed build' or set EITRI_SEED_ARCHIVE (a release image must boot offline)"
else
	warn "no data seed found (set EITRI_SEED_ARCHIVE or EITRI_SEED_DIR, build with 'make -C seed build'): /data is empty, Klipper, Moonraker and Klarity will not start"
fi

# The seed's files belong to uid/gid 1000 (eitri). Create them and the file system inside one fakeroot
# session, so the owners are recorded without needing root.
if [ "$(id -u)" = 0 ]; then
	"$BOARD_DIR/mkdata.sh" "${args[@]}"
else
	fakeroot=${HOST_DIR:+$HOST_DIR/bin/fakeroot}
	[ -x "${fakeroot:-}" ] || fakeroot=$(command -v fakeroot || true)
	[ -n "$fakeroot" ] || fail "fakeroot not found (needed to give the seed's files to uid 1000)"
	"$fakeroot" -- "$BOARD_DIR/mkdata.sh" "${args[@]}"
fi
