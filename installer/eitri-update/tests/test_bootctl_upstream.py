# SPDX-License-Identifier: Apache-2.0
import os
import stat

import pytest

from helpers import Rig, make_toolchain_archive
from eitri_mcuflash.errors import FlashError
from eitri_update import bootctl as bootctl_cli
from eitri_update.bootenv import BootControl, parse, serialize
from eitri_update.upstream import UpstreamMode, UpstreamPaths, render_upstream_update_manager


# ---- boot control -----------------------------------------------------------------------
def test_file_format_matches_what_uboot_exports(tmp_path):
    values = {"eitri_mode": "eitri", "eitri_slot": "B", "eitri_tries": "2", "eitri_state_A": "good", "eitri_state_B": "unknown"}
    text = serialize(values)
    assert text.splitlines() == ["eitri_mode=eitri", "eitri_slot=B", "eitri_tries=2", "eitri_state_A=good", "eitri_state_B=unknown"]
    assert parse(text + "\0\0\0") == values                     # U-Boot pads/terminates with NULs


def test_rauc_handler_contract(tmp_path, capsys):
    rig = Rig(tmp_path)
    ctl = rig.bootctl
    assert bootctl_cli.main(["get-primary"], ctl) == 0 and capsys.readouterr().out.strip() == "A"
    assert bootctl_cli.main(["get-current"], ctl) == 0 and capsys.readouterr().out.strip() == "A"
    assert bootctl_cli.main(["get-state", "A"], ctl) == 0 and capsys.readouterr().out.strip() == "good"
    assert bootctl_cli.main(["get-state", "B"], ctl) == 0 and capsys.readouterr().out.strip() == "bad"
    assert bootctl_cli.main(["set-primary", "B"], ctl) == 0
    env = ctl.read()
    assert env["eitri_slot"] == "B" and env["eitri_tries"] == "3" and env["eitri_mode"] == "eitri"
    assert env["eitri_state_B"] == "bad"                       # set-primary never promotes a slot to good
    assert bootctl_cli.main(["set-state", "B", "good"], ctl) == 0 and ctl.read()["eitri_state_B"] == "good"
    assert bootctl_cli.main(["set-state", "B", "maybe"], ctl) == 1
    assert bootctl_cli.main(["set-primary", "C"], ctl) == 1
    assert bootctl_cli.main(["nonsense"], ctl) == 2


def test_marking_the_current_slot_bad_forces_fallback_on_next_boot(tmp_path):
    rig = Rig(tmp_path)
    rig.bootctl.set_state("A", "bad")
    assert rig.bootctl.read()["eitri_tries"] == "0"


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    rig = Rig(tmp_path)
    rig.bootctl.update(eitri_tries="1")
    assert sorted(os.listdir(os.path.join(rig.boot, "eitri"))) == ["boot.env"]
    assert open(os.path.join(rig.boot, "eitri", "boot.env")).read().count("\n") == 5


