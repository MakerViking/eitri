# SPDX-License-Identifier: Apache-2.0
"""The verification and packaging code, exercised on synthetic images (no toolchain, no device)."""
import copy
import hashlib
import json
import shutil
import tarfile

import pytest

from conftest import DESCRIBE, LIVE, PARAMS, REPO, mcutool, resolved_params, synth_elf, synth_image

BOARDS = ("mainboard", "toolhead")


def problems(board, blob, params=PARAMS, **kw):
    return mcutool.check_image(blob, board, params["boards"][board], LIVE[board], describe=kw.pop("describe", DESCRIBE), **kw)


@pytest.mark.parametrize("board", BOARDS)
def test_a_correct_image_passes(board):
    assert problems(board, synth_image(board)) == []


@pytest.mark.parametrize("board", BOARDS)
@pytest.mark.parametrize("key", ["MCU", "CLOCK_FREQ", "SERIAL_BAUD", "RESERVE_PINS_serial"])
def test_every_live_constant_is_checked(board, key):
    wrong = {"MCU": "stm32f405xx", "CLOCK_FREQ": 168000000, "SERIAL_BAUD": 250000, "RESERVE_PINS_serial": "PA3,PA2"}[key]
    out = problems(board, synth_image(board, config_override={key: wrong}))
    assert any(key in p and "live printer reports" in p for p in out), out


def test_the_mainboard_crystal_reservation_is_checked_and_the_toolhead_has_none():
    out = problems("mainboard", synth_image("mainboard", config_override={"RESERVE_PINS_crystal": "PC14,PC15"}))
    assert any("RESERVE_PINS_crystal" in p for p in out)
    assert "RESERVE_PINS_crystal" not in LIVE["toolhead"]


def test_version_must_equal_the_host_describe():
    out = problems("mainboard", synth_image("mainboard", version="v0.13.0-813-ga08fc79fc-20261021_000000-host"))
    assert any("not the host's" in p for p in out)
    assert problems("mainboard", synth_image("mainboard"), describe=None) == []


@pytest.mark.parametrize("board", BOARDS)
def test_vector_table_must_sit_at_the_flash_offset(board):
    off = mcutool.effective_offset(PARAMS["boards"][board])
    wrong = off + 0x4000 if board == "mainboard" else 0x08008000
    out = problems(board, synth_image(board, offset=wrong, kconfig_offset=off))      # right config text, code linked elsewhere
    assert any("vector table entries" in p and "not linked for" in p for p in out), out
    out = problems(board, synth_image(board, kconfig_offset=wrong))                    # code right, build configuration says otherwise
    assert any("own build configuration" in p for p in out), out


def test_stack_pointer_must_be_the_top_of_ram():
    assert any("stack pointer" in p for p in problems("toolhead", synth_image("toolhead", sp=0x20010000)))


def test_image_must_fit_above_the_bootloader():
    big = synth_image("mainboard", size=230 * 1024)                    # inside 256 KiB, not inside 256 - 32 KiB
    assert any("fit above the bootloader" in p for p in problems("mainboard", big))
    assert not any("fit above" in p for p in problems("toolhead", synth_image("toolhead", size=230 * 1024)))


def test_elf_link_address_is_read_and_compared(tmp_path):
    elf = tmp_path / "klipper.elf"
    synth_elf(elf, 0x08008000)
    assert mcutool.elf_link_address(elf) == 0x08008000
    out = problems("mainboard", synth_image("mainboard"), link_address=0x08000000)
    assert any("ELF is linked at 0x08000000" in p for p in out)
    (tmp_path / "bad.elf").write_bytes(b"MZ" + b"\0" * 80)
    with pytest.raises(mcutool.McuError):
        mcutool.elf_link_address(tmp_path / "bad.elf")


def test_an_image_without_a_dictionary_fails():
    assert problems("mainboard", b"\0" * 4096) == ["mainboard: no data dictionary inside the image"]


# --- gate and packaging ----------------------------------------------------------------------------------------------
def builds(tmp_path, params=None):
    params = params or resolved_params()
    out = {}
    for board in BOARDS:
        d = tmp_path / "build" / board
        d.mkdir(parents=True)
        (d / "klipper.bin").write_bytes(synth_image(board, params["boards"][board]))
        synth_elf(d / "klipper.elf", mcutool.effective_offset(params["boards"][board]))
        out[board] = d
    return out


def package(tmp_path, out, params=None, **kw):
    params = params or resolved_params()
    return mcutool.package(flavor=kw.pop("flavor", "klipper"), version="0.0.0.dev0", describe=DESCRIBE,
                           commit="a08fc79fcfa342e5b95ac02a9a8b271f453523e9", epoch=1761000000, out=out,
                           builds=builds(tmp_path, params), toolchain={"name": "t", "version": "13.3.rel1", "sha256": "0" * 64, "gcc": "g"},
                           params=params, **kw)


