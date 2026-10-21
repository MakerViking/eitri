# SPDX-License-Identifier: Apache-2.0
"""Klipper MCU wire protocol: just enough to read the data dictionary.

The framing (length, sequence, CRC16-CCITT, 0x7e sync) and the variable length
integer encoding are the documented Klipper protocol
(https://www.klipper3d.org/Protocol.html).  Everything here is read-only
against the MCU: the `identify` exchange is how the real host learns the
command set, and is safe against a running or shut-down firmware.
"""
from __future__ import annotations

import hashlib
import json
import time
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .errors import ProtocolError
from .transport import Transport

MESSAGE_MIN = 5
MESSAGE_MAX = 64
MESSAGE_PAYLOAD_MAX = MESSAGE_MAX - MESSAGE_MIN
MESSAGE_SEQ_MASK = 0x0F
MESSAGE_DEST = 0x10
MESSAGE_SYNC = 0x7E

MSGID_IDENTIFY_RESPONSE = 0
MSGID_IDENTIFY = 1
IDENTIFY_CHUNK = 40

# Written to a UART with a running Klipper (built with bootloader entry
# support) to make it jump into the bootloader.  Same bytes Katapult's
# flashtool.py sends for `-r`.
SERIAL_BOOTLOADER_REQUEST = b"~ \x1c Request Serial Bootloader!! ~"


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        byte ^= crc & 0xFF
        byte ^= (byte & 0x0F) << 4
        crc = ((byte << 8) | (crc >> 8)) ^ (byte >> 4) ^ (byte << 3)
        crc &= 0xFFFF
    return crc


def vlq_encode(value: int) -> bytes:
    out = bytearray()
    v = value
    if v >= 0xC000000 or v < -0x4000000:
        out.append((v >> 28) & 0x7F | 0x80)
    if v >= 0x180000 or v < -0x80000:
        out.append((v >> 21) & 0x7F | 0x80)
    if v >= 0x3000 or v < -0x1000:
        out.append((v >> 14) & 0x7F | 0x80)
    if v >= 0x60 or v < -0x20:
        out.append((v >> 7) & 0x7F | 0x80)
    out.append(v & 0x7F)
    return bytes(out)


def vlq_decode(buf: bytes, pos: int = 0, signed: bool = False) -> Tuple[int, int]:
    try:
        c = buf[pos]
        pos += 1
        v = c & 0x7F
        if (c & 0x60) == 0x60:
            v |= -0x20
        while c & 0x80:
            c = buf[pos]
            pos += 1
            v = (v << 7) | (c & 0x7F)
    except IndexError:
        raise ProtocolError("truncated variable-length integer in MCU message") from None
    if not signed:
        v &= 0xFFFFFFFF
    return v, pos


def build_block(seq: int, payload: bytes) -> bytes:
    if len(payload) > MESSAGE_PAYLOAD_MAX:
        raise ValueError("payload too large for one Klipper block")
    head = bytes([MESSAGE_MIN + len(payload), (seq & MESSAGE_SEQ_MASK) | MESSAGE_DEST]) + payload
    crc = crc16_ccitt(head)
    return head + bytes([crc >> 8, crc & 0xFF, MESSAGE_SYNC])


@dataclass
class Block:
    seq: int  # low nibble only: the sender's *next expected* sequence
    payload: bytes


class BlockParser:
    """Incremental block deframer with the same resync rule as the host."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._need_sync = False

    def feed(self, data: bytes) -> List[Block]:
        self._buf.extend(data)
        out: List[Block] = []
        while True:
            if self._need_sync:
                idx = self._buf.find(bytes([MESSAGE_SYNC]))
                if idx < 0:
                    self._buf.clear()
                    break
                del self._buf[: idx + 1]
                self._need_sync = False
            if len(self._buf) < MESSAGE_MIN:
                break
            mlen = self._buf[0]
            if mlen < MESSAGE_MIN or mlen > MESSAGE_MAX or (self._buf[1] & ~MESSAGE_SEQ_MASK) != MESSAGE_DEST:
                self._need_sync = True
                continue
            if len(self._buf) < mlen:
                break
            block = bytes(self._buf[:mlen])
            crc = crc16_ccitt(block[: mlen - 3])
            if block[-1] != MESSAGE_SYNC or block[mlen - 3] != crc >> 8 or block[mlen - 2] != crc & 0xFF:
                self._need_sync = True
                continue
            del self._buf[:mlen]
            out.append(Block(block[1] & MESSAGE_SEQ_MASK, block[2 : mlen - 3]))
        return out


@dataclass
class McuDictionary:
    """The firmware's self-description (decompressed JSON)."""

    raw: Dict[str, Any]
    compressed_sha256: str = ""

    @property
    def app(self) -> str:
        return str(self.raw.get("app", ""))

    @property
    def version(self) -> str:
        return str(self.raw.get("version", ""))

    @property
    def build_versions(self) -> str:
        return str(self.raw.get("build_versions", ""))

    @property
    def config(self) -> Dict[str, Any]:
        return self.raw.get("config", {}) or {}

    @property
    def mcu(self) -> str:
        return str(self.config.get("MCU", ""))

    @property
    def clock_freq(self) -> Optional[int]:
        v = self.config.get("CLOCK_FREQ")
        return int(v) if v is not None else None

    @property
    def serial_baud(self) -> Optional[int]:
        v = self.config.get("SERIAL_BAUD")
        return int(v) if v is not None else None

    def command_ids(self) -> Dict[str, int]:
        """command name -> numeric message id."""
        out: Dict[str, int] = {}
        for fmt, mid in (self.raw.get("commands") or {}).items():
            out[fmt.split()[0]] = int(mid)
        return out

    def summary(self) -> Dict[str, Any]:
        return {
            "app": self.app,
            "version": self.version,
            "build_versions": self.build_versions,
            "mcu": self.mcu,
            "clock_freq": self.clock_freq,
            "serial_baud": self.serial_baud,
            "commands": len(self.command_ids()),
            "dictionary_sha256": self.compressed_sha256,
        }


