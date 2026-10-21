#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Resolve configs/eitri_plus4_defconfig against the pinned Buildroot and prove
# that every option in it took effect.
#
# "make <defconfig>" accepts unknown or unmet options without a word, which
# turns a typo into a silently missing package. This script runs the defconfig
# in a scratch output directory and then compares each assignment with the
# resulting .config.
#
# Usage: check-defconfig.sh BUILDROOT_DIR OUTPUT_DIR
set -euo pipefail

if [ "$#" -ne 2 ]; then
	echo "usage: $0 BUILDROOT_DIR OUTPUT_DIR" >&2
	exit 2
fi

here=$(cd "$(dirname "$0")/.." && pwd)
br=$(realpath "$1")
out=$2
rm -rf "$out"

make -s -C "$br" BR2_EXTERNAL="$here" O="$out" eitri_plus4_defconfig >/dev/null 2>"$out.log" || {
	cat "$out.log" >&2
	exit 1
}
out=$(realpath "$out")
config="$out/.config"
status=0
checked=0

# Unmet dependencies and renamed options show up as these markers.
if grep -q '^BR2_LEGACY=y' "$config"; then
	echo "LEGACY: the defconfig uses options Buildroot has renamed or removed:" >&2
	awk '/^# Legacy config options/{f=1} f && /^BR2_[A-Z0-9_]+=y$/{print "  " $0}' "$config" >&2 || true
	status=1
fi

while IFS= read -r line; do
	case "$line" in
	'' | '# ---'* | '#' | '# '[!B]*) continue ;;
	esac
	if [[ "$line" =~ ^#\ (BR2_[A-Za-z0-9_]+)\ is\ not\ set$ ]]; then
		sym=${BASH_REMATCH[1]}
		if grep -q "^${sym}=" "$config"; then
			echo "NOT UNSET: $sym"
			status=1
		fi
	elif [[ "$line" =~ ^(BR2_[A-Za-z0-9_]+)=(.*)$ ]]; then
		sym=${BASH_REMATCH[1]}
		want=${BASH_REMATCH[2]}
		have=$(grep -E "^${sym}=" "$config" | head -n1 | cut -d= -f2- || true)
		if [ "$have" != "$want" ]; then
			echo "NOT APPLIED: $sym wanted $want, got ${have:-<unset>}"
			status=1
		fi
	else
		continue
	fi
	checked=$((checked + 1))
done <"$here/configs/eitri_plus4_defconfig"

if [ "$status" -eq 0 ]; then
	echo "defconfig OK: $checked options applied"
fi
exit "$status"
