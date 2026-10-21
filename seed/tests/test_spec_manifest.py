# SPDX-License-Identifier: Apache-2.0
"""The manifest schema and the archive policy of SPEC.md, as code."""
from __future__ import annotations

import copy
import json
import tarfile

import pytest
import seedfix
import seedlib


def test_good_manifest_validates():
    assert seedlib.validate_manifest(seedfix.good_manifest()) == []
    assert seedlib.validate_manifest(seedfix.good_manifest("2026.10.0", "kalico")) == []


def mutate(path: str, value):
    m = seedfix.good_manifest()
    cur = m
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur[k]
    if value is KeyError:
        del cur[keys[-1]]
    else:
        cur[keys[-1]] = value
    return m


@pytest.mark.parametrize("path,value", [
    ("schema", 2),
    ("kind", "other"),
    ("layout", 2),
    ("version", "bad version"),
    ("flavor", "marlin"),
    ("archive.name", "data-seed-9.9.9-kalico.tar.zst"),     # disagrees with flavor
    ("archive.sha256", "A" * 64),                            # upper case
    ("archive.sha256", KeyError),
    ("archive.size", 0),
    ("archive.compression", "gzip"),
    ("created", "2026-10-21 12:00"),
    ("source_date_epoch", 0),
    ("target.arch", "x86_64"),
    ("target.python", "3.14"),
    ("target.python_abi", "cp313"),
    ("target.interpreter", "/data/venv/python"),
    ("target.glibc_max", "glibc"),
    ("sources.firmware.commit", "abc"),
    ("sources.firmware.describe", ""),
    ("sources.firmware.origin", "git@github.com:x/y"),
    ("sources.firmware.name", "kalico"),
    ("sources.moonraker.commit", KeyError),
    ("sources.klarity.wheel_sha256", "x"),
    ("sources.klipper_layer.tree_sha256", KeyError),
    ("venvs.klippy.lock_sha256", "x"),
    ("venvs.moonraker.packages", []),
    ("venvs.klarity.packages", [{"name": "x"}]),
    ("chelper.sha256", KeyError),
    ("chelper.path", "klippy/chelper/c_helper.so"),
    ("tree.digest", "short"),
    ("tree.entries", 0),
    ("tree.components", ["firmware/klipper"]),
])
def test_bad_manifest_is_rejected(path, value):
    assert seedlib.validate_manifest(mutate(path, value)) != [], path


def test_built_from_source_needs_both_digests():
    m = seedfix.good_manifest()
    m["venvs"]["moonraker"]["built_from_source"] = [{"name": "streaming-form-data", "sdist_sha256": "a" * 64}]
    assert seedlib.validate_manifest(m)
    m["venvs"]["moonraker"]["built_from_source"][0]["wheel_sha256"] = "b" * 64
    assert seedlib.validate_manifest(m) == []


def test_archive_name_must_match_the_file():
    assert seedlib.validate_manifest(seedfix.good_manifest(), archive_name="data-seed-9.9.9-klipper.tar.zst") == []
    assert seedlib.validate_manifest(seedfix.good_manifest(), archive_name="renamed.tar.zst")


@pytest.mark.parametrize("name,ok", [
    ("data-seed-2026.10.0-klipper.tar.zst", True),
    ("data-seed-0.0.0.dev0-kalico.tar.zst", True),
    ("data-seed-2026.10.0-rc.1+g123-klipper.tar.zst", True),
    ("data-seed-2026.10.0-marlin.tar.zst", False),
    ("data-seed-klipper.tar.zst", False),
    ("data-seed-1-klipper.tar.gz", False),
    ("../data-seed-1-klipper.tar.zst", False),
])
def test_archive_file_names(name, ok):
    assert bool(seedlib.ARCHIVE_RE.match(name)) is ok


def test_manifest_file_name_pattern():
    m = seedlib.MANIFEST_RE.match("data-seed-2026.10.0-kalico.manifest.json")
    assert m and m["version"] == "2026.10.0" and m["flavor"] == "kalico"
    assert seedlib.seed_names("1.2.3", "klipper") == ("data-seed-1.2.3-klipper.tar.zst", "data-seed-1.2.3-klipper.manifest.json")


def test_component_contract():
    # The /data layout every other component relies on (STATUS.md, os data-init, systemd units).
    assert set(seedlib.COMPONENTS) >= {
        "firmware/klipper", "firmware/moonraker", "venv/klippy", "venv/moonraker", "eitri/klarity/venv", "eitri/extras",
    }
    assert "eitri/flavor" in seedlib.FILES
    assert not any(c.startswith("printer_data") for c in seedlib.COMPONENTS), "a seed must never ship printer_data"


def member(name, typ=tarfile.REGTYPE, mode=0o644, link=""):
    return seedfix.tinfo(name, typ, mode, link)


