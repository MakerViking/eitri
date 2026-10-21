# SPDX-License-Identifier: Apache-2.0
"""Runs INSIDE the seed's Klarity venv: /data/eitri/klarity/venv/bin/python smoke_klarity.py"""
import importlib
import os
import sys

failures = []


def pyc_valid(modname):
    """The seed ships byte-code whose recorded source mtime/size must match the extracted source files."""
    import importlib.util
    import struct

    mod = sys.modules[modname]
    src = mod.__file__
    pyc = importlib.util.cache_from_source(src)
    try:
        with open(pyc, "rb") as f:
            magic, flags, mtime, size = struct.unpack("<4sIII", f.read(16))
    except OSError:
        return False
    st = os.stat(src)
    return magic == importlib.util.MAGIC_NUMBER and flags == 0 and mtime == (int(st.st_mtime) & 0xFFFFFFFF) and size == (st.st_size & 0xFFFFFFFF)


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


check(sys.prefix == "/data/eitri/klarity/venv", f"sys.prefix is {sys.prefix}")
for mod in ("serial", "serial_asyncio_fast", "websockets", "yaml", "PIL", "klarity", "klarity.daemon", "klarity.screen",
            "klarity.moonraker", "klarity.config", "klarity.__main__"):
    try:
        importlib.import_module(mod)
        check(True, f"import {mod}")
    except Exception as e:  # noqa: BLE001
        check(False, f"import {mod}: {e!r}")
script = "/data/eitri/klarity/venv/bin/klarity"
check(os.stat(script).st_mode & 0o111 == 0o111, f"{script} exists and is executable")
check(open(script).readline().strip() == "#!/data/eitri/klarity/venv/bin/python", "its shebang is the venv python on /data")
for _m in ('klarity.daemon', 'yaml', 'websockets.client'):
    if _m in sys.modules:
        check(pyc_valid(_m), f"pre-compiled byte-code of {_m} matches its source")
print("FAILED" if failures else "PASSED")
sys.exit(1 if failures else 0)
