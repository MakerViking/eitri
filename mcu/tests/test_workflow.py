# SPDX-License-Identifier: Apache-2.0
"""Shape of .github/workflows/mcu-firmware.yml (text checks: no YAML library in CI's bare Python)."""
import re

from conftest import REPO

WF = (REPO / ".github" / "workflows" / "mcu-firmware.yml").read_text()
SEED_WF = (REPO / ".github" / "workflows" / "data-seed.yml").read_text()


def test_every_action_is_pinned_to_a_full_commit_sha_like_the_seed_workflow():
    uses = re.findall(r"uses:\s*(\S+)\s*#\s*(\S+)", WF)
    assert uses and len(uses) == len(re.findall(r"^\s*-?\s*uses:", WF, re.M))
    for ref, _ver in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
        assert ref in SEED_WF, "%s differs from the SHA data-seed.yml pins" % ref


def test_it_builds_both_flavors_and_attaches_to_a_draft_release_like_data_seed():
    assert "'[\"klipper\", \"kalico\"]'" in WF and "workflow_call:" in WF and 'tags: ["v*"]' in WF
    assert "gh release create" in WF and "--draft" in WF and "isDraft" in WF
    assert re.search(r"^permissions:\n  contents: read", WF, re.M)
    assert WF.count("contents: write") == 1                                    # only the publish job


def test_the_build_is_behind_the_parameter_gate_and_a_tag_cannot_skip_it():
    assert re.search(r"build:\n(?:.*\n)*?    needs: test\n    if: \$\{\{ needs.test.outputs.go == 'true' \}\}", WF)
    assert "mcutool.py gate" in WF and "refusing to cut a release" in WF
    assert "--check-only" in WF                                                # configs are checked even while gated
    assert "fetch-toolchain.sh" in WF and "build-mcu.sh" in WF


def test_the_build_job_runs_the_artifact_tests_and_a_reproducibility_check():
    assert "mcu/tests/test_artifacts.py" in WF and "EITRI_MCU_OUT" in WF
    assert "not reproducible" in WF
