# SPDX-License-Identifier: Apache-2.0
"""params.json is the single statement of what is settled; every other place that encodes it must agree."""
import json

import pytest

from conftest import LIVE, PARAMS, REPO, mcutool

BOARDS = ("mainboard", "toolhead")


def registry_doc():
    return json.loads((REPO / "installer" / "mcuflash" / "eitri_mcuflash" / "boards.json").read_text())


@pytest.mark.parametrize("board", BOARDS)
def test_params_match_the_live_printer(board):
    bp, live = PARAMS["boards"][board], LIVE[board]
    assert bp["mcu"] == live["MCU"] and bp["clock_freq"] == live["CLOCK_FREQ"] and bp["baud"] == live["SERIAL_BAUD"]
    assert bp["flash_base"] == "0x08000000"


def test_live_constants_are_the_ones_from_the_klippy_log():
    assert LIVE["mainboard"] == {**LIVE["mainboard"], "MCU": "stm32f401xc", "CLOCK_FREQ": 84000000, "SERIAL_BAUD": 500000,
                                 "RESERVE_PINS_serial": "PA10,PA9", "RESERVE_PINS_crystal": "PH0,PH1", "port": "/dev/ttyS0"}
    assert LIVE["toolhead"] == {**LIVE["toolhead"], "MCU": "stm32f103xe", "CLOCK_FREQ": 72000000, "SERIAL_BAUD": 500000,
                                "RESERVE_PINS_serial": "PB7,PB6", "port": "/dev/ttyS2"}
    assert "RESERVE_PINS_crystal" not in LIVE["toolhead"]


def test_the_mainboard_is_settled_and_the_toolhead_is_not():
    main, tool = PARAMS["boards"]["mainboard"], PARAMS["boards"]["toolhead"]
    assert main["status"] == "resolved" and mcutool.hexint(main["flash_offset"]) == 0x08008000
    assert main["kconfig_flash_start"] == "STM32_FLASH_START_8000"
    assert tool["status"] == "unresolved" and tool["flash_offset"] is None and tool["open_questions"]
    assert mcutool.hexint(tool["flash_offset_if_katapult"]) == 0x08002000


def test_go_flag_agrees_with_the_board_states():
    problems = mcutool.gate(PARAMS)
    assert PARAMS["go"] is (not problems)
    assert any("toolhead" in p and "Katapult install over SWD" in p for p in problems)
    assert mcutool.gate(PARAMS, ["mainboard"]) == []


def test_offsets_are_inside_the_chip_and_a_bootloader_fits_below_them():
    for board, bp in PARAMS["boards"].items():
        off = mcutool.effective_offset(bp)
        assert 0x08000000 < off < 0x08000000 + bp["chip_flash_kib"] * 1024, board


@pytest.mark.parametrize("board", BOARDS)
def test_installer_registry_agrees_with_params(board):
    reg = registry_doc()["boards"][board]
    bp = PARAMS["boards"][board]
    assert reg["parameters"]["status"] == bp["status"] and reg["parameters"]["confidence"] == bp["confidence"]
    assert reg["mcu_candidates"] == [bp["mcu"][:9] + "*"]                     # stm32f401* / stm32f103*
    assert reg["app_baud"] == bp["baud"] == 500000
    assert reg["serial_hint"] == LIVE[board]["port"]
    expect = {k: v for k, v in LIVE[board].items() if k not in ("klipper_name", "port")}
    assert reg["expect_config"] == expect
    if bp["status"] == "resolved":
        assert mcutool.hexint(reg["parameters"]["app_offset"]) == mcutool.hexint(bp["flash_offset"])
    else:
        assert reg["parameters"]["app_offset"] is None
        assert mcutool.hexint(reg["parameters"]["app_offset_if_katapult"]) == mcutool.hexint(bp["flash_offset_if_katapult"])
        assert reg["parameters"]["open_questions"]


def test_registry_never_offers_katapult_on_the_mainboard_and_knows_both_chips():
    doc = registry_doc()
    assert doc["boards"]["mainboard"]["allow_katapult"] is False and doc["boards"]["mainboard"]["katapult_bauds"] == []
    assert doc["boards"]["toolhead"]["katapult_bauds"] == [250000, 500000]
    for board, bp in PARAMS["boards"].items():
        assert doc["chips"][bp["mcu"]]["flash_kib"] == bp["chip_flash_kib"]
