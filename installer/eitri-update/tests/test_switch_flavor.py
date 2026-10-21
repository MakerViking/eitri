# SPDX-License-Identifier: Apache-2.0
"""eitri-update switch-flavor: the other flavor's data seed and MCU firmware from the installed release, no reboot."""
import os

import pytest

import seedpack
from helpers import Rig
from eitri_mcuflash.errors import FlashError
from eitri_update import cli, seed as seedlib


def rig_on(tmp_path, flavor="klipper", **kw):
    kw.setdefault("seeds", True)
    kw.setdefault("seed_installed", True)
    kw.setdefault("versions", ("2026.10.0",))
    kw.setdefault("installed", "2026.10.0")
    return Rig(tmp_path, flavor=flavor, **kw)


def klippy(rig):
    return rig.read("firmware/klipper/klippy/klippy.py")


def mcuflash_calls(rig, verb):
    return [c for c in rig.run.calls if "eitri_mcuflash" in c and verb in c]


def systemctl(rig, verb):
    return [c[2] for c in rig.run.calls if c[:2] == ["systemctl", verb]]


def test_switching_installs_the_other_seed_flashes_the_mcus_and_restarts_the_services(tmp_path):
    rig = rig_on(tmp_path)
    plan = rig.updater.switch_flavor("kalico")
    assert plan["from"] == "klipper" and plan["to"] == "kalico"
    assert klippy(rig).startswith("# kalico 2026.10.0")
    assert rig.read("eitri/flavor").strip() == "kalico"
    rec = seedlib.read_record(rig.data)
    assert rec["SEED_FLAVOR"] == "kalico" and rec["SEED_MODE"] == "update"
    assert os.path.isfile(os.path.join(rig.data, "eitri/mcu/firmware/kalico/manifest.json"))
    flash = mcuflash_calls(rig, "flash")
    assert len(flash) == 1 and flash[0][flash[0].index("--flavor") + 1] == "kalico"
    assert flash[0][flash[0].index("--commit") + 1] == seedpack.commit_for("kalico", "2026.10.0")
    # services: stopped before the swap, started again afterwards, Klarity last
    assert systemctl(rig, "stop") == ["klarity", "moonraker", "klipper"]
    assert systemctl(rig, "start") == ["klipper", "moonraker", "klarity"]
    # nothing is left behind and nothing was rebooted
    assert not seedlib.leftovers(rig.data) and rig.rebooted == 0 and rig.state.load()["switch"] is None
    assert rig.state.load()["history"][-1]["event"] == "flavor-switched"
    assert not [f for f in os.listdir(rig.cfg.updates_dir) if f.startswith(("data-seed-", "mcu-firmware-"))]
    assert len(rig.run.overlay_calls) == 1                       # the extras are linked into the new clone
    assert not rig.run.installs                                  # RAUC and the boot slots are not involved


def test_the_installed_os_is_not_touched_and_switching_back_works(tmp_path):
    rig = rig_on(tmp_path)
    rig.updater.switch_flavor("kalico")
    rig.updater.switch_flavor("klipper")
    assert klippy(rig).startswith("# klipper 2026.10.0") and rig.read("eitri/flavor").strip() == "klipper"
    assert seedlib.read_record(rig.data)["SEED_FLAVOR"] == "klipper"
    assert rig.boot_env()["eitri_slot"] == "A"


def test_dry_run_shows_the_plan_and_changes_nothing(tmp_path):
    rig = rig_on(tmp_path)
    plan = rig.updater.switch_flavor("kalico", dry_run=True)
    assert [b["kind"] for b in plan["bundles"]] == ["mcu", "seed"]
    assert klippy(rig).startswith("# klipper") and not os.path.exists(rig.cfg.updates_dir)
    assert not mcuflash_calls(rig, "flash") and not systemctl(rig, "stop")


def test_already_on_that_flavor_is_not_an_error(tmp_path):
    rig = rig_on(tmp_path)
    plan = rig.updater.switch_flavor("klipper")
    assert plan["from"] == plan["to"] == "klipper" and not systemctl(rig, "stop") and not os.path.exists(rig.cfg.updates_dir)


