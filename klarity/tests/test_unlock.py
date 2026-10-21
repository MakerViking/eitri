"""The "Unlock setup" action: config, map, helper commands, guards, panel text, end to end."""

import asyncio
import contextlib
import copy
import json
import stat
from pathlib import Path

import pytest

from fakes import eventually
from helpers import TEST_MAP, fast_config, ready_state
from klarity.actions import ActionError, ActionRunner, _fmt, _Trusted
from klarity.config import ConfigError, KlarityConfig, UnlockConfig, parse_config
from klarity.daemon import Klarity
from klarity.screenmap import MapError, ScreenMap
from klarity.transport import MemoryTransport
from klarity.unlock import SetupUnlocker, UnlockError, run_command

FF3 = b"\xff\xff\xff"
NETWORK_PAGE, UNLOCK_WIDGET = 62, 9


#: Sandbox options that systemd implements with seccomp. For a unit whose user lacks CAP_SYS_ADMIN, systemd then sets
#: NoNewPrivs on the process even with NoNewPrivileges=false (measured with systemd 258 on the QEMU image), and sudo
#: refuses to run. Units that call sudo must not use any of them.
SECCOMP_OPTIONS = {
    "ProtectKernelTunables", "ProtectKernelModules", "RestrictNamespaces", "LockPersonality", "RestrictAddressFamilies",
    "MemoryDenyWriteExecute", "SystemCallFilter", "SystemCallArchitectures", "RestrictRealtime", "RestrictSUIDSGID",
    "ProtectClock", "ProtectHostname", "ProtectKernelLogs",
}

def touch(page: int, widget: int, typ: int = 0) -> bytes:
    return bytes([0x65, page, widget, typ]) + FF3


def write_pairing(path: Path, state: str = "pairing", code: str | None = "1234-5678") -> None:
    path.write_text(json.dumps({"state": state, "code": code, "updated": 1}), encoding="utf-8")


