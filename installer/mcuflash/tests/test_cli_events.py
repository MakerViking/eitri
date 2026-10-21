# SPDX-License-Identifier: Apache-2.0
import io
import json
import os

import pytest

from helpers import settle
from test_runner import NEW, STOCK_VERSION, Rig
from eitri_mcuflash import cli
from eitri_mcuflash.events import EventSink
from eitri_mcuflash.methods import KatapultMethod, SdcardFileMethod
from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.proc import RunResult


def run_cli(rig, args, capsys, stdin_tty=False, settled=True):
    def hook(ctx):
        if settled:
            settle(ctx.registry)      # the shipped registry keeps the toolhead unresolved; see test_params.py
        ctx.sleep, ctx.clock = rig.clock.sleep, rig.clock
        ctx.identify_timeout = 0.1
        ctx.exists_fn, ctx.users_fn = rig.bus.exists, rig.bus.users

    common = ["--firmware-root", str(rig.tmp / "fw"), "--state-dir", str(rig.tmp / "state"), "--klipper-dir", str(rig.klipper),
              "--printer-cfg", str(rig.tmp / "printer.cfg"), "--stock-bin", "mainboard=%s" % (rig.tmp / "qd_mcu_stock.bin")]
    code = cli.main(args[:1] + common + args[1:], runner=rig.runner, opener=rig.bus.opener, context_hook=hook)
    out = capsys.readouterr()
    return code, out.out, out.err


def events_of(out):
    return [json.loads(l) for l in out.splitlines() if l.strip()]


def test_plan_json_stream(tmp_path, capsys):
    rig = Rig(tmp_path)
    code, out, err = run_cli(rig, ["plan", "--json"], capsys)
    assert code == 0 and err == ""
    evs = events_of(out)
    assert evs[0]["type"] == "step" and evs[0]["step"] == "probe"
    assert evs[-1]["type"] == "result" and evs[-1]["ok"] is True
    assert evs[-1]["data"]["boards"]["toolhead"]["method"] == "katapult"
    assert all(e["schema"] == 1 and "ts" in e for e in evs)


def test_flash_is_dry_run_by_default(tmp_path, capsys):
    rig = Rig(tmp_path)
    snap = bytes(rig.tool.flash)
    code, out, _ = run_cli(rig, ["flash", "--json"], capsys)
    assert code == 0 and bytes(rig.tool.flash) == snap
    assert events_of(out)[-1]["data"]["dry_run"] is True


def test_apply_without_yes_in_json_mode_is_refused(tmp_path, capsys):
    rig = Rig(tmp_path)
    snap = bytes(rig.tool.flash)
    code, out, err = run_cli(rig, ["flash", "--json", "--apply"], capsys)
    assert code == 2 and "--yes" in err and bytes(rig.tool.flash) == snap


def test_apply_yes_flashes_and_exit_codes(tmp_path, capsys):
    rig = Rig(tmp_path)
    code, out, _ = run_cli(rig, ["flash", "--json", "--apply", "--yes"], capsys)
    assert code == 0 and rig.tool.app_dictionary()["version"] == NEW
    # now blocked: host/firmware mismatch -> exit 3 and nothing touched
    rig2 = Rig(tmp_path / "second") if (tmp_path / "second").mkdir() is None else None
    rig2.host_version = "v0.13.0-1-g1111111"
    code, out, _ = run_cli(rig2, ["flash", "--json", "--apply", "--yes"], capsys)
    assert code == 3 and rig2.tool.app_dictionary()["version"] == STOCK_VERSION


def test_stop_services_wraps_the_run(tmp_path, capsys):
    rig = Rig(tmp_path)
    run_cli(rig, ["plan", "--json", "--stop-services"], capsys)
    sysctl = [c for c in rig.calls if c[0] == "systemctl"]
    assert ["systemctl", "stop", "klipper.service"] in sysctl and ["systemctl", "start", "klipper.service"] in sysctl
    assert sysctl.index(["systemctl", "stop", "klipper.service"]) < sysctl.index(["systemctl", "start", "klipper.service"])