def parse_dictionary(compressed: bytes) -> McuDictionary:
    try:
        raw = json.loads(zlib.decompress(compressed))
    except (zlib.error, ValueError) as exc:
        raise ProtocolError("MCU data dictionary is corrupt: %s" % exc) from exc
    if not isinstance(raw, dict):
        raise ProtocolError("MCU data dictionary has unexpected structure")
    return McuDictionary(raw=raw, compressed_sha256=hashlib.sha256(compressed).hexdigest())


class KlipperLink:
    """Sequence-tracking request/response over one Transport."""

    def __init__(self, transport: Transport, *, clock=time.monotonic) -> None:
        self.transport = transport
        self._parser = BlockParser()
        self._seq = 1  # same starting point as the real host
        self._clock = clock

    def request(self, payload: bytes, want_msgid: int, timeout: float = 1.0, attempts: int = 4) -> bytes:
        """Send a command, return the response payload that follows *want_msgid*.

        Sequence handling mirrors the host: the MCU stamps every block it sends
        with the sequence number it expects next.  A different value than
        ``seq + 1`` is a NAK carrying the number to resume from.
        """
        last_error = "timeout"
        for _ in range(attempts):
            seq = self._seq
            expected = (seq + 1) & MESSAGE_SEQ_MASK
            self.transport.write(build_block(seq, payload))
            deadline = self._clock() + timeout
            resync: Optional[int] = None
            while self._clock() < deadline and resync is None:
                chunk = self.transport.read(256, min(0.1, max(0.0, deadline - self._clock())))
                for block in self._parser.feed(chunk) if chunk else []:
                    if block.seq != expected:
                        if block.seq != seq:
                            resync = block.seq
                        else:
                            last_error = "request not accepted"
                        break
                    self._seq = expected
                    if not block.payload:
                        continue  # pure ack, the data block follows
                    msgid, pos = vlq_decode(block.payload, 0)
                    if msgid == want_msgid:
                        return block.payload[pos:]
                    # Unsolicited message (stats, shutdown report): keep waiting.
            if resync is not None:
                self._seq = resync
                last_error = "sequence resync"
        raise ProtocolError("MCU did not answer (%s)" % last_error)


def read_dictionary(
    transport: Transport, *, timeout: float = 1.0, max_bytes: int = 256 * 1024, clock=time.monotonic
) -> McuDictionary:
    """Run the identify handshake and return the parsed data dictionary."""
    link = KlipperLink(transport, clock=clock)
    transport.discard_input()
    transport.write(bytes([MESSAGE_SYNC]))
    data = bytearray()
    while True:
        payload = vlq_encode(MSGID_IDENTIFY) + vlq_encode(len(data)) + vlq_encode(IDENTIFY_CHUNK)
        body = link.request(payload, MSGID_IDENTIFY_RESPONSE, timeout=timeout)
        offset, pos = vlq_decode(body, 0)
        if pos >= len(body):
            raise ProtocolError("malformed identify_response from MCU")
        length = body[pos]
        chunk = body[pos + 1 : pos + 1 + length]
        if offset != len(data):
            raise ProtocolError("identify_response out of order (offset %d, expected %d)" % (offset, len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ProtocolError("MCU data dictionary larger than %d bytes" % max_bytes)
    if not data:
        raise ProtocolError("MCU returned an empty data dictionary")
    return parse_dictionary(bytes(data))


