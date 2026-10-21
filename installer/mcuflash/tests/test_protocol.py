# SPDX-License-Identifier: Apache-2.0
import json
import os
import random
import struct
import sys
import zlib

import pytest

from fakes import FakeBoard, FakeBus, FakeClock, make_image
from eitri_mcuflash import katapult_proto as kp
from eitri_mcuflash import klipper_proto as kl
from eitri_mcuflash.errors import ProtocolError

KLIPPER_SRC = "/home/thomash/Koding/projects/Plus4/src/upstream-klipper/klippy"


def _klipper_msgproto():
    if not os.path.isdir(KLIPPER_SRC):
        pytest.skip("Klipper source tree not available")
    sys.path.insert(0, KLIPPER_SRC)
    try:
        import msgproto  # type: ignore

        return msgproto
    finally:
        sys.path.remove(KLIPPER_SRC)


def test_crc16_known_vector():
    # CRC-16/MCRF4XX check value
    assert kl.crc16_ccitt(b"123456789") == 0x6F91


def test_crc16_and_vlq_match_klipper_reference():
    mp = _klipper_msgproto()
    rnd = random.Random(7)
    for _ in range(200):
        buf = bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 60)))
        assert list(divmod(kl.crc16_ccitt(buf), 256)) == mp.crc16_ccitt(buf)
    pt = mp.PT_int32()
    for v in (0, 1, 31, 32, 95, 96, 8191, 8192, 100000, 2 ** 31 - 1, -1, -32, -33, -100000, -(2 ** 31)):
        out = []
        pt.encode(out, v)
        assert kl.vlq_encode(v) == bytes(out)
        assert kl.vlq_decode(bytes(out), 0, signed=True)[0] == v


def test_build_block_roundtrip_and_resync():
    p = kl.BlockParser()
    good = kl.build_block(3, b"\x01\x02")
    garbage = b"\x00\xff\x13\x37"
    # split feed, garbage first, then a corrupt block, then a good one
    bad = bytearray(kl.build_block(4, b"\x09"))
    bad[2] ^= 0x55
    stream = garbage + bytes(bad) + good
    blocks = []
    for i in range(0, len(stream), 3):
        blocks += p.feed(stream[i : i + 3])
    assert [(b.seq, b.payload) for b in blocks] == [(3, b"\x01\x02")]


def test_vlq_truncated_is_protocol_error():
    with pytest.raises(ProtocolError):
        kl.vlq_decode(b"\x81", 0)


def test_read_dictionary_with_nak_resync_and_sequence_wrap():
    rnd = random.Random(1)
    filler = "".join("%02x" % rnd.randrange(256) for _ in range(600))  # incompressible
    clock = FakeClock()
    board = FakeBoard("b", seq_state=11)
    board.install(
        make_image("v0.13.0-5-gdeadbee", spi=True, extra_config={"FILLER": filler}),
    )
    bus = FakeBus(clock)
    bus.add("/dev/ttyS0", board)
    with bus.opener("/dev/ttyS0", 500000) as tp:
        d = kl.read_dictionary(tp, timeout=0.5, clock=clock)
    assert d.mcu == "stm32f401xc"
    assert d.version == "v0.13.0-5-gdeadbee"
    assert d.config["FILLER"] == filler
    assert "config_spi" in d.command_ids()
    assert len(zlib.compress(json.dumps(d.raw).encode())) > 16 * 40  # exercised > 16 chunks


def test_read_dictionary_wrong_baud_times_out():
    clock = FakeClock()
    bus = FakeBus(clock)
    bus.add("/dev/ttyS0", FakeBoard("b"))
    with bus.opener("/dev/ttyS0", 115200) as tp:
        with pytest.raises(ProtocolError):
            kl.read_dictionary(tp, timeout=0.2, clock=clock)


def _flashtool_crc(buf):
    # verbatim algorithm of Katapult's flashtool.py (independent of our module)
    crc = 0xFFFF
    for data in buf:
        data ^= crc & 0xFF
        data ^= (data & 0x0F) << 4
        crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
    return crc & 0xFFFF


def test_katapult_framing_matches_flashtool():
    cmd = kp.build_command(kp.CMD_SEND_BLOCK, struct.pack("<I", 0x8008000) + bytes(range(64)))
    assert cmd[:2] == b"\x01\x88" and cmd[-2:] == b"\x99\x03"
    assert struct.unpack("<H", cmd[-4:-2])[0] == _flashtool_crc(cmd[2:-4])
    assert cmd[3] == (4 + 64) // 4
    with pytest.raises(ValueError):
        kp.build_command(1, b"abc")


def test_katapult_parse_frame_rejects_bad_crc():
    board = FakeBoard("b")
    frame = bytearray(board._kp_reply(kp.CMD_CONNECT, b"\0" * 8))
    kp.parse_frame(bytes(frame))
    frame[5] ^= 1
    with pytest.raises(ProtocolError):
        kp.parse_frame(bytes(frame))


def _katapult(board, clock):
    board.mode = "katapult"
    bus = FakeBus(clock)
    bus.add("/dev/ttyS2", board)
    tp = bus.opener("/dev/ttyS2", 250000)
    return kp.KatapultClient(tp, clock=clock, sleep=clock.sleep), tp


def test_katapult_connect_read_write_verify_complete():
    clock = FakeClock()
    board = FakeBoard("t", version="old")
    client, tp = _katapult(board, clock)
    info = client.connect()
    assert (info.app_start, info.block_size, info.mcu) == (0x08008000, 64, "stm32f401xc")
    assert info.protocol_version == (1, 1, 0)
    old = client.read_region(info.app_start, 512)
    assert old == board.app_image[:512]
    image = make_image("new", size=1000)
    sha = client.write_image(image)
    client.verify_image(sha, len(image))
    assert board.app_image[: len(image)] == image
    client.complete()
    assert board.mode == "app" and board.app_dictionary()["version"] == "new"


def test_katapult_retries_transient_error_and_detects_corruption():
    clock = FakeClock()
    board = FakeBoard("t")
    client, _ = _katapult(board, clock)
    client.connect()
    board.write_fail_at = 0x08008000 + 128
    image = make_image("new", size=600)
    sha = client.write_image(image)  # one ACK_ERROR on the way, retried
    client.verify_image(sha, len(image))

    board2 = FakeBoard("t2")
    client2, _ = _katapult(board2, clock)
    client2.connect()
    board2.corrupt_plan = {"addr": 0x08008000 + 64, "skip": 0, "count": 1}
    sha2 = client2.write_image(image)
    with pytest.raises(ProtocolError, match="mismatch"):
        client2.verify_image(sha2, len(image))


def test_katapult_silent_peer():
    clock = FakeClock()
    board = FakeBoard("t")
    bus = FakeBus(clock)
    bus.add("/dev/x", board)
    tp = bus.opener("/dev/x", 250000)  # board is in app mode: ignores katapult bytes
    with pytest.raises(ProtocolError):
        kp.KatapultClient(tp, clock=clock, sleep=clock.sleep).connect(tries=2, timeout=0.2)