def test_bad_assign_is_a_usage_error(tmp_path, capsys):
    rig = Rig(tmp_path)
    with pytest.raises(SystemExit):
        run_cli(rig, ["plan", "--assign", "oops"], capsys)


def test_rollback_dry_run_and_apply(tmp_path, capsys):
    rig = Rig(tmp_path)
    run_cli(rig, ["flash", "--json", "--apply", "--yes"], capsys)
    code, out, _ = run_cli(rig, ["rollback", "toolhead", "--json"], capsys)
    assert code == 0 and rig.tool.app_dictionary()["version"] == NEW  # dry run
    code, out, _ = run_cli(rig, ["rollback", "toolhead", "--json", "--apply", "--yes"], capsys)
    assert code == 0 and rig.tool.app_dictionary()["version"] == STOCK_VERSION


def test_backups_listing(tmp_path, capsys):
    rig = Rig(tmp_path)
    run_cli(rig, ["flash", "--json", "--apply", "--yes"], capsys)
    code, out, _ = run_cli(rig, ["backups", "--json"], capsys)
    res = events_of(out)[-1]["data"]
    assert res["toolhead"][0]["restorable"] is True and res["toolhead"][0]["version"] == STOCK_VERSION


def test_event_sink_human_and_file(tmp_path):
    buf, human = io.StringIO(), io.StringIO()
    f = tmp_path / "ev.jsonl"
    sink = EventSink(json_out=buf, human_out=human, events_file=str(f))
    sink.step("flash", "start", board="toolhead")
    sink.progress("flash", 140, board="toolhead", msg="x")
    sink.warn("careful")
    sink.result(False, msg="no")
    sink.close()
    lines = [json.loads(l) for l in buf.getvalue().splitlines()]
    assert [l["type"] for l in lines] == ["step", "progress", "log", "result"]
    assert lines[1]["pct"] == 100  # clamped
    assert f.read_text().count("\n") == 4
    assert "WARNING: careful" in human.getvalue() and "FAILED" in human.getvalue()


def test_flashtool_backend_runs_script_and_checks_output(tmp_path):
    rig = Rig(tmp_path)
    tool = tmp_path / "flashtool.py"
    tool.write_text("# stub")
    rig.ctx.katapult_tool, rig.ctx.katapult_backend = str(tool), "flashtool"
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        if argv[-2:] == ["-c", "import serial"] or "import serial" in argv:
            return RunResult(0, "")
        return RunResult(0, "Flashing...\n[####]\nProgramming Complete")

    rig.ctx.runner = runner
    board = rig.ctx.registry.boards["toolhead"]
    m = KatapultMethod(rig.ctx, board, "/dev/ttyS2")
    rig.tool.mode = "katapult"
    with m.bootloader(already_in_bootloader=True) as client:
        m.flash_image(client, b"x" * 64, "/tmp/klipper.bin")
    argv = [a for a in seen if "flashtool.py" in " ".join(a)][0]
    assert argv[argv.index("-d") + 1] == "/dev/ttyS2" and argv[argv.index("-b") + 1] == "500000" and argv[-1] == "/tmp/klipper.bin"

    rig.ctx.runner = lambda argv, **kw: RunResult(0, "") if "import serial" in argv else RunResult(1, "Flash Tool Error")
    rig.tool.mode = "katapult"
    with m.bootloader(already_in_bootloader=True) as client:
        with pytest.raises(FlashError, match="flashtool failed"):
            m.flash_image(client, b"x" * 64, "/tmp/klipper.bin")


def test_sdcard_file_method_copies_and_checks(tmp_path):
    rig = Rig(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    rig.ctx.sd_mount = str(sd)
    src = tmp_path / "fw.bin"
    src.write_bytes(b"abc" * 100)
    dest = SdcardFileMethod(rig.ctx, rig.ctx.registry.boards["mainboard"], "/dev/ttyS0").stage(str(src))
    assert os.path.basename(dest) == "qd_mcu.bin" and open(dest, "rb").read() == src.read_bytes()
    rig.ctx.sd_mount = str(tmp_path / "missing")
    with pytest.raises(FlashError):
        SdcardFileMethod(rig.ctx, rig.ctx.registry.boards["mainboard"], "/dev/ttyS0").stage(str(src))
