# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os

from fakes import make_image
from test_stockcfg_roles import STOCK_CFG  # noqa: F401  (re-exported for runner tests)


# What each board needs: its chip and the application offset its bootloader starts it at.
BOARD_FIX = {"mainboard": ("stm32f401xc", 0x08008000), "toolhead": ("stm32f103xe", 0x08002000)}


def settle(registry, toolhead_offset=0x08002000):
    """Pretend the toolhead's open question has been answered (a future registry would say so)."""
    t = registry.boards["toolhead"]
    t.params_status, t.app_offset = "resolved", toolhead_offset
    return registry


def write_release(root, flavor="klipper", version="v0.13.0-813-ga08fc79fc", boards=("mainboard", "toolhead"),
                  mcu=None, app_address=None, spi_for=("mainboard",), tamper=None, size=3000, extra_config=None):
    """Create <root>/<flavor>/manifest.json + images like the CI bundle does."""
    fdir = os.path.join(str(root), flavor)
    entries = {}
    images = {}
    for name in boards:
        os.makedirs(os.path.join(fdir, name), exist_ok=True)
        b_mcu, b_app = BOARD_FIX[name]
        if isinstance(app_address, dict):
            app_address_for = app_address.get(name)
        else:
            app_address_for = app_address
        b_mcu, b_app = mcu or b_mcu, app_address_for if app_address_for is not None else b_app
        image = make_image(version, b_mcu, spi=name in spi_for, size=size, extra_config=(extra_config or {}).get(name), app_address=b_app)
        images[name] = image
        path = os.path.join(fdir, name, "klipper.bin")
        with open(path, "wb") as fh:
            fh.write(image)
        digest = hashlib.sha256(image).hexdigest()
        if tamper == name:
            digest = "0" * 64
        entries[name] = {"file": "%s/klipper.bin" % name, "sha256": digest, "mcu": b_mcu, "version": version,
                         "app_address": "0x%08x" % b_app}
    with open(os.path.join(fdir, "manifest.json"), "w") as fh:
        json.dump({"schema": 1, "flavor": flavor, "source": {"commit": "a08fc79fcfa342e5b95ac02a9a8b271f453523e9"},
                   "boards": entries}, fh)
    return images
