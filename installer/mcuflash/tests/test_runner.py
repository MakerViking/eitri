# SPDX-License-Identifier: Apache-2.0
"""End-to-end flows against simulated boards."""
import json
import os

import pytest

from fakes import FakeBoard, FakeBus, FakeClock, make_image
from helpers import STOCK_CFG, write_release
from eitri_mcuflash.events import EventSink
from eitri_mcuflash.methods import FlashContext
from eitri_mcuflash.proc import RunResult
from eitri_mcuflash.registry import load_registry
from eitri_mcuflash.runner import BLOCK, EX_BLOCKED, EX_FAIL, EX_OK, Flasher, Options

NEW = "v0.13.0-813-ga08fc79fc"
STOCK_VERSION = "stock-1.7.1"


class Rig:
    def __init__(self, tmp_path, *, tool_katapult=True, **opt):
        self.tmp = tmp_path
        self.clock = FakeClock()
        self.bus = FakeBus(self.clock)
        self.main = self.bus.add("/dev/ttyS0", FakeBoard("mainboard", version=STOCK_VERSION, spi=True, has_katapult=False))
        self.tool = self.bus.add("/dev/ttyS2", FakeBoard("toolhead", version=STOCK_VERSION, has_katapult=tool_katapult))
        self.images = write_release(tmp_path / "fw")
        cfg = tmp_path / "printer.cfg"
        cfg.write_text(STOCK_CFG)
        self.klipper = tmp_path / "klipper"
        (self.klipper / ".git").mkdir(parents=True)
        (self.klipper / "scripts" / "spi_flash").mkdir(parents=True)
        (self.klipper / "scripts" / "spi_flash" / "spi_flash.py").write_text("# stub")
        (self.klipper / "scripts" / "spi_flash" / "fatfs.so").write_bytes(b"\x7fELF")
        stock = tmp_path / "qd_mcu_stock.bin"
        stock.write_bytes(make_image(STOCK_VERSION, spi=True))
        self.calls = []
        self.host_version = NEW
        self.sd_fail = False
        self.events = EventSink(keep=True, run_id="t")
        self.ctx = FlashContext(
            registry=load_registry(), opener=self.bus.opener, events=self.events, runner=self.runner,
            sleep=self.clock.sleep, clock=self.clock, klipper_dir=str(self.klipper),
            identify_timeout=0.1, exists_fn=self.bus.exists, users_fn=self.bus.users, work_dir=str(tmp_path),
        )
        defaults = dict(firmware_root=str(tmp_path / "fw"), state_dir=str(tmp_path / "state"), printer_cfgs=[str(cfg)],
                        stock_bins={"mainboard": str(stock)})
        defaults.update(opt)
        self.opt = Options(**defaults)
        self.flasher = Flasher(self.ctx, self.opt)

    def runner(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:2] == ["git", "-C"]:
            return RunResult(0, self.host_version + "\n")
        if argv[0] == "systemctl":
            return RunResult(0, "")
        if any(a.endswith("spi_flash.py") for a in argv):
            if self.sd_fail:
                return RunResult(1, "flash_sdcard: failed to reset SD Card")
            self.main.install(open(argv[-1], "rb").read())
            self.main.next_seq = 1
            return RunResult(0, "Flash successful")
        return RunResult(127, "unexpected command " + " ".join(argv))

    def plan(self):
        return self.flasher.make_plan(self.flasher.probe_all())

    def step_names(self, board=None):
        return [(e["step"], e["status"]) for e in self.events.events if e["type"] == "step" and (board is None or e.get("board") == board)]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_dry_run_changes_nothing(rig):
    before = (bytes(rig.tool.flash), bytes(rig.main.flash))
    plan = rig.plan()
    assert not plan.blocked, [c.detail for c in plan.all_checks() if c.status == BLOCK]
    assert [b.board.name for b in plan.boards] == ["toolhead", "mainboard"]  # katapult board first
    code, data = rig.flasher.execute(plan, apply=False)
    assert code == EX_OK and data["dry_run"] is True
    assert (bytes(rig.tool.flash), bytes(rig.main.flash)) == before
    assert "bootloader-request" not in rig.tool.log
    assert not os.path.exists(os.path.join(rig.opt.state_dir, "backup"))
    assert not any(c[0] == "systemctl" or any(a.endswith("spi_flash.py") for a in c) for c in rig.calls)
    assert rig.events.events[-1]["type"] == "result" and rig.events.events[-1]["ok"] is True
    assert data["boards"]["toolhead"]["method"] == "katapult"
    assert data["boards"]["mainboard"]["method"] == "sdcard-serial"


