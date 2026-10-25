import copy

import pytest

from fakes import DEFAULT_STATUS
from klarity.protocol import TOUCH_TAP
from klarity.config import packaged_map_path
from klarity.screenmap import MapError, ScreenMap
from klarity.state import PrinterState


def make_state(**overrides) -> PrinterState:
    st = PrinterState()
    st.set_connection(True)
    data = copy.deepcopy(DEFAULT_STATUS)
    for obj, fields in overrides.items():
        data[obj.replace("__", " ")].update(fields)
    st.apply(data)
    st.set_klippy("ready")
    return st


MAP = {
    "version": 1,
    "pages": {
        "home": {
            "id": 1,
            "widgets": {
                "temp": {
                    "obj": "t_noz",
                    "bind": {
                        "source": ["extruder.temperature", "extruder.target"],
                        "format": "{0:.0f} / {1:.0f}",
                    },
                },
                "pause": {
                    "id": 10,
                    "obj": "b_pause",
                    "bind": {"source": "derived.state", "attr": "vis", "test": {"eq": "printing"}},
                    "on_release": [
                        {"print": "pause", "guard": {"source": "derived.state", "eq": "printing"}}
                    ],
                    "on_press": [{"beep": 20}],
                },
                "pending": {
                    "id": 99,
                    "todo": True,
                    "on_release": [
                        {
                            "print": "cancel",
                            "guard": {"source": "derived.state", "in": ["printing", "paused"]},
                        }
                    ],
                },
            },
        },
        "other": {
            "id": 2,
            "todo": True,
            "widgets": {"w": {"id": 1, "on_release": [{"gcode": "G28"}]}},
        },
    },
    "markers": {0x91: {"name": "home", "page": "home"}, 0xF9: {"todo": True}},
}


def load(**kw) -> ScreenMap:
    return ScreenMap.from_dict(copy.deepcopy(MAP), **kw)


def test_packaged_map_loads_and_is_partial():
    """The shipped map holds the stock panel's ids; unconfirmed entries stay flagged todo."""
    m = ScreenMap.load(packaged_map_path())
    todo, total = m.todo_summary()
    assert total > 400 and 0 < todo < total
    assert m.has_active_content
    assert m.page_by_id(3).key == "home"
    assert m.page_by_id(0x11).key == "printing"


def test_packaged_map_destructive_actions_are_guarded():
    """Whatever is live in the shipped map must not be able to hurt a running print."""
    m = ScreenMap.load(packaged_map_path())
    live = [a for acts in (*m._touch.values(), *m._number.values()) for a in acts]
    assert live
    for a in live:
        if a.verb == "print":
            assert a.guard is not None, f"unguarded print control {a.arg}"
            assert a.arg != "emergency_stop"
    for a in live:
        if a.verb == "gcode" and any(w in a.arg for w in ("RESTART", "G28", "M84")):
            assert a.guard is not None, f"unguarded {a.arg!r}"


def test_packaged_map_cancel_needs_the_confirm_page():
    m = ScreenMap.load(packaged_map_path())
    confirm = m.pages["cancel_confirm"]
    assert [a.arg for a in confirm.widgets["confirm"].on_release] == ["cancel"]
    # The cancel button on the print page only opens the confirmation page.
    stop = m.pages["printing"].widgets["stop"]
    assert [(a.verb, a.arg) for a in stop.on_release] == [("page", "cancel_confirm")]


def test_packaged_map_with_allow_todo_exposes_core_functions():
    m = ScreenMap.load(packaged_map_path(), allow_todo=True)
    assert m.has_active_content
    verbs = {a.arg for acts in m._touch.values() for a in acts if a.verb == "print"}
    assert {"pause", "resume", "cancel"} <= verbs
    assert {"extruder", "heater_bed"} <= m.required_objects()
    assert "heater_generic chamber" in m.required_objects()


def test_touch_index_and_todo_gating():
    m = load()
    assert m.actions_for_touch(1, 10, 0)[0].verb == "print"
    assert m.actions_for_touch(1, 10, 1)[0].verb == "beep"
    assert m.actions_for_touch(1, 10, TOUCH_TAP)[0].verb == "print"  # a stock tap: release first
    assert m.actions_for_touch(1, 99, 0) is None  # todo widget
    assert m.actions_for_touch(2, 1, 0) is None  # todo page
    assert m.is_known_touch(1, 10) and not m.is_known_touch(1, 99)
    allowed = load(allow_todo=True)
    assert allowed.actions_for_touch(1, 99, 0) is not None
    assert allowed.actions_for_touch(2, 1, 0) is not None


