#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Boot smoke test and measurements of the Eitri OS on QEMU. See smoke.py for what it does.
#
#   os/qemu/smoke-test.sh [smoke.py options]
#
# Needs the output of build.sh (kernel and root filesystem). Exit status 0 when every check passes.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
command -v qemu-system-aarch64 >/dev/null || { echo "smoke-test.sh: qemu-system-aarch64 not found" >&2; exit 2; }
exec python3 -I "$HERE/smoke.py" "$@"
