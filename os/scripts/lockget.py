#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read one value from release.lock.

Usage: lockget.py KEY [LOCK_FILE]

KEY is a dotted TOML path such as os.buildroot.commit. The value is printed
as plain text. A missing key is an error, never an empty string, so a Makefile
that depends on a pin cannot silently continue without it.
"""
import sys
import tomllib
from pathlib import Path

DEFAULT_LOCK = Path(__file__).resolve().parents[2] / "release.lock"


def main(argv: list[str]) -> int:
    if not 2 <= len(argv) <= 3:
        print(__doc__, file=sys.stderr)
        return 2
    key = argv[1]
    lock = Path(argv[2]) if len(argv) == 3 else DEFAULT_LOCK
    with lock.open("rb") as handle:
        node = tomllib.load(handle)
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            print(f"lockget: {key} not found in {lock}", file=sys.stderr)
            return 1
        node = node[part]
    print(node)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
