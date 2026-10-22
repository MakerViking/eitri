# SPDX-License-Identifier: Apache-2.0
"""A board whose flash layout is unresolved is never written; the settled one is checked against the image."""
import json
import os

import pytest

from fakes import make_image
from helpers import write_release
from test_cli_events import events_of, run_cli
from test_runner import NEW, STOCK_VERSION, Rig
from eitri_mcuflash.registry import load_registry
from eitri_mcuflash.runner import BLOCK, EX_BLOCKED, EX_OK, WARN

# What the live printer's klippy.log reports (also stored in mcu/tests/live_constants.json).
LIVE = {
    "mainboard": {"MCU": "stm32f401xc", "CLOCK_FREQ": 84000000, "SERIAL_BAUD": 500000,
                  "RESERVE_PINS_serial": "PA10,PA9", "RESERVE_PINS_crystal": "PH0,PH1"},
    "toolhead": {"MCU": "stm32f103xe", "CLOCK_FREQ": 72000000, "SERIAL_BAUD": 500000, "RESERVE_PINS_serial": "PB7,PB6"},
}


def checks(plan, id_, board=None, status=None):
    return [c for c in plan.all_checks() if c.id == id_ and (board is None or c.board == board) and (status is None or c.status == status)]


# --- the registry itself ------------------------------------------------------------------------
def test_shipped_registry_matches_the_live_printer():
    reg = load_registry()
    for name, want in LIVE.items():
        assert reg.boards[name].expect_config == want
        assert reg.boards[name].app_baud == 500000
    assert reg.boards["mainboard"].serial_hint == "/dev/ttyS0" and reg.boards["toolhead"].serial_hint == "/dev/ttyS2"
    assert reg.flash_size("stm32f103xe") == 512 * 1024 and reg.flash_size("stm32f401xc") == 256 * 1024


def test_shipped_registry_settles_the_mainboard_and_not_the_toolhead():
    reg = load_registry()
    main, tool = reg.boards["mainboard"], reg.boards["toolhead"]
    assert main.resolved and main.app_offset == 0x08008000 and main.allow_katapult is False and main.katapult_bauds == []
    assert not tool.resolved and tool.app_offset is None and tool.resolved_by == "katapult"
    assert tool.katapult_bauds == [250000, 500000] and tool.open_questions
    assert all("stm32f4" not in c for c in tool.mcu_candidates)


def test_a_registry_that_says_nothing_is_not_resolved(tmp_path):
    doc = json.load(open(os.path.join(os.path.dirname(load_registry.__code__.co_filename), "boards.json")))
    del doc["boards"]["toolhead"]["parameters"]
    p = tmp_path / "boards.json"
    p.write_text(json.dumps(doc))
    assert load_registry(str(p)).boards["toolhead"].resolved is False


# --- refusal --------------------------------------------------------------------------------------
def test_unresolved_toolhead_is_refused_and_nothing_is_touched(tmp_path):
    rig = Rig(tmp_path, settled=False)
    snap = (bytes(rig.tool.flash), bytes(rig.main.flash))
    plan = rig.plan()
    block = checks(plan, "parameters", "toolhead", BLOCK)
    assert plan.blocked and block
    assert "unresolved" in block[0].detail and "Open question" in block[0].detail and "no bootloader" in block[0].detail
    assert checks(plan, "parameters", "mainboard", "ok")          # the settled board is not what blocks
    code, data = rig.flasher.execute(plan, apply=True)
    assert code == EX_BLOCKED
    assert (bytes(rig.tool.flash), bytes(rig.main.flash)) == snap
    assert "bootloader-request" not in rig.tool.log and not [c for c in rig.calls if any(a.endswith("spi_flash.py") for a in c)]
    assert not os.path.exists(os.path.join(rig.opt.state_dir, "backup"))
    tool = data["boards"]["toolhead"]["parameters"]
    assert tool["status"] == "unresolved" and tool["open_questions"] and data["boards"]["mainboard"]["parameters"]["open_questions"] == []


def test_backup_is_refused_too_because_it_resets_the_board(tmp_path):
    rig = Rig(tmp_path, settled=False)
    code, _ = rig.flasher.backup_only(rig.plan(), apply=True)
    assert code == EX_BLOCKED and "bootloader-request" not in rig.tool.log


