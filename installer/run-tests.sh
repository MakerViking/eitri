#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Syntax-check every script, then run every test suite.  Usage: ./run-tests.sh [pytest args...]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== bash -n"
for f in install.sh revert.sh stage2/stage2.sh lib/*.sh tools/*.sh run-tests.sh tests/shims/* tests/shims2/*; do
  head -1 "$f" | grep -q 'bash' && bash -n "$f" || sh -n "$f"
done
for f in bin/*; do sh -n "$f"; done

echo "== python: compile + Python 3.7 grammar"
python3 - <<'PY'
import ast, pathlib
n = 0
for p in pathlib.Path(".").rglob("*.py"):
    if "__pycache__" in p.parts:
        continue
    src = p.read_text()
    ast.parse(src, filename=str(p), feature_version=(3, 7))   # nothing newer than 3.7 syntax: the stock image runs 3.7
    compile(src, str(p), "exec")
    n += 1
print("%d files ok" % n)
PY

echo "== pytest"
status=0
for dir in mcuflash eitri-update tests; do
  (cd "$dir" && python3 -m pytest -q -p no:cacheprovider "$@") || status=1
done
exit $status
