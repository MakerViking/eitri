#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Print Buildroot .hash lines for downloaded sources.

Usage: hashfile.py [--cache DIR] [--name NAME] URL_OR_FILE [...]

Each argument is downloaded into DIR (default: ../../build/os/hash-cache next to
the repository) unless it is already a local file, and one
"sha256  <digest>  <filename>" line is printed per input. Redirect the output
into the package's .hash file and review the diff: a changed digest for an
unchanged version is a supply-chain event, not a routine refresh.
"""
import argparse
import hashlib
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = REPO_ROOT.parent / "build" / "os" / "hash-cache"


def fetch(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as out:  # noqa: S310
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--name", help="filename to record instead of the basename (single input only)")
    ap.add_argument("inputs", nargs="+")
    args = ap.parse_args()
    if args.name and len(args.inputs) != 1:
        ap.error("--name needs exactly one input")

    for item in args.inputs:
        if urllib.parse.urlparse(item).scheme in ("http", "https"):
            base = Path(urllib.parse.urlparse(item).path).name
            # Cache per URL so two inputs with the same basename (LICENSE) never collide.
            key = hashlib.sha256(item.encode()).hexdigest()[:12]
            path = args.cache / key / base
            if not path.exists():
                fetch(item, path)
        else:
            path = Path(item)
            base = path.name
        print(f"sha256  {sha256(path)}  {args.name or base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