def test_refusal_reaches_the_cli_as_exit_3_with_the_question(tmp_path, capsys):
    rig = Rig(tmp_path, settled=False)
    code, out, _ = run_cli(rig, ["flash", "--json", "--apply", "--yes"], capsys, settled=False)
    res = events_of(out)[-1]
    assert code == 3 and res["ok"] is False
    assert any(c["id"] == "parameters" and c["status"] == "block" and "Open question" in c["detail"] for c in res["data"]["checks"])
    assert "bootloader-request" not in rig.tool.log


def test_unresolved_toolhead_is_written_only_while_katapult_answers(tmp_path):
    rig = Rig(tmp_path, settled=False)
    rig.tool.mode = "katapult"                                       # seen answering as Katapult right now
    plan = rig.plan()
    assert not plan.blocked
    assert checks(plan, "parameters", "toolhead", WARN) and "accepted only because katapult answered" in checks(plan, "parameters", "toolhead")[0].detail
    code, data = rig.flasher.execute(plan, apply=True)
    assert code == EX_OK, data.get("error")
    assert rig.tool.app_dictionary()["version"] == NEW


def test_katapult_at_the_other_baud_rate_is_found(tmp_path):
    rig = Rig(tmp_path, settled=False)
    rig.tool.katapult_baud = 250000                                  # the FreeDi build; the community ones use 500000
    rig.tool.mode = "katapult"
    plan = rig.plan()
    assert not plan.blocked and plan.boards[0].probe.baud == 250000
    code, data = rig.flasher.execute(plan, apply=True)
    assert code == EX_OK, data.get("error")
    assert (rig.tool.app_dictionary() or {}).get("version") == NEW


def test_katapult_offset_the_image_was_not_linked_for_is_refused(tmp_path):
    rig = Rig(tmp_path, settled=False)
    rig.tool.mode = "katapult"
    write_release(tmp_path / "fw", app_address={"toolhead": 0x08008000})
    snap = bytes(rig.tool.flash)
    plan = rig.plan()
    assert plan.blocked and checks(plan, "offset", "toolhead", BLOCK)
    assert rig.flasher.execute(plan, apply=True)[0] == EX_BLOCKED and bytes(rig.tool.flash) == snap


def test_unresolved_board_needs_an_image_that_says_where_it_is_linked(tmp_path):
    rig = Rig(tmp_path, settled=False)
    rig.tool.mode = "katapult"
    write_release(tmp_path / "fw")
    mp = tmp_path / "fw" / "klipper" / "manifest.json"
    doc = json.loads(mp.read_text())
    del doc["boards"]["toolhead"]["app_address"]
    mp.write_text(json.dumps(doc))
    plan = rig.plan()
    assert plan.blocked and "cannot tell which application offset" in checks(plan, "offset", "toolhead", BLOCK)[0].detail


def test_mainboard_that_answers_as_katapult_is_refused(tmp_path):
    rig = Rig(tmp_path)
    rig.main.has_katapult, rig.main.mode, rig.main.katapult_baud = True, "katapult", 500000
    plan = rig.plan()
    assert plan.blocked and "stock bootloader is not Katapult" in checks(plan, "parameters", "mainboard", BLOCK)[0].detail


# --- the image must be for the board -----------------------------------------------------------------
def test_image_with_the_wrong_clock_or_pins_is_refused(tmp_path):
    rig = Rig(tmp_path)
    write_release(tmp_path / "fw", extra_config={"toolhead": {"CLOCK_FREQ": 84000000}, "mainboard": {"RESERVE_PINS_serial": "PB7,PB6"}})
    plan = rig.plan()
    t = checks(plan, "firmware_identity", "toolhead", BLOCK)
    m = checks(plan, "firmware_identity", "mainboard", BLOCK)
    assert plan.blocked and "CLOCK_FREQ=84000000 (board: 72000000)" in t[0].detail
    assert "RESERVE_PINS_serial=PB7,PB6 (board: PA10,PA9)" in m[0].detail