@pytest.mark.parametrize("args,bad", [
    (("firmware/klipper/klippy/klippy.py",), False),
    (("eitri/flavor",), False),
    (("eitri", tarfile.DIRTYPE, 0o755), False),
    (("venv/klippy/bin/python", tarfile.SYMTYPE, 0o777, "/usr/bin/python3"), False),
    (("eitri/klipper-layer/extras", tarfile.SYMTYPE, 0o777, "../extras"), False),
    (("firmware/klipper/klippy/extras/qd_probe.py", tarfile.SYMTYPE, 0o777, "/data/eitri/extras/qd_probe.py"), False),
    (("printer_data/config/printer.cfg",), True),                       # outside the layout
    (("eitri/state/seed-installed.env",), True),                        # OS state is not the seed's
    (("eitri/klarity/other",), True),
    (("../evil",), True),
    (("/etc/passwd",), True),
    (("firmware/../../evil",), True),
    (("./firmware/klipper/x",), True),
    (("firmware//klipper/x",), True),
    (("firmware/klipper/x", tarfile.LNKTYPE, 0o644, "eitri/flavor"), True),   # hard link
    (("firmware/klipper/dev", tarfile.CHRTYPE), True),
    (("firmware/klipper/fifo", tarfile.FIFOTYPE), True),
    (("firmware/klipper/su", tarfile.REGTYPE, 0o4755), True),
    (("firmware/klipper/sg", tarfile.REGTYPE, 0o2755), True),
    (("firmware/klipper/lnk", tarfile.SYMTYPE, 0o777, "/root/.ssh/authorized_keys"), True),
    (("firmware/klipper/lnk", tarfile.SYMTYPE, 0o777, "../../../../etc/shadow"), True),
    (("venv/klippy/lnk", tarfile.SYMTYPE, 0o777, "../../../.."), True),
])
def test_member_policy(args, bad):
    assert (seedlib.check_member(member(*args)) is not None) is bad, args


def test_tree_digest_ignores_owner_mtime_and_seed_info(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "eitri/seed").mkdir(parents=True)
        (d / "x").write_text("hello")
        (d / "eitri/seed/seed-info.json").write_text(str(d))
    import os

    os.utime(b / "x", (5, 5))
    assert seedlib.tree_digest(a) == seedlib.tree_digest(b)
    (b / "x").write_text("hellp")
    assert seedlib.tree_digest(a)[0] != seedlib.tree_digest(b)[0]
    (b / "x").write_text("hello")
    os.chmod(b / "x", 0o755)
    assert seedlib.tree_digest(a)[0] != seedlib.tree_digest(b)[0]


def test_tree_digest_covers_symlink_targets(tmp_path):
    import os

    a, b = tmp_path / "a", tmp_path / "b"
    for d, t in ((a, "one"), (b, "two")):
        d.mkdir()
        os.symlink(t, d / "l")
    assert seedlib.tree_digest(a)[0] != seedlib.tree_digest(b)[0]


def test_tar_is_deterministic_and_numeric_owner(tmp_path):
    import io

    stage = tmp_path / "s"
    seedfix.make_stage(stage)
    bufs = []
    for _ in range(2):
        buf = io.BytesIO()
        seedlib.write_tar(stage, buf)
        bufs.append(buf.getvalue())
    assert bufs[0] == bufs[1]
    with tarfile.open(fileobj=io.BytesIO(bufs[0])) as t:
        names = t.getnames()
        assert names == sorted(names, key=lambda n: n.encode())
        assert all(m.uid == 1000 and m.gid == 1000 and not m.uname and not m.gname for m in t.getmembers())
        assert all(m.mtime == seedfix.EPOCH or m.name.endswith("c_helper.so") for m in t.getmembers())
    assert bufs[0][257:265] == b"ustar  \0", "GNU tar magic expected"


def test_synthetic_archive_passes_the_policy(tmp_path):
    stage = tmp_path / "s"
    seedfix.make_stage(stage)
    arc, _ = seedfix.pack(stage, tmp_path)
    assert seedlib.check_archive(arc) == []


def test_load_lock_pins_agree_with_the_updater(repo):
    """Origins and branches in the seed must be the ones eitri-update's upstream mode points Moonraker at."""
    import ast

    src = (repo / "installer/eitri-update/eitri_update/upstream.py").read_text()
    default = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DEFAULT_ORIGINS":
            default = ast.literal_eval(node.value)
    assert default
    for flavor in seedlib.FLAVORS:
        p = seedlib.pins(repo, flavor)
        assert p["firmware"]["origin"] == default[flavor]["origin"]
        assert p["firmware"]["branch"] == default[flavor]["branch"]
        assert p["moonraker"]["origin"] == default["moonraker"]["origin"]
        assert p["moonraker"]["branch"] == default["moonraker"]["branch"]
        assert seedlib.COMMIT_RE.match(p["firmware"]["commit"]) and seedlib.COMMIT_RE.match(p["moonraker"]["commit"])


def test_json_roundtrip_canonical(tmp_path):
    p = tmp_path / "m.json"
    m = seedfix.good_manifest()
    seedlib.write_json(p, m)
    assert json.loads(p.read_text()) == m
    assert p.read_text().endswith("\n")
    assert list(json.loads(p.read_text())) == sorted(m)
