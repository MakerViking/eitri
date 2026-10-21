# SPDX-License-Identifier: Apache-2.0
"""seed.py (manifest checks, tool wrapper) and mcufw.py (hostile MCU firmware archives, swap/restore)."""
import copy
import io
import json
import os
import tarfile

import pytest

import seedpack
from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.proc import RunResult, run
from eitri_update import mcufw, seed as seedlib


@pytest.fixture
def good(tmp_path):
    info = seedpack.make_seed(str(tmp_path / "s"), "2026.10.0", "klipper")
    return info


def check(doc, **kw):
    a = seedpack_archive(kw.pop("good"))
    args = dict(flavor="klipper", version="2026.10.0", archive=a)
    args.update(kw)
    return seedlib.check_manifest(doc, **args)


def seedpack_archive(info):
    """What the signed document announces (not what the manifest under test claims)."""
    return {"name": info["name"], "sha256": info["sha256"], "size": info["size"]}


# ---- manifest ---------------------------------------------------------------------------------------------
def test_a_good_manifest_passes_and_reports_what_callers_need(good):
    info = check(good["doc"], good=good, release_flavor={"commit": good["commit"][:12], "describe": good["describe"]}, expect_abi=good["doc"]["target"]["python_abi"])
    assert info["commit"] == good["commit"] and info["describe"] == good["describe"] and info["uncompressed_size"] == good["unpacked"]


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("schema"), lambda d: d.update(schema=2), lambda d: d.update(kind="x"), lambda d: d.update(flavor="kalico"),
    lambda d: d.update(layout=0), lambda d: d.update(version="9"), lambda d: d.update(version=""), lambda d: d.update(version="../x"),
    lambda d: d.pop("archive"), lambda d: d["archive"].update(name="data-seed-2026.10.0-klipper.tar.gz"),
    lambda d: d["archive"].update(compression="gzip"), lambda d: d["archive"].update(tar_format="pax"),
    lambda d: d["archive"].update(sha256="ABC"), lambda d: d["archive"].update(size=0), lambda d: d["archive"].pop("uncompressed_size"),
    lambda d: d["archive"].update(uncompressed_size=True), lambda d: d["target"].update(arch="armv7"), lambda d: d["target"].pop("python_abi"),
    lambda d: d["target"].update(python_abi="python3"), lambda d: d["sources"]["firmware"].update(name="kalico"),
    lambda d: d["sources"]["firmware"].update(commit="abc"), lambda d: d["sources"]["firmware"].pop("describe"),
    lambda d: d.pop("sources"), lambda d: d["archive"].update(size=1),
])
def test_every_manifest_field_is_checked(good, mutate):
    doc = copy.deepcopy(good["doc"])
    mutate(doc)
    with pytest.raises(FlashError) as e:
        check(doc, good=good)
    assert e.value.code.startswith("seed")


def test_a_commit_the_release_does_not_pin_is_refused(good):
    with pytest.raises(FlashError) as e:
        check(good["doc"], good=good, release_flavor={"commit": "1" * 40})
    assert e.value.code == "seed_commit"


def test_an_unknown_interpreter_abi_is_refused(good):
    with pytest.raises(FlashError) as e:
        check(good["doc"], good=good, expect_abi="cp999")
    assert e.value.code == "seed_abi"


def test_archive_file_checks_size_then_digest(good, tmp_path):
    seedlib.check_archive_file(good["path"], good["sha256"], good["size"])
    with pytest.raises(FlashError):
        seedlib.check_archive_file(good["path"], good["sha256"], good["size"] + 1)
    with pytest.raises(FlashError):
        seedlib.check_archive_file(good["path"], "0" * 64)
    with pytest.raises(FlashError):
        seedlib.check_archive_file(str(tmp_path / "nope"), good["sha256"])