def test_apply_flashes_backs_up_and_verifies(rig):
    old_app = rig.tool.app_image
    code, data = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_OK, data.get("error")
    # new firmware really is in flash, byte for byte
    assert rig.tool.app_image[: len(rig.images["toolhead"])] == rig.images["toolhead"]
    assert rig.tool.mode == "app" and rig.tool.app_dictionary()["version"] == NEW
    assert rig.main.app_dictionary()["version"] == NEW
    # backup holds the OLD application region, restorable
    entry = rig.flasher.store.latest_restorable("toolhead")
    saved = open(entry.app_path, "rb").read()
    assert old_app.startswith(saved) and len(saved) % 64 == 0 and saved.rstrip(b"\xff") == old_app.rstrip(b"\xff")
    assert entry.meta["katapult"]["app_start"] == "0x08008000"
    assert rig.flasher.store.entries("mainboard")  # identity recorded for SD-bootloader board
    # board map persisted for the next run
    mapping = json.load(open(os.path.join(rig.opt.state_dir, "mcu-map.json")))
    assert mapping["boards"]["mainboard"]["port"] == "/dev/ttyS0" and mapping["boards"]["toolhead"]["version"] == NEW
    # every board proved recovery before any flash started
    names = [(e.get("board"), e["step"]) for e in rig.events.events if e["type"] == "step" and e["status"] == "start"]
    assert names.index(("mainboard", "backup")) < names.index(("toolhead", "flash"))
    assert ("toolhead", "roundtrip") in names
    assert rig.events.events[-1]["ok"] is True
    # every event is valid JSON-serialisable and schema-tagged
    for e in rig.events.events:
        json.dumps(e)
        assert e["schema"] == 1 and e["component"] == "mcuflash" and e["run"] == "t"


def test_flash_failure_rolls_back_to_saved_image(rig):
    rig.opt.roundtrip = False
    rig.tool.corrupt_plan = {"addr": 0x08008000 + 64, "skip": 0, "count": 3}  # all three passes get corrupted
    old_version = rig.tool.app_dictionary()["version"]
    code, data = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_FAIL
    assert data["error"]["step"] == "flash" and data["error"]["board"] == "toolhead"
    assert "restored automatically" in data["error"]["recovery"]
    assert rig.tool.mode == "app" and rig.tool.app_dictionary()["version"] == old_version
    assert rig.main.app_dictionary()["version"] == STOCK_VERSION  # later boards are not touched
    assert ("rollback", "ok") in rig.step_names("toolhead")
    assert rig.events.events[-1]["ok"] is False


def test_image_that_does_not_boot_is_rolled_back(rig):
    rig.tool.broken_versions = {NEW}
    code, data = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_FAIL
    assert data["error"]["code"] == "verify_failed"
    assert rig.tool.app_dictionary()["version"] == STOCK_VERSION and rig.tool.mode == "app"


def test_no_bootloader_means_nothing_is_erased_anywhere(rig):
    rig.tool.has_katapult = False
    snap = (bytes(rig.tool.flash), bytes(rig.main.flash))
    code, data = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_FAIL and data["error"]["code"] == "no_bootloader"
    assert "ST-Link" in data["error"]["recovery"]
    assert (bytes(rig.tool.flash), bytes(rig.main.flash)) == snap
    assert not [c for c in rig.calls if any(a.endswith("spi_flash.py") for a in c)]


