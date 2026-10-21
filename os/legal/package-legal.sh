#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Collect the licence-compliance files for one Eitri OS build.
#
#   package-legal.sh <version> <out-dir>
#
# Runs Buildroot's legal-info against the existing configured output (os/Makefile
# BUILD_ROOT), then writes into <out-dir>:
#   eitri-<version>-sources.tar          corresponding source of every GPL/LGPL package
#                                        (linux-firmware excluded unless KEEP_FIRMWARE_SRC=1)
#   eitri-<version>-licenses.tar.xz      licence texts, manifest.csv, buildroot.config, NOTICE files
#   eitri-<version>.cdx.json / .spdx.json / sbom-summary.txt
#   eitri-<version>-repo-source.tar.gz   git archive of the Eitri tree (local packages, DTS, fragments)
#   SHA256SUMS-legal
set -euo pipefail

version=${1:?usage: package-legal.sh <version> <out-dir>}
out=${2:?usage: package-legal.sh <version> <out-dir>}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
build_root=${BUILD_ROOT:-$repo/../build}
br_out=${OUT:-$build_root/br-out}

mkdir -p "$out"
out=$(cd "$out" && pwd)

make -C "$repo/os" legal-info
li="$br_out/legal-info"
# Every enabled Eitri package must be in the manifest; a package whose legal-info step
# failed is otherwise silently missing from the source offer.
while read -r sym; do
  pkg=$(echo "${sym#BR2_PACKAGE_}" | tr 'A-Z_' 'a-z-')
  grep -q "^\"$pkg\"," "$li/manifest.csv" || { echo "error: $pkg is enabled but absent from manifest.csv" >&2; exit 1; }
done < <(sed -n 's/^\(BR2_PACKAGE_\(EITRI\|GO2RTC\|UV\)[A-Z0-9_]*\)=y$/\1/p' "$br_out/.config")
[ -s "$li/manifest.csv" ] || { echo "error: $li/manifest.csv missing or empty" >&2; exit 1; }

python3 -I "$here/make-sbom.py" --legal-info "$li" --release-lock "$repo/release.lock" \
  --version "$version" --out "$out" --hash-sources --fail-on-unknown

# Sources. Tar them uncompressed: they are already .tar.xz/.gz/.zip.
excl=()
if [ "${KEEP_FIRMWARE_SRC:-0}" != 1 ]; then excl+=(--exclude='sources/linux-firmware-*'); fi
tar -C "$li" "${excl[@]}" -cf "$out/eitri-$version-sources.tar" sources

# Licence texts and the build recipe. NOTICE files are the project's own attribution.
stage=$(mktemp -d); trap 'rm -rf "$stage"' EXIT
cp -a "$li/licenses" "$li/manifest.csv" "$li/buildroot.config" "$stage/"
cp "$repo/LICENSE" "$repo/NOTICE" "$stage/"
cp "$repo/os/NOTICE" "$stage/NOTICE.os"
cp "$repo/release.lock" "$here/README.md" "$stage/"
tar -C "$stage" -cJf "$out/eitri-$version-licenses.tar.xz" .

# Everything that is not a Buildroot download: local packages, DTS, kernel/U-Boot fragments, scripts.
git -C "$repo" archive --format=tar.gz --prefix="eitri-$version/" -o "$out/eitri-$version-repo-source.tar.gz" HEAD

(cd "$out" && sha256sum eitri-"$version"* sbom-summary.txt > SHA256SUMS-legal)
du -h "$out"/eitri-"$version"-*
