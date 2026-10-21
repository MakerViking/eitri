import pytest

from klarity.protocol import (
    TERMINATOR,
    Cmd,
    FrameParser,
    Garbage,
    MarkerEvent,
    NumberEvent,
    PageEvent,
    ProtocolError,
    StringEvent,
    TouchEvent,
    TouchXYEvent,
    UnknownFrame,
    encode,
    parse_color,
    quote,
    rgb565,
)

FF3 = b"\xff\xff\xff"


def parse(*chunks: bytes):
    p = FrameParser()
    events = []
    for chunk in chunks:
        events += p.feed(chunk)
    return events, p


def test_touch_event():
    (ev,), p = parse(b"\x65\x02\x0b\x01" + FF3)
    assert isinstance(ev, TouchEvent)
    assert (ev.page, ev.widget, ev.type, ev.pressed) == (2, 11, 1, True)
    assert not p.pending


def test_release_event():
    (ev,), _ = parse(b"\x65\x00\x01\x00" + FF3)
    assert isinstance(ev, TouchEvent) and not ev.pressed


def test_string_event_utf8_and_gbk():
    (ev,), _ = parse(b"\x70hello" + FF3)
    assert isinstance(ev, StringEvent) and ev.text == "hello"
    (ev,), _ = parse(b"\x70" + "温度".encode("gbk") + FF3)
    assert ev.text == "温度"


def test_number_event_little_endian_and_signed():
    (ev,), _ = parse(b"\x71\x10\x27\x00\x00" + FF3)
    assert isinstance(ev, NumberEvent) and ev.value == 10000
    (ev,), _ = parse(b"\x71\xfe\xff\xff\xff" + FF3)
    assert ev.value == -2 and ev.unsigned == 0xFFFFFFFE


def test_number_containing_terminator_bytes_is_not_truncated():
    # -1 is FF FF FF FF on the wire: the value itself contains a terminator sequence.
    events, p = parse(b"\x71\xff\xff\xff\xff" + FF3 + b"\x65\x01\x02\x00" + FF3)
    assert isinstance(events[0], NumberEvent) and events[0].value == -1
    assert isinstance(events[1], TouchEvent)
    assert len(events) == 2 and not p.pending


def test_page_and_coordinates():
    events, _ = parse(
        b"\x66\x05" + FF3, b"\x67\x01\x90\x00\xc8\x01" + FF3, b"\x68\x00\x10\x00\x20\x00" + FF3
    )
    assert events[0] == PageEvent(b"\x66\x05" + FF3, 5)
    assert isinstance(events[1], TouchXYEvent) and (events[1].x, events[1].y) == (400, 200)
    assert events[2].asleep


@pytest.mark.parametrize("code", [0xF9, 0xFA, 0xFB, 0xFC, 0x91, 0x92, 0x93, 0x1A, 0xFD, 0xFE])
@pytest.mark.parametrize("terminated", [True, False])
def test_single_byte_markers(code, terminated):
    data = bytes([code]) + (FF3 if terminated else b"")
    events, p = parse(data + b"\x65\x01\x02\x01" + FF3)
    assert isinstance(events[0], MarkerEvent) and events[0].code == code
    assert isinstance(events[1], TouchEvent)
    assert len(events) == 2 and not p.pending


def test_marker_terminator_may_arrive_in_a_later_chunk():
    events, p = parse(b"\xf9", FF3, b"\x65\x01\x02\x01", FF3)
    assert [type(e) for e in events] == [MarkerEvent, TouchEvent]
    assert p.stray_terminator_bytes == 3


def test_split_at_every_byte_boundary():
    stream = b"\x65\x02\x0b\x01" + FF3 + b"\x70ok" + FF3 + b"\x71\x05\x00\x00\x00" + FF3 + b"\x91"
    whole, _ = parse(stream)
    p = FrameParser()
    pieces = []
    for byte in stream:
        pieces += p.feed(bytes([byte]))
    assert pieces == whole and len(whole) == 4


def test_partial_frame_waits_then_completes():
    p = FrameParser()
    assert p.feed(b"\x65\x01") == []
    assert p.pending == b"\x65\x01"
    (ev,) = p.feed(b"\x02\x01" + FF3)
    assert isinstance(ev, TouchEvent) and ev.widget == 2


def test_flush_drops_stale_partial_frame():
    p = FrameParser()
    p.feed(b"\x65\x01")
    (g,) = p.flush()
    assert isinstance(g, Garbage) and not p.pending
    assert p.flush() == []


def test_leading_garbage_terminators_are_swallowed():
    events, p = parse(FF3 + FF3 + b"\x65\x01\x02\x00" + FF3)
    assert len(events) == 1 and isinstance(events[0], TouchEvent)
    assert p.stray_terminator_bytes == 6


def test_truncated_fixed_frame_resyncs_on_next_terminator():
    # A touch frame that lost its last payload byte, followed by a good frame.
    events, p = parse(b"\x65\x01\x02" + FF3 + b"\x65\x03\x04\x01" + FF3)
    kinds = [type(e) for e in events]
    assert Garbage in kinds
    good = [e for e in events if isinstance(e, TouchEvent)]
    assert len(good) == 1 and (good[0].page, good[0].widget) == (3, 4)
    assert not p.pending


