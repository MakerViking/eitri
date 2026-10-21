#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the pinned RAUC (release.lock [os.rauc]) for the signing job.
#
#   tools/release/build-rauc.sh PREFIX [--tarball FILE]
#
# Why not the distribution's rauc: the bundle format and `rauc resign` options changed between versions, and the signing
# job must not depend on what a runner image happens to carry. Why not the rauc that the OS build produced: the signing
# job holds the release keys, and nothing built by a build job (which holds none) may run next to them.
#
# The source tarball is checked against the SHA-256 in release.lock before it is unpacked. The build options are the ones
# Buildroot uses for host-rauc (package/rauc/rauc.mk), so the tool is the one that made the bundle.
#
# Needs: meson ninja pkg-config a C compiler, and the development files of glib-2.0, json-glib-1.0 and openssl
# (Ubuntu: meson ninja-build pkg-config libglib2.0-dev libglib2.0-dev-bin libjson-glib-dev libssl-dev), plus curl unless
# --tarball is given. libglib2.0-dev-bin provides gdbus-codegen, which RAUC's build runs.
# Prints the path of the rauc binary on the last line.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
LOCK=${EITRI_RELEASE_LOCK:-$ROOT/release.lock}

prefix=${1:?usage: build-rauc.sh PREFIX [--tarball FILE]}
shift
tarball=""
while [ $# -gt 0 ]; do
	case $1 in
		--tarball) tarball=${2:?}; shift 2 ;;
		*) echo "build-rauc: unknown argument $1" >&2; exit 2 ;;
	esac
done

read -r version url sha256 < <(python3 -I - "$LOCK" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    r = tomllib.load(fh)["os"]["rauc"]
print(r["version"], r["source"], r["sha256"])
PY
)
[[ $version =~ ^[0-9]+(\.[0-9]+)+$ ]] || { echo "build-rauc: bad version in release.lock: $version" >&2; exit 1; }
[[ $sha256 =~ ^[0-9a-f]{64}$ ]] || { echo "build-rauc: release.lock has no SHA-256 for RAUC" >&2; exit 1; }
[[ $url == https://github.com/rauc/rauc/releases/download/v$version/rauc-$version.tar.xz ]] ||
	{ echo "build-rauc: unexpected source URL in release.lock: $url" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -z "$tarball" ]; then
	tarball=$work/rauc-$version.tar.xz
	curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 --output "$tarball" "$url"
fi
actual=$(sha256sum "$tarball" | cut -d' ' -f1)
[ "$actual" = "$sha256" ] || { echo "build-rauc: SHA-256 of $tarball is $actual, release.lock pins $sha256" >&2; exit 1; }

tar -C "$work" -xf "$tarball"
src=$work/rauc-$version
[ -d "$src" ] || { echo "build-rauc: the tarball does not unpack to rauc-$version/" >&2; exit 1; }
meson setup "$work/build" "$src" --prefix="$prefix" --buildtype=release \
	-Djson=enabled -Dnetwork=false -Dstreaming=false -Dservice=false -Dtests=false >&2
ninja -C "$work/build" >&2
ninja -C "$work/build" install >&2
bin=$prefix/bin/rauc
[ -x "$bin" ] || { echo "build-rauc: $bin was not built" >&2; exit 1; }
"$bin" --version >&2
echo "$bin"