def test_markers_gated_by_todo():
    m = load()
    assert m.marker(0x91).page_key == "home"
    assert m.marker(0xF9) is None
    assert load(allow_todo=True).marker(0xF9) is not None


def test_push_items_only_for_page_and_widgets_with_obj():
    m = load()
    items = m.push_items(1)
    assert [i.obj for i in items] == ["t_noz", "b_pause"]
    assert m.push_items(2) == [] and m.push_items(None) == []


def test_binding_format_multi_source():
    st = make_state(extruder={"temperature": 204.6, "target": 210.0})
    item = load().push_items(1)[0]
    assert item.binding.render(st) == "205 / 210"


def test_binding_vis_test():
    m = load()
    vis = m.push_items(1)[1].binding
    assert vis.render(make_state(print_stats={"state": "printing"})) == 1
    assert vis.render(make_state()) == 0


def test_binding_missing_source_is_skipped_and_default_applies():
    st = PrinterState()
    b = load().push_items(1)[0].binding
    assert b.render(st) is None
    raw = copy.deepcopy(MAP)
    raw["pages"]["home"]["widgets"]["temp"]["bind"] = {"source": "nope.value", "default": "--"}
    b = ScreenMap.from_dict(raw).push_items(1)[0].binding
    assert b.render(st) == "--"


def test_binding_map_colors_scale_and_default():
    raw = copy.deepcopy(MAP)
    raw["pages"]["home"]["widgets"]["temp"]["bind"] = [
        {
            "source": "derived.state",
            "attr": "pco",
            "map": {"printing": "#00ff00", "_default": "#ffffff"},
        },
        {"source": "virtual_sdcard.progress", "attr": "val", "scale": 100},
        {"source": "derived.state", "map": {"printing": "Go"}, "default": "?"},
    ]
    m = ScreenMap.from_dict(raw)
    colour, progress, text = (i.binding for i in m.push_items(1)[:3])
    assert colour.render(make_state(print_stats={"state": "printing"})) == 2016
    assert colour.render(make_state()) == 65535
    assert progress.render(make_state(virtual_sdcard={"progress": 0.456})) == 46
    assert text.render(make_state()) == "?"


def test_required_objects_include_guards_and_bindings():
    m = load()
    assert m.required_objects() == {"extruder"}


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(version=2), "version"),
        (lambda d: d.update(bogus=1), "unknown key"),
        (lambda d: d["pages"]["home"].update(id=300), "0..255"),
        (lambda d: d["pages"]["other"].update(id=1), "already used"),
        (lambda d: d["pages"]["home"]["widgets"]["pause"].update(id=99), "already used"),
        (
            lambda d: d["pages"]["home"]["widgets"]["pause"].update(
                on_release=[{"gcode": "G28", "print": "pause"}]
            ),
            "exactly one",
        ),
        (
            lambda d: d["pages"]["home"]["widgets"]["pause"].update(
                on_release=[{"print": "explode"}]
            ),
            "must be one of",
        ),
        (
            lambda d: d["pages"]["home"]["widgets"]["pause"].update(
                on_release=[{"page": "nowhere"}]
            ),
            "unknown page",
        ),
        (
            lambda d: d["pages"]["home"]["widgets"]["pause"].update(
                on_release=[{"print": "start"}]
            ),
            "needs a 'file'",
        ),
        (lambda d: d["pages"]["home"]["widgets"]["temp"].pop("obj"), "requires 'obj'"),
        (
            lambda d: d["pages"]["home"]["widgets"]["temp"]["bind"].update(format=None),
            "multiple sources",
        ),
        (lambda d: d["pages"]["home"]["widgets"]["temp"]["bind"].update(extra=1), "unknown key"),
        (
            lambda d: d["pages"]["home"]["widgets"]["pause"].update(
                on_release=[{"gcode": "G28", "guard": {"eq": 1}}]
            ),
            "'source' is required",
        ),
        (
            lambda d: d["pages"]["home"]["widgets"]["pause"].update(
                on_release=[{"gcode": "G28", "cooldown": -1}]
            ),
            "cooldown",
        ),
        (lambda d: d["markers"].update({300: {}}), "0..254"),
        (lambda d: d["markers"][0x91].update(page="ghost"), "unknown page"),
        (lambda d: d.update(pages={}), "at least one page"),
    ],
)
def test_validation_errors(mutate, message):
    raw = copy.deepcopy(MAP)
    mutate(raw)
    with pytest.raises(MapError, match=message):
        ScreenMap.from_dict(raw)


