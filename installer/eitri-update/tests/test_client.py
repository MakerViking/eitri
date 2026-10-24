# SPDX-License-Identifier: Apache-2.0
import json
import os

import pytest

from helpers import Rig, Signer, sha
from eitri_mcuflash.errors import FlashError
from eitri_update import fetch
from eitri_update.manifest import parse_manifest, plan_upgrade


# ---- manifest + signature -------------------------------------------------------
def test_check_reports_available_update(tmp_path):
    rig = Rig(tmp_path, versions=("2026.09.0", "2026.10.0"))
    res = rig.updater.check()
    assert res["latest"] == "2026.10.0" and res["update_available"] and res["installed"] == "2026.09.0"
    assert res["download_bytes"] == 4096 + 2048          # os + the klipper data bundle only
    assert rig.state.load()["manifest_sequence"] == 1


def test_up_to_date(tmp_path):
    rig = Rig(tmp_path, installed="2026.10.0")
    assert rig.updater.check()["update_available"] is False


def test_wrong_signer_is_rejected_before_anything_is_parsed(tmp_path):
    rig = Rig(tmp_path)
    attacker = Signer(tmp_path, "attacker")
    rig.channel.publish(sign_with=attacker)
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "bad_signature" and "unknown key" in e.value.message


def test_tampered_manifest_is_rejected(tmp_path):
    rig = Rig(tmp_path)
    rig.channel.publish(tamper=True)
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "bad_signature"


def test_manifest_replay_older_sequence_is_refused(tmp_path):
    rig = Rig(tmp_path, sequence=5)
    rig.updater.check()
    rig.channel.sequence = 4
    rig.channel.publish()
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "stale_manifest"


def test_insecure_http_is_refused(tmp_path):
    rig = Rig(tmp_path)
    rig.cfg.manifest_url = "http://updates.example.test/manifest.json"
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "insecure_url"


def test_expired_manifest(tmp_path):
    rig = Rig(tmp_path)
    doc = json.loads(open(os.path.join(rig.channel.dir, "manifest.json")).read())
    doc["expires"] = "2026-01-01T00:00:00Z"
    data = json.dumps(doc).encode()
    open(os.path.join(rig.channel.dir, "manifest.json"), "wb").write(data)
    open(os.path.join(rig.channel.dir, "manifest.json.minisig"), "w").write(rig.signer.sign(data))
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "expired_manifest"


def test_manifest_validation_rejects_malformed_entries(tmp_path):
    rig = Rig(tmp_path)
    good = json.loads(open(os.path.join(rig.channel.dir, "manifest.json")).read())
    for mutate, text in (
        (lambda d: d["releases"][0]["bundles"][0].update(sha256="XYZ"), "sha256"),
        (lambda d: d["releases"][0]["bundles"][0].update(url="http://x/y"), "https"),
        (lambda d: d["releases"][0].update(version="latest"), "bad version"),
        (lambda d: d.update(schema=2), "schema"),
        (lambda d: d["releases"][0].update(bundles=[b for b in d["releases"][0]["bundles"] if b["kind"] != "os"]), "no os bundle"),
    ):
        doc = json.loads(json.dumps(good))
        mutate(doc)
        with pytest.raises(FlashError, match=text):
            parse_manifest(json.dumps(doc).encode())


def test_plan_rules(tmp_path):
    rig = Rig(tmp_path, versions=("2026.08.0", "2026.09.0", "2026.10.0"))
    m = parse_manifest(open(os.path.join(rig.channel.dir, "manifest.json"), "rb").read())
    release, bundles = plan_upgrade(m, "2026.08.0", "kalico")
    assert release.version == "2026.10.0" and [b.kind for b in bundles] == ["os", "data"] and bundles[1].flavor == "kalico"
    with pytest.raises(FlashError) as e:
        plan_upgrade(m, "2026.10.0", "klipper")
    assert e.value.code == "up_to_date"
    with pytest.raises(FlashError) as e:
        plan_upgrade(m, "2026.10.0", "klipper", target="2026.08.0")
    assert e.value.code == "downgrade"
    assert plan_upgrade(m, "2026.10.0", "klipper", target="2026.08.0", allow_downgrade=True)[0].version == "2026.08.0"


def test_min_installed_and_rollback_floor(tmp_path):
    rig = Rig(tmp_path, versions=("2026.09.0",))
    rig.channel.releases.clear()
    rig.channel.add_release("2026.10.0", requires={"min_installed": "2026.09.0"}, rollback_floor="2026.09.0")
    rig.channel.publish()
    m = parse_manifest(open(os.path.join(rig.channel.dir, "manifest.json"), "rb").read())
    with pytest.raises(FlashError) as e:
        plan_upgrade(m, "2026.07.0", "klipper")
    assert e.value.code == "needs_intermediate"


