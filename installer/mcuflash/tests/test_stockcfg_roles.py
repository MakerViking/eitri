# SPDX-License-Identifier: Apache-2.0
import pytest

from fakes import FakeBoard, FakeBus
from eitri_mcuflash.probe import (
    BUSY, KATAPULT, KLIPPER, MISSING, SILENT, assign_roles, probe_port, require_verified,
)
from eitri_mcuflash.registry import load_registry
from eitri_mcuflash.stockcfg import parse_ownership, stock_port_roles

STOCK_CFG = """\
[mcu]
serial: /dev/ttyS2
baud:500000

[mcu U_1]
serial: /dev/ttyS0
baud:500000

[include macros/*.cfg]

[extruder]
step_pin: PB2           # toolhead pins have no prefix
dir_pin: !PB1
heater_pin: PB10
sensor_pin: PA0

[stepper_x]
step_pin:U_1:PB4
dir_pin:!U_1:PB3
endstop_pin:^U_1:PC0

[stepper_z]
step_pin:U_1:PB1
endstop_pin:probe:z_virtual_endstop

[heater_bed]
heater_pin: U_1:PC8
sensor_pin: U_1:PA1
"""


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "m.cfg").write_text("[stepper_y]\nstep_pin: U_1:PC14\n")
    p = tmp_path / "printer.cfg"
    p.write_text(STOCK_CFG)
    return str(p)


def test_parse_ownership_follows_includes_and_ignores_virtual_pins(cfg):
    o = parse_ownership(cfg)
    assert o["U_1"]["owns"] == {"stepper_x", "stepper_y", "stepper_z", "heater_bed"}
    assert o["mcu"]["owns"] == {"extruder"}
    assert o["U_1"]["serial"] == "/dev/ttyS0"


def test_stock_port_roles(cfg):
    roles = stock_port_roles(cfg, load_registry())
    assert roles["/dev/ttyS0"][0] == "mainboard"
    assert roles["/dev/ttyS2"][0] == "toolhead"
    assert "stepper_x" in roles["/dev/ttyS0"][1][0]


def _probes(bus, ports):
    clock = bus.clock
    return [
        probe_port(p, bus.opener, exists_fn=bus.exists, users_fn=bus.users, identify_timeout=0.1, clock=clock, sleep=clock.sleep)
        for p in ports
    ]


def test_probe_states():
    bus = FakeBus()
    bus.add("/dev/a", FakeBoard("a"))
    katapult = bus.add("/dev/k", FakeBoard("k"))
    katapult.mode = "katapult"
    bus.add("/dev/busy", FakeBoard("busy"))
    bus.busy["/dev/busy"] = [(123, "klippy /x/printer.cfg")]
    dead = bus.add("/dev/dead", FakeBoard("d"))
    dead.mode = "dead"
    states = {p.path: p for p in _probes(bus, ["/dev/a", "/dev/k", "/dev/busy", "/dev/dead", "/dev/none"])}
    assert states["/dev/a"].state == KLIPPER and states["/dev/a"].mcu == "stm32f401xc"
    assert states["/dev/k"].state == KATAPULT and states["/dev/k"].katapult.app_start == 0x08008000
    assert states["/dev/busy"].state == BUSY and "klippy" in states["/dev/busy"].detail
    assert states["/dev/dead"].state == SILENT
    assert states["/dev/none"].state == MISSING


def _two_boards():
    bus = FakeBus()
    bus.add("/dev/ttyS0", FakeBoard("m"))
    bus.add("/dev/ttyS2", FakeBoard("t", mcu="stm32f103xe", flash_kib=512))
    return bus, _probes(bus, ["/dev/ttyS0", "/dev/ttyS2"])


def _blind(reg):
    """A registry whose chip names tell the two boards apart no better than the hints do."""
    for b in reg.boards.values():
        b.mcu_candidates = ["stm32*"]
    return reg


def test_hint_alone_is_not_identification():
    reg = _blind(load_registry())
    _bus, probes = _two_boards()
    roles, problems, _ = assign_roles(probes, reg)
    assert roles["mainboard"].port == "/dev/ttyS0" and roles["mainboard"].confidence == "hint"
    assert require_verified(roles)  # flashing must be refused


