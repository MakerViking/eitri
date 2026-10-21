# SPDX-License-Identifier: Apache-2.0
"""The build tooling itself: syntax, small helpers, and the CI workflow."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import seedlib
import seedtool

SEED = Path(__file__).resolve().parent.parent
REPO = SEED.parent


@pytest.mark.parametrize("script", ["build-seed.sh", "verify-seed.sh", "tools/lock.sh"])
def test_bash_scripts_parse_and_are_executable(script):
    assert subprocess.run(["bash", "-n", str(SEED / script)]).returncode == 0
    assert os.access(SEED / script, os.X_OK)


def test_installer_parses_in_posix_sh():
    p = SEED / "bin/eitri-seed-install"
    assert subprocess.run(["sh", "-n", str(p)]).returncode == 0
    assert os.access(p, os.X_OK)
    text = p.read_text()
    assert text.startswith("#!/bin/sh\n")
    for bashism in ("[[", "<<<", "function ", "declare ", "local -"):
        assert bashism not in text.replace("function bad", ""), bashism


def test_python_tools_compile():
    for f in list((SEED / "tools").glob("*.py")) + list((SEED / "tools/guest").glob("*.py")):
        compile(f.read_text(), str(f), "exec")


def test_help_texts():
    for cmd in (["bash", str(SEED / "build-seed.sh"), "--help"], ["sh", str(SEED / "bin/eitri-seed-install"), "--help"],
                ["bash", str(SEED / "verify-seed.sh"), "--help"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.strip(), cmd


def test_build_script_rejects_bad_arguments():
    r = subprocess.run(["bash", str(SEED / "build-seed.sh"), "--flavor", "marlin"], capture_output=True, text=True)
    assert r.returncode != 0 and "--flavor" in r.stderr
    r = subprocess.run(["bash", str(SEED / "build-seed.sh"), "--version", "bad version"], capture_output=True, text=True)
    assert r.returncode != 0


def test_pins_command_prints_shell_assignments(repo):
    r = subprocess.run([sys.executable, "-I", str(SEED / "tools/seedtool.py"), "pins", "--repo", str(repo), "--flavor", "kalico"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    env = dict(line.split("=", 1) for line in r.stdout.splitlines())
    assert env["FW_BRANCH"] == "'main'" and env["FW_NAME"] == "'kalico'"
    assert re.fullmatch(r"'[0-9a-f]{40}'", env["MR_COMMIT"])
    assert env["PY_FULL_VERSION"].startswith("'3.14.")


def test_shebang_rewrite(tmp_path):
    f = tmp_path / "s"
    f.write_text("#!/home/build/venv/bin/python3.13\nprint(1)\n")
    assert seedtool.rewrite_shebang(f, "/data/venv/x/bin/python")
    assert f.read_text() == "#!/data/venv/x/bin/python\nprint(1)\n"
    b = tmp_path / "b"
    b.write_bytes(b"\x7fELF....")
    assert not seedtool.rewrite_shebang(b, "/x")


SAMPLE_KLIPPER = '''
GCC_CMD = "gcc"
COMPILE_ARGS = ("-Wall -g -O2 -shared -fPIC"
                " -flto -fwhole-program -fno-use-linker-plugin"
                " -o %s %s")
SSE_FLAGS = "-mfpmath=sse -msse2"
SOURCE_FILES = ['a.c', 'b.c']
DEST_LIB = "c_helper.so"
OTHER_FILES = ['a.h']
'''
SAMPLE_KALICO = '''
GCC_CMD = "gcc"
COMPILE_ARGS = (
    "-Wall -g -O2 -shared -fPIC"
    " -flto -fwhole-program -fno-use-linker-plugin"
    " -o %s %s"
)
NATIVE_FLAGS = "-march=native -mtune=native"
SOURCE_FILES = [
    "a.c",
]
DEST_LIB = "c_helper.so"
'''


@pytest.mark.parametrize("src", [SAMPLE_KLIPPER, SAMPLE_KALICO])
def test_chelper_constants_are_read_without_importing_it(tmp_path, src):
    f = tmp_path / "__init__.py"
    f.write_text("import cffi\nimport definitely_not_installed\n" + src)
    c = seedtool.read_chelper_constants(f)
    assert c["DEST_LIB"] == "c_helper.so" and "%s" in c["COMPILE_ARGS"] and c["SOURCE_FILES"]


def test_chelper_constants_fail_loudly_when_upstream_changes(tmp_path):
    f = tmp_path / "__init__.py"
    f.write_text("SOURCE_FILES = []\n")
    with pytest.raises(seedlib.SeedError):
        seedtool.read_chelper_constants(f)


def test_cross_sysconfigdata_points_at_the_sysroot(tmp_path):
    base = tmp_path / "_sysconfigdata__linux_aarch64-linux-gnu.py"
    base.write_text("build_time_vars = {'CC': 'gcc', 'INCLUDEPY': '/usr/include/python3.14', 'LIBDIR': '/usr/lib', 'X': 1, 'HOST': 'keep'}\n")
    out = tmp_path / "out"
    out.mkdir()
    name = seedtool.sysconfigdata_for_cross(base, out, "/opt/x/aarch64-gcc", "/sys/usr/include/python3.14")
    ns: dict = {}
    exec((out / f"{name}.py").read_text(), ns)
    v = ns["build_time_vars"]
    assert v["CC"] == "/opt/x/aarch64-gcc" and v["LDSHARED"] == "/opt/x/aarch64-gcc -shared"
    assert v["INCLUDEPY"] == "/sys/usr/include/python3.14"
    assert v["LIBDIR"] == "/sys/usr/lib" and v["X"] == 1 and v["HOST"] == "keep"


def test_prune_wheel_drops_c_sources_and_fixes_record(tmp_path):
    w = tmp_path / "x-1-cp314-cp314-linux_aarch64.whl"
    with zipfile.ZipFile(w, "w") as z:
        z.writestr("x/_p.c", "int x;")
        z.writestr("x/__init__.py", "")
        z.writestr("x-1.dist-info/RECORD", "x/_p.c,sha256=a,6\nx/__init__.py,sha256=b,0\nx-1.dist-info/RECORD,,\n")
    seedtool.prune_wheel(w, ["*.c"], 1_700_000_000)
    with zipfile.ZipFile(w) as z:
        assert sorted(z.namelist()) == ["x-1.dist-info/RECORD", "x/__init__.py"]
        assert "_p.c" not in z.read("x-1.dist-info/RECORD").decode()


def test_elf_machine_detection():
    assert seedtool.elf_machine(b"\x7fELF" + bytes(14) + b"\xb7\x00" + bytes(40)) == 183
    assert seedtool.elf_machine(b"\x7fELF" + bytes(14) + b"\x3e\x00" + bytes(40)) == 62
    assert seedtool.elf_machine(b"not elf") is None


# --- the CI workflow ---------------------------------------------------------------------------------------------

WF = REPO / ".github/workflows/data-seed.yml"


def test_workflow_exists_and_pins_every_action_by_sha():
    text = WF.read_text()
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.M)
    assert uses, "no actions used"
    for u in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u), f"action not pinned to a commit SHA: {u}"


def test_workflow_shape():
    text = WF.read_text()
    assert re.search(r"tags:\s*\[\s*\"v\*\"\s*\]", text), "must run on v* tags"
    assert "workflow_dispatch" in text
    assert re.search(r"^permissions:\s*\n\s+contents:\s*read", text, re.M), "default token must be read-only"
    for flavor in seedlib.FLAVORS:
        assert flavor in text
    assert "seed/build-seed.sh" in text and "seed/verify-seed.sh" in text
    assert "sha256sum" in text, "the workflow must publish checksums"
    # signing is not done here: deferred to the protected release environment
    assert "minisign" not in text.lower() or "environment" in text


def test_makefile_hooks():
    mk = (SEED / "Makefile").read_text()
    for target in ("seed:", "test:", "build:", "verify:", "lock:"):
        assert re.search(rf"^{re.escape(target)}", mk, re.M), target
