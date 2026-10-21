# SPDX-License-Identifier: Apache-2.0
"""bin/eitri-seed-install against synthetic seeds: the consumer contract of SPEC.md."""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import seedfix
from conftest import needs_git, needs_zstd

pytestmark = [needs_zstd, needs_git]

INSTALLER = Path(__file__).resolve().parent.parent / "bin" / "eitri-seed-install"
COMPONENTS = ["firmware/klipper", "firmware/moonraker", "venv/klippy", "venv/moonraker", "eitri/klarity/venv",
              "eitri/extras", "eitri/klipper-layer", "eitri/seed"]


def run(data: Path, arc: Path, sha: str, *extra: str, flavor: str = "klipper", env: dict | None = None):
    cmd = ["sh", str(INSTALLER), "--archive", str(arc), "--sha256", sha, "--flavor", flavor, "--data", str(data), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **(env or {})})


@pytest.fixture
def seed(tmp_path):
    stage = tmp_path / "stage"
    facts = seedfix.make_stage(stage, marker="one")
    arc, sha = seedfix.pack(stage, tmp_path)
    return {"arc": arc, "sha": sha, "facts": facts, "tmp": tmp_path}


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


def snapshot(root: Path) -> dict[str, tuple]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = ("l", os.readlink(p))
        elif p.is_dir():
            out[rel] = ("d",)
        else:
            out[rel] = ("f", p.read_bytes())
    return out


def test_install_into_blank_data(seed, data):
    r = run(data, seed["arc"], seed["sha"])
    assert r.returncode == 0, r.stderr + r.stdout
    for c in COMPONENTS:
        assert (data / c).is_dir(), c
    assert (data / "eitri/flavor").read_text().strip() == "klipper"
    assert (data / "firmware/klipper/.git").is_dir()
    assert os.readlink(data / "venv/klippy/bin/python") == "/usr/bin/python3"
    mark = (data / "eitri/state/seed-installed.env").read_text()
    assert "SEED_FLAVOR=klipper" in mark and f"SEED_FIRMWARE_COMMIT={seed['facts']['fw']}" in mark
    assert not (data / ".eitri-seed-staging").exists(), "staging directory must be removed"


def test_mtimes_survive_so_klippy_accepts_the_prebuilt_helper(seed, data):
    assert run(data, seed["arc"], seed["sha"]).returncode == 0
    chdir = data / "firmware/klipper/klippy/chelper"
    assert int((chdir / "c_helper.so").stat().st_mtime) == seedfix.EPOCH + 1
    assert int((chdir / "a.c").stat().st_mtime) == seedfix.EPOCH
    # klippy: rebuild only when a source is newer than the library
    assert not max(p.stat().st_mtime for p in chdir.glob("*.c")) > (chdir / "c_helper.so").stat().st_mtime


def test_install_is_idempotent(seed, data):
    assert run(data, seed["arc"], seed["sha"]).returncode == 0
    before = snapshot(data)
    r = run(data, seed["arc"], seed["sha"])
    assert r.returncode == 0, r.stderr
    assert "already installed" in r.stdout or "nothing to do" in r.stdout
    assert snapshot(data) == before


def test_install_replaces_empty_precreated_directories_only(seed, data):
    # data-init creates these empty before the seed arrives
    for d in ("firmware", "venv/klippy", "venv/moonraker", "eitri/klarity", "eitri/extras"):
        (data / d).mkdir(parents=True)
    assert run(data, seed["arc"], seed["sha"]).returncode == 0
    assert (data / "venv/klippy/pyvenv.cfg").exists()
    assert (data / "eitri/extras/qd_x.py").exists()


def test_install_never_clobbers_user_data(seed, data):
    cfg = data / "printer_data/config"
    cfg.mkdir(parents=True)
    (cfg / "printer.cfg").write_text("[mcu]\nserial: /dev/x\n")
    (cfg / "klarity.cfg").write_text("mine\n")
    (data / "printer_data/gcodes").mkdir()
    (data / "printer_data/gcodes/a.gcode").write_text("G28\n")
    mine = data / "firmware/klipper"
    mine.mkdir(parents=True)
    (mine / "my-file").write_text("user content")
    r = run(data, seed["arc"], seed["sha"])
    assert r.returncode == 0, r.stderr
    assert (cfg / "printer.cfg").read_text() == "[mcu]\nserial: /dev/x\n"
    assert (cfg / "klarity.cfg").read_text() == "mine\n"
    assert (data / "printer_data/gcodes/a.gcode").read_text() == "G28\n"
    assert (mine / "my-file").read_text() == "user content", "a non-empty component is never overwritten in install mode"
    assert not (mine / ".git").exists()
    assert (data / "firmware/moonraker/.git").is_dir(), "the other components are still installed"
    assert "firmware/klipper" in (data / "eitri/state/seed-installed.env").read_text().split("SEED_KEPT=")[1]