def test_a_front_end_that_already_wrote_the_flag_still_gets_a_real_switch(tmp_path):
    """The configurator writes /data/eitri/flavor before it calls the helper: the installed seed (the record) decides."""
    rig = rig_on(tmp_path)
    rig.state.set_text("flavor", "kalico")
    rig.updater.switch_flavor("kalico")
    assert klippy(rig).startswith("# kalico 2026.10.0") and rig.read("eitri/flavor").strip() == "kalico"
    assert seedlib.read_record(rig.data)["SEED_FLAVOR"] == "kalico"


def test_a_failed_switch_leaves_the_flag_on_the_installed_flavor(tmp_path):
    rig = rig_on(tmp_path, health_ok=False)
    rig.state.set_text("flavor", "kalico")
    with pytest.raises(FlashError):
        rig.updater.switch_flavor("kalico")
    assert rig.read("eitri/flavor").strip() == "klipper"


def test_only_the_flag_file_disagreeing_is_repaired_without_a_download(tmp_path):
    rig = rig_on(tmp_path, flavor="kalico")
    rig.state.set_text("flavor", "klipper")                       # the record says kalico (seed installed as kalico)
    rig.updater.switch_flavor("kalico")
    assert rig.read("eitri/flavor").strip() == "kalico" and not os.path.exists(rig.cfg.updates_dir)


def test_health_failure_restores_flavor_trees_firmware_and_services(tmp_path):
    rig = rig_on(tmp_path, health_ok=False)
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "health"
    assert klippy(rig).startswith("# klipper 2026.10.0") and rig.read("eitri/flavor").strip() == "klipper"
    assert seedlib.read_record(rig.data)["SEED_FLAVOR"] == "klipper" and not seedlib.leftovers(rig.data)
    assert not os.path.exists(os.path.join(rig.data, "eitri/mcu/firmware/kalico"))
    assert sorted(rig.run.mcu_rollbacks) == ["mainboard", "toolhead"]
    assert rig.run.active_units == {"klarity", "moonraker", "klipper"}          # the printer is running again
    assert rig.state.load()["switch"] is None and rig.state.load()["history"][-1]["event"] == "flavor-switch-failed"


def test_mcu_flash_failure_restores_everything(tmp_path):
    rig = rig_on(tmp_path)
    rig.run.mcu_fail = True
    rig.run.mcu_done_before_fail = ["mainboard"]
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "mcu_flash"
    assert klippy(rig).startswith("# klipper") and rig.read("eitri/flavor").strip() == "klipper"
    # the board that failed may be half-written as well: both go back to what they ran before
    assert sorted(rig.run.mcu_rollbacks) == ["mainboard", "toolhead"] and rig.run.active_units == {"klarity", "moonraker", "klipper"}


def test_a_failing_seed_install_restores_the_services_and_the_firmware_tree(tmp_path):
    rig = rig_on(tmp_path)
    rig.run.fail_seed_update = True
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "seed_failed"
    assert klippy(rig).startswith("# klipper") and not os.path.exists(os.path.join(rig.data, "eitri/mcu/firmware/kalico"))
    assert rig.run.active_units == {"klarity", "moonraker", "klipper"} and not mcuflash_calls(rig, "flash")


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_never_during_a_print(tmp_path, state):
    rig = rig_on(tmp_path)
    rig.updater.print_state = lambda url: state
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "printing" and klippy(rig).startswith("# klipper") and not systemctl(rig, "stop")


def test_an_unreachable_moonraker_does_not_block_the_switch(tmp_path):
    rig = rig_on(tmp_path)
    rig.updater.print_state = lambda url: None
    rig.updater.switch_flavor("kalico")
    assert klippy(rig).startswith("# kalico")


def test_upstream_mode_is_refused(tmp_path):
    rig = rig_on(tmp_path)
    rig.state.set_text("channel", "upstream")
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "upstream_mode"


def test_an_unconfirmed_system_is_refused(tmp_path):
    rig = rig_on(tmp_path)
    rig.bootctl.set_state("A", "bad")
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "unconfirmed_slot"


def test_the_seed_must_come_from_the_installed_release(tmp_path):
    rig = rig_on(tmp_path, versions=("2026.11.0",))               # the channel moved on; 2026.10.0 is gone
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "no_release" and "upgrade" in e.value.recovery


def test_a_release_without_the_other_flavor_is_refused(tmp_path):
    rig = rig_on(tmp_path, versions=())
    rig.channel.add_release("2026.10.0", flavors=("klipper",))
    rig.channel.publish()
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "no_flavor"


