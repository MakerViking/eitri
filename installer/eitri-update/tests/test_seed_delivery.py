# SPDX-License-Identifier: Apache-2.0
"""A release delivers the data seed and the MCU firmware: download + verification before the OS is touched, installation
after the reboot (before Klipper starts), commit, and rollback of the /data layer together with the OS slot."""
import json
import os

import pytest

import seedpack
from helpers import Rig
from eitri_mcuflash.errors import FlashError
from eitri_update import fetch, seed as seedlib
from eitri_update.manifest import parse_manifest


def seeded_rig(tmp_path, **kw):
    kw.setdefault("seeds", True)
    kw.setdefault("seed_installed", True)
    kw.setdefault("versions", ("2026.10.0",))
    return Rig(tmp_path, **kw)


def reboot_into_new_slot(rig):
    rig.run.rauc_slots = {"A": {"state": "inactive", "boot_status": "good", "version": "2026.09.0"},
                          "B": {"state": "booted", "boot_status": "bad", "version": "2026.10.0"}}
    rig.bootctl.set_primary("B")
    with open(rig.cmdline, "w") as fh:
        fh.write("root=PARTUUID=x ro eitri.slot=B quiet\n")
    rig.rebooted = 0
    rig.run.calls.clear()
    rig.run.active_units.clear()            # eitri-prepare runs before klipper, moonraker and klarity


def staged(tmp_path, **kw):
    rig = seeded_rig(tmp_path, **kw)
    rig.updater.upgrade()
    reboot_into_new_slot(rig)
    return rig


def klippy(rig):
    return rig.read("firmware/klipper/klippy/klippy.py")


def leftovers(rig):
    return seedlib.leftovers(rig.data)


# ---- planning ----------------------------------------------------------------------------------------------
def test_check_counts_the_seed_and_the_mcu_firmware(tmp_path):
    rig = seeded_rig(tmp_path)
    info = rig.channel.seed_info[("2026.10.0", "klipper")]
    res = rig.updater.check()
    mcu = [b for b in rig.channel.releases[0]["bundles"] if b["kind"] == "mcu" and b["flavor"] == "klipper"][0]
    assert res["download_bytes"] == 4096 + mcu["size"] + info["size"]


def test_dry_run_names_the_bundles_and_downloads_nothing(tmp_path):
    rig = seeded_rig(tmp_path)
    plan = rig.updater.upgrade(dry_run=True)
    assert [b["kind"] for b in plan["bundles"]] == ["os", "mcu", "seed"]
    assert not os.path.exists(rig.cfg.updates_dir) and not rig.run.installs


def test_the_flavor_of_the_printer_decides_which_seed_is_fetched(tmp_path):
    rig = seeded_rig(tmp_path, flavor="kalico")
    plan = rig.updater.upgrade(dry_run=True)
    assert all("kalico" in b["url"] for b in plan["bundles"] if b["kind"] != "os")


def test_a_release_with_a_seed_but_no_mcu_firmware_is_not_accepted(tmp_path):
    rig = seeded_rig(tmp_path)
    rig.channel.releases[0]["bundles"] = [b for b in rig.channel.releases[0]["bundles"] if b["kind"] != "mcu"]
    rig.channel.publish()
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "manifest" and "without the matching MCU firmware" in e.value.message


def test_a_seed_bundle_must_carry_its_manifest(tmp_path):
    rig = seeded_rig(tmp_path)
    for b in rig.channel.releases[0]["bundles"]:
        b.pop("manifest", None)
    rig.channel.publish()
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "manifest" and "manifest" in e.value.message


# ---- before the reboot --------------------------------------------------------------------------------------
def test_upgrade_stages_the_seed_but_touches_nothing_on_data(tmp_path):
    rig = seeded_rig(tmp_path)
    before = klippy(rig)
    rig.updater.upgrade()
    assert len(rig.run.installs) == 1 and "os-" in os.path.basename(rig.run.installs[0])
    pending = rig.state.load()["pending"]
    assert os.path.basename(pending["seed"]["archive"]) == "data-seed-2026.10.0-klipper.tar.zst"
    assert os.path.isfile(pending["seed"]["archive"]) and os.path.isfile(pending["seed"]["manifest"])
    assert os.path.basename(pending["mcu_fw"]["archive"]) == "mcu-firmware-2026.10.0-klipper.tar.zst"
    assert klippy(rig) == before and not leftovers(rig)
    assert not os.path.exists(os.path.join(rig.data, "eitri/mcu/firmware"))
    assert set(pending["restart_services"]) == {"klarity", "klipper", "moonraker"}