def test_wrong_digest_changes_nothing(seed, data):
    r = run(data, seed["arc"], "0" * 64)
    assert r.returncode == 3 and "SHA-256" in r.stderr
    assert list(data.iterdir()) == []


def test_malformed_digest_is_a_usage_error(seed, data):
    assert run(data, seed["arc"], "xyz").returncode == 2
    assert run(data, seed["arc"], seed["sha"].upper()).returncode == 2


def test_flavor_must_match_archive_name_and_contents(seed, data):
    r = run(data, seed["arc"], seed["sha"], flavor="kalico")
    assert r.returncode == 3 and list(data.iterdir()) == []
    # a klipper archive renamed to look like kalico: the content check catches it
    renamed = seed["tmp"] / "data-seed-9.9.9-kalico.tar.zst"
    shutil.copy(seed["arc"], renamed)
    r = run(data, renamed, seed["sha"], flavor="kalico")
    assert r.returncode == 3, r.stderr
    assert list(data.iterdir()) == [] or not (data / "firmware").exists()


def test_existing_flavor_file_must_agree(seed, data):
    (data / "eitri").mkdir()
    (data / "eitri/flavor").write_text("kalico\n")
    r = run(data, seed["arc"], seed["sha"])
    assert r.returncode == 4 and "kalico" in r.stderr
    assert not (data / "firmware").exists()


@pytest.mark.parametrize("members,why", [
    ([(seedfix.tinfo("eitri/flavor", size=8), b"klipper\n"), seedfix.tinfo("../evil", size=1)], "traversal"),
    ([seedfix.tinfo("/tmp/evil", size=0)], "absolute"),
    ([seedfix.tinfo("printer_data/config/printer.cfg")], "outside layout"),
    ([seedfix.tinfo("firmware/klipper/x", tarfile.SYMTYPE, 0o777, "/root/.ssh/authorized_keys")], "symlink out"),
    ([seedfix.tinfo("eitri/flavor"), seedfix.tinfo("eitri/extras/h", tarfile.LNKTYPE, 0o644, "eitri/flavor")], "hard link"),
    ([seedfix.tinfo("firmware/klipper/su", mode=0o4755)], "set-uid"),
    ([seedfix.tinfo("firmware/klipper/dev", tarfile.CHRTYPE)], "device"),
    ([seedfix.tinfo("firmware/klipper/a b")], "space in name"),
])
def test_hostile_archives_are_refused_before_anything_is_written(tmp_path, data, members, why):
    arc, sha = seedfix.raw_archive(tmp_path / "data-seed-9.9.9-klipper.tar.zst", members)
    r = run(data, arc, sha)
    assert r.returncode == 3, (why, r.stdout, r.stderr)
    assert "refused" in r.stderr
    assert list(data.iterdir()) == []
    assert not (tmp_path / "evil").exists() and not Path("/tmp/evil").exists()


def test_archive_without_seed_info_is_refused(tmp_path, data):
    arc, sha = seedfix.raw_archive(tmp_path / "data-seed-9.9.9-klipper.tar.zst",
                                   [seedfix.tinfo("eitri", tarfile.DIRTYPE, 0o755), (seedfix.tinfo("eitri/flavor", size=8), b"klipper\n")])
    r = run(data, arc, sha)
    assert r.returncode == 3 and "seed-info.env" in r.stderr
    assert not (data / "eitri/flavor").exists()


def test_not_enough_space(seed, data):
    r = run(data, seed["arc"], seed["sha"], env={"EITRI_SEED_FAKE_FREE_KIB": "100"})
    assert r.returncode == 5 and list(data.iterdir()) == []


def test_dry_run_changes_nothing(seed, data):
    r = run(data, seed["arc"], seed["sha"], "--dry-run")
    assert r.returncode == 0 and "dry run" in r.stdout
    assert list(data.iterdir()) == []


