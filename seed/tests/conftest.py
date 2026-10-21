# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the seed tests.

Light tests (specification, pins, locks, installer against synthetic seeds) always run. Tests that need a
built seed skip when the artifacts are absent (build them with `make -C seed build`); tests that need the
Buildroot image root and qemu-user skip when those are absent.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SEED = HERE.parent
REPO = SEED.parent
sys.path.insert(0, str(SEED / "tools"))
sys.path.insert(0, str(HERE))

BUILD_ROOT = Path(os.environ.get("EITRI_BUILD_ROOT", REPO.parent / "build"))
BR_OUT = Path(os.environ.get("EITRI_BR_OUT", BUILD_ROOT / "br-out"))
SEED_OUT = Path(os.environ.get("EITRI_SEED_OUT", BUILD_ROOT / "seed" / "out"))


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def seed_dir() -> Path:
    return SEED


@pytest.fixture(scope="session")
def built_manifests() -> list[Path]:
    found = sorted(SEED_OUT.glob("data-seed-*.manifest.json"))
    if not found:
        pytest.skip(f"no built seeds in {SEED_OUT} (run: make -C seed build)")
    return found


@pytest.fixture(scope="session")
def image_root() -> Path:
    root = BR_OUT / "target"
    if not (root / "usr/bin/python3.14").exists():
        pytest.skip(f"no Buildroot image root at {root}")
    return root


@pytest.fixture(scope="session")
def qemu() -> str:
    path = shutil.which(os.environ.get("QEMU_AARCH64", "qemu-aarch64"))
    if not path:
        pytest.skip("qemu-aarch64 (user-mode) not installed")
    return path


needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd not installed")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def make_sysroot(image_root: Path, dest: Path) -> None:
    """A private copy of the image root: hard links when dest is on the same file system (instant), else a copy."""
    import subprocess

    r = subprocess.run(["cp", "-al", str(image_root), str(dest)], capture_output=True)
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        subprocess.run(["cp", "-a", str(image_root), str(dest)], check=True)


@pytest.fixture(scope="session")
def scratch_next_to_image(image_root):
    """Scratch directory on the image's file system (so hard-linked sysroots work); removed afterwards."""
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="seed-test-", dir=image_root.parent))
    yield base
    shutil.rmtree(base, ignore_errors=True)
