# SPDX-License-Identifier: Apache-2.0
"""Runs INSIDE the seed's moonraker venv: /data/venv/moonraker/bin/python smoke_moonraker.py"""
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


check(sys.prefix == "/data/venv/moonraker", f"sys.prefix is {sys.prefix}")
for mod in ("tornado", "serial", "PIL", "streaming_form_data", "distro", "inotify_simple", "paho.mqtt.client",
            "zeroconf", "preprocess_cancellation", "jinja2", "dbus_fast", "apprise", "ldap3", "periphery",
            "importlib_metadata", "msgspec", "uvloop", "smart_open", "aiofiles", "pip", "yaml", "requests"):
    try:
        importlib.import_module(mod)
        check(True, f"import {mod}")
    except Exception as e:  # noqa: BLE001
        check(False, f"import {mod}: {e!r}")
try:
    import libnacl  # noqa: F401  (needs libsodium from the image)

    check(True, "import libnacl (libsodium present)")
except Exception as e:  # noqa: BLE001
    print(f"note libnacl: {e!r} (Moonraker loads it lazily; libsodium comes from the image)")

from streaming_form_data import StreamingFormDataParser  # noqa: E402
from streaming_form_data.targets import ValueTarget  # noqa: E402

t = ValueTarget()
p = StreamingFormDataParser(headers={"Content-Type": "multipart/form-data; boundary=X"})
p.register("a", t)
p.data_received(b'--X\r\nContent-Disposition: form-data; name="a"\r\n\r\nhello\r\n--X--\r\n')
check(t.value == b"hello", "streaming_form_data (cross-compiled here) parses a multipart body")

from PIL import Image  # noqa: E402

im = Image.new("RGB", (4, 4), (1, 2, 3))
check(im.resize((2, 2)).size == (2, 2), "Pillow works")

sys.path.insert(0, "/data/firmware/moonraker")
try:
    os.chdir("/data/firmware/moonraker")
except OSError:
    pass  # qemu-user does not translate chdir() paths; irrelevant to the checks
import moonraker  # noqa: E402,F401

check(True, "import moonraker (package at /data/firmware/moonraker)")
for mod in ("moonraker.server", "moonraker.confighelper", "moonraker.utils", "moonraker.components.file_manager.file_manager"):
    try:
        importlib.import_module(mod)
        check(True, f"import {mod}")
    except Exception as e:  # noqa: BLE001
        check(False, f"import {mod}: {e!r}")

for _m in ('tornado.web', 'moonraker.server', 'PIL.Image', 'zeroconf'):
    if _m in sys.modules:
        check(pyc_valid(_m), f"pre-compiled byte-code of {_m} matches its source")
print("FAILED" if failures else "PASSED")
sys.exit(1 if failures else 0)