def test_interrupted_install_resumes(seed, data):
    # as if power failed after two components were renamed into place and before the marker was written
    assert run(data, seed["arc"], seed["sha"]).returncode == 0
    (data / "eitri/state/seed-installed.env").unlink()
    shutil.rmtree(data / "venv/moonraker")
    shutil.rmtree(data / "eitri/extras")
    r = run(data, seed["arc"], seed["sha"])
    assert r.returncode == 0, r.stderr
    assert (data / "venv/moonraker/pyvenv.cfg").exists() and (data / "eitri/extras/qd_x.py").exists()
    assert (data / "eitri/state/seed-installed.env").exists()


def test_manifest_cross_check(seed, data, tmp_path):
    import seedlib

    m = seedfix.good_manifest()
    m["archive"]["sha256"] = seed["sha"]
    mp = tmp_path / "m.json"
    seedlib.write_json(mp, m)
    assert run(data, seed["arc"], seed["sha"], "--manifest", str(mp)).returncode == 0
    m["archive"]["sha256"] = "b" * 64
    seedlib.write_json(mp, m)
    d2 = tmp_path / "data2"
    d2.mkdir()
    assert run(d2, seed["arc"], seed["sha"], "--manifest", str(mp)).returncode == 3


# --- update mode -----------------------------------------------------------------------------------------------

@pytest.fixture
def two_seeds(tmp_path):
    out = {}
    for name, marker, ver in (("old", "one", "1.0.0"), ("new", "two", "2.0.0")):
        stage = tmp_path / f"stage-{name}"
        facts = seedfix.make_stage(stage, marker=marker, version=ver)
        arc, sha = seedfix.pack(stage, tmp_path, version=ver)
        out[name] = {"arc": arc, "sha": sha, "facts": facts}
    return out


def test_update_swaps_components_and_keeps_user_data(two_seeds, data):
    assert run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"]).returncode == 0
    (data / "printer_data/config").mkdir(parents=True)
    (data / "printer_data/config/printer.cfg").write_text("keep me")
    (data / "eitri/state/other").write_text("os state")
    r = run(data, two_seeds["new"]["arc"], two_seeds["new"]["sha"], "--mode", "update")
    assert r.returncode == 0, r.stderr + r.stdout
    assert (data / "venv/klippy/marker").read_text() == "two"
    assert (data / "firmware/klipper/README").read_text() == "firmware two"
    assert (data / "printer_data/config/printer.cfg").read_text() == "keep me"
    assert (data / "eitri/state/other").read_text() == "os state"
    assert "SEED_VERSION=2.0.0" in (data / "eitri/state/seed-installed.env").read_text()
    assert not list(data.rglob(".*.seed-old")), "old trees are deleted after a successful update"


