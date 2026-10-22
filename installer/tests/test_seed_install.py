# SPDX-License-Identifier: Apache-2.0
"""The data seed in the in-place conversion: verified by stage 1 before the point of no return, installed by stage 2."""
import json
import os
import shutil
import stat
import subprocess
import tarfile

import pytest

import seedpack
from helpers import Signer
from printerfix import INSTALLER, Printer, sha256
from test_install import make_backup_file, real_run
from test_stage2 import Stage2Rig


@pytest.fixture
def printer(tmp_path):
    return Printer(tmp_path, Signer(tmp_path))


def resign(printer):
    data = json.dumps(printer.install_doc, indent=1).encode()
    open(os.path.join(printer.bundle, "install.json"), "wb").write(data)
    open(os.path.join(printer.bundle, "install.json.minisig"), "w").write(printer.signer.sign(data))


def replace_seed(printer, flavor="klipper", **kw):
    """Publish a different seed for *flavor* and sign the new digests (as a release would)."""
    info = seedpack.make_seed(printer.bundle, "2026.10.0", flavor, **kw)
    entry = printer.install_doc["files"]["data_seed"][flavor]
    entry.update(sha256=info["sha256"], size=info["size"])
    entry["manifest"].update(sha256=info["manifest_sha256"], size=info["manifest_size"])
    resign(printer)
    printer.seeds[flavor] = info
    return info


# ---- stage 1: what is staged and checked -------------------------------------------------------------------------------
def test_the_dry_run_plans_to_fetch_the_seed_and_its_manifest_but_fetches_nothing(printer):
    r = printer.install("--flavor", "kalico")
    assert r.returncode == 0, r.stdout
    out = r.stdout
    assert "would stage data-seed-2026.10.0-kalico.manifest.json" in out and "would stage data-seed-2026.10.0-kalico.tar.zst" in out
    assert out.index("kalico.manifest.json") < out.index("kalico.tar.zst")       # the small signed manifest first
    assert "klipper.tar.zst" not in out                                           # only the flavor that was asked for
    assert "all 5 files of the local bundle verified in place" in out             # --bundle-dir: checked where it lies
    assert not os.path.exists(printer.state)                                      # and nothing was written on the printer


def test_a_real_run_verifies_the_seed_before_arming(printer):
    r = real_run(printer)
    assert r.returncode == 0, r.stdout
    assert "data seed 2026.10.0 (klipper): checksum, manifest, pinned firmware and unpack rules verified" in r.stdout
    payload = os.path.join(printer.state, "payload")
    assert os.path.isfile(payload + "/data-seed-2026.10.0-klipper.tar.zst") and os.path.isfile(payload + "/data-seed-2026.10.0-klipper.manifest.json")
    assert "data-seed-2026.10.0-klipper.manifest.json" in open(payload + "/payload.sha256").read()
    assert not os.path.exists(os.path.join(printer.state, "seed-rehearsal"))       # the scratch directory is gone
    assert os.path.exists(printer.p("/boot/boot.scr"))                             # armed


def test_the_kalico_seed_is_what_gets_staged_for_kalico(printer):
    path, digest = make_backup_file(printer)
    r = printer.install("--flavor", "kalico", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path,
                        "--backup-sha256", digest, "--confirm", "CONVERT")
    assert r.returncode == 0, r.stdout
    penv = dict(l.split("=", 1) for l in open(os.path.join(printer.state, "payload", "payload.env")).read().splitlines())
    assert penv["data_seed_file"] == "data-seed-2026.10.0-kalico.tar.zst" and penv["data_seed_sha256"] == printer.seeds["kalico"]["sha256"]


def test_an_install_json_without_the_seed_manifest_is_refused(printer):
    del printer.install_doc["files"]["data_seed"]["klipper"]["manifest"]
    resign(printer)
    r = printer.install("--flavor", "klipper")
    assert r.returncode == 12 and not os.path.exists(printer.p("/boot/eitri"))


@pytest.mark.parametrize("kw,text", [
    ({"commit": "c" * 40}, "release pins"),
    ({"describe": "v0.0.1-1-gabcdef012"}, "pins"),
    ({"abi": "cp399"}, "cp399"),
    ({"mutate_manifest": lambda d: d.update(layout=2)}, "layout"),
    ({"mutate_manifest": lambda d: d["target"].update(arch="x86_64")}, "aarch64"),
    ({"mutate_manifest": lambda d: d.update(flavor="kalico")}, "kalico seed"),
    ({"mutate_manifest": lambda d: d.update(version="2026.09.9")}, "release"),
    ({"uncompressed_size": 5 << 30}, "needs about"),
])
def test_a_seed_that_does_not_fit_the_signed_release_is_refused_before_anything_changes(printer, kw, text):
    replace_seed(printer, **kw)
    if "abi" in kw:
        printer.install_doc["target"]["python_abi"] = "cp314"
        resign(printer)
    r = real_run(printer)
    assert r.returncode == 12 and text in r.stdout, r.stdout
    assert not os.path.exists(printer.p("/boot/eitri")) and os.path.exists(printer.p("/boot/extlinux/extlinux.conf"))
    assert "systemctl reboot" not in printer.calls()


