# SPDX-License-Identifier: Apache-2.0
"""Subprocess wrapper with line streaming; tests substitute a fake runner."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence


@dataclass
class RunResult:
    returncode: int
    output: str


Runner = Callable[..., RunResult]


def run(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    on_line: Optional[Callable[[str], None]] = None,
) -> RunResult:
    """Run *argv* (never through a shell), streaming merged stdout/stderr by line."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
    except OSError as exc:
        return RunResult(127, "cannot execute %s: %s" % (argv[0], exc))
    lines: List[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if on_line:
                on_line(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        lines.append("timed out after %ss" % timeout)
        return RunResult(124, "\n".join(lines))
    return RunResult(proc.returncode, "\n".join(lines))
