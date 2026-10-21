# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the brick/update-safety review: GPT backend, blocked MCU plans, unsettled systems, unconfigured
printers, manifest freshness, key revocation and hostile toolchain archives."""
import datetime
import io
import json
import os
import tarfile

import pytest

from helpers import Rig, make_toolchain_archive
from eitri_mcuflash.errors import FlashError
from eitri_update import bootctl as bootctl_cli, manifest as manifest_mod, minisign
from eitri_update.bootenv import BootControl
from eitri_update.upstream import check_archive_members, check_tree_contained, UpstreamMode, UpstreamPaths


# ---- the flash-once (GPT) image: prepare / verify must work against rauc-bootloader -----------------
def _gpt_rig(tmp_path, **kw):
    rig = Rig(tmp_path, gpt=True, **kw)
    rig.bootctl.set_state("A", "good")             # the flash-once system has proven itself once
    rig.run.helper_calls.clear()
    return rig


def _reboot_into(rig, slot):
    other = "A" if slot == "B" else "B"
    rig.run.rauc_slots = {slot: {"state": "booted", "boot_status": "good", "version": "2026.10.0"},
                          other: {"state": "inactive", "boot_status": "good", "version": "2026.09.0"}}
    open(rig.cmdline, "w").write("root=/dev/mmcblk1p%d ro rootwait rauc.slot=%s eitri.disk=%s\n" % (2 if slot == "A" else 3, slot, rig.state_dev))
    rig.rebooted = 0
    rig.run.calls.clear()


def test_gpt_backend_refuses_to_write_the_raw_sector_without_eitri_disk(tmp_path):
    """No eitri.disk=: not started by the Eitri U-Boot (e.g. a bench microSD card whose boot.env is not mounted).
    The raw sector on the eMMC is then never written, the failure surfaces instead."""
    rig = _gpt_rig(tmp_path)
    open(rig.cmdline, "w").write("root=PARTUUID=e17a5d01-05 ro rootwait eitri.slot=A\n")
    before = open(rig.state_dev, "rb").read()
    with pytest.raises(FlashError) as exc:
        rig.bootctl.set_state("A", "good")
    assert "refusing" in exc.value.message
    assert open(rig.state_dev, "rb").read() == before


def test_gpt_backend_is_selected_without_a_vfat_boot_env(tmp_path):
    rig = _gpt_rig(tmp_path)
    assert rig.bootctl.backend == "gpt"
    assert rig.bootctl.current_slot() == "A"                         # rauc.slot=, not eitri.slot=
    assert rig.bootctl.get_primary() == "A"


def test_gpt_prepare_and_verify_run_instead_of_raising(tmp_path):
    rig = _gpt_rig(tmp_path)
    rig.bootctl.set_state("A", "bad")                                 # a fresh image: nothing confirmed yet
    assert rig.bootctl.get_state("A") == "bad"
    assert rig.updater.prepare() == {"pending": False}
    res = rig.updater.verify()
    assert res["confirmed"] == "A"
    assert rig.bootctl.get_state("A") == "good"
    assert rig.updater.verify() == {"pending": False}                 # now a no-op


def test_gpt_full_update_cycle_commits_and_clears_pending(tmp_path):
    rig = _gpt_rig(tmp_path)
    rig.updater.upgrade()
    assert rig.state.load()["pending"]["phase"] == "await-reboot"
    assert rig.bootctl.confirmed() == {"A": True}                     # B's old proof (if any) is gone, A's is kept
    rig.bootctl.set_primary("B")                                      # what `rauc install` does
    _reboot_into(rig, "B")
    rig.updater.post_boot()
    assert rig.state.installed_version == "2026.10.0" and rig.state.load()["pending"] is None
    assert rig.bootctl.get_state("B") == "good" and rig.bootctl.confirmed() == {"A": True, "B": True}


def test_gpt_failed_health_rolls_back_to_the_confirmed_slot(tmp_path):
    rig = _gpt_rig(tmp_path, health_ok=False)
    rig.updater.upgrade()
    rig.bootctl.set_primary("B")
    _reboot_into(rig, "B")
    rig.updater.post_boot()
    assert rig.rebooted == 1
    assert rig.bootctl.get_primary() == "A" and rig.bootctl.get_state("B") == "bad"


def test_gpt_unwritten_or_unproven_slot_is_never_a_rollback_target(tmp_path):
    """rauc-bootloader calls any slot with attempts left good, including the never-written rootfs-b."""
    rig = _gpt_rig(tmp_path)
    assert rig.gpt_raw_state("B") == "good"                          # the trap
    assert rig.bootctl.get_state("B") == "bad"                       # ... which eitri-update does not fall for
    with pytest.raises(FlashError) as e:
        rig.updater.rollback()
    assert e.value.code == "no_rollback_target"


def test_gpt_failed_health_without_a_confirmed_other_slot_does_not_switch_to_it(tmp_path):
    rig = _gpt_rig(tmp_path, health_ok=False)
    rig.bootctl._set_confirmed("A", False)                          # running slot A never proved itself
    rig.updater.verify()
    assert rig.rebooted == 1 and rig.bootctl.get_primary() == "A"   # retry, never the empty slot B
    assert rig.gpt_raw_state("B") == "good" and rig.bootctl.get_state("B") == "bad"


def test_gpt_bootctl_cli_contract(tmp_path, capsys):
    rig = _gpt_rig(tmp_path)
    ctl = rig.bootctl
    assert bootctl_cli.main(["get-primary"], ctl) == 0 and capsys.readouterr().out.strip() == "A"
    assert bootctl_cli.main(["get-current"], ctl) == 0 and capsys.readouterr().out.strip() == "A"
    assert bootctl_cli.main(["set-primary", "B"], ctl) == 0 and ctl.get_primary() == "B"
    assert bootctl_cli.main(["set-state", "B", "good"], ctl) == 0 and ctl.get_state("B") == "good"
    assert bootctl_cli.main(["status"], ctl) == 0
    assert "backend=gpt" in capsys.readouterr().out
    assert bootctl_cli.main(["set-primary", "C"], ctl) == 1


def test_gpt_confirmation_is_dropped_when_the_slot_is_marked_bad(tmp_path):
    rig = _gpt_rig(tmp_path)
    rig.bootctl.set_state("A", "bad")
    assert rig.bootctl.confirmed() == {} and rig.gpt_raw_state("A") == "bad"


# ---- MCU sync must not read a blocked plan as "already matches" ---------------------------------
def _blocked_plan(rig, **plan):
    orig = rig.run._mcuflash

    def fake(argv, kw):
        if argv[argv.index("eitri_mcuflash") + 1] == "plan":
            ev = {"type": "result", "ok": False, "msg": "blocked by preflight", "data": plan}
            if kw.get("on_line"):
                kw["on_line"](json.dumps(ev))
            from eitri_mcuflash.proc import RunResult
            return RunResult(3, json.dumps(ev))
        return orig(argv, kw)
    rig.run._mcuflash = fake


def test_blocked_mcu_plan_is_an_error_not_a_match(tmp_path):
    rig = Rig(tmp_path, installed=None)
    open(os.path.join(rig.data, "eitri", "state", "install.env"), "w").write("mcu_flash=auto\n")
    _blocked_plan(rig, blocked=True, boards={}, checks=[{"id": "port", "status": "block", "detail": "/dev/ttyS2 is busy"}])
    rig.bootctl.update(eitri_state_A="unknown")
    rig.updater.prepare()
    assert rig.state.load().get("firstboot_mcu_done") is not True       # will be retried, not forgotten
    assert rig.rebooted == 1                                            # the boot manager gets its say
    failed = [e for e in rig.events.events if e["type"] == "step" and e.get("status") == "failed"]
    assert failed and "cannot tell whether the MCU firmware matches" in failed[0]["msg"]


@pytest.mark.parametrize("plan", [
    {"boards": {}},
    {"boards": {"mainboard": {"needs_flash": False}}},                      # a board went missing from the plan
    {"boards": {"mainboard": {"needs_flash": False, "firmware": None}, "toolhead": {"needs_flash": False}}},
])
def test_incomplete_mcu_plan_is_refused(tmp_path, plan):
    rig = Rig(tmp_path)
    _blocked_plan(rig, **plan)
    with pytest.raises(FlashError) as e:
        rig.updater.mcu_sync()
    assert e.value.code == "mcu_plan_blocked"


def test_complete_matching_plan_still_means_nothing_to_do(tmp_path):
    rig = Rig(tmp_path)
    rig.run.mcu_boards = {"mainboard": False, "toolhead": False}
    assert rig.updater.mcu_sync() == {"mcu_flashed": []}


# ---- upgrade must not overwrite the only known-good slot -----------------------------------------
def test_upgrade_refused_while_an_earlier_update_is_pending(tmp_path):
    rig = Rig(tmp_path)
    rig.updater.upgrade()
    installs = len(rig.run.installs)
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == "update_pending" and len(rig.run.installs) == installs


def test_upgrade_refused_while_the_running_slot_is_unconfirmed(tmp_path):
    rig = Rig(tmp_path)
    rig.bootctl.update(eitri_state_A="unknown")
    with pytest.raises(FlashError) as e:
        rig.updater.upgrade()
    assert e.value.code == "unconfirmed_slot" and rig.run.installs == []


def test_upgrade_refused_when_the_running_slot_cannot_be_determined(tmp_path):
    rig = Rig(tmp_path)
    open(rig.cmdline, "w").write("root=/dev/mmcblk1p2 ro\n")
    with pytest.raises(FlashError):
        rig.updater.upgrade()
    assert rig.run.installs == []


def test_dry_run_upgrade_still_works_on_an_unsettled_system(tmp_path):
    rig = Rig(tmp_path)
    rig.bootctl.update(eitri_state_A="unknown")
    assert rig.updater.upgrade(dry_run=True)["to"] == "2026.10.0"


# ---- an unconfigured printer must not make a good OS look bad ---------------------------------------
def test_verify_commits_on_os_health_when_klipper_is_not_configured(tmp_path):
    rig = Rig(tmp_path, installed=None, configured=False, health_ok=False)
    rig.bootctl.update(eitri_state_A="unknown")
    res = rig.updater.verify(start_services=True)
    assert res["confirmed"] == "A" and rig.rebooted == 0
    assert rig.boot_env()["eitri_state_A"] == "good"
    assert not any(c[:2] == ["systemctl", "start"] for c in rig.run.calls)       # nothing to start or wait for


def test_verify_still_gates_on_klipper_once_it_is_configured(tmp_path):
    rig = Rig(tmp_path, installed=None, configured=True, health_ok=False)
    rig.bootctl.update(eitri_state_A="unknown")
    rig.updater.verify()
    assert rig.rebooted == 1


def test_a_half_configured_printer_still_gets_the_klipper_gate(tmp_path):
    """Only a printer where NONE of the application is missing is exempt; one missing file counts as unconfigured
    (the unit would not start either), but a fully configured one is always gated."""
    rig = Rig(tmp_path, installed=None, health_ok=False)
    assert rig.cfg.application_configured()
    os.unlink(os.path.join(rig.data, "printer_data", "config", "moonraker.conf"))
    assert not rig.cfg.application_configured()


def test_event_log_in_a_missing_directory_does_not_stop_the_command(tmp_path):
    from eitri_mcuflash.events import EventSink
    sink = EventSink(events_file=str(tmp_path / "nope" / "logs" / "update.jsonl"))
    sink.info("hello")
    sink.close()
    assert (tmp_path / "nope" / "logs" / "update.jsonl").exists()
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        if os.access(str(ro), os.W_OK):
            pytest.skip("running as root")
        EventSink(events_file=str(ro / "x" / "u.jsonl")).info("still fine")
    finally:
        ro.chmod(0o700)


# ---- manifest freshness ---------------------------------------------------------------------------------
def _manifest(**over):
    doc = {"schema": 1, "sequence": 3, "generated": "2026-10-21T00:00:00Z", "expires": "2030-01-01T00:00:00Z",
           "releases": [{"version": "2026.10.0", "bundles": [{"kind": "os", "url": "https://x/os", "sha256": "0" * 64, "size": 5}]}]}
    doc.update(over)
    return json.dumps({k: v for k, v in doc.items() if v is not None}).encode()


def test_expires_is_mandatory():
    with pytest.raises(FlashError) as e:
        manifest_mod.parse_manifest(_manifest(expires=None))
    assert e.value.code == "manifest" and "expires" in e.value.message


@pytest.mark.parametrize("bad", ["2030-01-01", "tomorrow", "2030-13-45T00:00:00Z", 5])
def test_malformed_expires_is_a_flash_error_not_a_crash(bad):
    with pytest.raises(FlashError) as e:
        manifest_mod.parse_manifest(_manifest(expires=bad))
    assert e.value.code == "manifest"


def test_an_unset_clock_cannot_resurrect_an_expired_manifest():
    m = manifest_mod.parse_manifest(_manifest(expires="2026-03-01T00:00:00Z", generated="2026-02-01T00:00:00Z"))
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    assert m.expired(now=epoch)                                   # clock says 1970; the floor says otherwise
    fresh = manifest_mod.parse_manifest(_manifest(expires="2099-01-01T00:00:00Z"))
    assert not fresh.expired(now=epoch)


def test_the_signed_generated_time_also_moves_the_clock_forward():
    m = manifest_mod.parse_manifest(_manifest(generated="2040-01-01T00:00:00Z", expires="2035-01-01T00:00:00Z"))
    assert m.expired(now=datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc))


def test_sequence_floor_shipped_in_the_image_survives_a_data_reset(tmp_path):
    rig = Rig(tmp_path, sequence=5)
    floor = tmp_path / "min-seq"
    floor.write_text("9\n")
    rig.cfg.min_sequence_file = str(floor)
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "stale_manifest"
    floor.write_text("5\n")
    assert rig.updater.check()["latest"] == "2026.10.0"


# ---- trust roots -------------------------------------------------------------------------------------------
def test_default_trust_root_is_only_the_signed_image():
    from eitri_update.client import Config, DEFAULT_KEY_DIRS
    assert DEFAULT_KEY_DIRS == ["/usr/share/eitri/keys"] and Config().key_paths == ["/usr/share/eitri/keys"]


def test_revoked_keys_are_dropped(tmp_path):
    rig = Rig(tmp_path)
    keys = minisign.load_keys([rig.signer.dir])
    assert len(keys) == 1
    (tmp_path / "keys-release" / "revoked-keys.txt").write_text("# compromised 2026-12\n%s\n" % keys[0].key_id_hex.lower())
    with pytest.raises(minisign.SignatureError):
        minisign.load_keys([rig.signer.dir])
    with pytest.raises(FlashError) as e:
        rig.updater.check()
    assert e.value.code == "bad_signature"


def test_a_second_key_keeps_working_when_the_first_is_revoked(tmp_path):
    from helpers import Signer
    rig = Rig(tmp_path)
    other = Signer(tmp_path, "next")
    d = tmp_path / "both"
    d.mkdir()
    for s in (rig.signer, other):
        (d / os.path.basename(s.pub)).write_text(open(s.pub).read())
    first = minisign.load_keys([rig.signer.dir])[0]
    (d / "revoked-keys.txt").write_text(first.key_id_hex + "\n")
    assert [k.key_id_hex for k in minisign.load_keys([str(d)])] == [minisign.load_keys([other.dir])[0].key_id_hex]


# ---- hostile toolchain archives -----------------------------------------------------------------------------
def _members(entries):
    out = []
    for name, kind, link in entries:
        ti = tarfile.TarInfo(name)
        ti.type = {"file": tarfile.REGTYPE, "sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE, "dir": tarfile.DIRTYPE, "dev": tarfile.CHRTYPE}[kind]
        ti.linkname = link
        out.append(ti)
    return out


def test_relative_links_inside_the_toolchain_are_allowed():
    check_archive_members(_members([("toolchain/bin/gcc", "file", ""), ("toolchain/bin/cc", "sym", "gcc"),
                                    ("toolchain/lib/libx.so", "sym", "../lib/libx.so.1"), ("toolchain/bin/gcc2", "hard", "toolchain/bin/gcc")]))


@pytest.mark.parametrize("entries", [
    [("toolchain/evil", "sym", "/etc")],
    [("toolchain/evil", "sym", "../../etc")],
    [("toolchain/bin/evil", "sym", "../../../etc")],
    [("toolchain/evil", "hard", "/etc/passwd")],
    [("toolchain/evil", "hard", "other/../../x")],
    [("toolchain/dev", "dev", "")],
    [("/abs", "file", "")],
    [("toolchain/../x", "file", "")],
])
def test_links_and_paths_that_escape_are_rejected(entries):
    with pytest.raises(FlashError) as e:
        check_archive_members(_members(entries))
    assert e.value.code == "bad_archive"


def _raw_tar(path, build):
    raw = path[: -len(".zst")]
    with tarfile.open(raw, "w") as tf:
        build(tf)
    import subprocess
    subprocess.run(["zstd", "-q", "-f", raw, "-o", path], check=True)
    os.unlink(raw)


def test_symlink_then_file_beneath_it_is_refused_before_anything_is_written(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    archive = str(tmp_path / "tc.tar.zst")

    def build(tf):
        link = tarfile.TarInfo("toolchain/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tf.addfile(link)
        info = tarfile.TarInfo("toolchain/escape/pwned")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    _raw_tar(archive, build)
    rig = Rig(tmp_path)
    rig.channel.add_toolchain(archive)
    mode = UpstreamMode(rig.state, rig.events, UpstreamPaths(data_root=rig.data), runner=rig.run)
    from eitri_update.manifest import parse_manifest
    manifest = parse_manifest(open(os.path.join(rig.channel.dir, "manifest.json"), "rb").read())
    os.makedirs(os.path.join(rig.data, "firmware", "klipper", ".git"))
    with pytest.raises(FlashError) as e:
        mode.enable(manifest, restart=False)
    assert e.value.code == "bad_archive" and not (outside / "pwned").exists()


def test_post_extraction_check_catches_a_link_that_slipped_through(tmp_path):
    root = tmp_path / "stage"
    root.mkdir()
    os.symlink("/etc", str(root / "x"))
    with pytest.raises(FlashError):
        check_tree_contained(str(root))


def test_torn_toolchain_without_version_marker_is_not_trusted(tmp_path):
    rig = Rig(tmp_path)
    paths = UpstreamPaths(data_root=rig.data)
    os.makedirs(os.path.join(paths.toolchain, "bin"))
    open(os.path.join(paths.toolchain, "bin", "arm-none-eabi-gcc"), "w").write("x")
    mode = UpstreamMode(rig.state, rig.events, paths, runner=rig.run)
    assert not mode.toolchain_installed()
    open(os.path.join(paths.toolchain, "VERSION"), "w").write("1\n")
    assert mode.toolchain_installed()
