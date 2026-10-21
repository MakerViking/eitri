# SPDX-License-Identifier: Apache-2.0
"""The hash-pinned dependency locks: offline checks that need no network and no build."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import seedlib

LOCKS = ("klippy-klipper", "klippy-kalico", "moonraker", "klarity", "build")


def parse_lock(text: str) -> dict[str, dict]:
    """name -> {version, hashes}"""
    out: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+?)(?: ;.*)? \\$|^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$", line)
        if m:
            name = (m.group(1) or m.group(3)).lower().replace("_", "-")
            cur = out.setdefault(name, {"version": m.group(2) or m.group(4), "hashes": []})
            continue
        h = re.match(r"^\s+--hash=sha256:([0-9a-f]{64})( \\)?$", line)
        if h and cur is not None:
            cur["hashes"].append(h.group(1))
    return out


@pytest.fixture(scope="module")
def req(seed_dir) -> Path:
    return seed_dir / "requirements"


@pytest.mark.parametrize("name", LOCKS)
def test_every_package_is_pinned_with_hashes(req, name):
    lock = parse_lock((req / f"{name}.txt").read_text())
    assert lock, f"{name}.txt has no packages"
    for pkg, info in lock.items():
        assert info["hashes"], f"{name}: {pkg}=={info['version']} has no --hash"
    text = (req / f"{name}.txt").read_text()
    assert "--find-links" not in text and "--index-url" not in text and "git+" not in text and "file:" not in text


@pytest.mark.parametrize("name", LOCKS)
def test_lock_has_no_unpinned_or_url_requirements(req, name):
    for line in (req / f"{name}.txt").read_text().splitlines():
        if line.strip() and not line.startswith((" ", "#")):
            assert re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S+( \\)?$", line), line


def test_inputs_exist_for_every_lock(req):
    for name in LOCKS:
        assert (req / f"{name}.in").exists()


def test_klipper_klippy_lock_covers_klippys_runtime_requirements(req):
    pk = parse_lock((req / "klippy-klipper.txt").read_text())
    for need in ("cffi", "greenlet", "jinja2", "markupsafe", "pyserial", "python-can", "pip"):
        assert need in pk, need
    assert pk["pyserial"]["version"] == "3.4"        # Klipper and Moonraker pin it
    assert "numpy" not in pk                         # Klipper does not use it; Kalico does
    assert "setuptools" not in pk


def test_kalico_klippy_lock_has_numpy(req):
    pk = parse_lock((req / "klippy-kalico.txt").read_text())
    for need in ("cffi", "greenlet", "jinja2", "markupsafe", "numpy", "pyserial", "python-can", "pip"):
        assert need in pk, need
    assert pk["numpy"]["version"].startswith("2.3."), "Kalico pins numpy~=2.3.0 on Python 3.14"
    assert pk["greenlet"]["version"].startswith("3.3.")


def test_moonraker_lock_respects_the_caps_in_release_lock(req, repo):
    pk = parse_lock((req / "moonraker.txt").read_text())

    def ver(name):
        return tuple(int(x) for x in re.findall(r"\d+", pk[name]["version"])[:3])

    assert ver("tornado") <= (6, 5, 8)
    assert ver("pillow") <= (12, 3, 0)
    assert ver("dbus-fast") <= (5, 0, 22)
    assert ver("zeroconf") <= (0, 150, 0)
    assert ver("importlib-metadata") <= (9, 0, 0)
    assert pk["pyserial"]["version"] == "3.4"
    assert pk["jinja2"]["version"] == "3.1.6"
    for exact, v in (("distro", "1.9.0"), ("inotify-simple", "2.0.1"), ("libnacl", "2.1.0"), ("paho-mqtt", "2.1.0"),
                     ("preprocess-cancellation", "0.2.1"), ("ldap3", "2.9.1"), ("python-periphery", "2.4.1")):
        assert pk[exact]["version"] == v, exact
    # streaming-form-data has no aarch64 wheel: built from source, so only its dependencies are in the lock
    assert "streaming-form-data" not in pk
    assert "smart-open" in pk and "aiofiles" in pk


def test_moonraker_caps_text_in_release_lock_matches_this_test(repo):
    text = (repo / "release.lock").read_text()
    for cap in ("tornado<=6.5.8", "pillow<=12.3.0", "dbus-fast<=5.0.22", "zeroconf<=0.150.0", "importlib_metadata<=9.0.0", "pyserial==3.4"):
        assert cap in text, f"release.lock no longer records the cap {cap}; update moonraker.in and this test"


def test_klarity_lock_matches_pyproject(req, repo):
    py = tomllib.loads((repo / "klarity/pyproject.toml").read_text())
    want = set()
    for dep in py["project"]["dependencies"] + py["project"]["optional-dependencies"]["camera"]:
        want.add(re.split(r"[<>=!~ ]", dep)[0].lower().replace("_", "-"))
    inp = {re.split(r"[<>=!~ ]", ln)[0].lower().replace("_", "-")
           for ln in (req / "klarity.in").read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    assert inp == want, "klarity.in must equal [project].dependencies + the camera extra"
    pk = parse_lock((req / "klarity.txt").read_text())
    assert want <= set(pk)
    assert "klarity" not in pk                       # built from the repository


def test_lock_inputs_target_the_image_python(req, repo):
    py = tomllib.loads((repo / "release.lock").read_text())["python"]["version"]
    assert py.startswith("3.14."), "the seed is built for CPython 3.14 (cp314 wheels); update tools/seedtool.py if the image moves"


def test_sdists_are_pinned_by_url_and_digest(req):
    cfg = tomllib.loads((req / "sdists.toml").read_text())
    assert cfg["schema"] == 1
    names = set()
    for s in cfg["sdist"]:
        assert s["url"].startswith("https://files.pythonhosted.org/"), s["name"]
        assert re.fullmatch(r"[0-9a-f]{64}", s["sha256"]), s["name"]
        assert s["venv"] in ("klippy", "moonraker", "klarity")
        assert s["version"] in s["url"]
        names.add(s["name"])
        # its runtime dependencies must be resolvable from the lock (checked via the lock input)
    assert "streaming-form-data" in names


def test_lock_script_targets_the_image(seed_dir):
    text = (seed_dir / "tools/lock.sh").read_text()
    assert "--only-binary :all:" in text and "--generate-hashes" in text
    assert "aarch64-manylinux_2_40" in text, "wheels must not need a newer glibc than the image's 2.42"
