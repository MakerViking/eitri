# SPDX-License-Identifier: Apache-2.0
"""Simulated STM32 boards for tests: a Klipper-protocol application plus Katapult.

No real serial port is ever opened.  A FakeBoard implements the MCU side of both
wire protocols and keeps a real flash array, so a flash run is judged by what
ends up in flash, not by what the code claims it did.
"""
from __future__ import annotations

import json
import struct
import zlib
from typing import Callable, Dict, List, Optional

from eitri_mcuflash import katapult_proto as kp
from eitri_mcuflash import klipper_proto as kl
from eitri_mcuflash.errors import TransportError
from eitri_mcuflash.firmware import extract_embedded_dictionary
from eitri_mcuflash.transport import Transport


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


# The constants the live X-Plus 4 reports for each chip (klippy.log), so fake images look like the real ones.
CHIP_CONFIG = {
    "stm32f401xc": {"CLOCK_FREQ": 84000000, "RESERVE_PINS_serial": "PA10,PA9", "RESERVE_PINS_crystal": "PH0,PH1"},
    "stm32f103xe": {"CLOCK_FREQ": 72000000, "RESERVE_PINS_serial": "PB7,PB6"},
}


def make_dictionary(version: str, mcu: str = "stm32f401xc", spi: bool = False, extra_config=None, kconfig: str = "") -> dict:
    commands = {
        "identify offset=%u count=%c": 1,
        "get_uptime": 2,
        "get_config": 3,
        "reset": 4,
        "config_spi oid=%c bus=%u": 5 if spi else 6,
    }
    if not spi:
        del commands["config_spi oid=%c bus=%u"]
    cfg = {"MCU": mcu, "CLOCK_FREQ": 84000000, "SERIAL_BAUD": 500000, "RECEIVE_WINDOW": 192}
    cfg.update(CHIP_CONFIG.get(mcu, {}))
    cfg.update(extra_config or {})
    return {
        "kconfig": kconfig,
        "app": "Klipper",
        "version": version,
        "build_versions": "gcc: (fake) 12.2 binutils: 2.40",
        "commands": commands,
        "responses": {"identify_response offset=%u data=%.*s": 0},
        "enumerations": {"pin": {"PA0": 0, "PA1": 1}},
        "config": cfg,
    }


def make_image(version: str, mcu: str = "stm32f401xc", spi: bool = False, size: int = 4096, extra_config=None, kconfig: str = "",
               app_address: int = 0x08008000) -> bytes:
    """Something shaped like klipper.bin: a Cortex-M vector table for *app_address*, opaque code and the zlib data dictionary."""
    doc = make_dictionary(version, mcu, spi, extra_config, kconfig)
    comp = zlib.compress(json.dumps(doc).encode(), 9)
    vectors = (0x20010000).to_bytes(4, "little") + ((app_address + 0x41) | 1).to_bytes(4, "little")
    body = vectors + bytes(range(1, 250)) * 3 + comp
    return body + b"\xA5" * max(0, size - len(body))


