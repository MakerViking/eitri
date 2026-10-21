# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the OS tree tests.

These tests run the shell tools that ship in the image against temporary files
and directories. They need no root, no hardware and no Buildroot build.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

OS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = OS_DIR.parent
BOARD = OS_DIR / "board" / "eitri-plus4"
OVERLAY = BOARD / "rootfs-overlay"
LIBEXEC = OVERLAY / "usr" / "libexec" / "eitri"


def run(args, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run([str(a) for a in args], **kwargs)


def need(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        pytest.skip("needs " + ", ".join(missing))


@pytest.fixture
def cmdline(tmp_path: Path):
    """Write a kernel command line file and return its path."""

    def write(text: str) -> Path:
        path = tmp_path / "cmdline"
        path.write_text(text + "\n")
        return path

    return write


@pytest.fixture
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("EITRI_"):
            monkeypatch.delenv(name)
