# SPDX-License-Identifier: Apache-2.0
"""Checks on the seeds that build-seed.sh produced (skipped when none are built: `make -C seed build`)."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import seedlib
from conftest import needs_git, needs_zstd

pytestmark = [needs_zstd]

SEED = Path(__file__).resolve().parent.parent
TOOL = ["python3", "-I", str(SEED / "tools/seedtool.py")]


def manifests(built_manifests):
    return [(p, seedlib.load_json(p)) for p in built_manifests]


def test_manifests_are_valid_and_current(built_manifests, repo):
    for path, m in manifests(built_manifests):
        assert seedlib.validate_manifest(m) == [], path
        assert seedlib.MANIFEST_RE.match(path.name)
        pins = seedlib.pins(repo, m["flavor"])
        # a seed older than release.lock / the locks / the layer is stale
        assert m["sources"]["firmware"]["commit"] == pins["firmware"]["commit"], f"{path.name}: built from another commit than release.lock; rebuild"
        assert m["sources"]["moonraker"]["commit"] == pins["moonraker"]["commit"], f"{path.name}: stale Moonraker; rebuild"
        assert m["target"]["python"] == pins["python"]
        req = SEED / "requirements"
        for venv, lock in (("klippy", f"klippy-{m['flavor']}.txt"), ("moonraker", "moonraker.txt"), ("klarity", "klarity.txt")):
            assert m["venvs"][venv]["lock_sha256"] == hashlib.sha256((req / lock).read_bytes()).hexdigest(), f"{path.name}: {lock} changed; rebuild"


def test_seed_carries_the_current_klarity_and_extras(built_manifests, repo, tmp_path):
    """The seed must contain the repository's Klarity and qd_* extras byte for byte. (uv's cache once installed a stale
    Klarity wheel with the same name and version, so a seed without the 'Unlock setup' feature looked up to date.)"""
    for path, m in manifests(built_manifests):
        out = tmp_path / m["flavor"]
        out.mkdir()
        arc = path.parent / m["archive"]["name"]
        pipe = subprocess.run(f"zstd -dc '{arc}' | tar -x -C '{out}' --wildcards 'eitri/klarity/venv/lib/python3.*/site-packages/klarity/*' "
                              "'eitri/extras/*'", shell=True, capture_output=True, text=True)
        assert pipe.returncode == 0, pipe.stderr
        site = next((out / "eitri/klarity/venv/lib").glob("python3.*/site-packages/klarity"))
        src = repo / "klarity/src/klarity"
        wanted = {p.relative_to(src): p for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        assert wanted, "no Klarity sources found"
        for rel, source in wanted.items():
            assert (site / rel).is_file(), f"{path.name}: klarity/{rel} is missing from the seed's Klarity; rebuild"
            assert (site / rel).read_bytes() == source.read_bytes(), f"{path.name}: klarity/{rel} differs from the repository; rebuild"
        for source in (repo / "klipper-layer/extras").glob("*.py"):
            assert (out / "eitri/extras" / source.name).read_bytes() == source.read_bytes(), f"{path.name}: extras/{source.name} is stale; rebuild"


def test_flavors_are_known_and_unique(built_manifests):
    flavors = [m["flavor"] for _p, m in manifests(built_manifests)]
    assert set(flavors) <= set(seedlib.FLAVORS)
    assert len(flavors) == len(set(flavors)), "two manifests for one flavor"


def test_archive_matches_manifest_and_unpacks_to_the_contract(built_manifests):
    for path, m in manifests(built_manifests):
        r = subprocess.run([*TOOL, "verify", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.startswith("ok")


def test_manifest_records_cross_built_wheels_and_hashes(built_manifests):
    for _p, m in manifests(built_manifests):
        built = m["venvs"]["moonraker"]["built_from_source"]
        assert [b["name"] for b in built] == ["streaming-form-data"]
        names = {p["name"] for p in m["venvs"]["moonraker"]["packages"]}
        assert {"tornado", "pillow", "streaming-form-data", "uvloop", "dbus-fast", "zeroconf"} <= names
        klippy = {p["name"] for p in m["venvs"]["klippy"]["packages"]}
        assert {"cffi", "greenlet", "jinja2", "markupsafe", "pyserial", "python-can"} <= klippy
        assert ("numpy" in klippy) == (m["flavor"] == "kalico")
        # every compiled package targets the image's interpreter and C library
        for venv in m["venvs"].values():
            for p in venv["packages"]:
                for tag in p.get("tags", []):
                    if "manylinux" in tag:
                        assert "aarch64" in tag or "any" in tag, (p["name"], tag)
                        assert "cp314" in tag or "py3" in tag or "abi3" in tag or "none-any" in tag, (p["name"], tag)


@needs_git
def test_installer_on_the_real_seed_and_klippy_would_not_rebuild_chelper(built_manifests, tmp_path):
    installer = SEED / "bin/eitri-seed-install"
    for path, m in manifests(built_manifests):
        data = tmp_path / m["flavor"]
        data.mkdir()
        arc = path.parent / m["archive"]["name"]
        r = subprocess.run(["sh", str(installer), "--archive", str(arc), "--sha256", m["archive"]["sha256"],
                            "--flavor", m["flavor"], "--data", str(data), "-q"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        chdir = data / "firmware/klipper/klippy/chelper"
        so = chdir / "c_helper.so"
        # klippy/chelper/__init__.py: rebuild when any source is newer than the library (or the library is missing)
        newest = max(p.stat().st_mtime for p in chdir.iterdir() if p.suffix in (".c", ".h", ".py"))
        assert so.stat().st_mtime >= newest, "klippy would call gcc on the printer"
        assert hashlib.sha256(so.read_bytes()).hexdigest() == m["chelper"]["sha256"]
        assert (data / "eitri/flavor").read_text().strip() == m["flavor"]
        # the extras are linked into the clone and resolve once /data is the real root
        extras = data / "firmware/klipper/klippy/extras"
        links = [p for p in extras.iterdir() if p.is_symlink()]
        assert any(p.name.startswith("qd_") for p in links)
        for p in links:
            assert os.readlink(p) == f"/data/eitri/extras/{p.name}"
            assert (data / "eitri/extras" / p.name).is_file()
        st = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(data / "firmware/klipper"), "status", "--porcelain"],
                            capture_output=True, text=True)
        assert st.stdout == "", "the overlay must leave git status clean"


def test_emulated_aarch64_environments_run(built_manifests, image_root, qemu):
    """The real proof: the seed's own venvs, run by the image's Python under qemu-user (verify-seed.sh)."""
    for path, _m in manifests(built_manifests):
        r = subprocess.run(["bash", str(SEED / "verify-seed.sh"), "--verbose", str(path)], capture_output=True, text=True, timeout=900)
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
        assert "verify-seed: PASSED" in r.stdout
        for needle in ("chelper.get_ffi() loaded the prebuilt c_helper.so", "streaming_form_data (cross-compiled here)", "import klarity.daemon"):
            assert needle in r.stdout, needle


def test_image_provides_libsodium_for_moonraker(image_root):
    assert list((image_root / "usr/lib").glob("libsodium.so*")), "no libsodium in the image"


def test_libraries_expected_from_the_image_are_declared_and_present(built_manifests, image_root, seed_dir):
    allowed = {ln.strip() for ln in (seed_dir / "requirements/image-libs.txt").read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    have = {p.name for sub in ("lib", "usr/lib") for p in (image_root / sub).iterdir()}
    for _p, m in manifests(built_manifests):
        for lib in m["target"]["image_libraries"]:
            assert lib in allowed, lib
            assert lib in have, f"the OS image lacks {lib}, which the {m['flavor']} seed needs"
        assert tuple(map(int, m["target"]["glibc_needed"].split("."))) <= tuple(map(int, m["target"]["glibc_max"].split(".")))
