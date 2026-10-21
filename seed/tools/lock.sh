#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Regenerate the hash-pinned requirement locks (requirements/*.txt) from the *.in inputs.
#
#   seed/tools/lock.sh [--upgrade]
#
# Locks are resolved for the *target* (CPython 3.14, aarch64, glibc <= 2.40 wheels, binary wheels only) with
# `uv pip compile`, so they can be produced on any host. Review the diff; commit it. The seed build installs
# only from these files, with `--require-hashes`.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REQ="$HERE/../requirements"
UV="${UV:-uv}"
# Highest manylinux baseline uv knows that does not exceed the image's glibc (2.42).
PLATFORM="${EITRI_SEED_PLATFORM:-aarch64-manylinux_2_40}"
PYVER="${EITRI_SEED_PYTHON_VERSION:-3.14}"
UPGRADE=()
[ "${1:-}" = "--upgrade" ] && UPGRADE=(--upgrade)

for name in klippy-klipper klippy-kalico moonraker klarity; do
    "$UV" pip compile "$REQ/$name.in" -o "$REQ/$name.txt" \
        --python-platform "$PLATFORM" --python-version "$PYVER" \
        --only-binary :all: --generate-hashes --no-header --no-annotate "${UPGRADE[@]}" \
        --custom-compile-command "seed/tools/lock.sh"
done
# Build tools run on the host; no cross platform.
"$UV" pip compile "$REQ/build.in" -o "$REQ/build.txt" --python-version "$PYVER" \
    --only-binary :all: --generate-hashes --no-header --no-annotate "${UPGRADE[@]}" \
    --custom-compile-command "seed/tools/lock.sh"
echo "locks written to $REQ"
