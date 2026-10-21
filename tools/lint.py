#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Syntax checks for everything in the repository that needs no extra tools.

    python3 -I tools/lint.py [-q]

* shell scripts (by extension or shebang): ``bash -n`` / ``sh -n``;
* Python (by extension or shebang): parsed and compiled, nothing written to disk;
* JavaScript: ``node --check`` when node is installed;
* JSON, TOML and YAML files: parsed;
* systemd units: every line is a comment, a section, a key=value or a continuation.

This is a syntax gate, not a style gate; ruff and shellcheck are run by CI where installed.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUIET = "-q" in sys.argv
errors: list[str] = []
counts: dict[str, int] = {}


def bad(path: Path, why: str) -> None:
    errors.append(f"{path.relative_to(ROOT)}: {why}")
    print("FAIL", errors[-1])


def count(kind: str) -> None:
    counts[kind] = counts.get(kind, 0) + 1


def files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout.decode()
    return [p for rel in filter(None, out.split("\0")) if (p := ROOT / rel).is_file() and not p.is_symlink()]


def shebang(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            first = fh.readline(200)
    except OSError:
        return ""
    return first.decode("utf-8", "replace").strip() if first.startswith(b"#!") else ""


def run(cmd: list[str], path: Path) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        bad(path, f"{' '.join(cmd[:2])}: {(r.stderr or r.stdout).strip().splitlines()[0] if (r.stderr or r.stdout).strip() else r.returncode}")


NODE = shutil.which("node")
try:
    import yaml  # type: ignore[import-not-found]
except ImportError:
    yaml = None

LINE_OK = re.compile(r"^\s*($|[#;]|\[[^\]]+\]\s*$|[A-Za-z0-9_.@-]+\s*=|.*\\$)")

for f in files():
    rel = f.relative_to(ROOT)
    sb = shebang(f)
    suffix = f.suffix.lower()
    try:
        if suffix == ".sh" or "bash" in sb:
            count("bash"); run(["bash", "-n", str(f)], f)
        elif "/sh" in sb or sb.endswith(" sh"):
            count("sh"); run(["sh", "-n", str(f)], f)
        elif suffix == ".py" or "python" in sb:
            count("python")
            src = f.read_text(encoding="utf-8")
            compile(ast.parse(src, filename=str(f)), str(f), "exec")
        elif suffix in (".js", ".mjs"):
            if NODE:
                count("javascript"); run([NODE, "--check", str(f)], f)
        elif suffix == ".json":
            count("json"); json.loads(f.read_text(encoding="utf-8"))
        elif suffix == ".toml" or f.name == "release.lock":
            count("toml"); tomllib.loads(f.read_text(encoding="utf-8"))
        elif suffix in (".yaml", ".yml"):
            if yaml:
                count("yaml"); yaml.safe_load(f.read_text(encoding="utf-8"))
        elif suffix in (".service", ".timer") and "avahi" not in f.parts:
            count("unit")
            cont = False
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if not cont and not LINE_OK.match(line):
                    bad(f, f"line {n} is not INI-shaped: {line[:50]}")
                    break
                cont = line.rstrip().endswith("\\")
    except (SyntaxError, ValueError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        bad(f, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # yaml.YAMLError and friends
        bad(f, f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")

if not QUIET or errors:
    print("checked " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + (" (no node: JavaScript skipped)" if not NODE else ""))
sys.exit(1 if errors else 0)
