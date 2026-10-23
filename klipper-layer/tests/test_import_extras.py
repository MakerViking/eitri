"""Every extra must import against both Klipper and Kalico (python -I)."""
import subprocess

import pytest

from conftest import PYTHON, TESTS, extra_files


@pytest.mark.parametrize("extra", extra_files(), ids=lambda p: p.name)
def test_extra_imports(flavor, source_tree, extra):
    res = subprocess.run(
        [PYTHON, "-I", "-B", str(TESTS / "import_check.py"), flavor,
         str(source_tree), str(extra)],
        capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("OK "), res.stdout
    is_stub = extra.name in ("qd_reverse_home.py",)
    assert ("stub=%s" % is_stub) in res.stdout
