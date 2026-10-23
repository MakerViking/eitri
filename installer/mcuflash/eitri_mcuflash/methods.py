# SPDX-License-Identifier: Apache-2.0
"""Flash methods.

katapult      Reset into Katapult over the UART, read back + write the application
              region.  The only method that can also back the old firmware up.
sdcard-serial Klipper's spi_flash.py: stage the image on the board's own SD card
              through the running firmware, then reset into the SD bootloader.
sdcard-file   Copy the image onto a microSD card the operator will move to the
              board (last resort; needs the case open).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Optional

from .errors import FlashError, ProtocolError
from .events import EventSink
from .firmware import DEFAULT_KLIPPER_DIR, FirmwareArtifact, extract_embedded_dictionary_raw
from .katapult_proto import KatapultClient
from .klipper_proto import SERIAL_BOOTLOADER_REQUEST
from .privs import as_owner_of
from .probe import KATAPULT, PortProbe
from .proc import Runner, run
from .registry import BoardDef, Registry
from .transport import Transport, port_users

Opener = Callable[[str, int], Transport]
ProgressFn = Callable[[float], None]

KATAPULT_METHOD = "katapult"
SDCARD_SERIAL = "sdcard-serial"
SDCARD_FILE = "sdcard-file"


@dataclass
class FlashContext:
    registry: Registry
    opener: Opener
    events: EventSink
    runner: Runner = run
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    klipper_dir: str = DEFAULT_KLIPPER_DIR
    klippy_python: Optional[str] = None
    katapult_tool: Optional[str] = None  # path to Katapult's scripts/flashtool.py
    katapult_backend: str = "auto"  # auto | native | flashtool
    sd_mount: Optional[str] = None
    work_dir: str = "/tmp"
    identify_timeout: float = 1.0
    exists_fn: Callable[[str], bool] = os.path.exists
    users_fn: Callable[[str], List[Any]] = port_users


def sdcard_capable(probe: PortProbe) -> bool:
    d = probe.dictionary
    if d is None:
        return False
    names = set(d.command_ids())
    return bool(names & {"config_spi", "config_sdio"})


def choose_method(ctx: FlashContext, board: BoardDef, probe: PortProbe) -> Optional[str]:
    """Pick the flash method for a board from what it exposes right now."""
    if probe.state == KATAPULT and board.allow_katapult:
        return KATAPULT_METHOD
    for method in board.methods:
        if method == KATAPULT_METHOD:
            return method
        if method == SDCARD_SERIAL and sdcard_capable(probe) and board.sdcard.get("board_type"):
            return method
        if method == SDCARD_FILE and ctx.sd_mount:
            return method
    return None


def _pct(events: EventSink, step: str, board: str, base: float = 0.0, span: float = 100.0) -> ProgressFn:
    """Throttled progress reporter (one event per 2 %)."""
    last = [-100.0]

    def report(frac: float) -> None:
        pct = base + frac * span
        if pct - last[0] >= 2.0 or frac >= 1.0:
            last[0] = pct
            events.progress(step, pct, board=board)

    return report


# ---------------------------------------------------------------------------
class KatapultMethod:
    name = KATAPULT_METHOD

    def __init__(self, ctx: FlashContext, board: BoardDef, port: str) -> None:
        self.ctx = ctx
        self.board = board
        self.port = port
        self.baud = board.katapult_baud   # the rate Katapult actually answered at (it is a compile-time choice)

    # -- entering the bootloader ------------------------------------------------
    def _connect(self, bauds, attempts: int) -> Optional[tuple]:
        for _attempt in range(attempts):
            for baud in bauds:
                tp = self.ctx.opener(self.port, baud)
                client = KatapultClient(tp, clock=self.ctx.clock, sleep=self.ctx.sleep)
                try:
                    client.connect(tries=1, timeout=0.8)
                    self.baud = baud
                    return tp, client
                except ProtocolError:
                    tp.close()
            self.ctx.sleep(0.5)
        return None

    def _request_from_app(self) -> None:
        with self.ctx.opener(self.port, self.board.app_baud) as tp:
            tp.write(SERIAL_BOOTLOADER_REQUEST)
        self.ctx.sleep(1.0)

    @contextlib.contextmanager
    def bootloader(self, already_in_bootloader: bool = False) -> Iterator[KatapultClient]:
        """Yield a connected Katapult client; the transport is closed on exit.

        Leaving the context does NOT start the application - callers decide
        whether to flash first (flash_image() / client.complete()).
        """
        conn = self._connect(self.board.katapult_bauds, 2 if already_in_bootloader else 1)
        if conn is None:
            self.ctx.events.info("requesting bootloader entry from the running firmware", board=self.board.name, step="bootloader")
            self._request_from_app()
            conn = self._connect(self.board.katapult_bauds, 8)
        if conn is None:
            raise FlashError(
                "no Katapult bootloader answered on %s at %s baud after a reset request"
                % (self.port, "/".join(str(b) for b in self.board.katapult_bauds)),
                code="no_bootloader",
                step="bootloader",
                board=self.board.name,
                port=self.port,
                bootloader="unknown",
                recovery="This board may not expose a serial bootloader. Do not erase it; flashing needs an ST-Link on the SWD header (see docs/recovery.md).",
            )
        tp, client = conn
        try:
            yield client
        finally:
            tp.close()

    # -- operations --------------------------------------------------------------
    def read_application(self, client: KatapultClient, mcu: str, progress: Optional[ProgressFn] = None) -> bytes:
        info = client.info
        assert info is not None
        total = self.ctx.registry.flash_size(info.mcu or mcu)
        if total is None:
            raise FlashError(
                "unknown flash size for chip %r; cannot size a complete backup" % (info.mcu or mcu),
                code="unknown_chip",
                step="backup",
                board=self.board.name,
                bootloader="katapult",
                recovery="Add the chip to boards.json (chips) with its flash size.",
            )
        base = self.ctx.registry.flash_base(info.mcu or mcu)
        length = base + total - info.app_start
        if length <= 0:
            raise FlashError("Katapult app start 0x%x is outside flash" % info.app_start, code="bad_layout", step="backup", board=self.board.name)
        data = client.read_region(info.app_start, length, progress)
        trimmed = data.rstrip(b"\xff")
        # keep whole blocks so a restore writes exactly what was there
        keep = ((len(trimmed) + info.block_size - 1) // info.block_size) * info.block_size
        return data[: max(keep, info.block_size)]

    def _use_flashtool(self) -> bool:
        backend = self.ctx.katapult_backend
        if backend == "native":
            return False
        tool = self.ctx.katapult_tool
        have = bool(tool and os.path.isfile(tool))
        if backend == "flashtool" and not have:
            raise FlashError("flashtool backend requested but %s does not exist" % tool, code="no_flashtool", board=self.board.name)
        if not have:
            return False
        check = self.ctx.runner([sys.executable, "-I", "-c", "import serial"])   # -I: never import from the cwd as root
        if check.returncode != 0:
            if backend == "flashtool":
                raise FlashError("flashtool.py needs pyserial, which is not installed", code="no_flashtool", board=self.board.name)
            self.ctx.events.warn("pyserial missing; using the built-in Katapult client instead of flashtool.py", board=self.board.name)
            return False
        return True

    def flash_image(
        self,
        client: KatapultClient,
        image: bytes,
        path_for_tool: str,
        progress: Optional[ProgressFn] = None,
    ) -> str:
        """Write + verify *image* and start the application.  Returns a digest string."""
        if self._use_flashtool():
            client.transport.close()
            argv = [sys.executable, self.ctx.katapult_tool or "", "-d", self.port, "-b", str(self.baud), "-f", path_for_tool]
            lines: List[str] = []

            def on_line(line: str) -> None:
                lines.append(line)
                if progress and "#" in line:
                    progress(min(1.0, line.count("#") / 50.0))

            res = self.ctx.runner(argv, on_line=on_line)
            if res.returncode != 0 or "Programming Complete" not in res.output:
                tail = "\n".join(res.output.strip().splitlines()[-6:])
                raise FlashError(
                    "Katapult flashtool failed:\n" + tail,
                    code="flash_failed",
                    step="flash",
                    board=self.board.name,
                    port=self.port,
                    bootloader="katapult",
                )
            return "flashtool"
        sha1 = client.write_image(image, progress)
        client.verify_image(sha1, len(image))
        client.complete()
        return sha1


# ---------------------------------------------------------------------------
class SdcardSerialMethod:
    name = SDCARD_SERIAL

    def __init__(self, ctx: FlashContext, board: BoardDef, port: str) -> None:
        self.ctx = ctx
        self.board = board
        self.port = port

    def script_path(self) -> str:
        return os.path.join(self.ctx.klipper_dir, "scripts", "spi_flash", "spi_flash.py")

    def preflight_problems(self) -> List[str]:
        problems = []
        if not os.path.isfile(self.script_path()):
            problems.append("Klipper's spi_flash.py not found at %s" % self.script_path())
        py = self.ctx.klippy_python
        if py and not os.path.isfile(py):
            problems.append("klippy python %s does not exist" % py)
        # spi_flash.py compiles its FatFS helper with the host C compiler on first use.  The Eitri base
        # image has no compiler, so a prebuilt fatfs.so (or the upstream toolchain add-on's host gcc) is needed.
        board_type = self.board.sdcard.get("board_type", "")
        defs = os.path.join(os.path.dirname(self.script_path()), "board_defs.py")
        if os.path.isfile(defs) and board_type:
            with open(defs, "r", encoding="utf-8", errors="replace") as fh:
                known = ("'%s'" % board_type) in fh.read() or ('"%s"' % board_type) in fh.read()
            if not known:
                problems.append(
                    "this firmware tree's spi_flash board_defs.py has no %r definition (Kalico ships none); "
                    "use the microSD file method (--sd-mount) or a Klipper script tree" % board_type)
        lib = os.path.join(os.path.dirname(self.script_path()), "fatfs.so")
        if not os.path.isfile(lib) and shutil.which("gcc") is None:
            problems.append(
                "spi_flash.py needs %s (prebuilt) or a host gcc to build it, and neither exists; "
                "build it once with a compiler or ship it in the data bundle" % lib)
        return problems

    def flash_image(self, image_path: str, artifact: Optional[FirmwareArtifact], progress: Optional[ProgressFn] = None) -> None:
        board_type = self.board.sdcard.get("board_type", "")
        argv = [self.ctx.klippy_python or sys.executable, self.script_path(), "-b", str(self.board.app_baud)]
        tmp: Optional[str] = None
        try:
            if artifact is not None:
                raw = extract_embedded_dictionary_raw(artifact.read())
                if raw is not None:
                    # the exact bytes the build embedded: spi_flash.py compares the dictionary it reads back from
                    # the board byte for byte, so a re-serialised copy (other separators or key order) would report
                    # "Version Mismatch" after a good flash
                    fd, tmp = tempfile.mkstemp(prefix="klipper-", suffix=".dict", dir=self.ctx.work_dir)
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(raw)
                    os.chmod(tmp, 0o644)                 # read by spi_flash.py, which runs as the clone's owner
                    argv += ["-d", tmp]
            argv += [self.port, board_type, image_path]
            # spi_flash.py (and the fatfs.so it builds, and the venv's python) come from the service user's trees on /data:
            # never run as root.  The clone's owner (eitri: dialout) can open the serial port.
            argv = as_owner_of(self.ctx.klipper_dir, argv)
            tail: List[str] = []

            def on_line(line: str) -> None:
                tail.append(line)
                self.ctx.events.log("debug", line, board=self.board.name, step="flash")
                if progress and line.startswith("Uploading"):
                    progress(0.5)

            res = self.ctx.runner(argv, cwd=self.ctx.klipper_dir, on_line=on_line)
            if res.returncode != 0:
                raise FlashError(
                    "spi_flash.py failed:\n" + "\n".join(res.output.strip().splitlines()[-8:]),
                    code="flash_failed",
                    step="flash",
                    board=self.board.name,
                    port=self.port,
                    bootloader="sdcard",
                    recovery=self.board.recovery,
                )
            if progress:
                progress(1.0)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)


class SdcardFileMethod:
    name = SDCARD_FILE

    def __init__(self, ctx: FlashContext, board: BoardDef, port: str) -> None:
        self.ctx = ctx
        self.board = board
        self.port = port

    def stage(self, image_path: str) -> str:
        mount = self.ctx.sd_mount
        if not mount or not os.path.isdir(mount):
            raise FlashError("--sd-mount is not a mounted directory", code="no_sdcard", step="flash", board=self.board.name)
        dest = os.path.join(mount, self.board.sdcard.get("firmware_name", "qd_mcu.bin"))
        shutil.copyfile(image_path, dest)
        # The stock bootloader flashes whatever file it finds, so a half-written file on the card would become a
        # corrupt application.  Force the data and the directory entry to the card before anything is read back or
        # the operator is told to pull it.
        self._sync_file(dest)
        with open(image_path, "rb") as b:
            want = b.read()
        with open(dest, "rb") as a:
            got = a.read()
        if got != want:
            raise FlashError("SD card copy does not match the source image", code="flash_failed", step="flash", board=self.board.name)
        return dest

    @staticmethod
    def _sync_file(path: str) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        except OSError:
            dfd = None
        if dfd is not None:
            try:
                os.fsync(dfd)
            except OSError:
                pass
            finally:
                os.close(dfd)
        if hasattr(os, "sync"):
            os.sync()


def make_method(name: str, ctx: FlashContext, board: BoardDef, port: str):
    if name == KATAPULT_METHOD:
        return KatapultMethod(ctx, board, port)
    if name == SDCARD_SERIAL:
        return SdcardSerialMethod(ctx, board, port)
    if name == SDCARD_FILE:
        return SdcardFileMethod(ctx, board, port)
    raise FlashError("unknown flash method %r" % name, code="method")
