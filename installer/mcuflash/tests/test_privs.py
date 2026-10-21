# SPDX-License-Identifier: Apache-2.0
"""Root never runs the service user's files: spi_flash.py, firmware builds and git in its trees run as that user."""
import os
import pwd
import subprocess

import pytest

from fakes import make_image
from eitri_mcuflash import firmware as fw
from eitri_mcuflash import privs
from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.proc import RunResult, run

EITRI = pwd.struct_passwd(("eitri", "*", 1000, 1000, "Eitri", "/data/eitri/home", "/bin/bash"))


@pytest.fixture
def as_root(monkeypatch):
    """Pretend to be root (nothing is executed with another identity: commands are only built)."""
    monkeypatch.setattr(privs, "geteuid", lambda: 0)
    monkeypatch.setattr(privs, "getpwnam", lambda name: EITRI if name == "eitri" else (_ for _ in ()).throw(KeyError(name)))
    chowned = []
    monkeypatch.setattr(privs.os, "chown", lambda path, uid, gid, follow_symlinks=True: chowned.append((path, uid, gid, follow_symlinks)))
    return chowned


def test_not_root_runs_commands_unchanged():
    assert privs.as_service_user(["/data/x.sh", "-q"]) == ["/data/x.sh", "-q"] or os.geteuid() == 0


def test_root_runs_them_as_the_service_user_with_a_fresh_environment(as_root):
    argv = privs.as_service_user(["/data/eitri/klipper-layer/scripts/overlay-install.sh", "-q"])
    assert privs.parse_run_as(argv) == {"uid": 1000, "gid": 1000, "user": "eitri", "env": {},
                                        "argv": ["/data/eitri/klipper-layer/scripts/overlay-install.sh", "-q"]}
    assert argv[:3] == [privs.python, "-I", "-c"]                  # the image's interpreter, isolated: nothing from /data
    kept = privs.parse_run_as(privs.as_service_user(["make", "-j4"], keep_env={"PATH": "/tc/bin:/usr/bin"}))
    assert kept["env"] == {"PATH": "/tc/bin:/usr/bin"} and kept["argv"] == ["make", "-j4"]


def test_the_identity_switch_really_execs_with_a_clean_environment(tmp_path):
    """run_as() executed for real (to our own uid: switching to another one needs root)."""
    out = tmp_path / "env.txt"
    argv = privs.run_as(os.getuid(), os.getgid(), "", ["sh", "-c", 'env > "$0"; id -u >> "$0"', str(out)], keep_env={"KEEP": "1"})
    res = run(argv, env={"LEAK": "secret", "PYTHONPATH": "/data/evil"})
    assert res.returncode == 0, res.output
    text = out.read_text()
    assert "KEEP=1" in text and "LEAK" not in text and "PYTHONPATH" not in text and text.split()[-1] == str(os.getuid())
    res = run(privs.run_as(os.getuid() + 1, os.getgid(), "", ["true"]))
    assert res.returncode != 0 and "could not switch" in res.output        # never runs as someone it did not become
    if os.geteuid() != 0:
        res = run(privs.run_as(0, 0, "", ["true"]))
        assert res.returncode != 0


def test_root_refuses_when_the_service_user_is_missing_or_root(as_root, monkeypatch):
    with pytest.raises(FlashError, match="does not exist"):
        privs.as_service_user(["x"], user="nobody-here")
    monkeypatch.setattr(privs, "getpwnam", lambda name: pwd.struct_passwd(("eitri", "*", 0, 0, "", "/", "/bin/sh")))
    with pytest.raises(FlashError, match="is root"):
        privs.as_service_user(["x"])


def test_git_is_hardened_and_runs_as_the_service_user_when_root(as_root):
    parts = privs.parse_run_as(privs.git_argv("/data/firmware/klipper", "describe", "--dirty"))
    assert parts["uid"] == 1000 and parts["user"] == "eitri"
    assert parts["argv"] == ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-c", "safe.directory=",
                   "-C", "/data/firmware/klipper", "describe", "--dirty"]


def test_host_version_uses_the_hardened_git(as_root):
    calls = []
    fw.host_version("/data/firmware/klipper", runner=lambda argv, **kw: calls.append(argv) or RunResult(0, "v0.13.0-1-gabc\n"))
    git = privs.parse_run_as(calls[0])["argv"]
    assert git[0] == "git" and "core.fsmonitor=" in git and "core.hooksPath=/dev/null" in git