def test_noise_between_frames():
    events, _ = parse(b"\x65\x01\x02\x01" + FF3 + b"\xff\xff" + b"\x65\x01\x03\x00" + FF3)
    assert [e.widget for e in events if isinstance(e, TouchEvent)] == [2, 3]


def test_unknown_delimited_frame():
    (ev,), _ = parse(b"\xa7\x01\x02\x03" + FF3)
    assert isinstance(ev, UnknownFrame) and ev.id == 0xA7 and ev.payload == b"\x01\x02\x03"


def test_extra_single_byte_ids_configurable():
    p = FrameParser(single_byte_ids=[0xA7])
    events = p.feed(b"\xa7\x65\x01\x02\x01" + FF3)
    assert isinstance(events[0], MarkerEvent) and isinstance(events[1], TouchEvent)


def test_oversized_garbage_without_terminator_is_discarded():
    p = FrameParser()
    events = p.feed(b"\xa7" + b"x" * 5000)
    assert len(events) == 1 and isinstance(events[0], Garbage)
    assert not p.pending


def test_random_noise_never_raises():
    import random

    rng = random.Random(1234)
    p = FrameParser()
    for _ in range(300):
        p.feed(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40))))
    p.flush()


# ----------------------------------------------------------------------------- outbound


def test_quote_escapes():
    assert quote('say "hi"') == r'"say \"hi\""'
    assert quote("a\\b") == r'"a\\b"'
    assert quote("l1\nl2") == r'"l1\rl2"'
    assert quote("tab\there") == '"tab here"'
    assert quote("bell\x07") == '"bell"'


def test_quote_replace_style():
    assert quote('a"b\\c', "replace") == '"a\'b/c"'


def test_quote_rejects_unknown_style():
    with pytest.raises(ProtocolError):
        quote("x", "nope")


def test_command_builders():
    assert Cmd.page(3) == "page 3"
    assert Cmd.page("main") == "page main"
    assert Cmd.set_text("t0", "205 / 210") == 't0.txt="205 / 210"'
    assert Cmd.add_text("t0", "x") == 't0.txt+="x"'
    assert Cmd.set_val("n0", 42) == "n0.val=42"
    assert Cmd.vis("b0", True) == "vis b0,1"
    assert Cmd.vis("b0", False) == "vis b0,0"
    assert Cmd.bco("t0", 63488) == "t0.bco=63488"
    assert Cmd.pco("t0", 0) == "t0.pco=0"
    assert Cmd.pic("p0", 7) == "p0.pic=7"
    assert Cmd.picc("p0", 7) == "p0.picc=7"
    assert Cmd.get("t0.txt") == "get t0.txt"
    assert Cmd.get("page0.t0.txt") == "get page0.t0.txt"
    assert Cmd.tsw("b0", False) == "tsw b0,0"
    assert Cmd.tsw(255, True) == "tsw 255,1"
    assert Cmd.beep(100) == "beep 100"
    assert Cmd.add(1, 0, 120) == "add 1,0,120"
    assert Cmd.set_attr("t0", "txt", "a") == 't0.txt="a"'
    assert Cmd.set_attr("n0", "val", 5) == "n0.val=5"
    assert Cmd.bkcmd(0) == "bkcmd=0"
    assert Cmd.twfile("ram/0.jpg", 10) == 'twfile "ram/0.jpg",10'


@pytest.mark.parametrize(
    "call",
    [
        lambda: Cmd.set_text("t0; reset", "x"),
        lambda: Cmd.set_text('t0"', "x"),
        lambda: Cmd.set_text("", "x"),
        lambda: Cmd.set_val("n0", 2**31),
        lambda: Cmd.set_val("n0", "5"),
        lambda: Cmd.set_attr("t0", "txt=1;x", "a"),
        lambda: Cmd.set_attr("n0", "val", "text"),
        lambda: Cmd.get("t0.txt; reset"),
        lambda: Cmd.bco("t0", 70000),
        lambda: Cmd.bkcmd(9),
        lambda: Cmd.page("a b"),
        lambda: Cmd.twfile("x", 0),
    ],
)
def test_builders_reject_unsafe_input(call):
    with pytest.raises(ProtocolError):
        call()


def test_encode_appends_terminator_and_uses_codec():
    assert encode("page 1") == b"page 1" + TERMINATOR
    assert encode('t0.txt="温"') == 't0.txt="温"'.encode() + TERMINATOR
    assert encode('t0.txt="温"', "gb18030") == 't0.txt="温"'.encode("gb18030") + TERMINATOR


def test_encode_replaces_unencodable_and_rejects_ff():
    assert encode('t0.txt="😀"', "gbk").endswith(b'"' + TERMINATOR)
    with pytest.raises(ProtocolError):
        encode("x" * 2000)
    with pytest.raises(ProtocolError):
        encode('t0.txt="ÿ"', "latin-1")


def test_colors():
    assert rgb565(255, 0, 0) == 63488
    assert parse_color("#ff0000") == 63488
    assert parse_color("00ff00") == 2016
    assert parse_color(1234) == 1234
    with pytest.raises(ProtocolError):
        parse_color("red")
    with pytest.raises(ProtocolError):
        parse_color(-1)
