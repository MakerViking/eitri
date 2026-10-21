# SPDX-License-Identifier: Apache-2.0
"""Katapult serial bootloader client.

Wire format as implemented by Katapult (https://github.com/Arksine/katapult,
GPL-3.0; this is an independent implementation of the protocol).

    request : 01 88 | cmd | words | payload (words*4 bytes) | crc16 LE | 99 03
    response: 01 88 | ack | words | cmd(u32) + payload       | crc16 LE | 99 03

The CRC covers everything between the header and the CRC itself.
"""
from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .errors import FlashError, ProtocolError
from .klipper_proto import crc16_ccitt
from .transport import Transport

CMD_HEADER = b"\x01\x88"
CMD_TRAILER = b"\x99\x03"

CMD_CONNECT = 0x11
CMD_SEND_BLOCK = 0x12
CMD_SEND_EOF = 0x13
CMD_REQUEST_BLOCK = 0x14
CMD_COMPLETE = 0x15

ACK_SUCCESS = 0xA0
NACK = 0xF1
ACK_ERROR = 0xF2
ACK_BUSY = 0xF3

VALID_BLOCK_SIZES = (64, 128, 256, 512)

ProgressFn = Callable[[float], None]


def build_command(cmd: int, payload: bytes = b"") -> bytes:
    if len(payload) % 4:
        raise ValueError("Katapult payloads are whole 32-bit words")
    body = bytes([cmd, (len(payload) // 4) & 0xFF]) + payload
    return CMD_HEADER + body + struct.pack("<H", crc16_ccitt(body)) + CMD_TRAILER


@dataclass
class Frame:
    ack: int
    cmd: int
    payload: bytes


def parse_frame(data: bytes) -> Frame:
    """Validate and unpack one complete response frame."""
    if len(data) < 8 or data[:2] != CMD_HEADER or data[-2:] != CMD_TRAILER:
        raise ProtocolError("malformed Katapult frame")
    words = data[3]
    if len(data) != words * 4 + 8:
        raise ProtocolError("Katapult frame length mismatch")
    (crc,) = struct.unpack("<H", data[-4:-2])
    if crc != crc16_ccitt(data[2:-4]):
        raise ProtocolError("Katapult frame CRC mismatch")
    cmd = struct.unpack("<I", data[4:8])[0] if words else 0
    return Frame(ack=data[2], cmd=cmd, payload=bytes(data[8 : 4 + words * 4]))


@dataclass
class KatapultInfo:
    protocol_version: Tuple[int, int, int]
    software_version: str
    app_start: int
    block_size: int
    mcu: str

    def summary(self) -> dict:
        return {
            "protocol_version": ".".join(str(v) for v in self.protocol_version),
            "software_version": self.software_version,
            "app_start": "0x%08x" % self.app_start,
            "block_size": self.block_size,
            "mcu": self.mcu,
        }


class KatapultClient:
    def __init__(self, transport: Transport, *, clock=time.monotonic, sleep=time.sleep) -> None:
        self.transport = transport
        self._clock = clock
        self._sleep = sleep
        self.info: Optional[KatapultInfo] = None
        self._buf = bytearray()

    # -- framing -------------------------------------------------------------
    def _read_frame(self, timeout: float) -> bytes:
        deadline = self._clock() + timeout
        while True:
            while len(self._buf) >= 2 and bytes(self._buf[:2]) != CMD_HEADER:
                del self._buf[0]
            if len(self._buf) >= 4:
                total = self._buf[3] * 4 + 8
                if len(self._buf) >= total:
                    frame = bytes(self._buf[:total])
                    del self._buf[:total]
                    return frame
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ProtocolError("Katapult did not answer")
            self._buf.extend(self.transport.read(1024, min(0.1, remaining)))

    def command(self, cmd: int, payload: bytes = b"", tries: int = 5, timeout: float = 2.0) -> bytes:
        out = build_command(cmd, payload)
        last = "no answer"
        for _ in range(tries):
            self._buf.clear()
            self.transport.discard_input()
            self.transport.write(out)
            try:
                frame = parse_frame(self._read_frame(timeout))
            except ProtocolError as exc:
                last = exc.message
                self._sleep(0.2)
                continue
            if frame.ack == ACK_BUSY:
                last = "bootloader busy"
                self._sleep(1.5)
                continue
            if frame.ack != ACK_SUCCESS:
                last = "bootloader answered 0x%02x" % frame.ack
                self._sleep(0.2)
                continue
            if frame.cmd != cmd:
                last = "bootloader acknowledged the wrong command"
                continue
            return frame.payload
        raise ProtocolError("Katapult command 0x%02x failed: %s" % (cmd, last), bootloader="katapult")

    # -- operations ------------------------------------------------------------
    def connect(self, tries: int = 3, timeout: float = 1.0) -> KatapultInfo:
        payload = self.command(CMD_CONNECT, tries=tries, timeout=timeout)
        if len(payload) < 12:
            raise ProtocolError("short Katapult CONNECT response", bootloader="katapult")
        ver, start, block = struct.unpack("<4sII", payload[:12])
        if block not in VALID_BLOCK_SIZES:
            raise ProtocolError("Katapult reports invalid block size %d" % block, bootloader="katapult")
        proto = tuple(reversed(ver[:3]))
        tail = payload[12:].rstrip(b"\0")
        software = "?"
        if proto >= (1, 1, 0):
            parts = tail.split(b"\0", 1)
            mcu = parts[0].decode("ascii", "replace")
            if len(parts) == 2:
                software = parts[1].decode("ascii", "replace")
        else:
            mcu = tail.decode("ascii", "replace")
        self.info = KatapultInfo(proto, software, start, block, mcu)  # type: ignore[arg-type]
        return self.info

    def _need_info(self) -> KatapultInfo:
        if self.info is None:
            raise FlashError("Katapult session not connected", code="internal")
        return self.info

    def read_block(self, addr: int) -> bytes:
        info = self._need_info()
        payload = self.command(CMD_REQUEST_BLOCK, struct.pack("<I", addr))
        (got,) = struct.unpack("<I", payload[:4])
        if got != addr or len(payload) - 4 != info.block_size:
            raise ProtocolError("Katapult returned the wrong block (0x%x for 0x%x)" % (got, addr), bootloader="katapult")
        return payload[4:]

    def read_region(self, start: int, length: int, progress: Optional[ProgressFn] = None) -> bytes:
        info = self._need_info()
        out = bytearray()
        addr = start
        end = start + length
        while addr < end:
            out.extend(self.read_block(addr))
            addr += info.block_size
            if progress:
                progress(min(1.0, (addr - start) / float(length)))
        return bytes(out[:length])

    def write_image(self, image: bytes, progress: Optional[ProgressFn] = None) -> str:
        """Write *image* at the application start; returns the SHA-1 of what was sent."""
        info = self._need_info()
        sha = hashlib.sha1()
        addr = info.app_start
        total = len(image)
        for off in range(0, total, info.block_size):
            block = image[off : off + info.block_size]
            if len(block) < info.block_size:
                block += b"\xff" * (info.block_size - len(block))
            sha.update(block)
            for _ in range(3):
                resp = self.command(CMD_SEND_BLOCK, struct.pack("<I", addr) + block, tries=3, timeout=5.0)
                if struct.unpack("<I", resp[:4])[0] == addr:
                    break
                self._sleep(0.1)
            else:
                raise ProtocolError("flash write failed at 0x%08x" % addr, bootloader="katapult")
            addr += info.block_size
            if progress:
                progress(min(1.0, (off + info.block_size) / float(total)))
        self.command(CMD_SEND_EOF)
        return sha.hexdigest()

    def verify_image(self, sha1_hex: str, length: int, progress: Optional[ProgressFn] = None) -> None:
        info = self._need_info()
        padded = ((length + info.block_size - 1) // info.block_size) * info.block_size
        data = self.read_region(info.app_start, padded, progress)
        got = hashlib.sha1(data).hexdigest()
        if got != sha1_hex:
            raise ProtocolError(
                "flash read-back mismatch (expected sha1 %s, read %s)" % (sha1_hex, got), bootloader="katapult"
            )

    def complete(self) -> None:
        """Leave the bootloader and start the application."""
        self.command(CMD_COMPLETE)