class FakeSpawn:
    """Stands in for sudo/systemctl: records argv, optionally publishes the pairing file."""

    def __init__(self, pairing: Path | None = None, *, code: str = "1234-5678") -> None:
        self.calls: list[list[str]] = []
        self.pairing = pairing
        self.code = code
        self.results: dict[str, tuple[int, str]] = {}

    async def __call__(self, argv: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        for key, result in self.results.items():
            if key in argv:
                return result
        if "unlock" in argv and self.pairing is not None:
            write_pairing(self.pairing, "pairing", self.code)
        return 0, ""


def make_unlocker(tmp_path: Path, spawn: FakeSpawn | None = None, **cfg_kw) -> SetupUnlocker:
    cfg = UnlockConfig(pairing_file=str(tmp_path / "pairing.json"), wait=0.3, **cfg_kw)
    return SetupUnlocker(
        cfg,
        spawn=spawn or FakeSpawn(tmp_path / "pairing.json"),
        address=lambda: "192.168.0.5",
        hostname=lambda: "eitri",
    )


# ------------------------------------------------------------------------------ config


def test_unlock_config_defaults_use_the_documented_helper():
    cfg = KlarityConfig().unlock
    assert cfg.enabled
    assert cfg.command == "/usr/bin/sudo -n /usr/bin/eitri-configurator unlock"
    assert cfg.pairing_file == "/data/eitri/configurator/pairing.json"
    assert cfg.port == 8099


def test_shipped_klarity_cfg_parses_and_matches_defaults():
    text = (Path(__file__).parent.parent / "config" / "klarity.cfg").read_text()
    shipped, defaults = parse_config(text).unlock, UnlockConfig()
    assert shipped == defaults


def test_unlock_section_overrides_and_validation():
    cfg = parse_config("[unlock]\nenabled: no\nhost: eitri.local\nport: 80\nwait: 3\n")
    assert (cfg.unlock.enabled, cfg.unlock.host, cfg.unlock.port, cfg.unlock.wait) == (
        False,
        "eitri.local",
        80,
        3.0,
    )
    for bad in ("port: 0", "port: 70000", "timeout: 0", "wait: -1", "nope: 1"):
        with pytest.raises(ConfigError):
            parse_config(f"[unlock]\n{bad}\n")


# --------------------------------------------------------------------------------- map


def test_shipped_map_exposes_unlock_on_the_network_page_with_a_state_guard():
    smap = ScreenMap.load(KlarityConfig().map_path)
    actions = smap.actions_for_touch(NETWORK_PAGE, UNLOCK_WIDGET, 0)
    assert actions is not None, "unlock_setup must be a live (non-todo) widget"
    verbs = [a.verb for a in actions]
    assert verbs == ["unlock", "set"]
    unlock = actions[0]
    assert unlock.guard is not None and unlock.guard.source == "derived.state"
    assert unlock.guard.op == "not_in" and set(unlock.guard.arg) == {"printing", "paused"}
    assert actions[1].arg == {"obj": "t7", "attr": "txt", "value": "{message}"}
    assert actions[1].guard == unlock.guard  # a blocked unlock defines no {message}


def test_map_rejects_unguarded_or_unknown_unlock():
    def one(action):
        raw = copy.deepcopy(TEST_MAP)
        raw["pages"]["home"]["widgets"]["to_move"]["on_release"] = [action]
        return ScreenMap.from_dict(raw)

    guard = {"source": "derived.state", "not_in": ["printing"]}
    one({"unlock": "setup", "guard": guard})
    with pytest.raises(MapError, match="needs a 'guard'"):
        one({"unlock": "setup"})
    with pytest.raises(MapError, match="only 'setup'"):
        one({"unlock": "root", "guard": guard})


# ------------------------------------------------------------------------ SetupUnlocker


async def test_unlock_runs_helper_then_starts_configurator_and_returns_code(tmp_path):
    spawn = FakeSpawn(tmp_path / "pairing.json", code="8765-4321")
    result = await make_unlocker(tmp_path, spawn).unlock()
    assert spawn.calls == [
        ["/usr/bin/sudo", "-n", "/usr/bin/eitri-configurator", "unlock"],
        ["/usr/bin/systemctl", "--no-block", "start", "eitri-configurator.service"],
    ]
    assert (result.code, result.host, result.url) == (
        "8765-4321",
        "192.168.0.5:8099",
        "http://192.168.0.5:8099",
    )


async def test_unlock_waits_until_the_configurator_publishes_the_code(tmp_path):
    pairing = tmp_path / "pairing.json"
    write_pairing(pairing, "locked", None)
    spawn = FakeSpawn()  # does not publish by itself

    async def publish_later():
        await asyncio.sleep(0.1)
        write_pairing(pairing, "pairing", "1111-2222")

    task = asyncio.create_task(publish_later())
    result = await make_unlocker(tmp_path, spawn).unlock()
    await task
    assert result.code == "1111-2222"


async def test_unlock_times_out_when_the_code_never_appears(tmp_path):
    write_pairing(tmp_path / "pairing.json", "locked", None)
    with pytest.raises(UnlockError, match="did not publish a pairing code") as err:
        await make_unlocker(tmp_path, FakeSpawn()).unlock()
    assert "locked" in str(err.value)


async def test_helper_failure_is_an_error_and_never_starts_the_configurator(tmp_path):
    spawn = FakeSpawn(tmp_path / "pairing.json")
    spawn.results["unlock"] = (1, "sudo: a password is required")
    with pytest.raises(UnlockError, match="status 1"):
        await make_unlocker(tmp_path, spawn).unlock()
    assert len(spawn.calls) == 1


async def test_failing_start_is_not_fatal_when_the_code_is_there(tmp_path):
    spawn = FakeSpawn(tmp_path / "pairing.json")
    spawn.results["start"] = (4, "Access denied")
    assert (await make_unlocker(tmp_path, spawn).unlock()).code == "1234-5678"


async def test_empty_start_command_skips_it(tmp_path):
    spawn = FakeSpawn(tmp_path / "pairing.json")
    await make_unlocker(tmp_path, spawn, start_command="").unlock()
    assert len(spawn.calls) == 1


async def test_disabled_never_runs_anything(tmp_path):
    spawn = FakeSpawn(tmp_path / "pairing.json")
    with pytest.raises(UnlockError, match="disabled"):
        await make_unlocker(tmp_path, spawn, enabled=False).unlock()
    assert spawn.calls == []


@pytest.mark.parametrize(
    "doc",
    [
        "not json",
        "[]",
        '{"state": "pairing", "code": null}',
        '{"state": "pairing", "code": "12345678"}',
        '{"state": "pairing", "code": "1234-5678\\"; reboot"}',
        '{"state": "pairing", "code": 12345678}',
    ],
)
def test_malformed_pairing_files_never_yield_a_code(tmp_path, doc):
    (tmp_path / "pairing.json").write_text(doc)
    state, code = make_unlocker(tmp_path).read_pairing()
    assert code is None


def test_missing_pairing_file_is_unknown(tmp_path):
    assert make_unlocker(tmp_path).read_pairing() == ("unknown", None)


def test_commands_are_split_not_shell_interpreted(tmp_path):
    u = make_unlocker(tmp_path, command="/usr/bin/sudo -n /x; reboot && id")
    assert u._argv(u.cfg.command, "command") == ["/usr/bin/sudo", "-n", "/x;", "reboot", "&&", "id"]
    with pytest.raises(UnlockError):
        u._argv("'unterminated", "command")
    with pytest.raises(UnlockError):
        u._argv("   ", "command")


def test_printer_host_choices(tmp_path):

    def make(addr, name="eitri-1.lan", **kw):
        return SetupUnlocker(UnlockConfig(**kw), address=lambda: addr, hostname=lambda: name)

    assert make("10.0.0.7").printer_host() == "10.0.0.7:8099"
    assert make(None).printer_host() == "eitri-1.local:8099"  # no route: mDNS name
    assert make("10.0.0.7", host="printer.local", port=80).printer_host() == "printer.local:80"
    with pytest.raises(UnlockError):
        make(None, name="").printer_host()
    with pytest.raises(UnlockError):
        make("10.0.0.7", host='x"; rm').printer_host()


# --------------------------------------------------------------- real subprocess runner


def script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "stub.sh"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


async def test_run_command_captures_status_and_output(tmp_path):
    rc, out = await run_command([script(tmp_path, "echo hi; echo oops >&2; exit 3")], 5)
    assert rc == 3 and "hi" in out and "oops" in out


async def test_run_command_reports_missing_binary_and_timeout(tmp_path):
    with pytest.raises(UnlockError, match="cannot run"):
        await run_command([str(tmp_path / "does-not-exist")], 5)
    with pytest.raises(UnlockError, match="timed out"):
        await run_command([script(tmp_path, "sleep 30")], 0.2)


async def test_unlock_with_real_subprocess_stub(tmp_path):
    """The default spawn, against a stub that behaves like `sudo -n eitri-configurator unlock`."""
    log = tmp_path / "calls"
    pairing = tmp_path / "pairing.json"
    stub = script(
        tmp_path,
        f'echo "$@" >> {log}\n'
        f'[ "$1" = unlock ] && echo \'{{"state":"pairing","code":"4242-1717"}}\' > {pairing}\n'
        "exit 0\n",
    )
    cfg = UnlockConfig(
        command=f"{stub} unlock",
        start_command=f"{stub} start",
        pairing_file=str(pairing),
        wait=2,
    )
    result = await SetupUnlocker(cfg, address=lambda: "192.168.1.9").unlock()
    assert result.code == "4242-1717" and result.url == "http://192.168.1.9:8099"
    assert log.read_text().split("\n")[:2] == ["unlock", "start"]


# ------------------------------------------------------------------ ActionRunner wiring


class StubScreen:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, command):
        self.sent.append(command)