def test_load_manifest_rejects_garbage(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("[1]")
    with pytest.raises(FlashError):
        seedlib.load_manifest(str(p))
    p.write_text("{not json")
    with pytest.raises(FlashError):
        seedlib.load_manifest(str(p))
    with pytest.raises(FlashError):
        seedlib.load_manifest(str(tmp_path / "missing.json"))


def test_file_names_follow_the_seed_contract():
    assert seedlib.archive_name("2026.10.0", "kalico") == "data-seed-2026.10.0-kalico.tar.zst"
    assert seedlib.manifest_name("2026.10.0", "kalico") == "data-seed-2026.10.0-kalico.manifest.json"
    assert seedlib.safe_version("0.0.0.dev0+g1a2b3c4") and not seedlib.safe_version("../x") and not seedlib.safe_version("")


# ---- state files -------------------------------------------------------------------------------------------
def test_record_flavor_and_leftovers(tmp_path):
    data = tmp_path
    assert seedlib.read_record(str(data)) == {} and seedlib.installed_flavor(str(data)) is None
    os.makedirs(data / "eitri/state")
    (data / "eitri/state/seed-installed.env").write_text("# c\nSEED_FLAVOR=kalico\nSEED_VERSION=1\n")
    assert seedlib.installed_flavor(str(data)) == "kalico" and seedlib.read_record(str(data))["SEED_VERSION"] == "1"
    assert seedlib.leftovers(str(data)) == []
    os.makedirs(data / "venv/.klippy.seed-old")
    os.makedirs(data / "eitri/klarity/.venv.seed-old")
    assert seedlib.leftovers(str(data)) == ["venv/klippy", "eitri/klarity/venv"]


def test_find_tool_prefers_the_explicit_path_and_refuses_a_missing_one(tmp_path, monkeypatch):
    fake = tmp_path / "tool"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert seedlib.find_tool(str(fake)) == str(fake)
    monkeypatch.setenv("EITRI_SEED_INSTALL", str(tmp_path / "nothing"))
    assert os.path.basename(seedlib.find_tool()) == "eitri-seed-install"          # falls through to the checkout copy
    monkeypatch.setattr(seedlib, "TOOL_PATHS", ())
    monkeypatch.setattr(seedlib.os.path, "isfile", lambda p: False)
    with pytest.raises(FlashError) as e:
        seedlib.find_tool()
    assert e.value.code == "seed_tool"


def test_tool_wrapper_maps_exit_codes_and_never_uses_a_shell(tmp_path):
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        return RunResult(4, "conflict: flavor")

    tool = seedlib.SeedTool(runner, "/bin/true", "/data")
    with pytest.raises(FlashError) as e:
        tool.update("/x/data-seed-1-klipper.tar.zst", "a" * 64, "klipper", switch_flavor=True)
    assert e.value.code == "seed_conflict"
    argv = seen[0]
    assert isinstance(argv, list) and argv[:3] == ["/bin/true", "--data", "/data"] and "--keep-old" in argv and "--switch-flavor" in argv
    for code, name in ((3, "seed_integrity"), (5, "no_space"), (6, "seed_failed"), (9, "seed_failed")):
        tool = seedlib.SeedTool(lambda a, c=code, **k: RunResult(c, "x"), "/bin/true")
        with pytest.raises(FlashError) as e:
            tool.finish()
        assert e.value.code == name


# ---- MCU firmware archives ------------------------------------------------------------------------------------
def archive(tmp_path, flavor="klipper", **kw):
    return seedpack.make_mcu_archive(str(tmp_path / "mcu.tar.zst"), flavor, "2026.10.0", **kw)


def test_install_swap_restore_finish(tmp_path):
    root = str(tmp_path / "fw")
    os.makedirs(root + "/klipper")
    open(root + "/klipper/OLD", "w").write("old")
    res = mcufw.install(archive(tmp_path), "klipper", root)
    assert res == {"had_old": True} and os.path.isfile(root + "/klipper/manifest.json") and not os.path.exists(root + "/klipper/OLD")
    assert os.path.isfile(root + "/klipper/mainboard/klipper.bin")
    mcufw.restore(root, "klipper", True)
    assert os.path.isfile(root + "/klipper/OLD") and not os.path.exists(root + "/klipper/manifest.json")
    mcufw.install(archive(tmp_path), "klipper", root)
    mcufw.finish(root, "klipper")
    assert os.path.isfile(root + "/klipper/manifest.json") and not os.path.exists(root + "/.klipper.release-old")


def test_restore_without_a_previous_tree_removes_the_new_one(tmp_path):
    root = str(tmp_path / "fw")
    assert mcufw.install(archive(tmp_path, flavor="kalico"), "kalico", root) == {"had_old": False}
    mcufw.restore(root, "kalico", False)
    assert not os.path.exists(root + "/kalico")


def test_a_second_install_keeps_the_original_set_aside(tmp_path):
    root = str(tmp_path / "fw")
    os.makedirs(root + "/klipper")
    open(root + "/klipper/OLD", "w").write("old")
    mcufw.install(archive(tmp_path), "klipper", root)
    assert mcufw.install(archive(tmp_path), "klipper", root) == {"had_old": True}
    mcufw.restore(root, "klipper", True)
    assert open(root + "/klipper/OLD").read() == "old"


def test_other_flavors_in_the_archive_are_ignored(tmp_path):
    root = str(tmp_path / "fw")
    mcufw.install(archive(tmp_path, other_flavors=("kalico",)), "klipper", root)
    assert os.path.isdir(root + "/klipper") and not os.path.exists(root + "/kalico")


def link(name, target, kind=tarfile.SYMTYPE):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info


@pytest.mark.parametrize("extra", [
    [("../evil", b"x")], [("/abs/evil", b"x")], [("klipper/../../evil", b"x")],
    [(link("klipper/l", "/etc"), None)], [(link("klipper/h", "klipper/manifest.json", tarfile.LNKTYPE), None)],
    [(link("klipper/dev", "", tarfile.FIFOTYPE), None)],
])
def test_hostile_members_are_refused_before_anything_is_written(tmp_path, extra):
    root = str(tmp_path / "fw")
    with pytest.raises(FlashError) as e:
        mcufw.install(archive(tmp_path, extra=extra), "klipper", root)
    assert e.value.code == "bad_archive"
    assert not os.path.exists(root + "/klipper") and not os.path.exists(root + "/.klipper.release-new")


def test_archives_without_the_flavor_tree_or_a_manifest_are_refused(tmp_path):
    with pytest.raises(FlashError):
        mcufw.install(archive(tmp_path, flavor="kalico"), "klipper", str(tmp_path / "fw"))
    raw = str(tmp_path / "x.tar")
    with tarfile.open(raw, "w") as tf:
        info = tarfile.TarInfo("klipper/mainboard/klipper.bin")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    run(["zstd", "-q", "-f", raw, "-o", raw + ".zst"])
    with pytest.raises(FlashError) as e:
        mcufw.install(raw + ".zst", "klipper", str(tmp_path / "fw"))
    assert "manifest.json" in e.value.message and not os.path.exists(str(tmp_path / "fw/klipper"))


def test_an_oversized_archive_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(mcufw, "MAX_BYTES", 10)
    with pytest.raises(FlashError) as e:
        mcufw.install(archive(tmp_path), "klipper", str(tmp_path / "fw"))
    assert "more than" in e.value.message
