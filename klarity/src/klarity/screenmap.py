"""Data-driven screen map: which widget does what, and which state feeds which widget.

The stock ``.tft`` identifies things by numeric page/widget ids on input and by component
names on output.  Both live in a YAML file so they can be filled in from the ScreenMap
reverse-engineering work (or from ``learn`` mode) without touching code.  See
``maps/default.yaml`` for the annotated schema.

Safety: any page or widget flagged ``todo`` is a placeholder whose ids have not been
confirmed against the real panel.  It is ignored at runtime unless the operator explicitly
sets ``allow_todo`` -- a wrong id on a *cancel print* button would otherwise be a live hazard.
"""

from __future__ import annotations

import math
import string
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .protocol import STRING_ATTRS, TOUCH_PRESS, TOUCH_RELEASE, parse_color
from .state import MISSING, PrinterState

SCHEMA_VERSION = 1
COLOR_ATTRS = frozenset({"bco", "pco", "bco2", "pco2", "pco1"})
PRINT_VERBS = frozenset(
    {"pause", "resume", "cancel", "start", "emergency_stop", "firmware_restart", "restart"}
)


class MapError(ValueError):
    """The screen map file is malformed."""


# ---------------------------------------------------------------------------- helpers


def _keys(node: Mapping[str, Any], allowed: set[str], ctx: str) -> None:
    if not isinstance(node, Mapping):
        raise MapError(f"{ctx}: expected a mapping, got {type(node).__name__}")
    unknown = set(node) - allowed
    if unknown:
        raise MapError(
            f"{ctx}: unknown key(s) {sorted(map(str, unknown))}; allowed: {sorted(allowed)}"
        )


def _opt_int(node: Mapping[str, Any], key: str, ctx: str, lo: int = 0, hi: int = 255) -> int | None:
    value = node.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise MapError(f"{ctx}.{key}: expected integer {lo}..{hi}, got {value!r}")
    return value


def _opt_str(node: Mapping[str, Any], key: str, ctx: str) -> str | None:
    value = node.get(key)
    if value is not None and not isinstance(value, str):
        raise MapError(f"{ctx}.{key}: expected string, got {value!r}")
    return value


def _is_todo(node: Mapping[str, Any]) -> bool:
    return bool(node.get("todo"))


# ------------------------------------------------------------------------------ tests

_TEST_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "in", "not_in", "truthy")


@dataclass(frozen=True, slots=True)
class Test:
    """A predicate over one state value.  ``source`` None means 'the binding's own value'."""

    op: str
    arg: Any
    source: str | None = None

    @staticmethod
    def parse(node: Mapping[str, Any], ctx: str, *, need_source: bool) -> Test:
        _keys(node, {"source", *_TEST_OPS}, ctx)
        ops = [op for op in _TEST_OPS if op in node]
        if len(ops) != 1:
            raise MapError(f"{ctx}: exactly one of {_TEST_OPS} is required")
        source = _opt_str(node, "source", ctx)
        if need_source and not source:
            raise MapError(f"{ctx}: 'source' is required")
        op = ops[0]
        arg = node[op]
        if op in ("in", "not_in") and not isinstance(arg, list):
            raise MapError(f"{ctx}.{op}: expected a list")
        return Test(op, arg, source)

    def check(self, value: Any) -> bool:
        op, arg = self.op, self.arg
        try:
            if op == "eq":
                return value == arg
            if op == "ne":
                return value != arg
            if op == "in":
                return value in arg
            if op == "not_in":
                return value not in arg
            if op == "truthy":
                return bool(value) == bool(arg)
            return {"gt": value > arg, "ge": value >= arg, "lt": value < arg, "le": value <= arg}[
                op
            ]
        except TypeError:
            return False

    def evaluate(self, state: PrinterState, own: Any = MISSING) -> bool:
        value = own if self.source is None else state.resolve(self.source)
        return False if value is MISSING else self.check(value)


# --------------------------------------------------------------------------- bindings


