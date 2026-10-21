#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Prepare the build inputs that normally come from a Buildroot tree, for hosts that have none (CI):
#
#   seed/tools/ci-toolchain.sh DEST
#
# Downloads the pinned python-build-standalone aarch64 CPython (requirements/ci-toolchain.toml, SHA-256
# checked) into DEST/python and assembles DEST/sysroot, an aarch64 root with that Python in /usr plus a C library, for
# verify-seed.sh --sysroot. The C library comes from EITRI_CI_LIBC_ROOT (default /usr/aarch64-linux-gnu, from
# the libc6-arm64-cross package). Prints the environment for build-seed.sh / verify-seed.sh on stdout:
#
#   eval "$(seed/tools/ci-toolchain.sh "$RUNNER_TEMP/toolchain")"
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DEST="${1:?usage: ci-toolchain.sh DEST}"
LIBC_ROOT="${EITRI_CI_LIBC_ROOT:-/usr/aarch64-linux-gnu}"
mkdir -p "$DEST"
DEST="$(cd -- "$DEST" && pwd -P)"

read -r URL SHA < <(python3 -I - "$HERE/../requirements/ci-toolchain.toml" <<'PY'
import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb"))["python-build-standalone"]
print(c["url"], c["sha256"])
PY
)
TGZ="$DEST/$(basename -- "${URL//%2B/+}")"
if [ ! -f "$TGZ" ] || [ "$(sha256sum "$TGZ" | cut -d' ' -f1)" != "$SHA" ]; then
    curl -fsSL --retry 3 -o "$TGZ.part" "$URL" >&2
    mv "$TGZ.part" "$TGZ"
fi
[ "$(sha256sum "$TGZ" | cut -d' ' -f1)" = "$SHA" ] || { echo "ci-toolchain: SHA-256 of $TGZ does not match requirements/ci-toolchain.toml" >&2; exit 1; }
rm -rf "$DEST/python" "$DEST/sysroot"
mkdir -p "$DEST/python-tmp"
tar -xzf "$TGZ" -C "$DEST/python-tmp"
mv "$DEST/python-tmp/python" "$DEST/python"
rmdir "$DEST/python-tmp"

SR="$DEST/sysroot"
mkdir -p "$SR/usr/bin" "$SR/usr/lib" "$SR/data" "$SR/etc"
cp -a "$DEST/python/lib/." "$SR/usr/lib/"
cp -a "$DEST/python/bin/python3.14" "$SR/usr/bin/python3.14"
ln -s python3.14 "$SR/usr/bin/python3"
ln -s usr/lib "$SR/lib"
ln -s usr/lib "$SR/lib64"
ln -s usr/bin "$SR/bin"
[ -e "$LIBC_ROOT/lib/ld-linux-aarch64.so.1" ] || { echo "ci-toolchain: no aarch64 C library in $LIBC_ROOT (apt install libc6-arm64-cross)" >&2; exit 1; }
cp -a "$LIBC_ROOT/lib/." "$SR/usr/lib/"
[ ! -d "$LIBC_ROOT/usr/lib" ] || cp -a "$LIBC_ROOT/usr/lib/." "$SR/usr/lib/" 2>/dev/null || true

cat <<EOS
export EITRI_PY_INCLUDE='$DEST/python/include/python3.14'
export EITRI_SYSCONFIGDATA='$DEST/python/lib/python3.14/_sysconfigdata__linux_aarch64-linux-gnu.py'
export EITRI_SYSROOT='$SR'
export EITRI_VERIFY_SYSROOT='$SR'
EOS
