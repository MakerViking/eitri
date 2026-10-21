#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Verify that a Kconfig fragment survives dependency resolution.
#
# Buildroot merges a fragment into a base defconfig and runs "make
# olddefconfig". Kconfig then silently drops any line whose dependencies are
# not met, so a typo or a renamed symbol turns into a missing feature that
# only shows up on the printer. This script performs the same merge against
# the real source tree and fails if any requested value did not stick.
#
# Usage: check-kconfig-fragment.sh [options] SRC_DIR FRAGMENT
#   -d NAME   base defconfig target ("defconfig" for the kernel,
#             "roc-cc-rk3328_defconfig" for U-Boot); default: defconfig
#   -a ARCH   ARCH= value for the make invocation (kernel: arm64)
#   -w DIR    work directory for the generated .config (default: a new temp dir)
#
# SRC_DIR is an extracted source tree. It is never modified; everything is
# generated out of tree. CROSS_COMPILE may name an aarch64 toolchain prefix
# for compiler-dependent symbols. Without one the host compiler is used,
# which is enough for the symbols the Eitri fragments set.
set -euo pipefail

defconfig=defconfig
arch=
work=
while getopts "d:a:w:" opt; do
	case "$opt" in
	d) defconfig=$OPTARG ;;
	a) arch=$OPTARG ;;
	w) work=$OPTARG ;;
	*) exit 2 ;;
	esac
done
shift $((OPTIND - 1))

if [ "$#" -ne 2 ]; then
	echo "usage: $0 [-d defconfig] [-a arch] [-w workdir] SRC_DIR FRAGMENT" >&2
	exit 2
fi

src=$(realpath "$1")
fragment=$(realpath "$2")
[ -n "$work" ] || work=$(mktemp -d)
mkdir -p "$work"
work=$(realpath "$work")

[ -f "$src/Makefile" ] || { echo "error: $src is not a source tree" >&2; exit 2; }
[ -f "$fragment" ] || { echo "error: $fragment not found" >&2; exit 2; }

kmake() {
	make -s -C "$src" O="$work" ${arch:+ARCH="$arch"} ${CROSS_COMPILE:+CROSS_COMPILE="$CROSS_COMPILE"} "$@"
}

kmake "$defconfig" >/dev/null
KCONFIG_CONFIG="$work/.config" "$src/scripts/kconfig/merge_config.sh" -m -O "$work" \
	"$work/.config" "$fragment" >/dev/null 2>"$work/merge.log" || {
	cat "$work/merge.log" >&2
	exit 1
}
kmake olddefconfig >/dev/null

config="$work/.config"
status=0
checked=0

while IFS= read -r line; do
	if [[ "$line" =~ ^#\ (CONFIG_[A-Za-z0-9_]+)\ is\ not\ set$ ]]; then
		sym=${BASH_REMATCH[1]}
		# "Not set" holds when the marker is present or when the symbol is gone
		# because its parent menu is disabled; it fails only if still enabled.
		if grep -Eq "^${sym}=" "$config"; then
			echo "NOT UNSET: $sym ($(grep -E "^${sym}=" "$config"))"
			status=1
		fi
	elif [[ "$line" =~ ^(CONFIG_[A-Za-z0-9_]+)=(.*)$ ]]; then
		sym=${BASH_REMATCH[1]}
		want=${BASH_REMATCH[2]}
		have=$(grep -E "^${sym}=" "$config" | head -n1 | cut -d= -f2- || true)
		if [ "$have" != "$want" ]; then
			echo "NOT APPLIED: $sym wanted '$want', got '${have:-<unset>}'"
			status=1
		fi
	else
		continue
	fi
	checked=$((checked + 1))
done <"$fragment"

if [ "$status" -eq 0 ]; then
	echo "fragment OK: $checked settings verified ($(grep -c '=m$' "$config") modules, $(grep -c '=y$' "$config") built-in)"
fi
exit "$status"