def test_a_firmware_build_runs_make_as_the_service_user_in_a_tree_handed_to_it(as_root, tmp_path):
    cfg = tmp_path / "toolhead.config"
    cfg.write_text("CONFIG_MACH_STM32=y\n")
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if argv[-1].startswith("-j"):
            out = [a for a in argv if a.startswith("OUT=")][0][4:]
            with open(os.path.join(out, "klipper.bin"), "wb") as fh:
                fh.write(make_image("v0.13.0-900-gabcdef012", size=2048))
        return RunResult(0, "")

    out = str(tmp_path / "out" / "toolhead")
    fw.build_artifact("toolhead", "klipper", klipper_dir=str(tmp_path), config_file=str(cfg), out_dir=out, runner=runner,
                      check_toolchain=False, toolchain_dir="/data/eitri/toolchain")
    owner = os.stat(str(tmp_path))                 # the clone's owner (on Eitri: eitri)
    parts = [privs.parse_run_as(c) for c in calls]
    assert all(p and p["uid"] == owner.st_uid and p["argv"][0] == "make" for p in parts), calls
    assert all(p["env"]["PATH"].startswith("/data/eitri/toolchain/bin") for p in parts)
    assert {(p, u, g) for p, u, g, _ in as_root} == {(out, owner.st_uid, owner.st_gid), (out + ".config", owner.st_uid, owner.st_gid)}
    assert all(f is False for *_, f in as_root)


def test_a_built_image_that_is_a_symlink_is_refused(tmp_path):
    cfg = tmp_path / "c"
    cfg.write_text("x")

    def runner(argv, **kw):
        if argv[-1].startswith("-j"):
            out = [a for a in argv if a.startswith("OUT=")][0][4:]
            os.symlink("/etc/shadow", os.path.join(out, "klipper.bin"))
        return RunResult(0, "")

    with pytest.raises(FlashError, match="not a plain file"):
        fw.build_artifact("toolhead", "klipper", klipper_dir=str(tmp_path), config_file=str(cfg), out_dir=str(tmp_path / "o"),
                          runner=runner, check_toolchain=False)


def _repo_with_traps(tmp_path):
    """A clone whose own config names an fsmonitor program and a hooks directory that leave marks when run."""
    repo, mark = tmp_path / "clone", tmp_path / "ran"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f").write_text("x")
    git = ["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=n"]
    subprocess.run(git + ["add", "f"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
    trap = tmp_path / "trap.sh"
    trap.write_text("#!/bin/sh\necho \"$0 $*\" >> %s\nexit 1\n" % mark)
    trap.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-index-change", "reference-transaction", "post-checkout"):
        (hooks / name).symlink_to(trap)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(trap)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
    (repo / "f").write_text("changed")            # status has to look at the index (and ask fsmonitor)
    return repo, mark


def test_the_trap_repository_really_runs_its_programs_without_the_hardening(tmp_path):
    repo, mark = _repo_with_traps(tmp_path)
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert mark.exists(), "git did not run core.fsmonitor: the hardening test below would prove nothing"


def test_a_root_owned_tree_runs_as_root_and_a_missing_one_as_the_service_user(as_root):
    assert privs.as_owner_of("/", ["/usr/bin/true"]) == ["/usr/bin/true"] or os.stat("/").st_uid != 0
    assert privs.parse_run_as(privs.as_owner_of("/nonexistent/clone", ["make"]))["user"] == "eitri"


def test_root_runs_git_as_the_owner_of_the_tree(as_root, tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    owner = os.stat(str(clone))
    monkeypatch.setattr(privs, "getpwuid", lambda uid: EITRI)
    argv = privs.git_argv(str(clone), "status")
    if owner.st_uid == 0:                          # a root-owned tree: nothing to switch to
        assert argv[0] == "git"
    else:
        parts = privs.parse_run_as(argv)
        assert (parts["uid"], parts["gid"], parts["user"]) == (owner.st_uid, owner.st_gid, "eitri")
        monkeypatch.setattr(privs, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
        assert privs.parse_run_as(privs.git_argv(str(clone), "status"))["user"] == ""   # an owner without an account: no groups


def test_hardened_git_runs_none_of_the_repositorys_programs(tmp_path):
    repo, mark = _repo_with_traps(tmp_path)
    res = run(privs.git_argv(str(repo), "status", "--porcelain"))
    assert res.returncode == 0 and "f" in res.output
    res = run(privs.git_argv(str(repo), "describe", "--always", "--dirty"))
    assert res.returncode == 0
    assert not mark.exists(), mark.read_text()


def test_spi_flash_from_the_clone_runs_as_the_service_user(as_root, tmp_path):
    """The mainboard's sdcard-serial flash runs Klipper's spi_flash.py out of the service user's clone: never as root."""
    from test_runner import NEW, Rig
    from eitri_mcuflash.runner import EX_OK
    rig = Rig(tmp_path)
    seen = []
    orig = rig.runner

    def runner(argv, **kw):
        if any(a.endswith("spi_flash.py") for a in argv):
            seen.append(list(argv))
            if "-d" in argv:
                seen.append(os.stat(argv[argv.index("-d") + 1]).st_mode & 0o777)
        return orig(argv, **kw)

    rig.ctx.runner = runner
    code, data = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_OK, data.get("error")
    assert rig.main.app_dictionary()["version"] == NEW
    parts = privs.parse_run_as(seen[0])
    owner = os.stat(str(rig.klipper)).st_uid       # the clone's owner (on Eitri: eitri)
    assert parts and parts["uid"] == owner and parts["argv"][1].endswith("spi_flash.py")
    assert seen[1] == 0o644                        # the dictionary file is readable by the service user