def test_mainboard_image_without_the_crystal_reservation_is_refused(tmp_path):
    rig = Rig(tmp_path)
    import eitri_mcuflash.firmware as fw
    write_release(tmp_path / "fw")
    mp = tmp_path / "fw" / "klipper" / "mainboard" / "klipper.bin"
    img = make_image(NEW, "stm32f401xc", spi=True, size=3000)
    # an image built for the internal oscillator does not reserve PH0/PH1 at all
    import zlib
    doc = fw.extract_embedded_dictionary(img)
    doc["config"].pop("RESERVE_PINS_crystal")
    img = bytes(range(1, 250)) * 3 + zlib.compress(json.dumps(doc).encode(), 9)
    img += b"\xA5" * (3000 - len(img))
    mp.write_bytes(img)
    import hashlib
    m = tmp_path / "fw" / "klipper" / "manifest.json"
    d = json.loads(m.read_text())
    d["boards"]["mainboard"]["sha256"] = hashlib.sha256(img).hexdigest()
    m.write_text(json.dumps(d))
    plan = rig.plan()
    assert plan.blocked and "RESERVE_PINS_crystal absent" in checks(plan, "firmware_identity", "mainboard", BLOCK)[0].detail


def test_image_must_fit_above_the_bootloader_not_just_in_the_chip(tmp_path):
    rig = Rig(tmp_path)
    write_release(tmp_path / "fw", size=230 * 1024)          # fits 256 KiB, not the 224 KiB above a 32 KiB bootloader
    plan = rig.plan()
    assert checks(plan, "fit", "mainboard", BLOCK) and not checks(plan, "fit", "toolhead", BLOCK)


def test_kalico_tree_without_the_qidi_board_definition_falls_back_to_the_card(tmp_path):
    rig = Rig(tmp_path)
    defs = rig.klipper / "scripts" / "spi_flash" / "board_defs.py"
    defs.write_text("BOARD_DEFS = {'btt-skr-2': {}}\n")
    plan = rig.plan()
    assert plan.blocked and "board_defs.py has no 'qidi-plus4' definition" in checks(plan, "method", "mainboard", BLOCK)[0].detail
    card = tmp_path / "card"
    card.mkdir()
    rig.ctx.sd_mount = str(card)
    plan = rig.plan()
    assert not plan.blocked and [b.method for b in plan.boards if b.board.name == "mainboard"] == ["sdcard-file"]
    defs.write_text("BOARD_DEFS = {\n    'qidi-x7': {},\n}\nBOARD_ALIASES = {'qidi-plus4': 1}\n".replace("{'qidi-plus4': 1}", "{\n    'qidi-plus4': 0}"))
    rig.ctx.sd_mount = None
    assert not rig.plan().blocked


# --- review findings: checks that must hold for the image itself, not only for its manifest ------------------
def _replace_image(tmp_path, board, img):
    import hashlib
    (tmp_path / "fw" / "klipper" / board / "klipper.bin").write_bytes(img)
    m = tmp_path / "fw" / "klipper" / "manifest.json"
    d = json.loads(m.read_text())
    d["boards"][board]["sha256"] = hashlib.sha256(img).hexdigest()
    m.write_text(json.dumps(d))


def test_image_linked_elsewhere_is_refused_even_when_the_manifest_lies(tmp_path):
    rig = Rig(tmp_path)
    # manifest and checksum say 0x08008000; the image's own vector table says it is linked for 0x0800c000
    _replace_image(tmp_path, "mainboard", make_image(NEW, "stm32f401xc", spi=True, size=3000, app_address=0x0800C000))
    plan = rig.plan()
    bad = checks(plan, "vector_table", "mainboard", BLOCK)
    assert plan.blocked and bad and "not linked for 0x08008000" in bad[0].remedy
    assert checks(plan, "vector_table", "toolhead", "ok")


def test_image_without_a_vector_table_is_refused(tmp_path):
    rig = Rig(tmp_path)
    img = bytearray(make_image(NEW, "stm32f401xc", spi=True, size=3000))
    img[0:4] = (0x08000000).to_bytes(4, "little")              # initial stack pointer in flash
    _replace_image(tmp_path, "mainboard", bytes(img))
    assert "not in SRAM" in checks(rig.plan(), "vector_table", "mainboard", BLOCK)[0].detail