def test_load_reports_unreadable_and_invalid_yaml(tmp_path):
    with pytest.raises(MapError, match="cannot read"):
        ScreenMap.load(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: [")
    with pytest.raises(MapError, match="cannot read"):
        ScreenMap.load(bad)


# ------------------------------------------------------------------ print-verb guard rules


@pytest.mark.parametrize("verb", ["cancel", "pause", "resume", "restart", "firmware_restart"])
def test_destructive_print_verbs_require_a_guard(verb):
    raw = copy.deepcopy(MAP)
    raw["pages"]["home"]["widgets"]["pending"]["on_release"] = [{"print": verb}]
    with pytest.raises(MapError, match="needs a 'guard'"):
        ScreenMap.from_dict(raw)


def test_print_start_requires_a_guard():
    raw = copy.deepcopy(MAP)
    raw["pages"]["home"]["widgets"]["pending"]["on_release"] = [
        {"print": "start", "file": "a.gcode"}
    ]
    with pytest.raises(MapError, match="needs a 'guard'"):
        ScreenMap.from_dict(raw)


def test_emergency_stop_needs_guard_or_explicit_unguarded():
    raw = copy.deepcopy(MAP)
    raw["pages"]["home"]["widgets"]["pending"]["on_release"] = [{"print": "emergency_stop"}]
    with pytest.raises(MapError, match="unguarded: true"):
        ScreenMap.from_dict(raw)
    raw["pages"]["home"]["widgets"]["pending"]["on_release"] = [
        {"print": "emergency_stop", "unguarded": "yes"}
    ]
    with pytest.raises(MapError, match="unguarded"):
        ScreenMap.from_dict(raw)
    raw["pages"]["home"]["widgets"]["pending"]["on_release"] = [
        {"print": "emergency_stop", "unguarded": True}
    ]
    ScreenMap.from_dict(raw)  # explicit acknowledgement is accepted


def test_todo_estop_widget_stays_inert_even_with_allow_todo():
    raw = copy.deepcopy(MAP)
    raw["pages"]["home"]["widgets"]["estop"] = {
        "id": 77,
        "todo": True,
        "on_release": [{"print": "emergency_stop", "unguarded": True}],
    }
    assert ScreenMap.from_dict(raw, allow_todo=True).actions_for_touch(1, 77, 0) is None
    raw["pages"]["home"]["widgets"]["estop"]["todo"] = False  # confirmed: armed
    assert ScreenMap.from_dict(raw).actions_for_touch(1, 77, 0) is not None


def test_todo_estop_marker_stays_inert_even_with_allow_todo():
    raw = copy.deepcopy(MAP)
    raw["markers"][0xFA] = {
        "todo": True,
        "do": [{"print": "emergency_stop", "unguarded": True}],
    }
    assert ScreenMap.from_dict(raw, allow_todo=True).marker(0xFA) is None


# --------------------------------------------------------------------- format validation


@pytest.mark.parametrize(
    "fmt, nsources",
    [
        ("{0.foo}", 1),
        ("{0[1]}", 1),
        ("{name}", 1),
        ("{1}", 1),
        ("{} {}", 1),
        ("{", 1),
        ("{0!x}", 1),
    ],
)
def test_bad_binding_formats_rejected_at_load(fmt, nsources):
    raw = copy.deepcopy(MAP)
    src = ["extruder.temperature", "extruder.target"][:nsources]
    raw["pages"]["home"]["widgets"]["temp"]["bind"] = {"source": src, "format": fmt}
    with pytest.raises(MapError, match="format"):
        ScreenMap.from_dict(raw)


def test_binding_render_never_raises_on_odd_state_values():
    m = load()
    st = make_state()
    item = m.push_items(1)[0]
    # An unhashable state value through a value map and a type-mismatched format spec.
    b = item.binding
    object.__setattr__(b, "format", "{0:.0f} / {1:d}")
    st.data["extruder"]["temperature"] = "hot"
    assert b.render(st) is None