def _check_format(fmt: str, nargs: int, ctx: str) -> None:
    """Reject format strings that could only fail at render time.

    Only plain positional fields are allowed: ``{}``, ``{0}``, ``{1:.0f}``.  Attribute and
    index access (``{0.foo}``, ``{0[1]}``) and out-of-range or named fields are refused at
    load time so a bad map surfaces as a MapError, not as an exception inside the push loop.
    """
    try:
        fields = list(string.Formatter().parse(fmt))
    except ValueError as exc:
        raise MapError(f"{ctx}.format: {exc}") from exc
    auto = 0
    for _, name, spec, conv in fields:
        if name is None:
            continue
        if conv is not None and conv not in "rsa":
            raise MapError(f"{ctx}.format: unknown conversion !{conv}")
        if spec and ("{" in spec or "}" in spec):
            raise MapError(f"{ctx}.format: nested replacement fields are not supported")
        if name == "":
            index = auto
            auto += 1
        elif name.isdigit():
            index = int(name)
        else:
            raise MapError(
                f"{ctx}.format: field {{{name}}} not supported (use {{}}, {{0}}, {{1}}...)"
            )
        if index >= nargs:
            raise MapError(
                f"{ctx}.format: field {{{name}}} refers to source #{index} but only {nargs} given"
            )


@dataclass(frozen=True, slots=True)
class Binding:
    """State -> one Nextion attribute."""

    sources: tuple[str, ...]
    attr: str
    format: str | None = None
    scale: float | None = None
    test: Test | None = None
    value_map: Mapping[Any, Any] | None = None
    default: Any = MISSING
    offset: float | None = None

    @staticmethod
    def parse(node: Mapping[str, Any], ctx: str, default_attr: str) -> Binding:
        _keys(node, {"source", "attr", "format", "scale", "offset", "test", "map", "default"}, ctx)
        raw = node.get("source")
        if isinstance(raw, str):
            sources: tuple[str, ...] = (raw,)
        elif isinstance(raw, list) and raw and all(isinstance(s, str) for s in raw):
            sources = tuple(raw)
        else:
            raise MapError(f"{ctx}.source: expected a state path or a list of paths")
        attr = node.get("attr", default_attr)
        if not isinstance(attr, str) or not attr.isidentifier():
            raise MapError(f"{ctx}.attr: invalid attribute {attr!r}")
        fmt = _opt_str(node, "format", ctx)
        if fmt is not None:
            _check_format(fmt, len(sources), ctx)
        if len(sources) > 1 and (fmt is None or attr not in STRING_ATTRS):
            raise MapError(f"{ctx}: multiple sources need a string attribute and a 'format'")
        scale = node.get("scale")
        if scale is not None and (isinstance(scale, bool) or not isinstance(scale, int | float)):
            raise MapError(f"{ctx}.scale: expected a number")
        offset = node.get("offset")
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int | float)):
            raise MapError(f"{ctx}.offset: expected a number")
        test = None
        if "test" in node:
            test = Test.parse(node["test"], f"{ctx}.test", need_source=False)
        vmap = node.get("map")
        if vmap is not None:
            if not isinstance(vmap, Mapping):
                raise MapError(f"{ctx}.map: expected a mapping")
            if attr in COLOR_ATTRS:
                vmap = {k: _color(v, f"{ctx}.map[{k!r}]") for k, v in vmap.items()}
        default = node.get("default", MISSING)
        if default is not MISSING and attr in COLOR_ATTRS:
            default = _color(default, f"{ctx}.default")
        return Binding(sources, attr, fmt, scale, test, vmap, default, offset)

    def render(self, state: PrinterState) -> int | str | None:
        """Wire value for the attribute, or None when there is nothing sensible to show."""
        values = [state.resolve(path) for path in self.sources]
        if any(v is MISSING or v is None for v in values):
            return None if self.default is MISSING else self._coerce(self.default)
        value: Any = values[0]
        if self.test is not None:
            value = self.test.evaluate(state, value)
        if self.value_map is not None:
            for key in (value, str(value)):
                try:
                    hit = key in self.value_map
                except TypeError:  # unhashable state value (list/dict)
                    continue
                if hit:
                    value = self.value_map[key]
                    break
            else:
                if "_default" not in self.value_map:
                    return None if self.default is MISSING else self._coerce(self.default)
                value = self.value_map["_default"]
        if self.attr in STRING_ATTRS:
            args = values if len(values) > 1 else [value]
            if self.scale is not None and len(args) == 1 and _is_number(args[0]):
                args = [args[0] * self.scale]
            try:
                return self.format.format(*args) if self.format else str(args[0])
            except (ValueError, IndexError, KeyError, AttributeError, TypeError):
                return None
        if self.scale is not None and _is_number(value):
            value = value * self.scale
        if self.offset is not None and _is_number(value):
            value = value + self.offset
        return self._coerce(value)

    def _coerce(self, value: Any) -> int | str | None:
        if self.attr in STRING_ATTRS:
            return str(value)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, str) and self.attr in COLOR_ATTRS:
            return parse_color(value)
        if _is_number(value):
            return int(round(value))
        return None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _color(value: Any, ctx: str) -> int:
    try:
        return parse_color(value)
    except Exception as exc:
        raise MapError(f"{ctx}: {exc}") from exc