def test_packaging_refuses_while_a_board_is_unresolved(tmp_path):
    with pytest.raises(mcutool.McuError, match="(?s)refusing to build a release bundle.*toolhead.*Open question"):
        package(tmp_path, tmp_path / "out", params=copy.deepcopy(PARAMS))
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd not installed")
def test_bundle_layout_manifest_and_reproducibility(tmp_path):
    side = package(tmp_path / "a", tmp_path / "out1")
    arc = tmp_path / "out1" / "mcu-firmware-0.0.0.dev0-klipper.tar.zst"
    assert side["archive"]["sha256"] == hashlib.sha256(arc.read_bytes()).hexdigest()
    assert json.loads((tmp_path / "out1" / "mcu-firmware-0.0.0.dev0-klipper.manifest.json").read_text())["archive"] == side["archive"]
    b = side["boards"]
    assert b["mainboard"]["mcu"] == "stm32f401xc" and b["mainboard"]["app_address"] == "0x08008000" and b["mainboard"]["baud"] == 500000
    assert b["toolhead"]["mcu"] == "stm32f103xe" and b["toolhead"]["app_address"] == "0x08002000"
    for entry in b.values():
        assert entry["version"] == entry["firmware_describe"] == DESCRIBE and entry["toolchain"] == "13.3.rel1"
        assert {"clock_ref", "serial", "bootloader", "method", "sha256", "size", "flash_offset", "parameters"} <= set(entry)
    # members: <flavor>/manifest.json and <flavor>/<board>/klipper.bin only, owner 0, one fixed time
    raw = mcutool._unzstd(arc)
    import io
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        names = [m.name for m in tar.getmembers() if m.isfile()]
        assert names == ["klipper/mainboard/klipper.bin", "klipper/manifest.json", "klipper/toolhead/klipper.bin"]
        assert {(m.uid, m.gid, m.mtime) for m in tar.getmembers()} == {(0, 0, 1761000000)}
    # byte-identical from a second build with the same inputs
    package(tmp_path / "b", tmp_path / "out2")
    assert (tmp_path / "out2" / arc.name).read_bytes() == arc.read_bytes()
    assert (tmp_path / "out2" / "mcu-firmware-0.0.0.dev0-klipper.manifest.json").read_bytes() == \
        (tmp_path / "out1" / "mcu-firmware-0.0.0.dev0-klipper.manifest.json").read_bytes()
    # and verify_bundle accepts it
    probs, _ = mcutool.verify_bundle(arc, params=resolved_params(), describe=DESCRIBE)
    assert probs == []


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd not installed")
def test_the_flasher_reads_the_bundle_it_is_given(tmp_path):
    """The bundle is exactly what eitri-mcuflash loads: manifest, checksum, dictionary, chip and offset."""
    package(tmp_path / "a", tmp_path / "out")
    root = tmp_path / "fw"
    root.mkdir()
    import subprocess
    subprocess.run(["zstd", "-q", "-d", "-c", str(tmp_path / "out" / "mcu-firmware-0.0.0.dev0-klipper.tar.zst")], stdout=open(tmp_path / "a.tar", "wb"), check=True)
    with tarfile.open(tmp_path / "a.tar") as tar:
        tar.extractall(root, filter="data")
    from eitri_mcuflash import firmware as fw
    m = fw.load_artifact(str(root), "klipper", "mainboard", expect_mcu="stm32f401xc")
    t = fw.load_artifact(str(root), "klipper", "toolhead", expect_mcu="stm32f103xe")
    assert (m.app_address, t.app_address) == (0x08008000, 0x08002000) and m.version == t.version == DESCRIBE
    assert m.config["CLOCK_FREQ"] == 84000000 and t.config["CLOCK_FREQ"] == 72000000
    # ... and the flasher's own preflight accepts the images for the (here: settled) registry
    from eitri_mcuflash.registry import load_registry
    reg = load_registry()
    for board, art in (("mainboard", m), ("toolhead", t)):
        for key, want in reg.boards[board].expect_config.items():
            assert (art.mcu if key == "MCU" else art.config[key]) == want


def test_packaging_rejects_an_image_that_fails_verification(tmp_path):
    params = resolved_params()
    b = builds(tmp_path / "x", params)
    (b["toolhead"] / "klipper.bin").write_bytes(synth_image("toolhead", params["boards"]["toolhead"], config_override={"CLOCK_FREQ": 8000000}))
    with pytest.raises(mcutool.McuError, match="CLOCK_FREQ"):
        mcutool.package(flavor="klipper", version="1", describe=DESCRIBE, commit="c", epoch=1, out=tmp_path / "o", builds=b,
                        toolchain={}, params=params)
    (b["toolhead"] / "klipper.bin").write_bytes(synth_image("toolhead", params["boards"]["toolhead"]))
    (b["toolhead"] / "klipper.elf").unlink()
    with pytest.raises(mcutool.McuError, match="klipper.elf is missing"):
        mcutool.package(flavor="klipper", version="1", describe=DESCRIBE, commit="c", epoch=1, out=tmp_path / "o", builds=b,
                        toolchain={}, params=params)
    assert not (tmp_path / "o").exists()


def test_release_lock_reader():
    assert mcutool.lock_value("firmware.klipper", "commit") == "a08fc79fcfa342e5b95ac02a9a8b271f453523e9"
    assert mcutool.lock_value("firmware.kalico", "commit") == "3526de3bfbdc64f40001a4070b95d28b79fa4147"
