# SPDX-License-Identifier: Apache-2.0
import json
import os

import pytest

from fakes import make_image
from helpers import write_release
from eitri_mcuflash import firmware as fw
from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.proc import RunResult


def test_extract_embedded_dictionary_finds_real_stream_not_noise():
    blob = b"\x78\x9c" * 40 + b"\x78\x01garbage" + make_image("v1-2-gabcdef0", size=5000)
    doc = fw.extract_embedded_dictionary(blob)
    assert doc["version"] == "v1-2-gabcdef0"
    assert fw.extract_embedded_dictionary(b"\x78\x9c" * 100) is None


def test_load_artifact_ok(tmp_path):
    write_release(tmp_path)
    a = fw.load_artifact(str(tmp_path), "klipper", "toolhead", expect_mcu="stm32f103xe")
    assert a.version == "v0.13.0-813-ga08fc79fc" and a.mcu == "stm32f103xe" and a.app_address == 0x08002000
    assert a.size == 3000 and a.origin == "bundle"
    assert a.config["CLOCK_FREQ"] == 72000000 and a.config["RESERVE_PINS_serial"] == "PB7,PB6"
    m = fw.load_artifact(str(tmp_path), "klipper", "mainboard", expect_mcu="stm32f401xc")
    assert m.app_address == 0x08008000 and m.config["RESERVE_PINS_crystal"] == "PH0,PH1"


def test_load_artifact_rejects_bad_inputs(tmp_path):
    write_release(tmp_path, tamper="toolhead")
    with pytest.raises(FlashError, match="checksum"):
        fw.load_artifact(str(tmp_path), "klipper", "toolhead")
    with pytest.raises(FlashError, match="but the board reports"):
        fw.load_artifact(str(tmp_path), "klipper", "mainboard", expect_mcu="stm32f103xe")
    with pytest.raises(FlashError, match="no MCU firmware manifest"):
        fw.load_artifact(str(tmp_path), "kalico", "mainboard")
    with pytest.raises(FlashError, match="unknown firmware flavor"):
        fw.load_artifact(str(tmp_path), "marlin", "mainboard")


def test_manifest_path_escape_is_refused(tmp_path):
    write_release(tmp_path)
    mp = tmp_path / "klipper" / "manifest.json"
    doc = json.loads(mp.read_text())
    doc["boards"]["toolhead"]["file"] = "../../../etc/passwd"
    mp.write_text(json.dumps(doc))
    with pytest.raises(FlashError, match="escapes"):
        fw.load_artifact(str(tmp_path), "klipper", "toolhead")


def test_manifest_version_must_match_embedded_dictionary(tmp_path):
    write_release(tmp_path)
    mp = tmp_path / "klipper" / "manifest.json"
    doc = json.loads(mp.read_text())
    doc["boards"]["toolhead"]["version"] = "v9.9.9-0-g0000000"
    mp.write_text(json.dumps(doc))
    with pytest.raises(FlashError, match="does not match the version"):
        fw.load_artifact(str(tmp_path), "klipper", "toolhead")


def test_versions_match_ignores_dirty_suffix_and_host_tag():
    assert fw.versions_match("v0.13.0-813-ga08fc79fc", "v0.13.0-813-ga08fc79fc")
    assert fw.versions_match("v0.13.0-813-ga08fc79fc-dirty", "v0.13.0-813-ga08fc79fc-20261021_0100-buildhost")
    assert not fw.versions_match("v0.13.0-813-ga08fc79fc", "v0.13.0-812-gffffffff")
    assert not fw.versions_match("v0.13.0-813-ga08fc79fc", "v0.12.0-813-ga08fc79fc")
    assert fw.versions_match("weird", "weird") and not fw.versions_match("weird", "other")