def test_stock_evidence_beats_the_hint_when_wiring_is_swapped(tmp_path):
    reg = load_registry()
    bus = FakeBus()
    bus.add("/dev/ttyS0", FakeBoard("x", mcu="stm32f103xe", flash_kib=512))
    bus.add("/dev/ttyS2", FakeBoard("y"))
    probes = _probes(bus, ["/dev/ttyS0", "/dev/ttyS2"])
    swapped = STOCK_CFG.replace("/dev/ttyS2", "/dev/TMP").replace("/dev/ttyS0", "/dev/ttyS2").replace("/dev/TMP", "/dev/ttyS0")
    p = tmp_path / "printer.cfg"
    p.write_text(swapped)
    roles, problems, notes = assign_roles(probes, reg, stock_evidence=stock_port_roles(str(p), reg))
    assert (roles["mainboard"].port, roles["toolhead"].port) == ("/dev/ttyS2", "/dev/ttyS0")
    assert roles["mainboard"].confidence == "verified" and not problems and not require_verified(roles)
    assert any("not its usual" in n for n in notes)


def test_override_and_ambiguity():
    reg = _blind(load_registry())
    bus, probes = _two_boards()
    roles, problems, _ = assign_roles(probes, reg, overrides={"mainboard": "/dev/ttyS2", "toolhead": "/dev/ttyS0"})
    assert roles["mainboard"].port == "/dev/ttyS2" and roles["mainboard"].confidence == "override"
    # single board visible: the other has no candidate
    roles, problems, _ = assign_roles(probes[:1], reg)
    assert any("no live MCU" in p for p in problems)
    # override pointing at a dead port is reported
    roles, problems, _ = assign_roles(probes, reg, overrides={"mainboard": "/dev/nope"})
    assert any("nothing answers" in p for p in problems)


def test_known_map_counts_as_verified():
    reg = load_registry()
    _bus, probes = _two_boards()
    known = {"mainboard": {"port": "/dev/ttyS0", "mcu": "stm32f401xc"}, "toolhead": {"port": "/dev/ttyS2", "mcu": "stm32f103xe"}}
    roles, problems, _ = assign_roles(probes, reg, known_map=known)
    assert roles["mainboard"].confidence == "mapped" and not require_verified(roles)


def test_chip_discrimination_is_used_only_when_it_discriminates():
    reg = load_registry()
    for b in reg.boards.values():
        b.mcu_candidates = ["stm32f401*"] if b.name == "mainboard" else ["stm32h7*"]
    bus = FakeBus()
    bus.add("/dev/ttyS0", FakeBoard("a", mcu="stm32h723xx"))   # fits only the "toolhead" definition
    bus.add("/dev/ttyS2", FakeBoard("b", mcu="stm32f401xc"))
    probes = _probes(bus, ["/dev/ttyS0", "/dev/ttyS2"])
    roles, problems, notes = assign_roles(probes, reg)
    assert roles["toolhead"].port == "/dev/ttyS0" and roles["toolhead"].confidence == "verified"
    assert roles["mainboard"].port == "/dev/ttyS2"


def test_the_chip_that_answers_names_the_board_whatever_the_port():
    reg = load_registry()
    bus = FakeBus()
    bus.add("/dev/ttyS0", FakeBoard("x", mcu="stm32f103xe", flash_kib=512))      # wired the other way round
    bus.add("/dev/ttyS2", FakeBoard("y"))
    roles, problems, notes = assign_roles(_probes(bus, ["/dev/ttyS0", "/dev/ttyS2"]), reg)
    assert (roles["mainboard"].port, roles["toolhead"].port) == ("/dev/ttyS2", "/dev/ttyS0") and not problems
    assert roles["mainboard"].confidence == "verified" and "only fits" in roles["mainboard"].evidence[0]
    assert not require_verified(roles) and len(notes) == 2


def test_a_port_is_never_bound_to_a_board_of_another_chip():
    reg = load_registry()
    bus, probes = _two_boards()
    # printer.cfg claiming the opposite and an operator override both lose against what the boards say they are
    roles, problems, _ = assign_roles(probes, reg, overrides={"mainboard": "/dev/ttyS2"})
    assert "mainboard" not in roles and any("answers as stm32f103xe" in p for p in problems)


def test_registry_identifies_boards_by_chip_name_only():
    reg = load_registry()
    assert reg.boards["mainboard"].mcu_matches("stm32f401xc") and not reg.boards["mainboard"].mcu_matches("stm32f103xe")
    assert reg.boards["toolhead"].mcu_matches("STM32F103xe") and not reg.boards["toolhead"].mcu_matches("stm32f401xc")
    assert reg.flash_size("stm32f103xe") == 512 * 1024 and reg.flash_size("stm32f401xc") == 256 * 1024
