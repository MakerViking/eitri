"""Nextion/TJC wire protocol: inbound frame parsing and outbound command building.

Clean-room implementation from the behavioural description in
``notes/screen-protocol.md`` plus the public Nextion instruction set.

Framing
-------
Every Nextion frame, in both directions, ends with ``0xFF 0xFF 0xFF``.  The first
byte of an inbound frame is an event id:

* fixed-length frames (``0x65`` touch, ``0x66`` current page, ``0x67``/``0x68`` touch
  coordinates, ``0x71`` number) are parsed by length, *not* by scanning for the
  terminator, because a 32-bit number such as ``-1`` legitimately contains ``FF FF FF``;
* ``0x70`` (string) and any unknown id are delimited by the terminator;
* the project-specific one-byte page markers (``0xF9``... see :data:`DEFAULT_MARKERS`) and
  the standard Nextion return codes carry no payload.  Whether the stock ``.tft`` follows
  them with a terminator is not known yet, so they are emitted as soon as the id byte
  arrives and any trailing ``FF`` bytes are swallowed as stray terminators.

The parser resynchronises after garbage: a malformed fixed-length frame is discarded up to
the next terminator, and :meth:`FrameParser.flush` lets the caller drop a half-received frame
after a read timeout.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterable
from dataclasses import dataclass

TERMINATOR = b"\xff\xff\xff"

# Event ids that have a fixed payload length (bytes between the id and the terminator).
FIXED_PAYLOAD: dict[int, int] = {
    0x65: 3,  # touch event: page, widget, type
    0x66: 1,  # current page id (reply to ``sendme``)
    0x67: 5,  # touch coordinate: x(be16) y(be16) type
    0x68: 5,  # touch coordinate in sleep mode
    0x71: 4,  # numeric data, 32-bit little endian
}
ID_TOUCH = 0x65
ID_PAGE = 0x66
ID_TOUCH_XY = 0x67
ID_TOUCH_XY_SLEEP = 0x68
ID_STRING = 0x70

# One-byte events.  The first group are standard Nextion return codes / system events,
# the second the app-specific markers the stock project emits with ``printh`` (see spec).
NEXTION_STATUS_CODES = frozenset(range(0x00, 0x25)) | {0x86, 0x87, 0x88, 0x89}
FILE_TRANSFER_READY = 0xFE  # twfile: screen is ready for the file bytes
FILE_TRANSFER_DONE = 0xFD  # twfile: file received
DEFAULT_MARKERS = frozenset({0xF9, 0xFA, 0xFB, 0xFC, 0x91, 0x92, 0x93})
SINGLE_BYTE_IDS = NEXTION_STATUS_CODES | DEFAULT_MARKERS | {FILE_TRANSFER_READY, FILE_TRANSFER_DONE}

# Upper bound for a single buffered frame; anything longer is treated as line noise.
MAX_FRAME = 4096

TOUCH_RELEASE = 0
TOUCH_PRESS = 1


class ProtocolError(ValueError):
    """Raised when a command cannot be built safely (bad identifier, 0xFF in payload...)."""


# --------------------------------------------------------------------------- inbound


@dataclass(frozen=True, slots=True)
class Event:
    """Base class of everything the parser can emit."""

    raw: bytes


@dataclass(frozen=True, slots=True)
class TouchEvent(Event):
    page: int
    widget: int
    type: int  # 1 = press, 0 = release


@dataclass(frozen=True, slots=True)
class PageEvent(Event):
    page: int


@dataclass(frozen=True, slots=True)
class TouchXYEvent(Event):
    x: int
    y: int
    type: int
    asleep: bool


@dataclass(frozen=True, slots=True)
class StringEvent(Event):
    text: str


@dataclass(frozen=True, slots=True)
class NumberEvent(Event):
    value: int  # signed 32-bit, as Nextion transmits negative numbers in two's complement

    @property
    def stock(self) -> tuple[int, int, int]:
        """``(page, widget, value)`` for the stock .tft's unsolicited numeric-entry frame.

        The stock display does not send the standard 32-bit value after ``0x71``: it sends
        ``71 <page> <widget> <lo> <hi>``.  A reply to a ``get`` is the standard 32-bit value
        (:attr:`value`); the daemon only uses this view for frames nobody asked for.
        """
        b = self.raw[1:5]
        return b[0], b[1], b[2] | (b[3] << 8)


@dataclass(frozen=True, slots=True)
class MarkerEvent(Event):
    """A one-byte event: standard Nextion status code or app-specific page marker."""

    code: int


@dataclass(frozen=True, slots=True)
class UnknownFrame(Event):
    """A well-delimited frame with an id we do not understand (learn-mode fodder)."""

    id: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class Garbage(Event):
    """Bytes that could not be framed and were dropped to resynchronise."""

    reason: str


def decode_text(data: bytes) -> str:
    """Decode screen text.  TJC panels with Chinese UI send GBK; plain ASCII is shared."""
    for codec in ("utf-8", "gb18030"):
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


class FrameParser:
    """Incremental parser; feed arbitrary chunks, get complete events."""

    def __init__(self, single_byte_ids: Iterable[int] = ()) -> None:
        self._buf = bytearray()
        self._single = SINGLE_BYTE_IDS | frozenset(single_byte_ids)
        self.stray_terminator_bytes = 0

    @property
    def pending(self) -> bytes:
        return bytes(self._buf)

    @property
    def single_byte_ids(self) -> frozenset[int]:
        return self._single

    def flush(self) -> list[Event]:
        """Drop a stale partial frame (call after a quiet period on the line)."""
        if not self._buf:
            return []
        raw = bytes(self._buf)
        self._buf.clear()
        return [Garbage(raw, "incomplete frame timed out")]

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        events: list[Event] = []
        buf = self._buf
        while buf:
            ident = buf[0]
            if ident == 0xFF:
                # Leftover terminator bytes (or the end of a marker we already emitted).
                n = 1
                while n < len(buf) and buf[n] == 0xFF:
                    n += 1
                self.stray_terminator_bytes += n
                del buf[:n]
                continue

            if ident in FIXED_PAYLOAD:
                need = 1 + FIXED_PAYLOAD[ident] + 3
                if len(buf) < need:
                    break
                if bytes(buf[need - 3 : need]) != TERMINATOR:
                    events.append(self._drop_through_terminator(buf, "bad terminator"))
                    continue
                events.append(self._fixed_event(bytes(buf[:need])))
                del buf[:need]
                continue

            if ident in self._single:
                events.append(MarkerEvent(bytes([ident]), ident))
                del buf[:1]
                if buf.startswith(TERMINATOR):
                    del buf[:3]
                continue

            end = buf.find(TERMINATOR, 1)
            # A stray byte directly in front of a complete, well-formed fixed-length frame
            # (e.g. line noise before a touch): drop just the stray byte so the real frame is
            # not swallowed with it as one terminator-delimited unknown frame.  Only when that
            # frame's own terminator is the first one, so real unknown payloads that merely
            # contain such bytes are left alone.
            if (
                end >= 0
                and len(buf) > 1
                and buf[1] in FIXED_PAYLOAD
                and end == FIXED_PAYLOAD[buf[1]] + 2
            ):
                events.append(Garbage(bytes([ident]), "stray byte before frame"))
                del buf[:1]
                continue
            if end < 0:
                if len(buf) > MAX_FRAME:
                    events.append(Garbage(bytes(buf), "oversized frame without terminator"))
                    buf.clear()
                break
            frame = bytes(buf[: end + 3])
            del buf[: end + 3]
            payload = frame[1:end]
            if ident == ID_STRING:
                events.append(StringEvent(frame, decode_text(payload)))
            else:
                events.append(UnknownFrame(frame, ident, payload))
        return events

    @staticmethod
    def _fixed_event(frame: bytes) -> Event:
        ident = frame[0]
        body = frame[1:-3]
        if ident == ID_TOUCH:
            return TouchEvent(frame, body[0], body[1], body[2])
        if ident == ID_PAGE:
            return PageEvent(frame, body[0])
        if ident in (ID_TOUCH_XY, ID_TOUCH_XY_SLEEP):
            x, y = struct.unpack(">HH", body[:4])
            return TouchXYEvent(frame, x, y, body[4], ident == ID_TOUCH_XY_SLEEP)
        return NumberEvent(frame, struct.unpack("<i", body)[0])

    @staticmethod
    def _drop_through_terminator(buf: bytearray, reason: str) -> Garbage:
        end = buf.find(TERMINATOR, 1)
        # With no terminator in sight skip just the bad id byte; the rest is re-examined.
        cut = end + 3 if end >= 0 else 1
        raw = bytes(buf[:cut])
        del buf[:cut]
        return Garbage(raw, reason)


# -------------------------------------------------------------------------- outbound

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_OBJ_RE = re.compile(rf"^(?:{_IDENT}\.)?{_IDENT}$")
_ATTR_RE = re.compile(rf"^{_IDENT}$")
_GET_RE = re.compile(rf"^{_IDENT}(?:\.{_IDENT}){{0,2}}$")
MAX_COMMAND_LEN = 1000

#: Attributes whose value is a string; everything else is sent as a number.
STRING_ATTRS = frozenset({"txt", "path", "dir", "text_ex"})


def _ident(value: str, what: str = "object") -> str:
    if not isinstance(value, str) or not _OBJ_RE.match(value):
        raise ProtocolError(f"invalid {what} name: {value!r}")
    return value


def _int(value: object, what: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if not isinstance(value, int):
        raise ProtocolError(f"{what} must be an integer, got {value!r}")
    if not -(2**31) <= value < 2**31:
        raise ProtocolError(f"{what} out of 32-bit range: {value}")
    return value


def quote(text: str, style: str = "backslash") -> str:
    """Render ``text`` as a Nextion string literal (including the surrounding quotes).

    ``backslash``  ``"`` -> ``\\"``, ``\\`` -> ``\\\\``, newline -> ``\\r`` (Nextion Enhanced).
    ``replace``    ``"`` -> ``'`` and ``\\`` -> ``/`` for firmware without escape support.
    Other control characters are dropped: they cannot be represented and ``0xFF`` would
    terminate the instruction early.
    """
    if style not in ("backslash", "replace"):
        raise ProtocolError(f"unknown quote style {style!r}")
    out: list[str] = []
    text = text.replace("\r\n", "\n")
    for ch in text:
        if ch == '"':
            out.append('\\"' if style == "backslash" else "'")
        elif ch == "\\":
            out.append("\\\\" if style == "backslash" else "/")
        elif ch in "\n\r":
            out.append("\\r" if style == "backslash" else " ")
        elif ch == "\t":
            out.append(" ")
        elif ord(ch) < 0x20 or ch == "\x7f":
            continue
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def encode(command: str, encoding: str = "utf-8") -> bytes:
    """Encode an instruction to wire bytes, appending the terminator."""
    if len(command) > MAX_COMMAND_LEN:
        raise ProtocolError(f"command too long ({len(command)} > {MAX_COMMAND_LEN})")
    data = command.encode(encoding, errors="replace")
    if b"\xff" in data:
        raise ProtocolError(f"command encodes to a 0xFF byte: {command!r}")
    return data + TERMINATOR


class Cmd:
    """Builders for Nextion instructions.  All return the instruction text (no terminator)."""

    quote_style = "backslash"

    @classmethod
    def page(cls, target: int | str) -> str:
        if isinstance(target, str):
            return f"page {_ident(target, 'page')}"
        return f"page {_int(target, 'page id')}"

    @classmethod
    def set_attr(cls, obj: str, attr: str, value: object) -> str:
        """``obj.attr=value`` with string/number chosen from the Python type."""
        if not _ATTR_RE.match(attr):
            raise ProtocolError(f"invalid attribute name: {attr!r}")
        if isinstance(value, str):
            if attr not in STRING_ATTRS:
                raise ProtocolError(f"attribute {attr!r} takes a number, got string")
            return f"{_ident(obj)}.{attr}={quote(value, cls.quote_style)}"
        return f"{_ident(obj)}.{attr}={_int(value, attr)}"

    @classmethod
    def vis(cls, obj: str, show: bool) -> str:
        return f"vis {_ident(obj)},{int(bool(show))}"

    @classmethod
    def get(cls, expr: str) -> str:
        """Request an attribute; the reply arrives as a string (0x70) or number (0x71) frame."""
        if not _GET_RE.match(expr):
            raise ProtocolError(f"invalid get expression: {expr!r}")
        return f"get {expr}"

    @classmethod
    def beep(cls, ms: int) -> str:
        return f"beep {_int(ms, 'beep duration')}"

    @classmethod
    def sendme(cls) -> str:
        return "sendme"

    @classmethod
    def bkcmd(cls, level: int) -> str:
        if level not in (0, 1, 2, 3):
            raise ProtocolError("bkcmd level must be 0..3")
        return f"bkcmd={level}"

    @classmethod
    def twfile(cls, path: str, size: int) -> str:
        """Start a transparent file write; the screen answers 0xFE when ready for ``size`` bytes."""
        if size <= 0:
            raise ProtocolError("file size must be positive")
        return f"twfile {quote(path, cls.quote_style)},{size}"


def _color(value: int) -> int:
    value = _int(value, "colour")
    if not 0 <= value <= 0xFFFF:
        raise ProtocolError(f"colour must be a 16-bit RGB565 value, got {value}")
    return value


def rgb565(red: int, green: int, blue: int) -> int:
    """Pack 8-bit channels into the RGB565 value Nextion colour attributes use."""
    return ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)


def parse_color(value: int | str) -> int:
    """Accept a raw RGB565 int, ``0xRRGGBB`` hex string (``#rrggbb``) -> RGB565."""
    if isinstance(value, int):
        return _color(value)
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if not match:
        raise ProtocolError(f"unrecognised colour {value!r}; use #rrggbb or an RGB565 integer")
    n = int(match.group(1), 16)
    return rgb565(n >> 16, (n >> 8) & 0xFF, n & 0xFF)