_IDLE = {"source": "derived.state", "not_in": ["printing", "paused"]}
UNLOCK_ACTIONS = [
    {
        "unlock": "setup",
        "guard": {"source": "derived.state", "not_in": ["printing", "paused"]},
        "else_beep": 80,
    },
    {"set": {"obj": "t7", "attr": "txt", "value": "{message}"}, "guard": _IDLE},
    {"set": {"obj": "t6", "attr": "txt", "value": "{url}"}, "guard": _IDLE},
]


def runner_with(tmp_path, *, state=None, dry_run=False, spawn=None, **cfg_kw):
    raw = copy.deepcopy(TEST_MAP)
    raw["pages"]["home"]["widgets"]["to_move"]["on_release"] = UNLOCK_ACTIONS
    smap = ScreenMap.from_dict(raw)
    screen = StubScreen()
    unlocker = make_unlocker(tmp_path, spawn, **cfg_kw)
    runner = ActionRunner(
        screen, None, state or ready_state(), smap, dry_run=dry_run, unlocker=unlocker
    )
    return runner, screen, smap.actions_for_touch(1, 14, 0)


async def test_unlock_action_writes_code_and_url_into_panel_text(tmp_path):
    runner, screen, actions = runner_with(tmp_path)
    await runner.run(actions, {"page": 1, "widget": 14})
    assert screen.sent == [
        't7.txt="Code 1234-5678 at 192.168.0.5:8099"',
        't6.txt="http://192.168.0.5:8099"',
    ]


