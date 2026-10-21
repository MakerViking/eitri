# SPDX-License-Identifier: Apache-2.0
"""Mutation tests for tools/check-repo.py: each case breaks one rule in a copy of the tree and expects that rule to fail.

The release-trust and licence checks are only worth having if they really fail when the thing they guard is broken.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def tree_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout.decode()
    return [rel for rel in filter(None, out.split("\0")) if (ROOT / rel).is_file() and not (ROOT / rel).is_symlink()]


@pytest.fixture(scope="module")
def files() -> list[str]:
    return tree_files()


@pytest.fixture
def copy(tmp_path, files):
    """A hard-linked copy of the working tree (mutations replace the file, they never write through the link)."""
    for rel in files:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(ROOT / rel, dest)
        except OSError:
            shutil.copy2(ROOT / rel, dest)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def edit(root: Path, rel: str, old: str | None, new: str) -> None:
    p = root / rel
    text = p.read_text()
    if old is not None:
        assert old in text, f"{old!r} not in {rel}"
        text = text.replace(old, new)
    else:
        text = new
    p.unlink()
    p.write_text(text)


def run_check(root: Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-I", str(root / "tools" / "check-repo.py")], capture_output=True, text=True, cwd=root)
    return r.returncode, r.stdout + r.stderr


def test_the_unmodified_tree_passes(copy):
    code, out = run_check(copy)
    assert code == 0, out


RELEASE_YML = ".github/workflows/release.yml"
OS_IMAGE_YML = ".github/workflows/os-image.yml"

MUTATIONS = [
    # --- deferred signing
    ("secret in a build workflow", OS_IMAGE_YML, "          EITRI_RELEASE: ${{ steps.mode.outputs.release }}\n",
     "          EITRI_RELEASE: ${{ steps.mode.outputs.release }}\n          LEAK: ${{ secrets.EITRI_RAUC_KEY }}\n", "os-image.yml: no secrets"),
    ("key file written by a build workflow", OS_IMAGE_YML, "      - name: Build\n",
     "      - name: Keys\n        run: printf '%s' \"$X\" > \"$RUNNER_TEMP/release.key\"\n\n      - name: Build\n", "writes no key material"),
    ("os-image publishes again", OS_IMAGE_YML, "  image:\n", "  publish:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: gh release upload x y\n\n  image:\n",
     "os-image: publishes nothing"),
    ("os-image names a signed artifact", OS_IMAGE_YML, "'-unsigned'", "''", "named as unsigned"),
    ("os-image loses deferred signing", OS_IMAGE_YML, "          EITRI_DEFERRED_SIGNING: ${{ steps.mode.outputs.release }}\n", "",
     "built for deferred signing"),
    ("sign job without the environment", RELEASE_YML, "    environment: release\n", "", "protected 'release' environment"),
    ("collect job gets an environment", RELEASE_YML, "    name: Collect the unsigned build outputs\n",
     "    name: Collect the unsigned build outputs\n    environment: release\n", "collect job has no environment"),
    ("secret outside the Sign step", RELEASE_YML, "          EXPECTED: ${{ needs.collect.outputs.inputs_sha256 }}\n",
     "          EXPECTED: ${{ needs.collect.outputs.inputs_sha256 }}\n          OOPS: ${{ secrets.EITRI_RAUC_KEY }}\n", "only the Sign step reads secrets"),
    ("secret in the publish job", RELEASE_YML, "      - name: Create or update the draft release\n        env:\n          GH_TOKEN: ${{ github.token }}\n        run: |\n          if gh release view",
     "      - name: Create or update the draft release\n        env:\n          GH_TOKEN: ${{ github.token }}\n          OOPS: ${{ secrets.EITRI_MINISIGN_KEY }}\n        run: |\n          if gh release view",
     "collect and publish read no secrets"),
    ("a fifth secret in the Sign step", RELEASE_YML, "          RAUC_CERT: ${{ secrets.EITRI_RAUC_CERT }}\n",
     "          RAUC_CERT: ${{ secrets.EITRI_RAUC_CERT }}\n          EXTRA: ${{ secrets.EITRI_OTHER }}\n", "exactly the four signing secrets"),
    ("release starts on push", RELEASE_YML, "on:\n  workflow_dispatch:\n", "on:\n  push:\n    tags: [\"v*\"]\n  workflow_dispatch:\n", "starts only by hand"),
    ("release starts from pull_request_target", RELEASE_YML, "on:\n  workflow_dispatch:\n", "on:\n  workflow_dispatch:\n  pull_request_target:\n", "no pull_request_target"),
    ("sign job may write", RELEASE_YML, "    environment: release\n    permissions:\n      contents: read\n",
     "    environment: release\n    permissions:\n      contents: write\n", "only the publish job may write"),
    ("checkout keeps the token", RELEASE_YML, "        with:\n          persist-credentials: false\n", "", "do not keep the token"),
    ("cache in the release workflow", RELEASE_YML, "      - name: Install tools\n        run: |\n          sudo apt-get update\n          sudo apt-get install -y --no-install-recommends zstd\n",
     "      - uses: actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830 # v4.3.0\n        with:\n          path: x\n          key: y\n\n      - name: Install tools\n        run: |\n          sudo apt-get update\n          sudo apt-get install -y --no-install-recommends zstd\n",
     "no caches"),
    ("unsigned set is not pinned", RELEASE_YML, "INPUTS.sha256", "INPUTS.txt", "pinned by digest"),
    ("artifacts from any run", RELEASE_YML, "--event push --commit \"$GITHUB_SHA\"", "--event push", "tag's own commit"),
    ("keys written without a trap", RELEASE_YML, "trap 'rm -rf \"$keys\"' EXIT", "true", "removes them on exit"),
    # --- numbers that move with a release
    ("clock floor drifts in release.lock", "release.lock", 'clock_floor = "2026-10-01T00:00:00Z"', 'clock_floor = "2026-11-01T00:00:00Z"', "clock_floor equals CLOCK_FLOOR"),
    ("clock floor drifts in the client", "installer/eitri-update/eitri_update/manifest.py", 'CLOCK_FLOOR = "2026-10-01T00:00:00Z"',
     'CLOCK_FLOOR = "2026-11-01T00:00:00Z"', "clock_floor equals CLOCK_FLOOR"),
    ("image floor above the sequence", "release.lock", "min_manifest_sequence = 1", "min_manifest_sequence = 7", "release.lock [release]"),
    # --- licences
    ("adapted config loses its AGPL tag", "klipper-layer/config/macros.cfg", "SPDX-License-Identifier: AGPL-3.0-or-later", "SPDX-License-Identifier: GPL-3.0-or-later",
     "macros.cfg"),
    ("config file without a tag", "klipper-layer/config/flavors/klipper.cfg", "# SPDX-License-Identifier: Apache-2.0\n", "", "flavors/klipper.cfg"),
    ("config file naming QIDI_PLUS4 but Apache", "klipper-layer/config/flavors/klipper.cfg", "# SPDX-License-Identifier: Apache-2.0\n",
     "# SPDX-License-Identifier: Apache-2.0\n# adapted from QIDITECH/QIDI_PLUS4\n", "must be AGPL-3.0-or-later"),
    ("extra under the wrong licence", "klipper-layer/extras/qd_plr.py", "SPDX-License-Identifier: GPL-3.0-or-later", "SPDX-License-Identifier: Apache-2.0",
     "extras are GPL-3.0"),
    ("unknown SPDX identifier", "tools/lint.py", "# SPDX-License-Identifier: Apache-2.0", "# SPDX-License-Identifier: Proprietary", "Proprietary"),
    ("copyleft tag where it is not allowed", "tools/lint.py", "# SPDX-License-Identifier: Apache-2.0", "# SPDX-License-Identifier: GPL-3.0-or-later",
     "outside the directories"),
    ("AGPL config licence text replaced", "klipper-layer/config/LICENSE", "GNU AFFERO GENERAL PUBLIC LICENSE", "SOMETHING ELSE", "AGPL text"),
    ("README forgets the AGPL", "README.md", "AGPL-3.0-or-later", "GPL", "states the AGPL licence"),
    ("lock stops treating QIDI_PLUS4 as AGPL", "release.lock", 'license = "AGPL-3.0-or-later"  #', 'license = "GPL-3.0-or-later"  #', "QIDI_PLUS4 as AGPL"),
    # --- decisions in the docs and the code they describe
    ("nginx listens on IPv6 again", "os/board/eitri-plus4/rootfs-overlay/etc/nginx/nginx.conf", "        listen 0.0.0.0:80 default_server;\n",
     "        listen 0.0.0.0:80 default_server;\n        listen [::]:80 default_server;\n", "no IPv6 listener"),
    ("INSTALL forgets the unlock action", "docs/INSTALL.md", "Unlock setup", "Reopen", "covers Unlock setup"),
    ("RELEASING forgets a secret", "docs/RELEASING.md", "EITRI_RAUC_CERT", "EITRI_X", "names the secret EITRI_RAUC_CERT"),
]


@pytest.mark.parametrize("label,rel,old,new,expect", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_check_repo_fails_when_a_rule_is_broken(copy, label, rel, old, new, expect):
    edit(copy, rel, old, new)
    code, out = run_check(copy)
    assert code == 1, f"{label}: check-repo still passes\n{out[-600:]}"
    assert expect in out, f"{label}: expected a failure mentioning {expect!r}\n{out[-800:]}"


def test_a_new_config_file_without_a_tag_fails(copy):
    (copy / "klipper-layer/config/extra.cfg").write_text("[gcode_macro X]\ngcode: M117 hi\n")
    code, out = run_check(copy)
    assert code == 1 and "klipper-layer/config/extra.cfg" in out
