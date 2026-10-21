#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the Eitri OS userspace for qemu-system-aarch64 ("virt" machine).
#
#   os/qemu/build.sh [make-target...]      default target: all
#
# Same Buildroot, same BR2_EXTERNAL tree, same rootfs overlay, packages and units as the printer
# image; see qemu.fragment for the (small) difference. Output goes to $OUT (default
# ../build/br-qemu, next to the repository); downloads and ccache are shared with the printer build.
#
# Results in $OUT/images/: Image (kernel), rootfs.ext4 (root filesystem).
#
# Environment:
#   BUILD_ROOT   build area                  (default: <repo>/../build)
#   BR_DIR       Buildroot checkout          (default: $BUILD_ROOT/buildroot)
#   OUT          Buildroot output directory  (default: $BUILD_ROOT/br-qemu)
#   BR2_DL_DIR   download cache              (default: $BUILD_ROOT/br-dl)
#   BR2_CCACHE_DIR                           (default: $BUILD_ROOT/br-ccache)
#   EITRI_QEMU_PASSWORD   password of the "eitri" user in this bench image (default: eitri)
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OS_DIR=$(cd "$HERE/.." && pwd)
REPO_ROOT=$(cd "$OS_DIR/.." && pwd)
BUILD_ROOT=${BUILD_ROOT:-$(cd "$REPO_ROOT/.." && pwd)/build}
BR_DIR=${BR_DIR:-$BUILD_ROOT/buildroot}
OUT=${OUT:-$BUILD_ROOT/br-qemu}
export BR2_DL_DIR=${BR2_DL_DIR:-$BUILD_ROOT/br-dl}
export BR2_CCACHE_DIR=${BR2_CCACHE_DIR:-$BUILD_ROOT/br-ccache}

[ -f "$BR_DIR/Makefile" ] || { echo "build.sh: no Buildroot in $BR_DIR; run 'make -C os buildroot' first" >&2; exit 1; }
mkdir -p "$OUT" "$BR2_DL_DIR" "$BR2_CCACHE_DIR"

# The image is a bench image: the eitri user gets a password (post-fakeroot.sh) so that the smoke
# test can log in on the serial console. A release image has no console and no password.
unset EITRI_RELEASE EITRI_DEV_PASSWORD_HASH
EITRI_QEMU_PASSWORD_HASH=$(openssl passwd -6 "${EITRI_QEMU_PASSWORD:-eitri}")
export EITRI_QEMU_PASSWORD_HASH
export EITRI_VERSION=${EITRI_VERSION:-0.0.0-qemu}

DEFCONFIG=$OUT/eitri_qemu_defconfig
python3 -I "$HERE/mkdefconfig.py" "$OS_DIR/configs/eitri_plus4_defconfig" "$HERE/qemu.fragment" >"$DEFCONFIG.new"
if ! cmp -s "$DEFCONFIG.new" "$DEFCONFIG" || [ ! -f "$OUT/.config" ]; then
	mv "$DEFCONFIG.new" "$DEFCONFIG"
	make -C "$BR_DIR" BR2_EXTERNAL="$OS_DIR" O="$OUT" BR2_DEFCONFIG="$DEFCONFIG" defconfig
else
	rm -f "$DEFCONFIG.new"
fi

# Local-site packages (SITE_METHOD = local) are not re-synced by Buildroot after their first build;
# rebuild them so the QEMU image tests the current repo, not a stale copy.
if [ $# -eq 0 ] || [ "$*" = all ]; then
	local_pkgs=$(grep -l "SITE_METHOD = local" "$OS_DIR"/package/*/*.mk | xargs -n1 basename | sed "s/\.mk$//")
	# shellcheck disable=SC2046
	make -C "$BR_DIR" BR2_EXTERNAL="$OS_DIR" O="$OUT" $(printf "%s-rebuild " $local_pkgs)
fi
make -C "$BR_DIR" BR2_EXTERNAL="$OS_DIR" O="$OUT" "$@"

# Every line of both kernel fragments must have survived Kconfig; a silently reverted option is a bug.
kconfig=$(ls -d "$OUT"/build/linux-[0-9]*/.config 2>/dev/null | head -1 || true)
if [ -n "$kconfig" ]; then
	status=0
	for frag in "$OS_DIR/board/eitri-plus4/linux.fragment" "$HERE/linux-qemu.fragment"; do
		while IFS= read -r line; do
			case "$line" in
			CONFIG_*=*)
				grep -qxF -- "$line" "$kconfig" || { echo "build.sh: kernel option not applied: $line  ($(basename "$frag"))" >&2; status=1; }
				;;
			"# CONFIG_"*" is not set")
				# Fine when the option is off or does not exist in this kernel; wrong only when it is on.
				sym=${line#\# }
				sym=${sym%% *}
				if grep -q "^$sym=" "$kconfig"; then echo "build.sh: kernel option still set: $sym  ($(basename "$frag"))" >&2; status=1; fi
				;;
			esac
		done <"$frag"
	done
	[ "$status" -eq 0 ] && echo "build.sh: all kernel fragment options are in $kconfig"
fi
ls -lh "$OUT/images/Image" "$OUT/images/rootfs.ext4" 2>/dev/null || true