@pytest.mark.parametrize("member", [("../evil", b"x"), ("etc/cron.d/x", b"x"), ("/abs/evil", b"x"), ("firmware/klipper/../../../x", b"x")])
def test_a_seed_with_members_outside_its_layout_fails_the_unpack_rehearsal(printer, member):
    replace_seed(printer, extra_members=[member])
    r = real_run(printer)
    assert r.returncode == 12 and "failed its unpack check" in r.stdout, r.stdout
    assert not os.path.exists(printer.p("/boot/eitri")) and "systemctl reboot" not in printer.calls()


def test_a_seed_with_a_device_node_or_an_escaping_link_fails_the_rehearsal(printer):
    link = tarfile.TarInfo("eitri/extras/evil")
    link.type, link.linkname = tarfile.SYMTYPE, "../../../../etc"
    replace_seed(printer, extra_members=[(link, None)])
    r = real_run(printer)
    assert r.returncode == 12 and "failed its unpack check" in r.stdout and not os.path.exists(printer.p("/boot/eitri"))


def test_a_seed_that_is_really_the_other_flavor_is_refused(printer):
    replace_seed(printer, flavor_inside="kalico")
    r = real_run(printer)
    assert r.returncode == 12 and not os.path.exists(printer.p("/boot/eitri"))


def test_a_tampered_seed_download_never_gets_armed(printer):
    with open(os.path.join(printer.bundle, "data-seed-2026.10.0-klipper.tar.zst"), "ab") as fh:
        fh.write(b"bitrot")
    r = real_run(printer)
    assert r.returncode == 12 and "does not match the signed checksum" in r.stdout and not os.path.exists(printer.p("/boot/eitri"))


def test_a_tampered_seed_manifest_is_caught_too(printer):
    with open(os.path.join(printer.bundle, "data-seed-2026.10.0-klipper.manifest.json"), "a") as fh:
        fh.write(" ")
    r = real_run(printer)
    assert r.returncode == 12 and "does not match the signed checksum" in r.stdout


def test_a_full_stock_disk_only_warns_about_the_rehearsal(printer):
    """The rehearsal unpacks nothing; its space check is about the stock disk and stage 2 checks the real /data."""
    r = real_run(printer, EITRI_SEED_FAKE_FREE_KIB="1")
    assert r.returncode == 0, r.stdout
    assert "too full to rehearse" in r.stdout