def test_build_artifact_uses_out_of_tree_build(tmp_path):
    calls = []
    cfg = tmp_path / "toolhead.config"
    cfg.write_text("CONFIG_MACH_STM32=y\n")
    klipper = tmp_path / "klipper"
    klipper.mkdir()

    def runner(argv, **kw):
        calls.append(argv)
        if argv[-1].startswith("-j"):
            out = [a for a in argv if a.startswith("OUT=")][0][4:]
            with open(os.path.join(out, "klipper.bin"), "wb") as fh:
                fh.write(make_image("v0.13.0-900-gabcdef012", size=2048))
        return RunResult(0, "")

    a = fw.build_artifact("toolhead", "klipper", klipper_dir=str(klipper), config_file=str(cfg),
                          out_dir=str(tmp_path / "out" / "toolhead"), runner=runner, check_toolchain=False)
    assert a.origin == "built" and a.version == "v0.13.0-900-gabcdef012" and a.source_commit == "abcdef012"
    assert [c[-1] for c in calls] == ["olddefconfig", "-j4"]
    for c in calls:
        assert any(x.startswith("KCONFIG_CONFIG=") for x in c)          # command line, not env (Makefile assigns it)
        assert any(x.startswith("OUT=") and x.endswith("/") for x in c)  # trailing slash is required by Klipper
    assert not (klipper / "out").exists() and not (klipper / ".config").exists()  # tree untouched


def test_build_failure_reports_tail(tmp_path):
    cfg = tmp_path / "c"
    cfg.write_text("x")
    runner = lambda argv, **kw: RunResult(2, "line1\nboom: no such file")
    with pytest.raises(FlashError, match="boom"):
        fw.build_artifact("toolhead", "klipper", klipper_dir=str(tmp_path), config_file=str(cfg),
                          out_dir=str(tmp_path / "o"), runner=runner, check_toolchain=False)


def test_application_address_comes_from_the_embedded_defconfig():
    assert fw.dictionary_app_address({"kconfig": "CONFIG_MACH_STM32=y\nCONFIG_STM32_FLASH_START_8000=y\n"}) == 0x08008000
    assert fw.dictionary_app_address({"kconfig": "CONFIG_STM32_FLASH_START_2000=y\n"}) == 0x08002000
    assert fw.dictionary_app_address({"kconfig": "CONFIG_STM32_FLASH_START_0000=y\n"}) == 0x08000000
    assert fw.dictionary_app_address({"kconfig": "CONFIG_FLASH_APPLICATION_ADDRESS=0x8010000\n"}) == 0x08010000
    assert fw.dictionary_app_address({"kconfig": "CONFIG_MACH_STM32=y\n"}) is None
    assert fw.dictionary_app_address({}) is None and fw.dictionary_app_address(None) is None


def test_manifest_offset_must_agree_with_the_image_itself(tmp_path):
    write_release(tmp_path)
    mp = tmp_path / "klipper" / "manifest.json"
    doc = json.loads(mp.read_text())
    img = make_image("v0.13.0-813-ga08fc79fc", "stm32f401xc", spi=True, size=3000, kconfig="CONFIG_STM32_FLASH_START_C000=y\n")
    path = tmp_path / "klipper" / "mainboard" / "klipper.bin"
    path.write_bytes(img)
    import hashlib
    doc["boards"]["mainboard"]["sha256"] = hashlib.sha256(img).hexdigest()
    mp.write_text(json.dumps(doc))                     # manifest still says 0x08008000, the image says 0x0800c000
    with pytest.raises(FlashError, match="linked for"):
        fw.load_artifact(str(tmp_path), "klipper", "mainboard")


def test_kalico_firmware_names_itself_kalico_and_is_still_found():
    import json as _json, zlib
    from fakes import make_dictionary
    doc = make_dictionary("v2026.10.00-0-g3526de3b0")
    doc["app"] = "Kalico"
    blob = bytes(range(1, 250)) * 3 + zlib.compress(_json.dumps(doc).encode(), 9) + b"\xa5" * 100
    assert fw.extract_embedded_dictionary(blob)["version"] == "v2026.10.00-0-g3526de3b0"
    doc["app"] = "Marlin"
    assert fw.extract_embedded_dictionary(zlib.compress(_json.dumps(doc).encode(), 9)) is None
