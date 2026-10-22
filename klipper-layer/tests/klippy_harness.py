"""Run the real klippy in batch mode (``-i file``) against a rendered Eitri
config with the overlay installed into a throw-away clone.

Needs a firmware data dictionary per flavor (tests/build-dicts.sh builds them
with arm-none-eabi-gcc) and a Python with cffi, greenlet, jinja2 and pyserial.
G-code errors terminate klippy with a non-zero status; messages sent with
respond_info end up in klippy's log, which is what the tests assert on.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import pytest

from conftest import (DICT_DIR, EXTRAS, PYTHON, SCRIPTS, TESTS)

# Waiting for real temperatures is impossible in batch mode (sensors read 0)
STUBS = """
[gcode_macro TEMPERATURE_WAIT]
rename_existing: BASE_TEMPERATURE_WAIT
gcode:
    { action_respond_info("stub TEMPERATURE_WAIT " ~ rawparams) }
"""

ERROR_PATTERNS = [
    r"Error evaluating", r"Unknown command", r"Unhandled exception",
    r"Internal error", r"Config error", r"is not a valid config section",
    r"Option '[^']*' is not valid", r"Traceback \(most recent call last\):\n"
    r"(?:(?!/proc/device-tree).)*",
]


def klippy_deps_available():
    res = subprocess.run([PYTHON, "-c",
                          "import cffi, greenlet, jinja2, serial"],
                         capture_output=True)
    return res.returncode == 0


def dictionary(flavor):
    path = DICT_DIR / ("%s-stm32f446.dict" % flavor)
    if not path.is_file():
        pytest.skip("no firmware dictionary %s (run tests/build-dicts.sh)"
                    % path)
    return path


class Result:
    def __init__(self, returncode, log, stdout):
        self.returncode = returncode
        self.log = log
        self.stdout = stdout

    def errors(self):
        # Benign: klippy tries to read the board model from /proc/device-tree
        log = re.sub(r"Exception on read /proc/device-tree/model:.*?\n\n", "",
                     self.log, flags=re.S)
        return [m.group(0).strip().splitlines()[0]
                for p in ERROR_PATTERNS for m in re.finditer(p, log)]


class Env:
    """A configured, overlay-installed clone ready to run G-code."""

    def __init__(self, clone, flavor, tmp, chamber=False, dicts=None, **render_vars):
        # dicts: (toolhead, mainboard) dictionary files; default one generic dictionary for both.
        self.clone = clone
        self.flavor = flavor
        self.tmp = tmp
        self.pd = tmp / "printer_data"
        self.gcodes = self.pd / "gcodes"
        self.gcodes.mkdir(parents=True)
        self.dict = dictionary(flavor)
        self.dicts = tuple(dicts) if dicts else (self.dict, self.dict)
        _install_chelper(flavor, clone)
        res = subprocess.run(
            ["bash", str(SCRIPTS / "overlay-install.sh"), "--klipper-dir",
             str(clone), "--printer-data", str(self.pd), "-q"],
            capture_output=True, text=True,
            env={**os.environ, "EITRI_PYTHON": PYTHON})
        assert res.returncode == 0, res.stdout + res.stderr
        self.cfg_path = self.pd / "config" / "printer.cfg"
        values = {"printer_data": str(self.pd),
                  "mcu_toolhead_serial": str(tmp / "ttyT"),
                  "mcu_mainboard_serial": str(tmp / "ttyM"),
                  "probe_z_offset": "1.5",
                  "chamber_enabled": "true" if chamber else "false"}
        values.update({k: str(v) for k, v in render_vars.items()})
        cmd = [sys.executable, "-B", str(SCRIPTS / "render-config.py"),
               "--output", str(self.cfg_path)]
        for k, v in values.items():
            cmd += ["--set", "%s=%s" % (k, v)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        (self.pd / "config" / "stubs.cfg").write_text(STUBS)
        self.append("[include stubs.cfg]\n")

    def append(self, text):
        with open(self.cfg_path, "a") as f:
            f.write("\n" + text)

    def link_extra(self, link_name, source_name):
        """Expose an extra under another module name (test-only, in the clone)."""
        (self.clone / "klippy" / "extras" / link_name).symlink_to(
            EXTRAS / source_name)

    def add_test_extra(self, name):
        """Install tests/klippy_extras/<name>.py in the clone and load it."""
        (self.clone / "klippy" / "extras" / (name + ".py")).symlink_to(
            TESTS / "klippy_extras" / (name + ".py"))
        self.append("[%s]\n" % name)

    def run(self, gcode, timeout=240, wait_for=None):
        """Feed `gcode` to klippy and return the Result.

        By default the commands come from a file, so klippy exits when they
        are done. Kalico (unlike Klipper) then exits without waiting for a
        virtual_sdcard print that is still running; pass `wait_for`, a regex
        for a log line, to keep input open (a FIFO) until that line appears or
        klippy exits on its own.
        """
        log = self.tmp / "klippy.log"
        if log.exists():
            log.unlink()
        gfile = self.tmp / "in.gcode"
        cmd = [PYTHON, "-B", "klippy/klippy.py", str(self.cfg_path),
               "-i", str(gfile), "-o", str(self.tmp / "mcu.out"),
               "-d", str(self.dicts[0]), "-d", "U_1=%s" % self.dicts[1],
               "-l", str(log)]
        envvars = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        if gfile.exists():
            gfile.unlink()          # may be the FIFO of an earlier run
        if wait_for is None:
            gfile.write_text(gcode.strip() + "\n")
            res = subprocess.run(cmd, cwd=self.clone, capture_output=True,
                                 text=True, timeout=timeout, env=envvars)
            text = log.read_text() if log.exists() else ""
            return Result(res.returncode, text, res.stdout + res.stderr)
        os.mkfifo(gfile)
        # Holding the write end ourselves means klippy sees no EOF until we close
        fd = os.open(gfile, os.O_RDWR)
        proc = subprocess.Popen(cmd, cwd=self.clone, env=envvars,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        try:
            os.write(fd, (gcode.strip() + "\n").encode())
            deadline = time.monotonic() + timeout
            pattern = re.compile(wait_for)
            while proc.poll() is None and time.monotonic() < deadline:
                if log.exists() and pattern.search(log.read_text()):
                    break
                time.sleep(0.2)
        finally:
            os.close(fd)
        try:
            out, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        text = log.read_text() if log.exists() else ""
        return Result(proc.returncode, text, out)


_CHELPER = {}


def _install_chelper(flavor, clone):
    """Compile klippy's C helper once per flavor and reuse the binary."""
    rel = pathlib.Path("klippy") / "chelper" / "c_helper.so"
    if flavor not in _CHELPER:
        snippet = ("import sys; sys.path.insert(0, %r); "
                   "import %s as c; c.get_ffi()" % (
                       str(clone / ("klippy" if flavor == "klipper" else "")),
                       "chelper" if flavor == "klipper" else "klippy.chelper"))
        res = subprocess.run([PYTHON, "-B", "-c", snippet], cwd=clone,
                             capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        cache = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / (
            "eitri-chelper-%s-%d.so" % (flavor, os.getpid()))
        shutil.copy2(clone / rel, cache)
        _CHELPER[flavor] = cache
    else:
        shutil.copy2(_CHELPER[flavor], clone / rel)


def pytest_cleanup():
    for path in _CHELPER.values():
        path.unlink(missing_ok=True)