@pytest.mark.parametrize("mutate,code,text", [
    (lambda d: d["sources"]["firmware"].update(commit="c" * 40), "seed_commit", "release pins"),
    (lambda d: d["sources"]["firmware"].update(describe="v9.9.9-1-gdeadbeef0"), "seed_commit", "pins"),
    (lambda d: d.update(version="2026.09.9"), "seed_manifest", "release"),
    (lambda d: d["target"].update(python_abi="cp399"), "seed_abi", "cp399"),
    (lambda d: d.update(flavor="kalico"), "seed_manifest", "kalico seed"),
    (lambda d: d.update(layout=2), "seed_layout", "layout"),
    (lambda d: d["target"].update(arch="x86_64"), "seed_manifest", "aarch64"),
    (lambda d: d["archive"].update(sha256="0" * 64), "seed_manifest", "does not match the signed value"),
    (lambda d: d["target"].update(glibc_needed="9.99"), "seed_abi", "glibc"),
    (lambda d: d.update(kind="something-else"), "seed_manifest", "not an Eitri data seed"),
])
def test_a_seed_that_does_not_belong_to_the_release_stops_before_the_os_is_written(tmp_path, mutate, code, text):
    rig = seeded_rig(tmp_path, versions=())
    rig.channel.add_release("2026.10.0", seed_kw={"mutate_manifest": mutate})
    rig.channel.publish()
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == code and text in e.value.message
    assert not rig.run.installs and rig.rebooted == 0 and rig.state.load().get("pending") is None


def test_a_tampered_seed_archive_never_reaches_rauc(tmp_path):
    rig = seeded_rig(tmp_path)
    path = rig.channel.seed_info[("2026.10.0", "klipper")]["path"]
    with open(path, "r+b") as fh:
        fh.write(b"\xde\xad")
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == "checksum" and not rig.run.installs and rig.rebooted == 0


def test_a_tampered_seed_manifest_is_caught_by_the_signed_digest(tmp_path):
    rig = seeded_rig(tmp_path)
    path = rig.channel.seed_info[("2026.10.0", "klipper")]["manifest_path"]
    with open(path, "a") as fh:
        fh.write(" ")
    with pytest.raises((FlashError)) as e:
        rig.updater.upgrade()
    assert e.value.code in ("checksum", "size_mismatch") and not rig.run.installs


def test_the_unpacked_size_of_the_seed_counts_towards_the_space_needed(tmp_path, monkeypatch):
    rig = seeded_rig(tmp_path, versions=())
    rig.channel.add_release("2026.10.0", seed_kw={"uncompressed_size": 50 << 30})
    rig.channel.publish()
    monkeypatch.setattr(fetch, "free_bytes", lambda p: 1 << 30)
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == "no_space" and not rig.run.installs


# ---- after the reboot ----------------------------------------------------------------------------------------
def test_post_boot_installs_the_seed_and_the_mcu_firmware_before_flashing(tmp_path):
    rig = staged(tmp_path)
    rig.updater.post_boot()
    assert klippy(rig).startswith("# klipper 2026.10.0")
    rec = seedlib.read_record(rig.data)
    assert rec["SEED_VERSION"] == "2026.10.0" and rec["SEED_FLAVOR"] == "klipper" and rec["SEED_MODE"] == "update"
    assert rig.read("eitri/flavor").strip() == "klipper"
    fw = os.path.join(rig.data, "eitri/mcu/firmware/klipper")
    assert os.path.isfile(fw + "/manifest.json") and os.path.isfile(fw + "/mainboard/klipper.bin")
    flash = [c for c in rig.run.calls if "eitri_mcuflash" in c and "flash" in c]
    assert len(flash) == 1 and "--commit" in flash[0] and flash[0][flash[0].index("--commit") + 1] == seedpack.commit_for("klipper", "2026.10.0")
    # a committed update leaves nothing behind
    assert rig.state.installed_version == "2026.10.0" and not leftovers(rig)
    assert not os.path.exists(os.path.join(rig.data, "eitri/mcu/firmware/.klipper.release-old"))
    assert not [f for f in os.listdir(rig.cfg.updates_dir) if f.startswith(("data-seed-", "mcu-firmware-"))]
    assert rig.state.load()["pending"] is None


def test_the_services_are_not_stopped_at_boot_where_they_have_not_started_yet(tmp_path):
    rig = staged(tmp_path)
    rig.updater.prepare()                 # eitri-prepare runs Before=klipper moonraker klarity
    assert not any(c[:2] == ["systemctl", "stop"] for c in rig.run.calls)


def test_a_running_service_is_stopped_before_its_trees_are_swapped(tmp_path):
    rig = staged(tmp_path)
    rig.run.active_units.update({"klipper", "moonraker"})
    rig.updater.prepare()
    stops = [c[2] for c in rig.run.calls if c[:2] == ["systemctl", "stop"]]
    assert stops == ["moonraker", "klipper"]


def test_the_layer_is_linked_again_after_the_swap(tmp_path):
    rig = staged(tmp_path)
    rig.updater.prepare()
    assert len(rig.run.overlay_calls) == 1
    assert "--klipper-dir" in rig.run.overlay_calls[0] and "--owner" in rig.run.overlay_calls[0]