# ------------------------------------------------------------------------------ actions

_ACTION_VERBS = {"gcode", "print", "page", "beep", "set", "log"}
_ACTION_MODS = {
    "guard",
    "cooldown",
    "read",
    "file",
    "else_beep",
    "clamp",
    "scale",
    "zero_ok",
    "unguarded",
}
#: Print-control verbs that must never run on a bare touch: each needs a state ``guard``
#: (e.g. cancel only while printing/paused).  ``emergency_stop`` is the one verb that has to
#: work in any state, so it instead demands an explicit ``unguarded: true`` acknowledgement.
GUARDED_PRINT_VERBS = frozenset(
    {"pause", "resume", "cancel", "start", "restart", "firmware_restart"}
)


@dataclass(frozen=True, slots=True)
class Action:
    verb: str
    arg: Any
    guard: Test | None = None
    cooldown: float = 0.0
    read: Mapping[str, str] = field(default_factory=dict)  # variable -> Nextion `get` expression
    file: str | None = None
    else_beep: int = 0
    # Numeric entry (``{value}``): clamp to [lo, hi] (0 passes if zero_ok), then multiply by
    # ``scale`` and round.  Only applied when the action runs with a ``value``.
    clamp: tuple[float, float] | None = None
    scale: float | None = None
    zero_ok: bool = False

    def adjust(self, value: int | float) -> int:
        """Apply clamp/zero_ok/scale to a number typed on the panel."""
        if self.clamp is not None and not (self.zero_ok and value == 0):
            value = min(max(value, self.clamp[0]), self.clamp[1])
        if self.scale is not None:
            value = value * self.scale
        return int(round(value))

    @staticmethod
    def parse(node: Mapping[str, Any], ctx: str) -> Action:
        _keys(node, _ACTION_VERBS | _ACTION_MODS, ctx)
        verbs = [v for v in _ACTION_VERBS if v in node]
        if len(verbs) != 1:
            raise MapError(f"{ctx}: exactly one of {sorted(_ACTION_VERBS)} is required")
        verb = verbs[0]
        arg = node[verb]
        if verb == "gcode" and not (isinstance(arg, str) and arg.strip()):
            raise MapError(f"{ctx}.gcode: expected a non-empty string")
        if verb == "print" and arg not in PRINT_VERBS:
            raise MapError(f"{ctx}.print: must be one of {sorted(PRINT_VERBS)}")
        if verb == "print" and arg == "start" and not node.get("file"):
            raise MapError(f"{ctx}: 'print: start' needs a 'file' (may use {{text}})")
        if verb == "page" and not isinstance(arg, str):
            raise MapError(f"{ctx}.page: expected a page key from this map")
        if verb == "beep" and (isinstance(arg, bool) or not isinstance(arg, int) or arg < 0):
            raise MapError(f"{ctx}.beep: expected milliseconds")
        if verb == "set":
            _keys(arg, {"obj", "attr", "value"}, f"{ctx}.set")
            if not all(k in arg for k in ("obj", "attr", "value")):
                raise MapError(f"{ctx}.set: obj, attr and value are required")
        guard = None
        if "guard" in node:
            guard = Test.parse(node["guard"], f"{ctx}.guard", need_source=True)
        if verb == "print" and guard is None:
            if arg in GUARDED_PRINT_VERBS:
                raise MapError(
                    f"{ctx}: 'print: {arg}' needs a 'guard' (a state test) so a stray touch "
                    "cannot run it in the wrong state"
                )
            if arg == "emergency_stop" and node.get("unguarded") is not True:
                raise MapError(
                    f"{ctx}: 'print: emergency_stop' without a 'guard' needs an explicit "
                    "'unguarded: true' (an e-stop must work in every state, so say so)"
                )
        if "unguarded" in node and not isinstance(node["unguarded"], bool):
            raise MapError(f"{ctx}.unguarded: expected true/false")
        cooldown = node.get("cooldown", 0.0)
        if isinstance(cooldown, bool) or not isinstance(cooldown, int | float) or cooldown < 0:
            raise MapError(f"{ctx}.cooldown: expected seconds >= 0")
        read = node.get("read", {})
        if not isinstance(read, Mapping) or not all(
            isinstance(k, str) and k.isidentifier() and isinstance(v, str) for k, v in read.items()
        ):
            raise MapError(f"{ctx}.read: expected {{variable: 'obj.attr'}}")
        clamp = node.get("clamp")
        if clamp is not None and not (
            isinstance(clamp, list | tuple)
            and len(clamp) == 2
            and all(isinstance(c, int | float) and not isinstance(c, bool) for c in clamp)
            and clamp[0] <= clamp[1]
        ):
            raise MapError(f"{ctx}.clamp: expected [low, high]")
        scale = node.get("scale")
        if scale is not None and (isinstance(scale, bool) or not isinstance(scale, int | float)):
            raise MapError(f"{ctx}.scale: expected a number")
        return Action(
            verb,
            arg,
            guard,
            float(cooldown),
            dict(read),
            node.get("file"),
            int(node.get("else_beep", 0)),
            tuple(clamp) if clamp is not None else None,
            scale,
            bool(node.get("zero_ok", False)),
        )