async def test_unlock_action_failure_shows_a_message_and_beeps(tmp_path):
    spawn = FakeSpawn(tmp_path / "pairing.json")
    spawn.results["unlock"] = (1, "")
    runner, screen, actions = runner_with(tmp_path, spawn=spawn)
    await runner.run(actions, {})
    assert screen.sent == ["beep 300", 't7.txt="Unlock failed - see the log"', 't6.txt=""']


@pytest.mark.parametrize("busy", ["printing", "paused"])
async def test_unlock_is_refused_during_a_print_by_guard_and_by_runner(tmp_path, busy):
    spawn = FakeSpawn(tmp_path / "pairing.json")
    state = ready_state()
    state.apply({"print_stats": {"state": busy}})
    runner, screen, actions = runner_with(tmp_path, state=state, spawn=spawn)
    await runner.run(actions, {})
    # The unlock action beeps; the guarded text actions after it stay silent.
    assert spawn.calls == [] and screen.sent == ["beep 80"]

    # Even a map whose guard was loosened cannot unlock mid-print.
    loose = copy.deepcopy(actions[0])
    object.__setattr__(loose, "guard", None)
    screen.sent.clear()
    await runner.run((loose, actions[1]), {})
    assert spawn.calls == [] and screen.sent == ["beep 300"]


async def test_unlock_during_a_print_leaves_the_pairing_state_untouched(tmp_path):
    state = ready_state()
    state.apply({"print_stats": {"state": "printing"}})
    runner, screen, actions = runner_with(tmp_path, state=state)
    await runner.run(actions, {})
    assert not (tmp_path / "pairing.json").exists()


async def test_dry_run_only_shows_an_already_open_pairing(tmp_path):
    spawn = FakeSpawn(tmp_path / "pairing.json")
    runner, screen, actions = runner_with(tmp_path, dry_run=True, spawn=spawn)
    await runner.run(actions, {})
    assert spawn.calls == []
    assert 't7.txt="Unlock failed - see the log"' in screen.sent  # (--dry-run is silent: no beeps)

    screen.sent.clear()
    write_pairing(tmp_path / "pairing.json")
    await runner.run(actions, {})
    assert screen.sent[0] == 't7.txt="Code 1234-5678 at 192.168.0.5:8099"'
    assert spawn.calls == []


async def test_runner_without_an_unlocker_fails_safely(tmp_path):
    runner, screen, actions = runner_with(tmp_path)
    runner.unlocker = None
    await runner.run(actions, {})
    assert 't7.txt="Unlock failed - see the log"' in screen.sent