def test_unknown_flavor_and_unknown_release(tmp_path):
    rig = rig_on(tmp_path)
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("marlin")
    assert e.value.code == "usage"
    rig2 = rig_on(tmp_path / "b", installed=None)
    with pytest.raises(FlashError) as e:
        rig2.updater.switch_flavor("kalico")
    assert e.value.code == "no_release"


def test_a_seed_for_the_wrong_commit_is_refused_before_anything_is_stopped(tmp_path):
    rig = rig_on(tmp_path, versions=())
    rig.channel.add_release("2026.10.0", seed_kw={"commit": "c" * 40})
    rig.channel.publish()
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "seed_commit" and not systemctl(rig, "stop") and klippy(rig).startswith("# klipper")


def test_an_interrupted_switch_is_undone_at_the_next_boot(tmp_path):
    rig = rig_on(tmp_path)

    def die(job, key="pending"):
        raise KeyboardInterrupt        # the process is gone: no cleanup code runs
    rig.updater._mcu_sync = die
    with pytest.raises(KeyboardInterrupt):
        rig.updater.switch_flavor("kalico")
    assert klippy(rig).startswith("# kalico") and rig.state.load()["switch"]["phase"] == "mcu"
    # --- reboot: eitri-prepare runs before klipper, moonraker and klarity
    rig.run.active_units.clear()
    rig.run.calls.clear()
    from eitri_update.client import Updater
    boot = Updater(rig.cfg, rig.events, runner=rig.run, rauc=rig.updater.rauc, bootctl=rig.bootctl, state=rig.state,
                   reboot=lambda: None, health_check=rig.health, sleep=lambda s: None)
    boot.prepare()
    assert klippy(rig).startswith("# klipper") and rig.read("eitri/flavor").strip() == "klipper"
    assert seedlib.read_record(rig.data)["SEED_FLAVOR"] == "klipper" and not seedlib.leftovers(rig.data)
    assert sorted(rig.run.mcu_rollbacks) == ["mainboard", "toolhead"]        # which board was half-written is unknown: both
    assert rig.state.load()["switch"] is None
    assert not systemctl(rig, "start") and not systemctl(rig, "stop")          # systemd starts the services itself


def test_a_pending_switch_blocks_a_new_one_until_the_reboot_cleaned_it_up(tmp_path):
    rig = rig_on(tmp_path)
    rig.state.update(switch={"to": "kalico", "from": "klipper", "phase": "applying"})
    with pytest.raises(FlashError) as e:
        rig.updater.switch_flavor("kalico")
    assert e.value.code == "switch_pending"


# ---- command line -----------------------------------------------------------------------------------------------
def test_cli_switch_flavor_needs_yes_without_a_terminal(tmp_path, capsys):
    rig = rig_on(tmp_path)
    args = ["--data-root", rig.data, "--manifest-url", rig.channel_url(), "--key-dir", rig.signer.dir]
    rc = cli.main(args + ["switch-flavor", "kalico"], runner=rig.run, updater_factory=lambda cfg, ev: rig.updater)
    assert rc == 2 and "--yes" in capsys.readouterr().err and klippy(rig).startswith("# klipper")


def test_cli_switch_flavor_yes_does_it(tmp_path):
    rig = rig_on(tmp_path)
    args = ["--data-root", rig.data, "--manifest-url", rig.channel_url(), "--key-dir", rig.signer.dir]
    rc = cli.main(args + ["switch-flavor", "kalico", "--yes"], runner=rig.run, updater_factory=lambda cfg, ev: rig.updater)
    assert rc == 0 and klippy(rig).startswith("# kalico")


def test_cli_exit_code_for_a_refusal_during_a_print(tmp_path):
    rig = rig_on(tmp_path)
    rig.updater.print_state = lambda url: "printing"
    args = ["--data-root", rig.data, "--manifest-url", rig.channel_url(), "--key-dir", rig.signer.dir]
    rc = cli.main(args + ["switch-flavor", "kalico", "--yes"], runner=rig.run, updater_factory=lambda cfg, ev: rig.updater)
    assert rc == 3


def test_cli_status_reports_a_pending_switch(tmp_path, capsys):
    rig = rig_on(tmp_path)
    rig.state.update(switch={"to": "kalico"})
    args = ["--data-root", rig.data, "--manifest-url", rig.channel_url()]
    cli.main(args + ["status"], runner=rig.run, updater_factory=lambda cfg, ev: rig.updater)
    assert '"switch"' in capsys.readouterr().out
