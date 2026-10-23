"""qd_probe: the prepare_gcode hook runs once per probing session, fails closed."""
import pytest

import fakes
from fakes import FakeConfig, FakeGcmd, FakePrinter

mod = fakes.load_extra("qd_probe")


class FakeTemplate:
    def __init__(self, text):
        self.text = text

    def render(self, context=None):
        return self.text


class FakeGcodeMacro:
    def load_template(self, config, option, default=None):
        return FakeTemplate(config.get(option, default))


class FakeProbe:
    def __init__(self):
        self.sessions = 0

    def start_probe_session(self, gcmd):
        self.sessions += 1
        return "SESSION"


def build(**opts):
    printer = FakePrinter()
    printer.objects["gcode_macro"] = FakeGcodeMacro()
    probe = FakeProbe()
    printer.objects["probe"] = probe
    cfg = FakeConfig(opts, "qd_probe", printer)
    qp = mod.QdProbe(cfg)
    for cb in printer.handlers["klippy:connect"]:
        cb()
    return qp, printer, probe


def test_prepare_runs_before_session():
    qp, printer, probe = build(prepare_gcode="M117 wipe\nG1 Z5")
    res = probe.start_probe_session(FakeGcmd())        # the wrapped entry point
    assert res == "SESSION" and probe.sessions == 1
    assert printer.gcode.scripts == ["M117 wipe\nG1 Z5"]


def test_blank_prepare_is_a_noop():
    qp, printer, probe = build()                       # default prepare_gcode = ''
    probe.start_probe_session(FakeGcmd())
    assert printer.gcode.scripts == [] and probe.sessions == 1


def test_command_runs_the_snippet():
    qp, printer, probe = build(prepare_gcode="M117 hi")
    printer.gcode.commands["QD_PROBE_PREPARE"](FakeGcmd())
    assert printer.gcode.scripts == ["M117 hi"]


def test_disabled_skips_prepare_but_still_probes():
    qp, printer, probe = build(prepare_gcode="M117 x", enable=False)
    probe.start_probe_session(FakeGcmd())
    assert printer.gcode.scripts == [] and probe.sessions == 1


def test_missing_probe_fails_closed():
    printer = FakePrinter()
    printer.objects["gcode_macro"] = FakeGcodeMacro()
    cfg = FakeConfig({}, "qd_probe", printer)
    mod.QdProbe(cfg)
    with pytest.raises(fakes.ConfigError):
        for cb in printer.handlers["klippy:connect"]:
            cb()


def test_snippet_that_itself_probes_does_not_recurse():
    qp, printer, probe = build(prepare_gcode="PROBE")

    def recursive(script):
        printer.gcode.scripts.append(script)
        probe.start_probe_session(FakeGcmd())          # the snippet re-enters a session

    printer.gcode.run_script_from_command = recursive
    probe.start_probe_session(FakeGcmd())
    # prepare ran exactly once; the nested session did not re-run it
    assert printer.gcode.scripts == ["PROBE"] and probe.sessions == 2
