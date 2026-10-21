"""Shared fixtures for the klipper-layer tests.

Paths to the trees under test come from the environment, with defaults that
match the Eitri workspace layout (the repo sits next to ``src/`` and
``build/``):

  EITRI_KLIPPER_SRC   mainline Klipper clone      (../src/upstream-klipper)
  EITRI_KALICO_SRC    Kalico clone at the pinned tag (../build/kalico)
  EITRI_KLIPPY_DICTS  directory holding klipper-stm32f446.dict and
                      kalico-stm32f446.dict (see tests/build-dicts.sh)
  EITRI_KLIPPY_PYTHON interpreter with cffi/greenlet/jinja2/pyserial
                      (defaults to the one running pytest)

Nothing in the source trees is ever modified: tests that need a working copy
clone it into a temporary directory first.
"""
import os
import pathlib
import subprocess
import sys

import pytest

LAYER = pathlib.Path(__file__).resolve().parent.parent
REPO = LAYER.parent
WORKSPACE = REPO.parent
EXTRAS = LAYER / "extras"
CONFIG = LAYER / "config"
SCRIPTS = LAYER / "scripts"
TESTS = LAYER / "tests"

SOURCES = {
    "klipper": pathlib.Path(os.environ.get(
        "EITRI_KLIPPER_SRC", WORKSPACE / "src" / "upstream-klipper")),
    "kalico": pathlib.Path(os.environ.get(
        "EITRI_KALICO_SRC", WORKSPACE / "build" / "kalico")),
}
DICT_DIR = pathlib.Path(os.environ.get(
    "EITRI_KLIPPY_DICTS", WORKSPACE / "build" / "klipper-layer" / "dicts"))
PYTHON = os.environ.get("EITRI_KLIPPY_PYTHON", sys.executable)

FLAVORS = ["klipper", "kalico"]


def extra_files():
    return sorted(p for p in EXTRAS.glob("*.py") if not p.name.startswith("_"))


@pytest.fixture(params=FLAVORS)
def flavor(request):
    return request.param


@pytest.fixture
def source_tree(flavor):
    src = SOURCES[flavor]
    if not (src / "klippy" / "extras").is_dir():
        pytest.skip("%s tree not found at %s (set EITRI_%s_SRC)"
                    % (flavor, src, flavor.upper()))
    return src


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout


@pytest.fixture
def clone(source_tree, tmp_path):
    """A throw-away clone of the tree under test."""
    dest = tmp_path / "clone"
    # --shared: borrow the (large) object store instead of copying it, or the
    # ~100 clones of one run fill a RAM-backed /tmp. The source is only read.
    git("clone", "-q", "--shared", str(source_tree), str(dest))
    return dest