def test_stock_recovery_image_must_be_linked_for_the_application_offset(tmp_path):
    rig = Rig(tmp_path)
    wrong = tmp_path / "wrong_stock.bin"
    wrong.write_bytes(make_image(STOCK_VERSION, "stm32f401xc", spi=True, app_address=0x08000000))
    rig.opt.stock_bins["mainboard"] = str(wrong)
    plan = rig.plan()
    rec = checks(plan, "recovery", "mainboard", BLOCK)
    assert plan.blocked and rec and "not linked for 0x08008000" in rec[0].detail


def test_on_device_build_is_checked_after_the_build(tmp_path):
    """--build: the artifact does not exist at plan time, so the identity/offset checks must run when it does."""
    import eitri_mcuflash.firmware as fw
    from eitri_mcuflash.errors import FlashError

    rig = Rig(tmp_path, build=True)
    plan = rig.plan()
    assert not plan.blocked
    # a build from the wrong config: wrong chip clock and linked for 0x0800c000
    bad = make_image(NEW, "stm32f401xc", spi=True, size=3000, app_address=0x0800C000, extra_config={"CLOCK_FREQ": 96000000})
    out = tmp_path / "builtbin"

    def fake_build(board, flavor, **kw):
        path = out / "klipper.bin"
        out.mkdir(exist_ok=True)
        path.write_bytes(bad)
        doc = fw.extract_embedded_dictionary(bad)
        import hashlib
        return fw.FirmwareArtifact(board=board, flavor=flavor, path=str(path), sha256=hashlib.sha256(bad).hexdigest(), size=len(bad),
                                   version=NEW, mcu="stm32f401xc", app_address=0x0800C000, origin="built", config=dict(doc["config"]))

    orig = fw.build_artifact
    fw.build_artifact = fake_build
    try:
        bp = [b for b in plan.boards if b.board.name == "mainboard"][0]
        with pytest.raises(FlashError) as ei:
            rig.flasher._build_if_needed(bp)
    finally:
        fw.build_artifact = orig
    assert ei.value.code == "build_mismatch" and "CLOCK_FREQ" in ei.value.message


def test_build_config_is_found_under_the_names_the_repository_ships(tmp_path):
    rig = Rig(tmp_path)
    d = tmp_path / "cfgs"
    d.mkdir()
    (d / "mainboard-stm32f401.config").write_text("CONFIG_MACH_STM32=y\n")
    rig.opt.build_config_dir = str(d)
    assert rig.flasher._build_config("mainboard").endswith("mainboard-stm32f401.config")
    (d / "mainboard.config").write_text("x")
    assert rig.flasher._build_config("mainboard").endswith("/mainboard.config")
    assert rig.flasher._build_config("toolhead").endswith("/toolhead.config")      # absent: the caller reports no_config


def test_spi_flash_gets_the_dictionary_byte_for_byte(tmp_path):
    """spi_flash.py compares the dictionary read back from the board with this file as raw bytes."""
    import eitri_mcuflash.firmware as fw
    from eitri_mcuflash.methods import SdcardSerialMethod

    rig = Rig(tmp_path)
    art = fw.load_artifact(str(tmp_path / "fw"), "klipper", "mainboard")
    seen = {}

    def runner(argv, **kw):
        seen["dict"] = open(argv[argv.index("-d") + 1], "rb").read()
        return type("R", (), {"returncode": 0, "output": ""})()

    rig.ctx.runner = runner
    SdcardSerialMethod(rig.ctx, rig.ctx.registry.boards["mainboard"], "/dev/ttyS0").flash_image(art.path, art)
    raw = fw.extract_embedded_dictionary_raw(art.read())
    assert seen["dict"] == raw
    # and Klipper's own dictionaries are compact + sorted, which a plain json.dump() would not reproduce
    doc = json.loads(raw)
    assert json.dumps(doc, separators=(",", ":"), sort_keys=True).encode() != json.dumps(doc).encode()


def test_sdcard_file_copy_is_forced_to_the_card_before_the_operator_is_prompted(tmp_path, monkeypatch):
    from eitri_mcuflash.methods import SdcardFileMethod

    rig = Rig(tmp_path)
    card = tmp_path / "card"
    card.mkdir()
    rig.ctx.sd_mount = str(card)
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    SdcardFileMethod(rig.ctx, rig.ctx.registry.boards["mainboard"], "/dev/ttyS0").stage(str(tmp_path / "fw" / "klipper" / "mainboard" / "klipper.bin"))
    assert len(synced) >= 2                                  # the file and its directory
