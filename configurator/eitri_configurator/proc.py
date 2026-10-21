"""Run a helper program and capture its combined output."""
from __future__ import annotations

import asyncio


async def run(*argv: str, timeout: float = 30) -> tuple[int, str]:
    """(exit code, stdout+stderr). The process is killed and TimeoutError raised after `timeout` seconds."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace")