def has_emergency_stop(actions: Iterable[Action]) -> bool:
    return any(a.verb == "print" and a.arg == "emergency_stop" for a in actions)


def _parse_actions(node: Any, ctx: str) -> tuple[Action, ...]:
    if node is None:
        return ()
    if isinstance(node, Mapping):
        node = [node]
    if not isinstance(node, list):
        raise MapError(f"{ctx}: expected an action or a list of actions")
    return tuple(Action.parse(a, f"{ctx}[{i}]") for i, a in enumerate(node))


# --------------------------------------------------------------------- widgets & pages


@dataclass(frozen=True, slots=True)
class Widget:
    key: str
    page_key: str
    id: int | None
    obj: str | None
    todo: bool
    bindings: tuple[Binding, ...]
    on_press: tuple[Action, ...]
    on_release: tuple[Action, ...]
    on_number: tuple[Action, ...] = ()


@dataclass(frozen=True, slots=True)
class Page:
    key: str
    id: int | None
    name: str | None
    todo: bool
    widgets: Mapping[str, Widget]
    on_enter: tuple[Action, ...]
    on_number: tuple[Action, ...]
    on_string: tuple[Action, ...]


@dataclass(frozen=True, slots=True)
class Marker:
    code: int
    name: str
    page_key: str | None
    actions: tuple[Action, ...]
    todo: bool = False


@dataclass(frozen=True, slots=True)
class CameraSpec:
    page_key: str
    obj: str
    picture: str
    interval: float
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class PushItem:
    obj: str
    binding: Binding
    key: str  # unique diff-cache key