def test_offset_mismatch_is_caught_before_erase(tmp_path):
    rig = Rig(tmp_path)
    write_release(tmp_path / "fw", app_address=0x08004000)
    snap = bytes(rig.tool.flash)
    code, data = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_FAIL and data["error"]["code"] == "offset_mismatch"
    assert bytes(rig.tool.flash) == snap


def test_toolhead_already_in_bootloader(rig):
    rig.tool.mode = "katapult"
    plan = rig.plan()
    assert not plan.blocked
    code, data = rig.flasher.execute(plan, apply=True)
    assert code == EX_OK
    assert rig.tool.app_dictionary()["version"] == NEW


def test_sdcard_board_without_stock_image_is_blocked_until_acknowledged(tmp_path):
    rig = Rig(tmp_path, stock_bins={})
    plan = rig.plan()
    assert plan.blocked
    blocked = [c for c in plan.all_checks() if c.status == BLOCK]
    assert blocked[0].id == "recovery" and "stock" in blocked[0].detail
    code, _ = rig.flasher.execute(plan, apply=True)
    assert code == EX_BLOCKED
    assert rig.main.app_dictionary()["version"] == STOCK_VERSION and rig.tool.app_dictionary()["version"] == STOCK_VERSION
    rig.opt.accept_no_mcu_backup = True
    assert not rig.plan().blocked


def test_hint_only_identification_is_blocked(tmp_path):
    rig = Rig(tmp_path, printer_cfgs=[])
    plan = rig.plan()
    assert plan.blocked and any("default wiring hint" in c.detail for c in plan.all_checks())
    # an explicit operator assignment unblocks it
    rig.opt.assign = {"mainboard": "/dev/ttyS0", "toolhead": "/dev/ttyS2"}
    assert not rig.plan().blocked


def test_swapped_wiring_is_followed_by_evidence_not_constants(tmp_path):
    rig = Rig(tmp_path)
    rig.bus.boards["/dev/ttyS0"], rig.bus.boards["/dev/ttyS2"] = rig.bus.boards["/dev/ttyS2"], rig.bus.boards["/dev/ttyS0"]
    # now ttyS2 hosts the SPI-capable board; printer.cfg says ttyS2 is the mainboard's port
    cfg = tmp_path / "printer.cfg"
    cfg.write_text(STOCK_CFG.replace("/dev/ttyS2", "/dev/TMP").replace("/dev/ttyS0", "/dev/ttyS2").replace("/dev/TMP", "/dev/ttyS0"))
    plan = rig.plan()
    assert not plan.blocked
    roles = {b.board.name: b.role.port for b in plan.boards}
    assert roles == {"toolhead": "/dev/ttyS0", "mainboard": "/dev/ttyS2"}


def test_version_lock_blocks_mismatched_host(rig):
    rig.host_version = "v0.13.0-700-g1234567ab"
    plan = rig.plan()
    assert plan.blocked and any(c.id == "version_lock" and c.status == BLOCK for c in plan.all_checks())


def test_busy_port_is_reported_with_owner(rig):
    rig.bus.busy["/dev/ttyS2"] = [(412, "/data/venv/klippy/bin/python klippy.py")]
    plan = rig.plan()
    assert plan.blocked
    port_check = [c for c in plan.all_checks() if c.id == "port" and c.status == BLOCK][0]
    assert "pid 412" in port_check.detail and "stop" in port_check.remedy.lower()


def test_tampered_firmware_is_never_flashed(tmp_path):
    rig = Rig(tmp_path)
    write_release(tmp_path / "fw", tamper="toolhead")
    plan = rig.plan()
    assert plan.blocked and any("checksum" in c.detail for c in plan.all_checks())