# ---- offline -------------------------------------------------------------------------------------------------------------
def curl_logger(tmp_path):
    d = tmp_path / "curlshim"
    d.mkdir()
    log = tmp_path / "curl.log"
    (d / "curl").write_text("#!/bin/sh\nfor a; do echo \"$a\" >>%s; done\nexec %s \"$@\"\n" % (log, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shims", "curl")))
    (d / "curl").chmod(0o755)
    return d, log


def test_a_bundle_on_a_usb_stick_installs_without_any_network_access(printer, tmp_path):
    shim, log = curl_logger(tmp_path)
    path, digest = make_backup_file(printer)
    env = printer.env()
    env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    r = subprocess.run([os.path.join(INSTALLER, "install.sh"), "--bundle-dir", printer.bundle, "--pubkey", printer.signer.pub, "--flavor", "klipper",
                        "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest, "--confirm", "CONVERT"],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
    assert r.returncode == 0, r.stdout
    urls = [l for l in open(log).read().split() if "://" in l]
    assert urls and all(u.startswith(("file://", "http://127.0.0.1:7125")) for u in urls), urls


def test_seed_file_replaces_the_download_but_not_the_verification(printer, tmp_path):
    seed_dir = tmp_path / "from-my-pc"
    seed_dir.mkdir()
    info = printer.seeds["klipper"]
    shutil.move(info["path"], seed_dir / info["name"])
    shutil.move(info["manifest_path"], seed_dir / os.path.basename(info["manifest_path"]))
    path, digest = make_backup_file(printer)
    common = ["--flavor", "klipper", "--i-have-a-backup", "--yes", "--mcu", "skip", "--backup-file", path, "--backup-sha256", digest, "--confirm", "CONVERT"]
    # the bundle no longer has the seed: the normal run cannot find it ...
    assert printer.install(*common).returncode == 12
    # ... but the files from the PC work
    r = printer.install(*common, "--seed-file", str(seed_dir / info["name"]), "--seed-manifest", str(seed_dir / os.path.basename(info["manifest_path"])))
    assert r.returncode == 0, r.stdout
    assert "taken from" in r.stdout and "no download" in r.stdout
    assert os.path.isfile(os.path.join(printer.state, "payload", info["name"]))


def test_a_wrong_seed_file_is_still_caught_by_the_signed_digest(printer, tmp_path):
    other = seedpack.make_seed(str(tmp_path / "other"), "2026.10.0", "klipper", extra_members=[("firmware/moonraker/EXTRA", b"x")])
    r = real_run(printer, "--seed-file", other["path"], "--seed-manifest", other["manifest_path"])
    assert r.returncode == 12 and "does not match the signed checksum" in r.stdout and not os.path.exists(printer.p("/boot/eitri"))


def test_seed_file_options_come_as_a_pair_and_must_be_readable(printer, tmp_path):
    f = tmp_path / "x"
    f.write_text("x")
    assert printer.install("--flavor", "klipper", "--seed-file", str(f)).returncode == 2
    assert printer.install("--flavor", "klipper", "--seed-file", str(f), "--seed-manifest", str(tmp_path / "missing")).returncode == 2
    spaced = tmp_path / "a b"
    spaced.write_text("x")
    assert printer.install("--flavor", "klipper", "--seed-file", str(spaced), "--seed-manifest", str(f)).returncode == 2


# ---- prepare_payload ------------------------------------------------------------------------------------------------------
def pp(*args):
    return subprocess.run(["python3", "-I", os.path.join(INSTALLER, "tools", "prepare_payload.py")] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


def test_check_seed_reports_what_it_verified(printer):
    r = pp("check-seed", "--install-json", os.path.join(printer.bundle, "install.json"), "--flavor", "kalico", "--dir", printer.bundle)
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info["archive_sha256"] == printer.seeds["kalico"]["sha256"] and info["commit"] == printer.seeds["kalico"]["commit"]


def test_the_plan_lists_the_small_manifest_before_the_big_archive(printer, tmp_path):
    out = tmp_path / "plan"
    assert pp("plan", "--install-json", os.path.join(printer.bundle, "install.json"), "--flavor", "klipper", "--out-dir", str(out)).returncode == 0
    names = [l.split("\t")[0] for l in open(out / "downloads.tsv").read().splitlines()]
    assert names.index("data-seed-2026.10.0-klipper.manifest.json") < names.index("data-seed-2026.10.0-klipper.tar.zst")
    assert "data-seed.tar.zst" not in names


def test_the_seed_must_belong_to_the_release_version(printer):
    printer.install_doc["version"] = "2026.11.0"
    resign(printer)
    r = pp("check-seed", "--install-json", os.path.join(printer.bundle, "install.json"), "--flavor", "klipper", "--dir", printer.bundle)
    assert r.returncode == 1                       # files named for 2026.11.0 do not exist; the old name is never guessed


# ---- stage 2 -------------------------------------------------------------------------------------------------------------
@pytest.fixture
def rig(tmp_path):
    return Stage2Rig(tmp_path)


def swap_seed(rig, **kw):
    """Replace the staged seed (and the values stage 2 reads about it) as if stage 1 had staged that one."""
    info = seedpack.make_seed(rig.payload, "2026.10.0", kw.pop("flavor", "klipper"), **kw)
    sums = [l for l in open(os.path.join(rig.payload, "payload.sha256")).read().splitlines() if "data-seed-" not in l]
    sums += ["%s  %s" % (info["sha256"], info["name"]), "%s  %s" % (info["manifest_sha256"], os.path.basename(info["manifest_path"]))]
    open(os.path.join(rig.payload, "payload.sha256"), "w").write("\n".join(sums) + "\n")
    env = open(os.path.join(rig.payload, "payload.env")).read().replace(rig.fixture.seeds["klipper"]["sha256"], info["sha256"])
    open(os.path.join(rig.payload, "payload.env"), "w").write(env)
    return info


def stock_still_boots(rig):
    env = rig.boot_env()
    return env["eitri_mode"] == "stock" and os.path.exists(rig.p("/mnt/boot/eitri/LAST_ERROR"))


def test_stage_two_hands_the_digest_from_the_signed_install_json_to_the_seed_installer(rig):
    penv = open(os.path.join(rig.payload, "payload.env")).read()
    assert "data_seed_sha256=" + rig.fixture.seeds["klipper"]["sha256"] in penv
    r = rig.run()
    assert r.returncode == 0, r.stdout
    assert os.path.isfile(rig.p("/mnt/data/eitri/state/seed-installed.env"))
    assert "seed_version=2026.10.0" in open(rig.p("/mnt/data/eitri/state/install.env")).read()


def test_a_digest_that_differs_from_the_signed_one_stops_before_the_switch(rig):
    penv = open(os.path.join(rig.payload, "payload.env")).read().replace(rig.fixture.seeds["klipper"]["sha256"], "0" * 64)
    open(os.path.join(rig.payload, "payload.env"), "w").write(penv)
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "installing the data seed failed" in r.stdout
    assert stock_still_boots(rig)
    assert not os.listdir(rig.p("/mnt/data")) or not os.path.exists(rig.p("/mnt/data/firmware"))


@pytest.mark.parametrize("member", [("../evil", b"x"), ("etc/passwd", b"x")])
def test_a_seed_with_hostile_members_is_refused_by_stage_two_as_well(rig, member):
    swap_seed(rig, extra_members=[member])
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "installing the data seed failed" in r.stdout
    assert stock_still_boots(rig) and not os.path.exists(rig.p("/mnt/data/firmware"))
    assert not os.path.exists(rig.root + "/mnt/evil") and not os.path.exists(rig.p("/mnt/data/etc"))


def test_a_seed_of_the_wrong_flavor_is_refused(rig):
    rig.write_conf(flavor="kalico")
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1 and stock_still_boots(rig)


def test_a_missing_seed_installer_is_an_early_boot_failure(rig):
    r = rig.run(S2_FAIL_DELAY="0", S2_SEED_INSTALL="/nonexistent/eitri-seed-install")
    assert r.returncode == 1 and "eitri-seed-install is missing" in r.stdout and stock_still_boots(rig)


@pytest.mark.parametrize("key", ["data_seed_sha256", "data_seed_file"])
def test_payload_env_must_carry_the_seed_fields(rig, key):
    lines = [l for l in open(os.path.join(rig.payload, "payload.env")).read().splitlines() if not l.startswith(key + "=")]
    open(os.path.join(rig.payload, "payload.env"), "w").write("\n".join(lines) + "\n")
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "payload.env lacks" in r.stdout and not [c for c in rig.calls() if c == "mke2fs"]


def test_a_seed_file_name_that_could_climb_out_is_refused(rig):
    env = open(os.path.join(rig.payload, "payload.env")).read().replace("data_seed_file=data-seed", "data_seed_file=../data-seed")
    open(os.path.join(rig.payload, "payload.env"), "w").write(env)
    r = rig.run(S2_FAIL_DELAY="0")
    assert r.returncode == 1 and "bad data seed file name" in r.stdout and not [c for c in rig.calls() if c == "mke2fs"]


def test_the_stage_two_tool_list_includes_what_the_seed_installer_needs(rig):
    text = open(os.path.join(INSTALLER, "stage2", "stage2.sh")).read()
    for tool in ("find", "grep", "date", "df", "mv", "rm", "rmdir", "mkdir", "cat", "tr"):
        assert " %s" % tool in text.split("S2_REQUIRED_TOOLS:-")[1].split("}")[0]


def test_the_kalico_flavor_installs_the_kalico_seed(tmp_path):
    rig = Stage2Rig(tmp_path)
    shutil.rmtree(rig.payload)
    shutil.copytree(rig.fixture.bundle, rig.payload)
    subprocess.run(["python3", os.path.join(INSTALLER, "tools", "prepare_payload.py"), "finalize", "--install-json",
                    os.path.join(rig.payload, "install.json"), "--flavor", "kalico", "--dir", rig.payload], check=True)
    rig.write_conf(flavor="kalico")
    r = rig.run()
    assert r.returncode == 0, r.stdout
    data = rig.p("/mnt/data")
    assert open(data + "/eitri/flavor").read().strip() == "kalico"
    assert open(data + "/firmware/klipper/klippy/klippy.py").read().startswith("# kalico 2026.10.0")


# ---- the switch-flavor helper the configurator calls --------------------------------------------------------------------
def test_the_switch_flavor_helper_only_takes_a_flavor_and_goes_through_sudo(tmp_path):
    helper = os.path.join(INSTALLER, "bin", "eitri-switch-flavor")
    for args in ([], ["marlin"], ["klipper", "extra"], ["--help"], ["klipper;reboot"]):
        r = subprocess.run([helper] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        assert r.returncode == 2 and "usage" in r.stderr
    shim = tmp_path / "bin"
    shim.mkdir()
    log = tmp_path / "log"
    for name in ("sudo", "eitri-update"):
        (shim / name).write_text("#!/bin/sh\necho \"%s $*\" >>%s\n" % (name, log))
        (shim / name).chmod(0o755)
    # as an unprivileged user: sudo -n with the absolute path of eitri-update (the sudoers rule names that path)
    if os.geteuid() != 0:
        r = subprocess.run([helper, "kalico"], env=dict(os.environ, PATH=str(shim) + os.pathsep + os.environ["PATH"]), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        assert r.returncode == 0
        line = open(log).read().strip()
        assert line.startswith("sudo -n /usr/bin/eitri-update --json") and line.endswith("switch-flavor --yes kalico")