def test_unsafe_text_filter_still_applies_to_panel_input():
    """Only Klarity's own values are exempt; text read from the panel keeps the allow-list."""
    with pytest.raises(ActionError, match="unsafe text"):
        _fmt.vformat("{name}", (), {"name": "a:b"})
    assert _fmt.vformat("{name}", (), {"name": _Trusted("a:b")}) == "a:b"


# --------------------------------------------------------------------------- end to end


async def test_tapping_the_stock_button_unlocks_and_paints_the_code(moonraker, tmp_path):
    pairing = tmp_path / "pairing.json"
    write_pairing(pairing, "locked", None)
    log = tmp_path / "calls"
    stub = script(
        tmp_path,
        f'echo "$@" >> {log}\n'
        f'[ "$1" = unlock ] && echo \'{{"state":"pairing","code":"9090-1212"}}\' > {pairing}\n'
        "exit 0\n",
    )
    cfg = fast_config(moonraker.url)
    cfg.unlock = UnlockConfig(
        command=f"{stub} unlock", start_command=f"{stub} start", pairing_file=str(pairing), wait=2
    )
    transport = MemoryTransport()
    daemon = Klarity(
        cfg,
        transport=transport,
        unlocker=SetupUnlocker(cfg.unlock, address=lambda: "192.168.0.77"),
    )
    task = asyncio.create_task(daemon.run())
    try:
        await eventually(lambda: transport.is_open and "sendme" in transport.commands())
        await eventually(lambda: daemon.state.data.get("extruder"), what="moonraker state")
        transport.clear()
        transport.inject(touch(NETWORK_PAGE, UNLOCK_WIDGET))
        await eventually(
            lambda: 't7.txt="Code 9090-1212 at 192.168.0.77:8099"' in transport.commands(),
            what="pairing code on the panel",
        )
        assert log.read_text().split() == ["unlock", "start"]
        assert moonraker.scripts() == []  # nothing was sent to Klipper
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_tapping_during_a_print_does_nothing_but_beep(moonraker, tmp_path):
    log = tmp_path / "calls"
    stub = script(tmp_path, f'echo "$@" >> {log}\nexit 0\n')
    cfg = fast_config(moonraker.url)
    cfg.unlock = UnlockConfig(
        command=f"{stub} unlock",
        start_command="",
        pairing_file=str(tmp_path / "pairing.json"),
        wait=0.2,
    )
    transport = MemoryTransport()
    daemon = Klarity(cfg, transport=transport)
    task = asyncio.create_task(daemon.run())
    try:
        await eventually(lambda: transport.is_open and "sendme" in transport.commands())
        await moonraker.push_status({"print_stats": {"state": "printing"}})
        await eventually(lambda: daemon.state.resolve("derived.state") == "printing")
        transport.clear()
        transport.inject(touch(NETWORK_PAGE, UNLOCK_WIDGET))
        await eventually(lambda: "beep 80" in transport.commands())
        await daemon.wait_idle()
        assert not log.exists()
        assert not any(c.startswith("t7.txt") for c in transport.commands())
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ----------------------------------------------------------------------------- the unit


def test_klarity_unit_can_run_the_unlock_helper_and_matches_the_seed_layout():
    unit = (Path(__file__).parent.parent / "systemd" / "klarity.service").read_text()
    lines = dict(
        ln.split("=", 1) for ln in unit.splitlines() if "=" in ln and not ln.startswith("#")
    )
    # sudo is setuid: NoNewPrivileges would block it, and so does every option that systemd implements with seccomp
    # (it then sets NoNewPrivs anyway, measured on the QEMU image).
    assert lines["NoNewPrivileges"] == "false"
    assert not SECCOMP_OPTIONS & set(lines)
    assert "/data/eitri/configurator" in lines["ReadWritePaths"].split()
    # The venv location is the data-seed layout (seed/SPEC.md).
    assert lines["ExecStart"].startswith("/data/eitri/klarity/venv/bin/klarity ")
    assert lines["ConditionPathExists"] in (
        "/data/eitri/klarity/venv/bin/klarity",
        "/data/printer_data/config/klarity.cfg",
    )