def test_post_boot_failed_health_restores_the_previous_data_layer(tmp_path):
    rig = staged(tmp_path, health_ok=False)
    old_klippy = klippy(rig)
    rig.updater.post_boot()
    assert rig.rebooted == 1
    assert klippy(rig) == old_klippy and not leftovers(rig)
    rec = seedlib.read_record(rig.data)
    assert rec["SEED_VERSION"] == "2026.09.0"                      # the record of the previous seed is back
    assert not os.path.exists(os.path.join(rig.data, "eitri/mcu/firmware/klipper"))       # there was none before
    assert sorted(rig.run.mcu_rollbacks) == ["mainboard", "toolhead"]
    assert rig.state.installed_version == "2026.09.0"


def test_a_failing_seed_install_rolls_the_whole_update_back(tmp_path):
    rig = staged(tmp_path)
    rig.run.fail_seed_update = True
    old_klippy = klippy(rig)
    rig.updater.post_boot()
    assert rig.rebooted == 1 and klippy(rig) == old_klippy
    assert rig.boot_env()["eitri_slot"] == "A"
    # the firmware had been unpacked before the seed failed: it is put back too
    assert not os.path.exists(os.path.join(rig.data, "eitri/mcu/firmware/klipper"))
    assert not rig.run.mcu_rollbacks                              # no board was flashed


def test_a_failing_overlay_rolls_back_the_trees(tmp_path):
    rig = staged(tmp_path)
    rig.run.overlay_fail = True
    old_klippy = klippy(rig)
    rig.updater.post_boot()
    assert rig.rebooted == 1 and klippy(rig) == old_klippy and not leftovers(rig)


def test_mcu_flash_failure_after_a_seed_update_restores_everything(tmp_path):
    rig = staged(tmp_path)
    rig.run.mcu_fail = True
    rig.run.mcu_done_before_fail = ["mainboard"]
    old_klippy = klippy(rig)
    rig.updater.post_boot()
    assert rig.rebooted == 1 and klippy(rig) == old_klippy and not leftovers(rig)
    assert rig.run.mcu_rollbacks == ["mainboard"]
    assert seedlib.read_record(rig.data)["SEED_VERSION"] == "2026.09.0"


def test_a_repeated_prepare_does_not_swap_the_trees_twice(tmp_path):
    """Power cut after the swap but before it was recorded: the next prepare must not push the NEW trees into the slot
    that holds the only copy of the old ones."""
    rig = staged(tmp_path, health_ok=False)
    old_klippy = klippy(rig)
    rig.updater.prepare()
    pending = rig.state.load()["pending"]
    pending["seed"]["installed"] = False                        # the record of progress is lost
    pending["mcu_fw"]["installed"] = False
    rig.state.update(pending=pending)
    rig.updater.prepare()
    assert klippy(rig).startswith("# klipper 2026.10.0")
    rig.updater.verify()                                        # health fails: roll back
    assert klippy(rig) == old_klippy and not leftovers(rig)


def test_leftovers_of_a_half_done_swap_are_undone_before_a_new_attempt(tmp_path):
    rig = staged(tmp_path)
    live = os.path.join(rig.data, "firmware/klipper")
    os.rename(live, os.path.join(rig.data, "firmware/.klipper.seed-old"))       # the originals, set aside ...
    os.makedirs(os.path.join(live, "klippy"))
    open(os.path.join(live, "klippy/klippy.py"), "w").write("# half-written\n")  # ... and a partial new tree
    rig.updater.prepare()
    assert klippy(rig).startswith("# klipper 2026.10.0")
    assert os.path.isdir(os.path.join(rig.data, "firmware/.klipper.seed-old"))   # kept until the verdict
    rig.updater.health_check = lambda *a, **k: type("H", (), {"ok": False, "detail": "no"})()
    rig.updater.verify()
    assert klippy(rig).startswith("# klipper 2026.09.0")


def test_a_firmware_tree_the_user_moved_forward_is_not_replaced_and_the_update_does_not_take(tmp_path):
    """Upstream-style `git pull` between releases: the pinned MCU firmware would not match the kept tree, so the plan is
    blocked and the update rolls back instead of leaving host and MCUs on different versions."""
    rig = staged(tmp_path)
    rec_path = os.path.join(rig.data, "eitri/state/seed-installed.env")
    text = open(rec_path).read().replace("SEED_FIRMWARE_COMMIT=", "SEED_FIRMWARE_COMMIT=0")
    open(rec_path, "w").write(text)                        # the recorded commit no longer equals the clone's HEAD
    # make the clone a real repository whose HEAD differs from the record
    import subprocess
    clone = os.path.join(rig.data, "firmware/klipper")
    subprocess.run(["git", "init", "-q", clone], check=True)
    subprocess.run(["git", "-C", clone, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    old_klippy = klippy(rig)
    rig.run.mcu_boards = {"mainboard": True, "toolhead": True}
    rig.updater.prepare()
    assert klippy(rig) == old_klippy                       # not replaced
    assert "firmware/klipper" in seedlib.read_record(rig.data)["SEED_KEPT"]


def test_a_release_without_a_seed_still_works_with_data_bundles(tmp_path):
    rig = Rig(tmp_path)                                    # the older shape: RAUC data bundles only
    rig.updater.upgrade()
    assert "seed" not in rig.state.load()["pending"] and "mcu_fw" not in rig.state.load()["pending"]
