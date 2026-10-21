# SPDX-License-Identifier: Apache-2.0
import json

from helpers import Rig
from eitri_update import cli


def run_cli(rig, args, capsys):
    base = ["--config", "/nonexistent", "--data-root", rig.data, "--manifest-url", rig.channel_url(), "--key-dir", rig.signer.pub]
    code = cli.main(base + args, runner=rig.run, updater_factory=lambda cfg, events: __import__("helpers").Updater(
        cfg, events, runner=rig.run, rauc=rig.updater.rauc, bootctl=rig.bootctl, state=rig.state, reboot=rig._reboot,
        health_check=rig.health, sleep=lambda s: None))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_check_json(tmp_path, capsys):
    rig = Rig(tmp_path)
    code, out, _ = run_cli(rig, ["--json", "check"], capsys)
    events = [json.loads(l) for l in out.splitlines()]
    assert code == 0 and events[-1]["type"] == "result" and events[-1]["data"]["latest"] == "2026.10.0"
    assert all(e["component"] == "update" and e["schema"] == 1 for e in events)


def test_upgrade_requires_yes_without_a_terminal(tmp_path, capsys):
    rig = Rig(tmp_path)
    code, out, err = run_cli(rig, ["--json", "upgrade"], capsys)
    assert code == 2 and "--yes" in err and not rig.run.installs
    code, out, _ = run_cli(rig, ["--json", "upgrade", "--dry-run"], capsys)
    assert code == 0 and not rig.run.installs
    code, out, _ = run_cli(rig, ["--json", "upgrade", "--yes"], capsys)
    assert code == 0 and len(rig.run.installs) == 1 and rig.rebooted == 1


def test_failure_exit_codes_and_result_event(tmp_path, capsys):
    rig = Rig(tmp_path, installed="2026.10.0")
    code, out, _ = run_cli(rig, ["--json", "upgrade", "--yes"], capsys)
    last = json.loads(out.splitlines()[-1])
    assert code == 3 and last["ok"] is False and last["data"]["code"] == "up_to_date"


def test_status_and_confirm(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.bootctl.update(eitri_state_A="unknown")
    code, out, _ = run_cli(rig, ["--json", "confirm"], capsys)
    assert code == 0 and rig.boot_env()["eitri_state_A"] == "good"
    code, out, _ = run_cli(rig, ["--json", "status"], capsys)
    data = json.loads(out.splitlines()[-1])["data"]
    assert data["installed"] == "2026.09.0" and data["booted_slot"] == "A" and data["flavor"] == "klipper"
