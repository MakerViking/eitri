# SPDX-License-Identifier: Apache-2.0
"""Runs INSIDE the seed's klippy venv (aarch64, under qemu-user or on the printer):

    /data/venv/klippy/bin/python smoke_klippy.py

Proves that the venv is relocatable and complete, that klippy's modules import, that every Eitri extra
imports through the symlinks in klippy/extras, and that the prebuilt c_helper.so is accepted (klippy would
otherwise call gcc, which the printer does not have).
"""
import importlib
import os
import sys

KLIPPY = "/data/firmware/klipper/klippy"
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


check(sys.prefix == "/data/venv/klippy", f"sys.prefix is {sys.prefix}")
check(sys.version_info[:2] == (3, 14), f"python {sys.version.split()[0]}")
check(sys.base_prefix != sys.prefix, f"the venv runs the system interpreter (base prefix {sys.base_prefix})")

for mod in ("cffi", "greenlet", "jinja2", "markupsafe", "serial", "can", "typing_extensions", "packaging", "wrapt", "pip"):
    try:
        importlib.import_module(mod)
        check(True, f"import {mod} from {os.path.dirname(sys.modules[mod].__file__)[:60]}")
    except Exception as e:  # noqa: BLE001
        check(False, f"import {mod}: {e!r}")
flavor = open("/data/eitri/flavor").read().strip()
if flavor == "kalico":
    import numpy

    check(float(numpy.array([1.5, 2.5]).sum()) == 4.0, f"numpy {numpy.__version__} computes")
else:
    try:
        import msgspec

        check(msgspec.json.decode(b'{"a": 1}') == {"a": 1}, f"msgspec {msgspec.__version__} decodes")
    except ImportError as e:
        check(False, f"msgspec: {e!r}")

ROOT = "/data/firmware/klipper"
if flavor == "kalico":
    # Kalico is a package: <root>/klippy/{printer,chelper,extras}; extras are klippy.extras.<name>.
    sys.path.insert(0, ROOT)
    CHELPER, EXTRAS_PKG, MAIN = "klippy.chelper", "klippy.extras", "klippy.printer"
else:
    # Klipper runs klippy.py as a script with klippy/ on the path; extras are extras.<name>.
    sys.path.insert(0, KLIPPY)
    CHELPER, EXTRAS_PKG, MAIN = "chelper", "extras", "klippy"
try:
    os.chdir(ROOT)
except OSError:
    pass  # qemu-user does not translate chdir() paths; irrelevant to the checks
chelper = importlib.import_module(CHELPER)

check(os.path.dirname(os.path.realpath(chelper.__file__)).startswith("/data/firmware/klipper/klippy/chelper"), "chelper comes from the seed")


def no_compiler(cmd):
    raise RuntimeError("chelper wants to run the compiler: " + cmd)


chelper.do_build_code = no_compiler
chelper.check_gcc_option = lambda option: False
ffi, lib = chelper.get_ffi()
check(True, "chelper.get_ffi() loaded the prebuilt c_helper.so without compiling")
sk = lib.cartesian_stepper_alloc(b"x")
check(sk != ffi.NULL, "c_helper.so is functional (cartesian_stepper_alloc)")

importlib.import_module(MAIN)
check(True, f"import {MAIN} (the host program and its direct imports)")

extras_dir = os.path.join(KLIPPY, "extras")
qd = sorted(n[:-3] for n in os.listdir(extras_dir) if n.startswith("qd_") and n.endswith(".py"))
check(len(qd) >= 6, f"{len(qd)} qd_* extras linked into klippy/extras: {', '.join(qd)}")
for name in qd + ["gcode_shell_command"]:
    path = os.path.join(extras_dir, name + ".py")
    # Absolute /data/... target on the printer; the emulation harness rewrites it to an equivalent relative link.
    target = os.path.normpath(os.path.join(os.path.dirname(path), os.readlink(path))) if os.path.islink(path) else ""
    if name == "gcode_shell_command" and not os.path.islink(path):
        check(True, "gcode_shell_command is tracked upstream in this flavor (not shadowed)")
        continue
    check(target == f"/data/eitri/extras/{name}.py" and os.path.exists(path),
          f"extras/{name}.py is a working symlink into /data/eitri/extras")

skipped = []
for n in sorted(os.listdir(extras_dir)):
    if not n.endswith(".py") or n.startswith("_"):
        continue
    name = n[:-3]
    try:
        importlib.import_module(EXTRAS_PKG + "." + name)
    except Exception as e:  # noqa: BLE001
        if name.startswith("qd_") or name == "gcode_shell_command":
            check(False, f"import {EXTRAS_PKG}.{name}: {e!r}")
        else:
            skipped.append(f"{name}: {e!r}")
if not any(f.startswith("import " + EXTRAS_PKG) for f in failures):
    check(True, "every qd_* extra and gcode_shell_command imports")
print(f"imported {sum(1 for m in sys.modules if m.startswith(EXTRAS_PKG + '.'))} klippy extras modules, {len(skipped)} not importable standalone")
for s in skipped:
    print("note", s)

for _m in (CHELPER, 'jinja2.environment', 'greenlet', 'serial.serialutil'):
    if _m in sys.modules:
        check(pyc_valid(_m), f"pre-compiled byte-code of {_m} matches its source")
print("FAILED" if failures else "PASSED")
sys.exit(1 if failures else 0)