def test_sdcard_flash_failure_reports_recovery(rig):
    rig.sd_fail = True
    code, data = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_FAIL and data["error"]["board"] == "mainboard"
    assert "microSD" in data["recovery"]["mainboard"][0] or "SD" in " ".join(data["recovery"]["mainboard"])
    assert rig.tool.app_dictionary()["version"] == NEW  # toolhead already done; reported in results
    assert "toolhead" in data["results"]


def test_manual_rollback_restores_previous_firmware(rig):
    code, _ = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_OK
    plan = rig.plan()
    tool_plan = [b for b in plan.boards if b.board.name == "toolhead"][0]
    rig.flasher.rollback_board(tool_plan)
    assert rig.tool.app_dictionary()["version"] == STOCK_VERSION


def test_unknown_chip_refuses_to_size_a_backup(tmp_path):
    rig = Rig(tmp_path)
    for b in (rig.main, rig.tool):
        b.mcu = "stm32zz9999"
    write_release(tmp_path / "fw", mcu="stm32zz9999")
    rig.tool.install(make_image(STOCK_VERSION, mcu="stm32zz9999"))
    rig.main.install(make_image(STOCK_VERSION, mcu="stm32zz9999", spi=True))
    snap = bytes(rig.tool.flash)
    code, data = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_FAIL and data["error"]["code"] == "unknown_chip"
    assert bytes(rig.tool.flash) == snap


def test_only_filter(rig):
    rig.opt.only = ["toolhead"]
    plan = rig.plan()
    assert [b.board.name for b in plan.boards] == ["toolhead"]
    code, _ = rig.flasher.execute(plan, apply=True)
    assert code == EX_OK and rig.main.app_dictionary()["version"] == STOCK_VERSION


def test_backup_only_proves_recovery_without_flashing(rig):
    old = rig.tool.app_image
    code, data = rig.flasher.backup_only(rig.plan(), apply=True)
    assert code == EX_OK and set(data["backups"]) == {"toolhead", "mainboard"}
    assert rig.tool.app_image == old and rig.tool.app_dictionary()["version"] == STOCK_VERSION      # roundtrip restored it, bit for bit
    assert rig.main.app_dictionary()["version"] == STOCK_VERSION
    assert rig.flasher.store.latest_restorable("toolhead") is not None
    assert not [c for c in rig.calls if any(a.endswith("spi_flash.py") for a in c)]


def test_backup_only_dry_run_resets_nothing(rig):
    code, data = rig.flasher.backup_only(rig.plan(), apply=False)
    assert code == EX_OK and "bootloader-request" not in rig.tool.log


def test_backup_only_fails_closed_without_bootloader(rig):
    rig.tool.has_katapult = False
    code, data = rig.flasher.backup_only(rig.plan(), apply=True)
    assert code == EX_FAIL and data["error"]["code"] == "no_bootloader"


def test_pinned_commit_must_match_the_firmware(rig):
    rig.opt.commit = "a08fc79fcfa3"
    plan = rig.plan()
    assert not plan.blocked and any(c.id == "commit" and c.status == "ok" for c in plan.all_checks())
    rig.opt.commit = "0123456789ab"
    plan = rig.plan()
    assert plan.blocked and any(c.id == "commit" and c.status == BLOCK and "pinned" in c.detail for c in plan.all_checks())
    code, _ = rig.flasher.execute(plan, apply=True)
    assert code == EX_BLOCKED and rig.tool.app_dictionary()["version"] == STOCK_VERSION


def test_pinned_commit_with_on_device_build_checks_the_clone(rig):
    rig.opt.build = True
    rig.opt.commit = "feedface"
    orig = rig.runner
    rig.ctx.runner = lambda argv, **kw: RunResult(0, "a08fc79fcfa342e5b95ac02a9a8b271f453523e9\n") if argv[-2:] == ["rev-parse", "HEAD"] else orig(argv, **kw)
    plan = rig.plan()
    assert any(c.id == "commit" and c.status == BLOCK for c in plan.all_checks())