def test_readonly_boot_is_remounted_around_the_write(tmp_path):
    rig = Rig(tmp_path)
    os.chmod(os.path.join(rig.boot, "eitri"), stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(os.path.join(rig.boot, "eitri"), os.W_OK):
            pytest.skip("running as root: directory permissions are not enforced")

        def runner(argv, **kw):
            rig.run.calls.append(argv)
            if argv[:3] == ["mount", "-o", "remount,rw"]:
                os.chmod(os.path.join(rig.boot, "eitri"), 0o755)
            return type("R", (), {"returncode": 0, "output": ""})()

        ctl = BootControl(rig.boot, runner=runner, cmdline_path=rig.cmdline, lock_path=os.path.join(str(tmp_path), "l"))
        ctl.update(eitri_tries="2")
        assert ["mount", "-o", "remount,rw", rig.boot] in rig.run.calls and ["mount", "-o", "remount,ro", rig.boot] in rig.run.calls
    finally:
        os.chmod(os.path.join(rig.boot, "eitri"), 0o755)


def test_not_booted_through_eitri_boot_manager(tmp_path):
    rig = Rig(tmp_path)
    open(rig.cmdline, "w").write("root=/dev/mmcblk1p2 rw\n")
    with pytest.raises(FlashError, match="eitri.slot"):
        rig.bootctl.current_slot()


# ---- upstream mode -------------------------------------------------------------------------
def upstream_rig(tmp_path, **kw):
    rig = Rig(tmp_path, **kw)
    paths = UpstreamPaths(rig.data, rig.data + "/printer_data")
    os.makedirs(os.path.join(paths.klipper, ".git"))
    archive = make_toolchain_archive(str(tmp_path / "toolchain.tar.zst"))
    rig.channel.add_toolchain(archive)
    return rig, paths, UpstreamMode(rig.state, rig.events, paths, runner=rig.run)


def test_enable_installs_toolchain_and_flips_update_manager(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path)
    mode.enable(rig.updater.fetch_manifest())
    assert os.path.isfile(os.path.join(paths.toolchain, "bin", "arm-none-eabi-gcc"))
    assert open(os.path.join(paths.toolchain, "VERSION")).read().strip() == "13.3-2026.10"
    assert os.path.islink(paths.link) and os.path.basename(os.readlink(paths.link)) == "update_manager.upstream.conf"
    conf = open(os.path.join(paths.templates, "update_manager.upstream.conf")).read()
    assert "origin: https://github.com/Klipper3d/klipper.git" in conf and "path: %s" % paths.klipper in conf
    assert "enable_system_updates: False" in conf and "managed_services: klipper" in conf
    assert rig.state.channel == "upstream" and ["systemctl", "restart", "moonraker"] in rig.run.calls
    assert not os.listdir(paths.updates)                       # archive cleaned up


def test_kalico_flavor_points_at_the_kalico_repository(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path, flavor="kalico")
    mode.enable(rig.updater.fetch_manifest())
    conf = open(os.path.join(paths.templates, "update_manager.upstream.conf")).read()
    assert "KalicoCrew/kalico.git" in conf and "primary_branch: main" in conf


def test_manifest_can_override_upstream_origin(tmp_path):
    rig, paths, _ = upstream_rig(tmp_path)
    text = render_upstream_update_manager("klipper", paths, {"klipper": {"origin": "https://example.test/fork.git", "branch": "next", "evil": "x"}})
    assert "origin: https://example.test/fork.git" in text and "primary_branch: next" in text and "evil" not in text


def test_disable_returns_to_eitri_channel_and_removes_toolchain(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path)
    mode.enable(rig.updater.fetch_manifest())
    mode.disable()
    assert os.path.basename(os.readlink(paths.link)) == "update_manager.eitri.conf"
    assert rig.state.channel == "eitri" and not os.path.exists(paths.toolchain)
    assert rig.state.load()["needs_pin_reset"] is True
    assert [h["event"] for h in rig.state.load()["history"]][-2:] == ["upstream-enabled", "upstream-disabled"]


def test_disable_can_keep_toolchain(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path)
    mode.enable(rig.updater.fetch_manifest())
    mode.disable(remove_toolchain=False)
    assert os.path.isdir(paths.toolchain)


def test_dry_run_changes_nothing(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path)
    mode.enable(rig.updater.fetch_manifest(), dry_run=True)
    assert not os.path.exists(paths.toolchain) and not os.path.lexists(paths.link) and rig.state.channel == "eitri"


def test_unsafe_archive_is_rejected_and_nothing_is_installed(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path)
    bad = make_toolchain_archive(str(tmp_path / "bad.tar.zst"), bad_member="../escape")
    rig.channel.add_toolchain(bad)
    with pytest.raises(FlashError) as e:
        mode.enable(rig.updater.fetch_manifest())
    assert e.value.code == "bad_archive" and not os.path.exists(paths.toolchain) and not os.path.exists(str(tmp_path / "data" / "escape"))
    assert rig.state.channel == "eitri"


def test_archive_without_compiler_is_rejected(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path)
    bad = make_toolchain_archive(str(tmp_path / "empty.tar.zst"), files={"toolchain/readme": b"hi"})
    rig.channel.add_toolchain(bad)
    with pytest.raises(FlashError, match="arm-none-eabi-gcc"):
        mode.enable(rig.updater.fetch_manifest())
    assert not os.path.exists(paths.toolchain + ".new")


def test_not_a_git_clone(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path)
    os.rmdir(os.path.join(paths.klipper, ".git"))
    with pytest.raises(FlashError) as e:
        mode.enable(rig.updater.fetch_manifest())
    assert e.value.code == "no_clone"


def test_no_space_for_toolchain(tmp_path, monkeypatch):
    from eitri_update import fetch
    rig, paths, mode = upstream_rig(tmp_path)
    monkeypatch.setattr(fetch, "free_bytes", lambda p: 1 << 20)
    with pytest.raises(FlashError) as e:
        mode.enable(rig.updater.fetch_manifest())
    assert e.value.code == "no_space"


def test_status(tmp_path):
    rig, paths, mode = upstream_rig(tmp_path)
    assert mode.status()["toolchain"] is False and mode.status()["channel"] == "eitri"
    mode.enable(rig.updater.fetch_manifest())
    st = mode.status()
    assert st["toolchain"] is True and st["channel"] == "upstream" and st["update_manager"] == "update_manager.upstream.conf"


def test_boot_dir_is_found_where_the_os_image_mounts_it(tmp_path, monkeypatch):
    from eitri_update import bootenv
    fw = tmp_path / "boot" / "firmware" / "eitri"
    fw.mkdir(parents=True)
    (fw / "boot.env").write_text("eitri_slot=B\n")
    monkeypatch.setattr(bootenv, "BOOT_DIRS", (str(tmp_path / "boot" / "firmware"), str(tmp_path / "boot")))
    assert bootenv.BootControl(cmdline_path="/nonexistent").get_primary() == "B"


# ---- upstream mode must not downgrade the seed's environments ----------------------------------------------------------
def test_upstream_mode_points_the_update_manager_at_the_seed_locks_when_they_exist(tmp_path):
    paths = UpstreamPaths(str(tmp_path), str(tmp_path / "printer_data"))
    os.makedirs(paths.seed_locks)
    for name in ("klippy", "moonraker"):
        open(os.path.join(paths.seed_locks, name + ".txt"), "w").write("# lock\n")
    text = render_upstream_update_manager("klipper", paths)
    assert "requirements: %s/klippy.txt" % paths.seed_locks in text and "requirements: %s/moonraker.txt" % paths.seed_locks in text
    assert "scripts/klippy-requirements.txt" not in text and "scripts/moonraker-requirements.txt" not in text


def test_without_seed_locks_no_pip_step_is_configured_at_all(tmp_path):
    paths = UpstreamPaths(str(tmp_path), str(tmp_path / "printer_data"))
    text = render_upstream_update_manager("kalico", paths)
    assert "requirements" not in text and "virtualenv: %s" % paths.venv_klippy in text and "managed_services: klipper" in text
