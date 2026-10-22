# SPDX-License-Identifier: Apache-2.0
import subprocess

import pytest

from conftest import MCU, pinned_tree, seed_manifest

SCRIPT = MCU / "build-mcu.sh"


def run(*args):
    return subprocess.run(["bash", str(SCRIPT), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)


def test_a_real_build_is_refused_while_the_toolhead_is_unresolved(tmp_path):
    res = run("--flavor", "both", "--version", "0.0.0.dev0", "--out", str(tmp_path / "out"), "--work", str(tmp_path / "w"),
              "--toolchain", str(tmp_path / "no-toolchain"))
    assert res.returncode == 3, res.stderr
    assert "unresolved: toolhead" in res.stderr and "Katapult install over SWD" in res.stderr
    assert not (tmp_path / "out").exists() and not (tmp_path / "w" / "src").exists()      # nothing fetched, nothing built


def test_usage_errors():
    assert run("--bogus").returncode == 2
    assert run("--flavor", "marlin", "--work", "/tmp/x").returncode == 2
    assert run("--check-only", "--flavor", "klipper").returncode == 2                      # --work is required


@pytest.mark.parametrize("flavor", ["klipper", "kalico"])
def test_check_only_matches_the_host_version_of_the_seed(flavor, tmp_path):
    tree, seed = pinned_tree(flavor), seed_manifest(flavor)
    if tree is None:
        pytest.skip("no pinned %s tree (set EITRI_SOURCE_%s)" % (flavor, flavor.upper()))
    args = ["--check-only", "--flavor", flavor, "--work", str(tmp_path / "w"), "--source", "%s=%s" % (flavor, tree)]
    if seed:
        (tmp_path / "seed.json").write_text(__import__("json").dumps(seed))
        args += ["--seed-manifest", str(tmp_path / "seed.json")]
    res = run(*args)
    assert res.returncode == 0, res.stderr
    assert "matches the host's" in res.stderr if seed else "not cross-checked" in res.stderr
    assert "mainboard: ok (stm32f401xc, application 0x8008000)" in res.stdout and "toolhead: ok (stm32f103xe, application 0x8002000)" in res.stdout


def test_check_only_refuses_a_describe_that_is_not_the_hosts(tmp_path):
    tree = pinned_tree("klipper")
    if tree is None:
        pytest.skip("no pinned klipper tree")
    res = run("--check-only", "--flavor", "klipper", "--work", str(tmp_path / "w"), "--source", "klipper=%s" % tree,
              "--describe", "klipper=v0.13.0-813-gdeadbeef0")
    assert res.returncode == 1 and "git describe here is" in res.stderr