def test_release_without_the_chosen_flavor_is_refused(tmp_path):
    rig = Rig(tmp_path, flavor="kalico")
    rig.channel.releases.clear()
    rig.channel.add_release("2026.10.0", flavors=("klipper",))
    rig.channel.publish()
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade(dry_run=True)
    assert e.value.code == "no_flavor"


# ---- download ---------------------------------------------------------------------
def test_download_verifies_and_resumes(tmp_path):
    src = tmp_path / "blob"
    src.write_bytes(os.urandom(300000))
    url = "file://" + str(src)
    dest = str(tmp_path / "dl" / "blob")
    os.makedirs(os.path.dirname(dest))
    (open(dest + ".part", "wb")).write(src.read_bytes()[:1000])      # earlier interrupted download
    out = fetch.download(url, dest, sha256=sha(str(src)), size=src.stat().st_size, sleep=lambda s: None)
    assert open(out, "rb").read() == src.read_bytes() and not os.path.exists(dest + ".part")


def test_download_rejects_wrong_checksum_and_oversize(tmp_path):
    src = tmp_path / "blob"
    src.write_bytes(b"x" * 5000)
    url = "file://" + str(src)
    with pytest.raises(FlashError) as e:
        fetch.download(url, str(tmp_path / "a"), sha256="0" * 64, size=5000, sleep=lambda s: None)
    assert e.value.code == "checksum" and not os.path.exists(str(tmp_path / "a"))
    with pytest.raises(FlashError) as e:
        fetch.download(url, str(tmp_path / "b"), sha256=sha(str(src)), size=100, sleep=lambda s: None)
    assert e.value.code == "size_mismatch"


# ---- upgrade -------------------------------------------------------------------------
def test_upgrade_dry_run_downloads_nothing(tmp_path):
    rig = Rig(tmp_path)
    plan = rig.updater.upgrade(dry_run=True)
    assert plan["to"] == "2026.10.0" and not rig.run.installs and not os.path.exists(rig.cfg.updates_dir)
    assert rig.rebooted == 0


def test_upgrade_installs_os_first_defers_data_and_reboots(tmp_path):
    rig = Rig(tmp_path)
    rig.updater.upgrade()
    assert len(rig.run.installs) == 1 and "os-" in os.path.basename(rig.run.installs[0])   # only the OS bundle before the reboot
    pending = rig.state.load()["pending"]
    assert pending["phase"] == "await-reboot" and pending["slot_before"] == "A" and len(pending["remaining"]) == 1
    assert "data-" in os.path.basename(pending["remaining"][0]) and rig.rebooted == 1
    assert rig.state.installed_version == "2026.09.0"        # not committed until it proved itself


def test_corrupt_download_never_reaches_rauc(tmp_path):
    rig = Rig(tmp_path)
    path = [r for r in rig.channel.releases[0]["bundles"] if r["kind"] == "os"][0]["url"][len("file://"):]
    with open(path, "r+b") as fh:
        fh.write(b"\xde\xad")
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == "checksum" and not rig.run.installs and rig.rebooted == 0
    assert rig.state.load().get("pending") is None


def test_rauc_failure_leaves_no_pending_update(tmp_path):
    rig = Rig(tmp_path)
    rig.run.rauc_fail_install = True
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == "rauc_install" and rig.rebooted == 0 and rig.state.load().get("pending") is None


def test_upstream_mode_blocks_bundle_updates(tmp_path):
    rig = Rig(tmp_path)
    rig.state.set_text("channel", "upstream")
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == "upstream_mode" and "upstream disable" in e.value.recovery


