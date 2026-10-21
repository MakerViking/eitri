# SPDX-License-Identifier: Apache-2.0
"""The seed CI built (seed/build-seed.sh -> build/seed/out) through the installer's checks.  Skipped when it was not built."""
import glob
import json
import os
import subprocess

import pytest

from printerfix import INSTALLER

OUT = os.environ.get("EITRI_SEED_OUT", os.path.join(INSTALLER, "..", "..", "build", "seed", "out"))
SEEDS = sorted(glob.glob(os.path.join(OUT, "data-seed-*-*.manifest.json")))
pytestmark = pytest.mark.skipif(not SEEDS, reason="no built seeds in %s (make -C seed build)" % OUT)


def install_doc(manifest, tmp_path):
    m = json.load(open(manifest))
    fl = m["flavor"]
    arc = os.path.join(OUT, m["archive"]["name"])
    mdoc = {"url": os.path.basename(manifest), "sha256": __import__("hashlib").sha256(open(manifest, "rb").read()).hexdigest(),
            "size": os.path.getsize(manifest)}
    doc = {"schema": 1, "version": m["version"], "channel": "dev", "target": {"compatible": "firefly,roc-rk3328-cc", "python_abi": m["target"]["python_abi"]},
           "layout": {"root_mib": 2048, "data_min_mib": 4096}, "kernel_cmdline": "console=tty1",
           "flavors": {fl: {"commit": m["sources"]["firmware"]["commit"], "describe": m["sources"]["firmware"]["describe"]}},
           "files": {"rootfs": {"url": "r", "sha256": "0" * 64, "size": 1, "raw_size": 1, "raw_sha256": "0" * 64},
                     "stage2_initramfs": {"url": "s", "sha256": "0" * 64, "size": 1},
                     "mcu_firmware": {fl: {"url": "m", "sha256": "0" * 64, "size": 1}},
                     "data_seed": {fl: {"url": m["archive"]["name"], "sha256": m["archive"]["sha256"], "size": m["archive"]["size"], "manifest": mdoc}}}}
    path = tmp_path / "install.json"
    path.write_text(json.dumps(doc))
    return str(path), fl, arc


@pytest.mark.parametrize("manifest", SEEDS, ids=[os.path.basename(s).split(".manifest")[0] for s in SEEDS])
def test_the_built_seed_passes_the_stage_one_checks(manifest, tmp_path):
    ij, flavor, arc = install_doc(manifest, tmp_path)
    r = subprocess.run(["python3", "-I", os.path.join(INSTALLER, "tools", "prepare_payload.py"), "check-seed", "--install-json", ij,
                        "--flavor", flavor, "--dir", OUT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info["archive_name"] == os.path.basename(arc) and len(info["commit"]) == 40 and info["uncompressed_size"] > 100 << 20


@pytest.mark.parametrize("manifest", SEEDS, ids=[os.path.basename(s).split(".manifest")[0] for s in SEEDS])
def test_the_built_seed_passes_the_unpack_rehearsal(manifest, tmp_path):
    m = json.load(open(manifest))
    sandbox = tmp_path / "data"
    sandbox.mkdir()
    r = subprocess.run([os.path.join(INSTALLER, "bin", "eitri-seed-install"), "--archive", os.path.join(OUT, m["archive"]["name"]),
                        "--sha256", m["archive"]["sha256"], "--flavor", m["flavor"], "--data", str(sandbox), "--mode", "install", "--dry-run", "-q"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    assert r.returncode == 0, r.stdout
    assert os.listdir(sandbox) == []


def test_the_built_klipper_seed_installs_updates_and_rolls_back_through_the_wrapper(tmp_path):
    from eitri_update import seed as seedlib
    manifest = [s for s in SEEDS if s.endswith("-klipper.manifest.json")]
    if not manifest:
        pytest.skip("no klipper seed built")
    m = json.load(open(manifest[0]))
    arc, sha = os.path.join(OUT, m["archive"]["name"]), m["archive"]["sha256"]
    data = tmp_path / "data"
    data.mkdir()
    tool_path = os.path.join(INSTALLER, "bin", "eitri-seed-install")
    tool = seedlib.SeedTool(path=tool_path, data_root=str(data))
    r = subprocess.run([tool_path, "--archive", arc, "--sha256", sha, "--flavor", "klipper", "--data", str(data),
                        "--mode", "install", "-q"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    assert r.returncode == 0, r.stdout
    assert (data / "firmware/klipper/klippy/klippy.py").is_file() and (data / "venv/klippy/bin/python").is_symlink()
    assert seedlib.installed_flavor(str(data)) == "klipper" and seedlib.leftovers(str(data)) == []
    (data / "firmware/klipper/MARK").write_text("user state")          # something that only the old tree has
    tool.update(arc, sha, "klipper", keep_old=True)
    assert not (data / "firmware/klipper/MARK").exists() and "firmware/klipper" in seedlib.leftovers(str(data))
    tool.rollback()
    assert (data / "firmware/klipper/MARK").exists() and seedlib.leftovers(str(data)) == []
    tool.update(arc, sha, "klipper", keep_old=True)
    tool.finish()
    assert seedlib.leftovers(str(data)) == [] and seedlib.read_record(str(data))["SEED_MODE"] == "update"
