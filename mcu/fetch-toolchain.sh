#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Install the pinned Arm GNU Toolchain (arm-none-eabi, x86_64 Linux host) used by build-mcu.sh.
#
#   mcu/fetch-toolchain.sh DEST_DIR
#
# The version, URL and SHA-256 come from release.lock [mcu.toolchain] (fields: version, source, sha256). The SHA-256 is
# the one Arm publishes next to the download; it is checked BEFORE anything is unpacked. The entry does not exist
# yet (the MCU parameters are not settled, mcu/README.md), so this script stops until it is added.
# Downloaded files are untrusted data: they are only unpacked, never executed here.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TOOL=(python3 -I "$HERE/mcutool.py")
die() { printf 'fetch-toolchain: error: %s\n' "$*" >&2; exit 1; }
[ $# -eq 1 ] || { echo "usage: $0 DEST_DIR" >&2; exit 2; }
dest="$1"

version="$("${TOOL[@]}" lock mcu.toolchain version 2>/dev/null)" || die "release.lock has no [mcu.toolchain] entry yet (mcu/README.md, \"Toolchain\")"
url="$("${TOOL[@]}" lock mcu.toolchain source)"
sha="$("${TOOL[@]}" lock mcu.toolchain sha256)"
case "$url" in https://*) ;; *) die "toolchain source must be an https URL";; esac
[[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "toolchain sha256 in release.lock is not 64 hex digits"

[ ! -e "$dest" ] || die "$dest already exists"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
file="$tmp/toolchain.tar.xz"
curl --fail --location --proto '=https' --tlsv1.2 --silent --show-error --output "$file" "$url"
echo "$sha  $file" | sha256sum --check --strict --quiet || die "SHA-256 of the download does not match release.lock; not unpacking"

# Refuse absolute or ..-containing member names before extracting.
if tar -tf "$file" | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then die "archive has unsafe member names"; fi
mkdir -p "$dest"
tar -xf "$file" -C "$dest" --strip-components=1 --no-same-owner
[ -x "$dest/bin/arm-none-eabi-gcc" ] || die "no bin/arm-none-eabi-gcc in the archive"
printf '%s\n' "$version" > "$dest/.eitri-toolchain-version"
echo "installed arm-none-eabi toolchain $version into $dest" >&2