class ScreenMap:
    def __init__(
        self,
        pages: Mapping[str, Page],
        markers: Mapping[int, Marker],
        camera: CameraSpec | None,
        init: tuple[str, ...],
        meta: Mapping[str, Any],
        allow_todo: bool,
    ) -> None:
        self.pages = dict(pages)
        self.markers = dict(markers)
        self.camera = camera
        self.init = init
        self.meta = dict(meta)
        self.allow_todo = allow_todo
        self._by_id = {
            p.id: p for p in self.pages.values() if p.id is not None and self._active_page(p)
        }
        self._touch: dict[tuple[int, int, int], tuple[Action, ...]] = {}
        for page in self._by_id.values():
            for w in page.widgets.values():
                if w.id is None or not self._active(w):
                    continue
                for typ, acts in ((TOUCH_PRESS, w.on_press), (TOUCH_RELEASE, w.on_release)):
                    if acts:
                        self._touch[(page.id, w.id, typ)] = acts  # type: ignore[index]
        self._number: dict[tuple[int, int], tuple[Action, ...]] = {
            (page.id, w.id): w.on_number
            for page in self._by_id.values()
            for w in page.widgets.values()
            if w.id is not None and w.on_number and self._active(w)
        }
        self._known_widgets = {
            (p.id, w.id)
            for p in self._by_id.values()
            for w in p.widgets.values()
            if w.id is not None and self._active(w)
        }

    # -- activity / todo gating ----------------------------------------------------------
    def is_page_active(self, page: Page) -> bool:
        return self._active_page(page)

    def marker(self, code: int) -> Marker | None:
        """The marker definition for ``code`` if it is confirmed (or TODO ids are allowed)."""
        m = self.markers.get(code)
        if m is None or not (self.allow_todo or not m.todo):
            return None
        # An unconfirmed (todo) marker never fires the e-stop, even with allow_todo.
        if m.todo and has_emergency_stop(m.actions):
            return None
        return m

    def _active_page(self, page: Page) -> bool:
        return self.allow_todo or not page.todo

    def _active(self, widget: Widget) -> bool:
        unconfirmed = widget.todo or self.pages[widget.page_key].todo
        if not unconfirmed:
            return True
        # allow_todo lets placeholder ids run, but never an unconfirmed emergency stop: a
        # wrong id there would halt a live print.  Confirm the widget (drop `todo`) to arm it.
        return self.allow_todo and not has_emergency_stop(
            (*widget.on_press, *widget.on_release, *widget.on_number)
        )

    @property
    def has_active_content(self) -> bool:
        return bool(self._touch) or bool(self._number) or any(True for _ in self.push_items_all())

    def todo_summary(self) -> tuple[int, int]:
        """(widgets still TODO, total widgets)."""
        widgets = [w for p in self.pages.values() for w in p.widgets.values()]
        return sum(1 for w in widgets if w.todo or self.pages[w.page_key].todo), len(widgets)

    # -- lookups -------------------------------------------------------------------------
    def page_by_id(self, page_id: int | None) -> Page | None:
        return self._by_id.get(page_id)

    def actions_for_touch(self, page: int, widget: int, typ: int) -> tuple[Action, ...] | None:
        return self._touch.get((page, widget, typ))

    def actions_for_number(self, page: int, widget: int) -> tuple[Action, ...] | None:
        """Actions for a numeric entry the stock .tft reported for (page, widget)."""
        return self._number.get((page, widget))

    def is_known_touch(self, page: int, widget: int) -> bool:
        return (page, widget) in self._known_widgets

    def push_items(self, page_id: int | None) -> list[PushItem]:
        page = self.page_by_id(page_id)
        if page is None:
            return []
        return [
            PushItem(w.obj, b, f"{page.key}.{w.key}.{i}.{b.attr}")
            for w in page.widgets.values()
            if w.obj and self._active(w)
            for i, b in enumerate(w.bindings)
        ]

    def push_items_all(self) -> Iterator[PushItem]:
        for page in self.pages.values():
            if page.id is not None:
                yield from self.push_items(page.id)

    def required_objects(self) -> set[str]:
        """Klipper objects the map reads, for the Moonraker subscription."""
        paths: set[str] = set()
        for item in self.push_items_all():
            paths.update(item.binding.sources)
            if item.binding.test and item.binding.test.source:
                paths.add(item.binding.test.source)
        for acts in (*self._touch.values(), *self._number.values()):
            paths.update(a.guard.source for a in acts if a.guard and a.guard.source)
        return {p.partition(".")[0] for p in paths} - {"derived"}

    # -- loading -------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path, *, allow_todo: bool = False) -> ScreenMap:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            raise MapError(f"cannot read screen map {path}: {exc}") from exc
        return cls.from_dict(raw, allow_todo=allow_todo)

    @classmethod
    def from_dict(cls, raw: Any, *, allow_todo: bool = False) -> ScreenMap:
        _keys(raw, {"version", "meta", "screen", "pages", "markers", "camera"}, "map")
        if raw.get("version") != SCHEMA_VERSION:
            raise MapError(f"map.version must be {SCHEMA_VERSION}")
        pages_raw = raw.get("pages")
        if not isinstance(pages_raw, Mapping) or not pages_raw:
            raise MapError("map.pages: at least one page is required")

        pages: dict[str, Page] = {}
        seen_pages: dict[int, str] = {}
        for pkey, pnode in pages_raw.items():
            ctx = f"pages.{pkey}"
            _keys(
                pnode, {"id", "name", "todo", "widgets", "on_enter", "on_number", "on_string"}, ctx
            )
            pid = _opt_int(pnode, "id", ctx)
            if pid is not None:
                if pid in seen_pages:
                    raise MapError(f"{ctx}.id {pid} already used by page {seen_pages[pid]!r}")
                seen_pages[pid] = pkey
            widgets: dict[str, Widget] = {}
            seen_widgets: dict[int, str] = {}
            for wkey, wnode in (pnode.get("widgets") or {}).items():
                widgets[wkey] = _parse_widget(
                    pkey, wkey, wnode, f"{ctx}.widgets.{wkey}", seen_widgets
                )
            pages[pkey] = Page(
                pkey,
                pid,
                _opt_str(pnode, "name", ctx),
                _is_todo(pnode),
                widgets,
                _parse_actions(pnode.get("on_enter"), f"{ctx}.on_enter"),
                _parse_actions(pnode.get("on_number"), f"{ctx}.on_number"),
                _parse_actions(pnode.get("on_string"), f"{ctx}.on_string"),
            )

        markers: dict[int, Marker] = {}
        for code, mnode in (raw.get("markers") or {}).items():
            ctx = f"markers.{code}"
            if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 0xFE:
                raise MapError(f"{ctx}: marker code must be an integer 0..254")
            _keys(mnode, {"name", "page", "do", "todo"}, ctx)
            if mnode.get("page") is not None and mnode["page"] not in pages:
                raise MapError(f"{ctx}.page: unknown page {mnode['page']!r}")
            markers[code] = Marker(
                code,
                str(mnode.get("name", f"marker_{code:#04x}")),
                mnode.get("page"),
                _parse_actions(mnode.get("do"), f"{ctx}.do"),
                _is_todo(mnode),
            )

        camera = None
        if raw.get("camera"):
            cnode = raw["camera"]
            _keys(cnode, {"page", "obj", "picture", "interval", "width", "height"}, "camera")
            if cnode.get("page") not in pages:
                raise MapError("camera.page: unknown page")
            camera = CameraSpec(
                cnode["page"],
                str(cnode["obj"]),
                str(cnode.get("picture", "ram/0.jpg")),
                float(cnode.get("interval", 2.0)),
                int(cnode["width"]),
                int(cnode["height"]),
            )

        screen_node = raw.get("screen") or {}
        _keys(screen_node, {"init"}, "screen")
        init = tuple(screen_node.get("init", ()))
        if not all(isinstance(c, str) for c in init):
            raise MapError("screen.init: expected a list of Nextion instructions")

        smap = cls(pages, markers, camera, init, raw.get("meta") or {}, allow_todo)
        # Page references in actions must exist.
        for page in pages.values():
            for acts in [
                page.on_enter,
                page.on_number,
                page.on_string,
                *(
                    a
                    for w in page.widgets.values()
                    for a in (w.on_press, w.on_release, w.on_number)
                ),
            ]:
                for act in acts:
                    if act.verb == "page" and act.arg not in pages:
                        raise MapError(f"pages.{page.key}: action targets unknown page {act.arg!r}")
        return smap


