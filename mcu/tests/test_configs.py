# SPDX-License-Identifier: Apache-2.0
import shutil

import pytest

from conftest import MCU, PARAMS, mcutool, pinned_tree

CONFIGS = {"mainboard": MCU / "configs" / "mainboard-stm32f401.config", "toolhead": MCU / "configs" / "toolhead-stm32f103.config"}


def settings(path):
    return mcutool.parse_defconfig(path.read_text())


def test_configs_are_minimal_and_name_the_right_chip_and_offset():
    main, tool = settings(CONFIGS["mainboard"]), settings(CONFIGS["toolhead"])
    assert main["CONFIG_MACH_STM32F401"] == "y" and main["CONFIG_STM32_FLASH_START_8000"] == "y"
    assert tool["CONFIG_MACH_STM32F103"] == "y" and tool["CONFIG_STM32_FLASH_START_2000"] == "y"
    for cfg in (main, tool):
        assert cfg["CONFIG_STM32_CLOCK_REF_8M"] == "y" and cfg["CONFIG_SERIAL_BAUD"] == "500000" and cfg["CONFIG_LOW_LEVEL_OPTIONS"] == "y"
        assert len(cfg) <= 8                                              # an input to olddefconfig, not a dump
    assert main["CONFIG_STM32_SERIAL_USART1"] == "y" and tool["CONFIG_STM32_SERIAL_USART1_ALT_PB7_PB6"] == "y"
    assert PARAMS["boards"]["mainboard"]["kconfig_flash_start"] == "STM32_FLASH_START_8000"


def test_the_toolhead_config_says_it_is_unresolved_and_the_mainboard_one_does_not():
    assert "UNRESOLVED" in CONFIGS["toolhead"].read_text() and "MUST NOT BE BUILT" in CONFIGS["toolhead"].read_text()
    assert "RESOLVED" in CONFIGS["mainboard"].read_text() and "UNRESOLVED" not in CONFIGS["mainboard"].read_text()


def test_params_name_the_config_files():
    for board, path in CONFIGS.items():
        assert (MCU / PARAMS["boards"][board]["config"]) == path and path.is_file()


@pytest.mark.parametrize("flavor", ["klipper", "kalico"])
def test_configs_expand_to_params_in_the_pinned_tree(flavor, tmp_path):
    tree = pinned_tree(flavor)
    if tree is None or shutil.which("make") is None:
        pytest.skip("no %s tree (set EITRI_SOURCE_%s or build the seed)" % (flavor, flavor.upper()))
    before = mcutool.subprocess.run(["git", "-C", str(tree), "status", "--porcelain"], stdout=mcutool.subprocess.PIPE, universal_newlines=True).stdout
    for board, bp in PARAMS["boards"].items():
        cfg = mcutool.expand_config(CONFIGS[board], tree, tmp_path / board)
        assert mcutool.check_expanded(board, bp, cfg) == [], board
    after = mcutool.subprocess.run(["git", "-C", str(tree), "status", "--porcelain"], stdout=mcutool.subprocess.PIPE, universal_newlines=True).stdout
    assert after == before                                               # out-of-tree: the clone is never written to


def test_check_expanded_catches_a_wrong_offset_and_a_wrong_clock():
    bp = PARAMS["boards"]["mainboard"]
    good = {"CONFIG_MCU": "stm32f401xc", "CONFIG_CLOCK_FREQ": "84000000", "CONFIG_SERIAL_BAUD": "500000", "CONFIG_STM32_CLOCK_REF_8M": "y",
            "CONFIG_CLOCK_REF_FREQ": "8000000", "CONFIG_STM32_SERIAL_USART1": "y", "CONFIG_RAM_SIZE": "0x10000",
            "CONFIG_FLASH_APPLICATION_ADDRESS": "0x8008000", "CONFIG_FLASH_BOOT_ADDRESS": "0x8000000"}
    assert mcutool.check_expanded("mainboard", bp, good) == []
    bad = dict(good, CONFIG_FLASH_APPLICATION_ADDRESS="0x800c000", CONFIG_CLOCK_REF_FREQ="12000000")
    msgs = mcutool.check_expanded("mainboard", bp, bad)
    assert len(msgs) == 2 and any("FLASH_APPLICATION_ADDRESS" in m for m in msgs) and any("CLOCK_REF_FREQ" in m for m in msgs)
