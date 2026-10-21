# SPDX-License-Identifier: Apache-2.0
"""eitri-update through sudo (the eitri account, etc/sudoers.d/eitri) cannot be turned against root."""
import os
import pwd

import pytest

from helpers import Rig
from eitri_mcuflash import privs
from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.proc import RunResult
from eitri_update import cli
from eitri_update.upstream import UpstreamMode, UpstreamPaths

EITRI = pwd.struct_passwd(("eitri", "*", 1000, 1000, "Eitri", "/data/eitri/home", "/bin/bash"))
SUDO_ENV = {"SUDO_UID": "1000", "SUDO_USER": "eitri", "SUDO_COMMAND": "/usr/bin/eitri-update"}


class Reached(Exception):
    """The option check passed and eitri-update went on to build its updater."""


def reached(cfg, events):
    raise Reached(cfg)


@pytest.fixture
def root(monkeypatch):
    monkeypatch.setattr(privs, "geteuid", lambda: 0)
    monkeypatch.setattr(privs, "getpwnam", lambda name: EITRI if name == "eitri" else (_ for _ in ()).throw(KeyError(name)))


@pytest.mark.parametrize("argv, option", [
    (["--config", "/data/eitri/home/evil.conf", "status"], "--config"),
    (["--manifest-url", "file:///data/eitri/home/manifest.json", "check"], "--manifest-url"),
    (["--key-dir", "/data/eitri/home/keys", "upgrade", "--yes"], "--key-dir"),
    (["--data-root", "/data/eitri/home/fake", "upgrade", "--yes"], "--data-root"),
    (["--allow-insecure", "check"], "--allow-insecure"),
    (["--events-file", "/etc/sudoers.d/zz", "status"], "--events-file"),
    (["upgrade", "--yes", "--allow-downgrade", "--version", "2020.01.0"], "--allow-downgrade"),
])
def test_trust_affecting_options_are_refused_through_sudo(root, capsys, argv, option):
    code = cli.main(argv, updater_factory=reached, environ=SUDO_ENV)
    err = capsys.readouterr().err
    assert code == 2 and option in err and "through sudo" in err


def test_the_screens_flavor_switch_and_plain_commands_still_work_through_sudo(root):
    # installer/bin/eitri-switch-flavor's exact command line, and the plain subcommands
    for argv in (["--json", "--events-file", "/data/eitri/logs/update.jsonl", "switch-flavor", "--yes", "kalico"],
                 ["status"], ["check"], ["upgrade", "--yes"], ["--config", "/etc/eitri/update.conf", "--data-root", "/data", "rollback"]):
        with pytest.raises(Reached):
            cli.main(argv, updater_factory=reached, environ=SUDO_ENV)


def test_root_itself_keeps_every_option(root):
    with pytest.raises(Reached) as e:
        cli.main(["--data-root", "/srv/data", "--allow-insecure", "--manifest-url", "http://x/m.json", "check"],
                 updater_factory=reached, environ={})
    cfg = e.value.args[0]
    assert cfg.data_root == "/srv/data" and cfg.allow_insecure and cfg.manifest_url == "http://x/m.json"


def test_an_unprivileged_caller_is_not_restricted(monkeypatch):
    monkeypatch.setattr(privs, "geteuid", lambda: 1000)
    with pytest.raises(Reached):
        cli.main(["--data-root", "/tmp/x", "check"], updater_factory=reached, environ=SUDO_ENV)


def test_the_refusal_happens_before_anything_is_written(root, tmp_path):
    target = tmp_path / "victim"
    assert cli.main(["--events-file", str(target), "--data-root", str(tmp_path), "status"], updater_factory=reached, environ=SUDO_ENV) == 2
    assert not target.exists()


# --- nothing on /data runs as root ----------------------------------------------------------------------------------------

def test_the_klipper_layer_overlay_script_runs_as_the_service_user(root, tmp_path):
    rig = Rig(tmp_path)
    script = os.path.join(rig.data, "eitri", "klipper-layer", "scripts", "overlay-install.sh")
    os.makedirs(os.path.dirname(script))
    with open(script, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    calls = []
    rig.updater.runner = lambda argv, **kw: calls.append(list(argv)) or RunResult(0, "")
    rig.updater._overlay()
    assert len(calls) == 1
    parts = privs.parse_run_as(calls[0])
    assert parts and parts["uid"] == 1000 and parts["gid"] == 1000 and parts["user"] == "eitri"
    assert parts["argv"][0] == script and "--klipper-dir" in parts["argv"]
    assert calls[0][0] != script and calls[0][:2] == [privs.python, "-I"]    # root itself never executes the /data script


def test_no_service_user_means_the_overlay_is_not_run_as_root(root, tmp_path, monkeypatch):
    monkeypatch.setattr(privs, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    rig = Rig(tmp_path)
    script = os.path.join(rig.data, "eitri", "klipper-layer", "scripts", "overlay-install.sh")
    os.makedirs(os.path.dirname(script))
    open(script, "w").close()
    calls = []
    rig.updater.runner = lambda argv, **kw: calls.append(list(argv)) or RunResult(0, "")
    with pytest.raises(FlashError, match="service user"):
        rig.updater._overlay()
    assert not calls


def test_upstream_mode_inspects_the_clone_with_hardened_git_as_the_service_user(root, tmp_path):
    rig = Rig(tmp_path)
    paths = UpstreamPaths(rig.data, rig.data + "/printer_data")
    os.makedirs(os.path.join(paths.klipper, ".git"))
    calls = []
    mode = UpstreamMode(rig.state, rig.events, paths, runner=lambda argv, **kw: calls.append(list(argv)) or RunResult(0, ""))
    mode._check_tree("klipper")
    argv = calls[0]
    owner = os.stat(paths.klipper).st_uid
    if owner != 0:                                  # root runs git as the owner of the clone
        parts = privs.parse_run_as(argv)
        assert parts["uid"] == owner
        argv = parts["argv"]
    git = argv[argv.index("git"):]
    assert git[:7] == ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-c", "safe.directory="]
    assert git[7:] == ["-C", paths.klipper, "status", "--porcelain"]