def test_not_enough_space(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    monkeypatch.setattr(fetch, "free_bytes", lambda p: 1024)
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == "no_space" and not rig.run.installs


# ---- after the reboot --------------------------------------------------------------------
def _staged(tmp_path, **kw):
    rig = Rig(tmp_path, **kw)
    rig.updater.upgrade()
    # simulate the reboot into the new slot
    rig.run.rauc_slots = {"A": {"state": "inactive", "boot_status": "good", "version": "2026.09.0"},
                          "B": {"state": "booted", "boot_status": "bad", "version": "2026.10.0"}}
    rig.bootctl.set_primary("B")
    open(rig.cmdline, "w").write("root=PARTUUID=x ro eitri.slot=B quiet\n")
    rig.rebooted = 0
    rig.run.calls.clear()
    return rig


def test_post_boot_commits_a_healthy_update(tmp_path):
    rig = _staged(tmp_path)
    rig.updater.post_boot()
    assert len(rig.run.installs) == 2 and "data-" in os.path.basename(rig.run.installs[1])
    assert rig.state.installed_version == "2026.10.0" and rig.state.load()["pending"] is None
    assert rig.boot_env()["eitri_state_B"] == "good" and rig.boot_env()["eitri_tries"] == "3"
    assert ["rauc", "status", "mark-good", "booted"] in rig.run.calls
    assert rig.rebooted == 0
    assert any(c[:2] == ["systemctl", "start"] for c in rig.run.calls)
    assert rig.state.load()["history"][-1]["event"] == "upgraded"


def test_post_boot_flashes_mismatched_mcus_before_the_health_check(tmp_path):
    rig = _staged(tmp_path)
    order = []
    orig = rig.updater.health_check
    rig.updater.health_check = lambda *a, **k: (order.append("health"), orig(*a, **k))[1]
    rig.updater.post_boot()
    flash = [c for c in rig.run.calls if "flash" in c and "eitri_mcuflash" in c]
    assert len(flash) == 1 and flash[0].count("--only") == 2 and "--apply" in flash[0] and "--yes" in flash[0]
    assert rig.run.mcu_boards == {"mainboard": False, "toolhead": False}


def test_post_boot_failed_health_rolls_back_and_restores_mcus(tmp_path):
    rig = _staged(tmp_path, health_ok=False)
    rig.updater.post_boot()
    assert rig.rebooted == 1
    env = rig.boot_env()
    assert env["eitri_slot"] == "A" and env["eitri_state_B"] == "bad" and env["eitri_tries"] == "3"
    assert sorted(rig.run.mcu_rollbacks) == ["mainboard", "toolhead"]          # MCUs go back with the host
    assert rig.state.installed_version == "2026.09.0" and rig.state.load()["pending"] is None
    assert rig.state.load()["history"][-1]["event"] == "rolled-back"


def test_post_boot_mcu_failure_rolls_back_the_os_too(tmp_path):
    rig = _staged(tmp_path)
    rig.run.mcu_fail = True
    rig.updater.post_boot()
    assert rig.boot_env()["eitri_slot"] == "A" and rig.rebooted == 1


def test_post_boot_when_the_new_system_never_booted(tmp_path):
    rig = Rig(tmp_path)
    rig.updater.upgrade()
    rig.rebooted = 0                      # bootloader fell back: still slot A
    rig.updater.post_boot()
    assert rig.state.load()["pending"] is None and rig.state.installed_version == "2026.09.0"
    assert len(rig.run.installs) == 1     # the data bundle meant for the new OS was NOT applied


def test_first_boot_after_conversion_is_verified_and_marked_good(tmp_path):
    rig = Rig(tmp_path, installed=None)
    rig.bootctl.update(eitri_state_A="unknown")
    rig.updater.post_boot()
    assert rig.boot_env()["eitri_state_A"] == "good" and rig.rebooted == 0


def test_first_boot_failure_without_previous_system_retries_then_boot_manager_falls_back(tmp_path):
    rig = Rig(tmp_path, installed=None, health_ok=False, tries="2")
    rig.bootctl.update(eitri_state_A="unknown", eitri_state_B="unknown")
    rig.updater.post_boot()
    assert rig.rebooted == 1 and rig.boot_env()["eitri_slot"] == "A" and rig.boot_env()["eitri_tries"] == "2"


def test_manual_rollback_needs_a_known_good_other_slot(tmp_path):
    rig = Rig(tmp_path)
    with pytest.raises(FlashError) as e:
        rig.updater.rollback()                    # slot B is "bad" in the fixture
    assert e.value.code == "no_rollback_target" and "revert.sh" in e.value.recovery
    rig.bootctl.update(eitri_state_B="good")
    rig.run.rauc_slots["B"]["version"] = "2026.08.0"
    assert rig.updater.rollback()["to_slot"] == "B" and rig.boot_env()["eitri_slot"] == "B"


# ---- first boot after the in-place conversion --------------------------------------------------
def _converted(tmp_path, mcu_flash="auto", **kw):
    rig = Rig(tmp_path, installed=None, **kw)
    rig.bootctl.update(eitri_state_A="unknown", eitri_state_B="unknown")
    with open(os.path.join(rig.data, "eitri", "state", "install.env"), "w") as fh:
        fh.write("flavor=klipper\nmcu_flash=%s\n" % mcu_flash)
    return rig


def test_first_boot_flashes_the_mcus_before_klipper_starts(tmp_path):
    rig = _converted(tmp_path)
    out = rig.updater.prepare()
    assert out == {"pending": False}
    flash = [c for c in rig.run.calls if "flash" in c and "eitri_mcuflash" in c]
    assert len(flash) == 1 and "--apply" in flash[0]
    assert not [c for c in rig.run.calls if c[:2] == ["systemctl", "start"]]        # the verify half starts nothing itself
    assert rig.state.load()["firstboot_mcu_done"] is True
    rig.updater.prepare()                                                           # idempotent: runs once
    assert len([c for c in rig.run.calls if "flash" in c and "eitri_mcuflash" in c]) == 1
    rig.updater.verify()
    assert rig.boot_env()["eitri_state_A"] == "good" and rig.rebooted == 0


def test_first_boot_respects_manual_mcu_mode(tmp_path):
    rig = _converted(tmp_path, mcu_flash="manual")
    rig.updater.prepare()
    assert not [c for c in rig.run.calls if "eitri_mcuflash" in c]


def test_first_boot_mcu_failure_reboots_towards_the_stock_system(tmp_path):
    rig = _converted(tmp_path, tries="2")
    rig.run.mcu_fail = True
    out = rig.updater.prepare()
    assert out["rolled_back"] is None or out["rolled_back"] is True
    assert rig.rebooted == 1 and rig.boot_env()["eitri_tries"] == "2" and rig.boot_env()["eitri_state_A"] == "unknown"
    assert not rig.state.load().get("firstboot_mcu_done")


@pytest.mark.parametrize("mode", ["skip", "manual"])
def test_an_update_leaves_the_mcus_alone_when_the_owner_chose_so(tmp_path, mode):
    # A keep-stock install (mcu_flash=skip; the X-Plus 4's toolhead has no bootloader yet) must take OS updates
    # without flashing a board, or rolling back because one cannot be flashed.
    rig = _staged(tmp_path)
    with open(os.path.join(rig.data, "eitri", "state", "install.env"), "w") as fh:
        fh.write("flavor=klipper\nmcu_flash=%s\ntoolhead_firmware=stock\n" % mode)
    before = dict(rig.run.mcu_boards)
    rig.updater.post_boot()
    assert not [c for c in rig.run.calls if "eitri_mcuflash" in c and "flash" in c]
    assert rig.run.mcu_boards == before                                  # untouched, still "needs a flash"
    assert any("MCU flashing is %s" % mode in (e.get("msg") or "") for e in rig.events.events)
    assert rig.state.installed_version == "2026.10.0" and rig.state.load()["pending"] is None
    assert rig.boot_env()["eitri_state_B"] == "good" and rig.rebooted == 0


def test_an_update_still_syncs_the_mcus_when_install_env_says_auto(tmp_path):
    rig = _staged(tmp_path)
    with open(os.path.join(rig.data, "eitri", "state", "install.env"), "w") as fh:
        fh.write("flavor=klipper\nmcu_flash=auto\n")
    rig.updater.post_boot()
    assert len([c for c in rig.run.calls if "flash" in c and "eitri_mcuflash" in c]) == 1


def test_verify_without_pending_and_good_slot_is_a_noop(tmp_path):
    rig = Rig(tmp_path)
    assert rig.updater.verify() == {"pending": False}
    assert not rig.run.calls or all(c[0] != "systemctl" for c in rig.run.calls)


def test_every_healthy_boot_of_a_confirmed_slot_restores_its_boot_attempts(tmp_path):
    # U-Boot's boot.scr spends one attempt on every boot. Seen in the rehearsal on a copy of the real eMMC: after the
    # commit on the first boot, the second boot left eitri_tries=2, so the fifth boot would have fallen back to stock.
    rig = Rig(tmp_path)
    rig.bootctl.update(eitri_slot="A", eitri_state_A="good", eitri_tries="1")
    assert rig.updater.verify() == {"pending": False}
    env = rig.boot_env()
    assert env["eitri_tries"] == "3" and env["eitri_state_A"] == "good" and env["eitri_mode"] == "eitri"
    assert not rig.run.calls or all(c[0] != "systemctl" for c in rig.run.calls)       # services are not touched



def test_release_commit_pin_is_passed_to_mcuflash(tmp_path):
    rig = Rig(tmp_path)
    rig.channel.releases[0]["flavors"] = {"klipper": {"commit": "a08fc79fcfa342e5b95ac02a9a8b271f453523e9"}}
    rig.channel.publish()
    rig.updater.upgrade()
    rig.run.rauc_slots = {"A": {"state": "inactive", "boot_status": "good", "version": "2026.09.0"},
                          "B": {"state": "booted", "boot_status": "bad", "version": "2026.10.0"}}
    rig.bootctl.set_primary("B")
    open(rig.cmdline, "w").write("root=PARTUUID=x ro eitri.slot=B quiet\n")
    rig.run.calls.clear()
    rig.updater.prepare()
    mcu = [c for c in rig.run.calls if "eitri_mcuflash" in c]
    assert mcu and all("--commit" in c and "a08fc79fcfa342e5b95ac02a9a8b271f453523e9" in c for c in mcu)


def _inplace_layout(rig, dev_a="/dev/mmcblk1p5", dev_b="/dev/mmcblk1p6", partuuids=None):
    with open(os.path.join(rig.boot, "eitri", "layout.env"), "w") as fh:
        fh.write("eitri_part_A=5\neitri_part_B=6\n")
        if partuuids:
            fh.write("eitri_partuuid_A=%s\neitri_partuuid_B=%s\n" % partuuids)
    orig = rig.run._rauc

    def rauc(argv, kw):
        res = orig(argv, kw)
        if argv[1] == "status" and argv[2].startswith("--output-format"):
            doc = json.loads(res.output)
            for entry in doc["slots"]:
                for _name, info in entry.items():
                    info["device"] = dev_a if info["bootname"] == "A" else dev_b
            return type(res)(0, json.dumps(doc))
        return res

    rig.run._rauc = rauc


def test_upgrade_refuses_when_rauc_slots_do_not_match_the_inplace_layout(tmp_path):
    rig = Rig(tmp_path)
    _inplace_layout(rig, dev_a="/dev/mmcblk1p2", dev_b="/dev/mmcblk1p3")      # the flash-once system.conf
    with pytest.raises(FlashError) as exc:
        rig.updater.upgrade(reboot=False)
    assert exc.value.code == "slot_layout" and "partition 5" in exc.value.message
    assert rig.run.installs == [] and rig.state.load().get("pending") is None


def test_upgrade_proceeds_when_rauc_slots_match_the_inplace_layout(tmp_path):
    rig = Rig(tmp_path)
    _inplace_layout(rig)
    rig.updater.upgrade(reboot=False)
    assert len(rig.run.installs) == 1


def test_upgrade_accepts_by_partuuid_slots_that_name_the_layouts_own_partitions(tmp_path):
    """The bench microSD image addresses its RAUC slots by PARTUUID, so they cannot resolve to the eMMC."""
    rig = Rig(tmp_path)
    _inplace_layout(rig, dev_a="/dev/disk/by-partuuid/e17a5d01-05", dev_b="/dev/disk/by-partuuid/e17a5d01-06",
                    partuuids=("e17a5d01-05", "e17a5d01-06"))
    rig.updater.upgrade(reboot=False)
    assert len(rig.run.installs) == 1


@pytest.mark.parametrize("dev_b", ["/dev/disk/by-partuuid/6a783df4-06",     # right number, another disk (the stock eMMC id)
                                   "/dev/disk/by-partuuid/e17a5d01-02"])    # right disk, wrong partition
def test_upgrade_refuses_by_partuuid_slots_that_are_not_the_layouts_partitions(tmp_path, dev_b):
    rig = Rig(tmp_path)
    _inplace_layout(rig, dev_a="/dev/disk/by-partuuid/e17a5d01-05", dev_b=dev_b, partuuids=("e17a5d01-05", "e17a5d01-06"))
    with pytest.raises(FlashError) as exc:
        rig.updater.upgrade(reboot=False)
    assert exc.value.code == "slot_layout" and rig.run.installs == []


def test_partially_flashed_mcus_are_all_put_back_when_a_later_board_fails(tmp_path):
    rig = _staged(tmp_path)
    rig.run.mcu_fail = True
    rig.run.mcu_done_before_fail = ["mainboard"]           # mainboard already runs the new firmware, toolhead failed
    rig.updater.post_boot()
    assert rig.run.mcu_rollbacks == ["mainboard"] and rig.rebooted == 1


def test_first_boot_partial_mcu_failure_restores_the_boards_that_were_flashed(tmp_path):
    rig = _converted(tmp_path, tries="2")
    rig.run.mcu_fail = True
    rig.run.mcu_done_before_fail = ["mainboard"]
    rig.updater.prepare()
    assert rig.run.mcu_rollbacks == ["mainboard"]