def _parse_widget(pkey: str, wkey: str, node: Any, ctx: str, seen: dict[int, str]) -> Widget:
    _keys(node, {"id", "obj", "todo", "bind", "on_press", "on_release", "on_number"}, ctx)
    wid = _opt_int(node, "id", ctx)
    if wid is not None:
        if wid in seen:
            raise MapError(f"{ctx}.id {wid} already used by widget {seen[wid]!r}")
        seen[wid] = wkey
    obj = _opt_str(node, "obj", ctx)
    bind = node.get("bind")
    bindings: tuple[Binding, ...] = ()
    if bind is not None:
        if not obj:
            raise MapError(f"{ctx}: 'bind' requires 'obj' (the component name on the panel)")
        items = bind if isinstance(bind, list) else [bind]
        default_attr = "txt"
        bindings = tuple(
            Binding.parse(b, f"{ctx}.bind[{i}]", default_attr) for i, b in enumerate(items)
        )
    return Widget(
        wkey,
        pkey,
        wid,
        obj,
        _is_todo(node),
        bindings,
        _parse_actions(node.get("on_press"), f"{ctx}.on_press"),
        _parse_actions(node.get("on_release"), f"{ctx}.on_release"),
        _parse_actions(node.get("on_number"), f"{ctx}.on_number"),
    )
