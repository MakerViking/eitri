# SPDX-License-Identifier: Apache-2.0
"""eitri-update runs as root through sudo, and sudo keeps the caller's working directory. Its eitri_mcuflash child
must never import a package planted there."""
import os
import subprocess
import sys

import eitri_mcuflash

from eitri_update import client


def plant(tmp_path):
    fake = tmp_path / "eitri_mcuflash"
    fake.mkdir()
    marker = tmp_path / "PWNED"
    (fake / "__init__.py").write_text("")
    (fake / "__main__.py").write_text("open(%r, 'w').write('x')\n" % str(marker))
    return marker


def test_plain_dash_m_would_import_from_the_cwd(tmp_path):
    """Control: the old invocation runs the planted package, so the test below can catch a regression."""
    marker = plant(tmp_path)
    subprocess.run([sys.executable, "-m", "eitri_mcuflash", "--help"], cwd=str(tmp_path), capture_output=True)
    assert marker.exists()


def test_mcuflash_child_ignores_a_package_in_the_cwd(tmp_path):
    marker = plant(tmp_path)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(eitri_mcuflash.__file__)))
    res = subprocess.run([sys.executable, "-I", "-c", client._RUN_MODULE, pkg_parent, "eitri_mcuflash", "--help"],
                         cwd=str(tmp_path), capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(tmp_path)))
    assert not marker.exists()
    assert res.returncode == 0, res.stderr
    assert "usage" in res.stdout.lower()