def test_update_keep_old_then_finish(two_seeds, data):
    assert run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"]).returncode == 0
    r = run(data, two_seeds["new"]["arc"], two_seeds["new"]["sha"], "--mode", "update", "--keep-old")
    assert r.returncode == 0, r.stderr
    assert (data / "venv/.klippy.seed-old/marker").read_text() == "one"
    assert (data / "eitri/state/seed-installed.env.prev").exists()
    r = subprocess.run(["sh", str(INSTALLER), "--mode", "finish", "--data", str(data)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not list(data.rglob(".*.seed-old")) and not (data / "eitri/state/seed-installed.env.prev").exists()
    assert (data / "venv/klippy/marker").read_text() == "two"


def test_update_keep_old_then_rollback(two_seeds, data):
    assert run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"]).returncode == 0
    before = snapshot(data)
    assert run(data, two_seeds["new"]["arc"], two_seeds["new"]["sha"], "--mode", "update", "--keep-old").returncode == 0
    assert snapshot(data) != before
    r = subprocess.run(["sh", str(INSTALLER), "--mode", "rollback", "--data", str(data)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    after = snapshot(data)
    after.pop("eitri/state/seed-installed.env.prev", None)
    assert after == before, "rollback restores every tree, the record of what is installed and the flavor"


def test_rollback_restores_a_flavor_switch(tmp_path, data):
    sk, sc = tmp_path / "sk", tmp_path / "sc"
    seedfix.make_stage(sk, flavor="klipper", version="1.0.0")
    ak, hk = seedfix.pack(sk, tmp_path, "1.0.0", "klipper")
    seedfix.make_stage(sc, flavor="kalico", version="1.0.0", marker="cal")
    ac, hc = seedfix.pack(sc, tmp_path, "1.0.0", "kalico")
    assert run(data, ak, hk).returncode == 0
    assert run(data, ac, hc, "--mode", "update", "--switch-flavor", "--keep-old", flavor="kalico").returncode == 0
    assert (data / "eitri/flavor").read_text().strip() == "kalico"
    assert subprocess.run(["sh", str(INSTALLER), "--mode", "rollback", "--data", str(data)], capture_output=True).returncode == 0
    assert (data / "eitri/flavor").read_text().strip() == "klipper"
    assert (data / "venv/klippy/marker").read_text() == "one"


def test_a_second_update_replaces_kept_copies(two_seeds, data):
    assert run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"]).returncode == 0
    assert run(data, two_seeds["new"]["arc"], two_seeds["new"]["sha"], "--mode", "update", "--keep-old").returncode == 0
    assert run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"], "--mode", "update").returncode == 0
    assert not list(data.rglob(".*.seed-old"))
    assert (data / "venv/klippy/marker").read_text() == "one"


def test_concurrent_run_is_refused_and_stale_lock_is_taken_over(two_seeds, data):
    lock = data / ".eitri-seed.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))      # a live process: this test
    r = run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"])
    assert r.returncode == 6 and "running" in r.stderr
    (lock / "pid").write_text("999999999")           # not a process
    r = run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"])
    assert r.returncode == 0, r.stderr
    assert not lock.exists(), "the lock is released"


def test_lock_is_released_after_a_refusal(seed, data):
    assert run(data, seed["arc"], "0" * 64).returncode == 3
    assert not (data / ".eitri-seed.lock").exists()


def test_update_does_not_replace_a_tree_the_user_moved_forward(two_seeds, data):
    assert run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"]).returncode == 0
    klipper = data / "firmware/klipper"
    (klipper / "README").write_text("my change\n")
    seedfix.git(klipper, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-am", "pulled something newer")
    r = run(data, two_seeds["new"]["arc"], two_seeds["new"]["sha"], "--mode", "update")
    assert r.returncode == 0, r.stderr
    assert (klipper / "README").read_text() == "my change\n"
    assert "moved forward" in r.stdout
    assert (data / "venv/klippy/marker").read_text() == "one", "the venv of a kept tree is kept as well"
    assert (data / "venv/moonraker/marker").read_text() == "two", "the independent pair is updated"
    # --force replaces it
    r = run(data, two_seeds["new"]["arc"], two_seeds["new"]["sha"], "--mode", "update", "--force")
    assert r.returncode == 0
    assert (klipper / "README").read_text() == "firmware two"


def test_update_refuses_a_flavor_change_unless_asked(tmp_path, data):
    sk = tmp_path / "sk"
    seedfix.make_stage(sk, flavor="klipper", version="1.0.0")
    ak, hk = seedfix.pack(sk, tmp_path, "1.0.0", "klipper")
    sc = tmp_path / "sc"
    seedfix.make_stage(sc, flavor="kalico", version="1.0.0", marker="cal")
    ac, hc = seedfix.pack(sc, tmp_path, "1.0.0", "kalico")
    assert run(data, ak, hk).returncode == 0
    r = run(data, ac, hc, "--mode", "update", flavor="kalico")
    assert r.returncode == 4
    assert (data / "venv/klippy/marker").read_text() == "one"
    r = run(data, ac, hc, "--mode", "update", "--switch-flavor", flavor="kalico")
    assert r.returncode == 0, r.stderr
    assert (data / "eitri/flavor").read_text().strip() == "kalico"
    assert (data / "venv/klippy/marker").read_text() == "cal"


def test_failed_update_rolls_back(two_seeds, data, tmp_path):
    assert run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"]).returncode == 0
    before = snapshot(data)
    # Make the swap of the third component fail: replace its parent with a file the staging move cannot satisfy.
    # A read-only parent directory is the portable way to make rename fail for a non-root user.
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    os.chmod(data / "eitri/klarity", 0o555)
    try:
        r = run(data, two_seeds["new"]["arc"], two_seeds["new"]["sha"], "--mode", "update")
    finally:
        os.chmod(data / "eitri/klarity", 0o755)
    assert r.returncode == 6, r.stdout + r.stderr
    assert snapshot(data) == before, "every swapped component is put back"
    assert not list(data.rglob(".*.seed-old"))


def test_interrupted_update_is_recovered_at_the_next_run(two_seeds, data):
    assert run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"]).returncode == 0
    # crash between the two renames: the tree is only available as .seed-old
    os.rename(data / "venv/klippy", data / "venv/.klippy.seed-old")
    r = run(data, two_seeds["old"]["arc"], two_seeds["old"]["sha"])
    assert r.returncode == 0, r.stderr
    assert (data / "venv/klippy/marker").read_text() == "one"
    assert not (data / "venv/.klippy.seed-old").exists()