def test_stock_image_is_kept_so_the_mainboard_can_be_rolled_back(rig):
    code, _ = rig.flasher.execute(rig.plan(), apply=True)
    assert code == EX_OK and rig.main.app_dictionary()["version"] == NEW
    kept = rig.flasher.store.stock_image("mainboard")
    assert kept and open(kept, "rb").read() == open(rig.opt.stock_bins["mainboard"], "rb").read()
    # a later invocation that is NOT told about --stock-bin (revert.sh on Eitri) still finds it
    rig.opt.stock_bins = {}
    main_plan = [b for b in rig.plan().boards if b.board.name == "mainboard"][0]
    rig.flasher.rollback_board(main_plan, original=True)
    assert rig.main.app_dictionary()["version"] == STOCK_VERSION


def test_revert_target_is_the_original_firmware_not_the_latest_backup(rig):
    rig.opt.origin = "stock"
    code, _ = rig.flasher.backup_only(rig.plan(), apply=True)
    assert code == EX_OK
    rig.opt.origin = ""
    assert rig.flasher.execute(rig.plan(), apply=True)[0] == EX_OK          # toolhead now runs NEW
    assert rig.flasher.execute(rig.plan(), apply=True)[0] == EX_OK          # second run: the latest backup holds NEW
    tool_plan = [b for b in rig.plan().boards if b.board.name == "toolhead"][0]
    latest = rig.flasher.store.latest_restorable("toolhead")
    original = rig.flasher.store.original_restorable("toolhead")
    assert latest.meta.get("origin") is None and original.meta["origin"] == "stock" and latest.directory != original.directory
    rig.flasher.rollback_board(tool_plan, original=True)
    assert rig.tool.app_dictionary()["version"] == STOCK_VERSION


def test_stock_image_for_the_wrong_chip_is_refused(tmp_path):
    rig = Rig(tmp_path)
    wrong = tmp_path / "wrong.bin"
    wrong.write_bytes(make_image(STOCK_VERSION, mcu="stm32f103xb", spi=True))
    rig.opt.stock_bins = {"mainboard": str(wrong)}
    plan = rig.plan()
    assert plan.blocked and any(c.id == "recovery" and c.status == BLOCK and "stm32f103xb" in c.detail for c in plan.all_checks())
    stub = tmp_path / "tiny.bin"
    stub.write_bytes(b"x" * 10)
    rig.opt.stock_bins = {"mainboard": str(stub)}
    assert rig.plan().blocked


def test_spi_flash_needs_fatfs_helper_or_a_compiler(rig, monkeypatch):
    import eitri_mcuflash.methods as methods
    monkeypatch.setattr(methods.shutil, "which", lambda *_a, **_k: None)
    os.unlink(rig.klipper / "scripts" / "spi_flash" / "fatfs.so")
    plan = rig.plan()
    assert plan.blocked and any("fatfs.so" in c.detail for c in plan.all_checks() if c.status == BLOCK)
    rig.opt.sd_tooling = False          # a pure backup (stage 1 on the stock system) does not need it
    assert not rig.plan().blocked


def test_missing_spi_flash_tooling_falls_back_to_the_microsd_card_when_offered(rig, monkeypatch, tmp_path):
    import eitri_mcuflash.methods as methods
    monkeypatch.setattr(methods.shutil, "which", lambda *_a, **_k: None)
    os.unlink(rig.klipper / "scripts" / "spi_flash" / "fatfs.so")
    card = tmp_path / "card"
    card.mkdir()
    rig.ctx.sd_mount = str(card)
    plan = rig.plan()
    assert not plan.blocked
    assert [b.method for b in plan.boards if b.board.name == "mainboard"] == ["sdcard-file"]