class FakeBoard:
    def __init__(
        self,
        name: str,
        *,
        version: str = "stock-1.7.1",
        mcu: str = "stm32f401xc",
        flash_kib: int = 256,
        app_start: int = 0x08008000,
        flash_base: int = 0x08000000,
        block_size: int = 64,
        has_katapult: bool = True,
        bootloader_entry: bool = True,
        spi: bool = False,
        app_baud: int = 500000,
        katapult_baud: int = 250000,
        sd_flash: Optional[Callable[["FakeBoard", bytes], None]] = None,
        seq_state: int = 9,
    ) -> None:
        self.name = name
        self.mcu = mcu
        self.flash_base = flash_base
        self.flash = bytearray(b"\xff" * (flash_kib * 1024))
        self.app_start = app_start
        self.block_size = block_size
        self.has_katapult = has_katapult
        self.bootloader_entry = bootloader_entry
        self.spi = spi
        self.app_baud = app_baud
        self.katapult_baud = katapult_baud
        self.mode = "app"
        self.next_seq = seq_state & 15  # a "used" MCU: sequence state left over from a previous host
        self._blocks = kl.BlockParser()
        self._kbuf = bytearray()
        self._dict_cache: Optional[bytes] = None
        self.sd_flash = sd_flash
        self.log: List[str] = []
        self.broken_versions: set = set()  # images with these versions never boot (stay in the bootloader)
        self.write_fail_at: Optional[int] = None  # flash address where SEND_BLOCK errors once
        self.corrupt_plan: Optional[dict] = None  # {'addr':, 'skip': n, 'count': m}: bit-flip m writes after skipping n
        self.install(make_image(version, mcu, spi))

    # -- helpers ---------------------------------------------------------------
    def install(self, image: bytes) -> None:
        off = self.app_start - self.flash_base
        self.flash[off : off + len(image)] = image
        self._dict_cache = None

    @property
    def app_image(self) -> bytes:
        return bytes(self.flash[self.app_start - self.flash_base :])

    def app_dictionary(self) -> Optional[dict]:
        return extract_embedded_dictionary(self.app_image)

    def _identify_blob(self) -> Optional[bytes]:
        if self._dict_cache is None:
            doc = self.app_dictionary()
            if doc is None:
                return None
            self._dict_cache = zlib.compress(json.dumps(doc).encode())
        return self._dict_cache

    # -- wire ------------------------------------------------------------------
    def receive(self, baud: int, data: bytes) -> bytes:
        if self.mode == "app" and baud == self.app_baud:
            return self._app_rx(data)
        if self.mode == "katapult" and baud == self.katapult_baud:
            return self._kp_rx(data)
        return b""

    def _app_rx(self, data: bytes) -> bytes:
        if kl.SERIAL_BOOTLOADER_REQUEST[1:] in data or kl.SERIAL_BOOTLOADER_REQUEST in data:
            self.log.append("bootloader-request")
            if self.has_katapult and self.bootloader_entry:
                self.mode = "katapult"
                self._kbuf.clear()
            return b""
        out = b""
        for block in self._blocks.feed(data):
            if block.seq != self.next_seq:
                out += kl.build_block(self.next_seq, b"")  # NAK carrying the expected sequence
                continue
            self.next_seq = (self.next_seq + 1) & 15
            if not block.payload:
                continue
            msgid, pos = kl.vlq_decode(block.payload, 0)
            if msgid == kl.MSGID_IDENTIFY:
                offset, pos = kl.vlq_decode(block.payload, pos)
                count, pos = kl.vlq_decode(block.payload, pos)
                blob = self._identify_blob()
                if blob is None:
                    out += kl.build_block(self.next_seq, b"")
                    continue
                chunk = blob[offset : offset + count]
                body = kl.vlq_encode(kl.MSGID_IDENTIFY_RESPONSE) + kl.vlq_encode(offset) + bytes([len(chunk)]) + chunk
                out += kl.build_block(self.next_seq, body)
            else:
                out += kl.build_block(self.next_seq, b"")
        return out

    def _kp_rx(self, data: bytes) -> bytes:
        self._kbuf.extend(data)
        out = b""
        while True:
            while len(self._kbuf) >= 2 and bytes(self._kbuf[:2]) != kp.CMD_HEADER:
                del self._kbuf[0]
            if len(self._kbuf) < 4:
                break
            total = self._kbuf[3] * 4 + 8
            if len(self._kbuf) < total:
                break
            frame = bytes(self._kbuf[:total])
            del self._kbuf[:total]
            cmd = frame[2]
            payload = frame[4:-4]
            out += self._kp_handle(cmd, payload)
        return out

    def _kp_reply(self, cmd: int, payload: bytes = b"", ack: int = kp.ACK_SUCCESS) -> bytes:
        payload = payload + b"\0" * (-(len(payload)) % 4)
        body = bytes([ack, (4 + len(payload)) // 4]) + struct.pack("<I", cmd) + payload
        return kp.CMD_HEADER + body + struct.pack("<H", kl.crc16_ccitt(body)) + kp.CMD_TRAILER

    def _kp_handle(self, cmd: int, payload: bytes) -> bytes:
        if cmd == kp.CMD_CONNECT:
            ver = bytes([0, 1, 1, 0])  # reversed(ver[:3]) == (1, 1, 0)
            info = self.mcu.encode() + b"\0" + b"v0.0.1-fake" + b"\0"
            return self._kp_reply(cmd, ver + struct.pack("<II", self.app_start, self.block_size) + info)
        if cmd == kp.CMD_SEND_BLOCK:
            (addr,) = struct.unpack("<I", payload[:4])
            if self.write_fail_at is not None and addr == self.write_fail_at:
                self.write_fail_at = None
                return self._kp_reply(cmd, ack=kp.ACK_ERROR)
            data = payload[4:]
            plan = self.corrupt_plan
            if plan and addr == plan["addr"]:
                if plan["skip"] > 0:
                    plan["skip"] -= 1
                elif plan["count"] > 0:
                    plan["count"] -= 1
                    data = bytes([data[0] ^ 0xFF]) + data[1:]
            off = addr - self.flash_base
            self.flash[off : off + len(data)] = data
            self._dict_cache = None
            return self._kp_reply(cmd, struct.pack("<I", addr))
        if cmd == kp.CMD_SEND_EOF:
            return self._kp_reply(cmd, struct.pack("<I", 1))
        if cmd == kp.CMD_REQUEST_BLOCK:
            (addr,) = struct.unpack("<I", payload[:4])
            off = addr - self.flash_base
            return self._kp_reply(cmd, struct.pack("<I", addr) + bytes(self.flash[off : off + self.block_size]))
        if cmd == kp.CMD_COMPLETE:
            reply = self._kp_reply(cmd)
            doc = self.app_dictionary()
            if doc is not None and doc.get("version") not in self.broken_versions:
                self.mode = "app"
                self.next_seq = 1  # fresh boot
                self._blocks = kl.BlockParser()
            return reply
        return self._kp_reply(cmd, ack=kp.NACK)


class FakeTransport(Transport):
    def __init__(self, board: FakeBoard, baud: int, path: str, clock: FakeClock) -> None:
        self.board, self.baud, self.path, self._clock = board, baud, path, clock
        self._rx = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self._rx.extend(self.board.receive(self.baud, data))

    def read(self, size: int, timeout: float) -> bytes:
        if not self._rx:
            self._clock.sleep(timeout)
            return b""
        chunk = bytes(self._rx[:size])
        del self._rx[:size]
        return chunk

    def discard_input(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        self.closed = True


class FakeBus:
    """Maps device paths to boards; usable as FlashContext.opener / exists_fn."""

    def __init__(self, clock: Optional[FakeClock] = None) -> None:
        self.clock = clock or FakeClock()
        self.boards: Dict[str, FakeBoard] = {}
        self.busy: Dict[str, list] = {}
        self.opened: List[tuple] = []

    def add(self, path: str, board: FakeBoard) -> FakeBoard:
        self.boards[path] = board
        return board

    def opener(self, path: str, baud: int) -> FakeTransport:
        if path not in self.boards:
            raise TransportError("serial device %s does not exist" % path, port=path)
        self.opened.append((path, baud))
        return FakeTransport(self.boards[path], baud, path, self.clock)

    def exists(self, path: str) -> bool:
        return path in self.boards

    def users(self, path: str):
        return self.busy.get(path, [])
