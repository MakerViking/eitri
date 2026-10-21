# SPDX-License-Identifier: Apache-2.0
"""Re-export of the mcuflash test helpers (they live in another tests directory)."""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(os.path.dirname(path))
    return mod


_fakes = _load("mcu_fakes", os.path.join(_HERE, "..", "mcuflash", "tests", "fakes.py"))


def write_release(root, **kw):
    import hashlib
    import json
    flavor = kw.get("flavor", "klipper")
    version = kw.get("version", "v0.13.0-813-ga08fc79fc")
    fdir = os.path.join(str(root), flavor)
    entries = {}
    for name in ("mainboard", "toolhead"):
        os.makedirs(os.path.join(fdir, name), exist_ok=True)
        image = _fakes.make_image(version, "stm32f401xc", spi=(name == "mainboard"), size=3000)
        with open(os.path.join(fdir, name, "klipper.bin"), "wb") as fh:
            fh.write(image)
        entries[name] = {"file": "%s/klipper.bin" % name, "sha256": hashlib.sha256(image).hexdigest(), "mcu": "stm32f401xc",
                         "version": version, "app_address": "0x08008000"}
    with open(os.path.join(fdir, "manifest.json"), "w") as fh:
        json.dump({"schema": 1, "flavor": flavor, "boards": entries}, fh)
