# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os

from fakes import make_image
from test_stockcfg_roles import STOCK_CFG  # noqa: F401  (re-exported for runner tests)


def write_release(root, flavor="klipper", version="v0.13.0-813-ga08fc79fc", boards=("mainboard", "toolhead"),
                  mcu="stm32f401xc", app_address=0x08008000, spi_for=("mainboard",), tamper=None):
    """Create <root>/<flavor>/manifest.json + images like the CI bundle does."""
    fdir = os.path.join(str(root), flavor)
    entries = {}
    images = {}
    for name in boards:
        os.makedirs(os.path.join(fdir, name), exist_ok=True)
        image = make_image(version, mcu, spi=name in spi_for, size=3000)
        images[name] = image
        path = os.path.join(fdir, name, "klipper.bin")
        with open(path, "wb") as fh:
            fh.write(image)
        digest = hashlib.sha256(image).hexdigest()
        if tamper == name:
            digest = "0" * 64
        entries[name] = {"file": "%s/klipper.bin" % name, "sha256": digest, "mcu": mcu, "version": version,
                         "app_address": "0x%08x" % app_address}
    with open(os.path.join(fdir, "manifest.json"), "w") as fh:
        json.dump({"schema": 1, "flavor": flavor, "source": {"commit": "a08fc79fcfa342e5b95ac02a9a8b271f453523e9"},
                   "boards": entries}, fh)
    return images
