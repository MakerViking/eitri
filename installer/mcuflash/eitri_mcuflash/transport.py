# SPDX-License-Identifier: Apache-2.0
"""Serial transport.

Dependency-free (termios + select) so the same code runs on the stock image
(Python 3.7, no pyserial) and on Eitri.  Non-standard rates such as the 250000
baud Katapult uses are programmed through the Linux termios2 ioctls, the same
mechanism pyserial uses.
"""
from __future__ import annotations

import abc
import errno
import fcntl
import os
import select
import struct
import termios
import time
from typing import List, Tuple

from .errors import TransportError

# asm-generic ioctl numbers (identical on x86-64 and aarch64).
_TCGETS2 = 0x802C542A
_TCSETS2 = 0x402C542B
_BOTHER = 0o010000
_CBAUD = 0o010017
_TERMIOS2 = struct.Struct("=IIIIB19sII")  # 44 bytes


class Transport(abc.ABC):
    """Minimal byte pipe to one MCU."""

    path = ""
    baud = 0

    @abc.abstractmethod
    def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def read(self, size: int, timeout: float) -> bytes:
        """Return 1..size bytes, or b"" if nothing arrived within *timeout*."""

    @abc.abstractmethod
    def discard_input(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    def read_until_idle(self, timeout: float, quiet: float = 0.05) -> bytes:
        """Collect everything that arrives until the line goes quiet."""
        out = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.read(4096, min(quiet, max(0.0, deadline - time.monotonic())))
            if not chunk:
                if out:
                    break
                continue
            out += chunk
        return out

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class PosixSerial(Transport):
    def __init__(self, path: str, baud: int, exclusive: bool = True) -> None:
        self.path = path
        self.baud = baud
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise TransportError(_describe_open_error(path, exc), port=path) from exc
        try:
            if exclusive:
                try:
                    fcntl.ioctl(self._fd, termios.TIOCEXCL)
                except OSError:
                    # Not a tty (tests use pipes/ptys); exclusivity is best effort.
                    pass
            self._configure(baud)
        except TransportError:
            os.close(self._fd)
            raise
        except OSError as exc:
            os.close(self._fd)
            raise TransportError("cannot configure %s at %d baud: %s" % (path, baud, exc), port=path) from exc

    def _configure(self, baud: int) -> None:
        fd = self._fd
        attrs = termios.tcgetattr(fd)
        iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = attrs
        iflag = 0
        oflag = 0
        lflag = 0
        cflag = (cflag & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | getattr(termios, "CRTSCTS", 0))) | (
            termios.CS8 | termios.CREAD | termios.CLOCAL
        )
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        std = getattr(termios, "B%d" % baud, None)
        if std is not None:
            termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, std, std, cc])
        else:
            termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, termios.B38400, termios.B38400, cc])
            self._set_custom_baud(baud)
        termios.tcflush(fd, termios.TCIOFLUSH)

    def _set_custom_baud(self, baud: int) -> None:
        buf = bytearray(_TERMIOS2.size)
        try:
            fcntl.ioctl(self._fd, _TCGETS2, buf)
            c_iflag, c_oflag, c_cflag, c_lflag, c_line, c_cc, _is, _os = _TERMIOS2.unpack(bytes(buf))
            c_cflag = (c_cflag & ~_CBAUD) | _BOTHER
            fcntl.ioctl(self._fd, _TCSETS2, _TERMIOS2.pack(c_iflag, c_oflag, c_cflag, c_lflag, c_line, c_cc, baud, baud))
        except OSError as exc:
            raise TransportError(
                "kernel refused custom baud rate %d on %s: %s" % (baud, self.path, exc), port=self.path
            ) from exc

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + 5.0 + len(data) * 10.0 / max(self.baud, 1200)
        while view:
            try:
                n = os.write(self._fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TransportError("write timeout on %s" % self.path, port=self.path) from None
                select.select([], [self._fd], [], 0.1)
            except OSError as exc:
                raise TransportError("write failed on %s: %s" % (self.path, exc), port=self.path) from exc

    def read(self, size: int, timeout: float) -> bytes:
        try:
            ready, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
            if not ready:
                return b""
            return os.read(self._fd, size)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno == errno.EIO:
                # Peer (pty master) went away.
                return b""
            raise TransportError("read failed on %s: %s" % (self.path, exc), port=self.path) from exc

    def discard_input(self) -> None:
        try:
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None  # type: ignore[assignment]


def _describe_open_error(path: str, exc: OSError) -> str:
    if exc.errno == errno.ENOENT:
        return "serial device %s does not exist" % path
    if exc.errno == errno.EACCES:
        return "no permission to open %s (run as root or join the dialout group)" % path
    if exc.errno == errno.EBUSY:
        return "serial device %s is busy" % path
    return "cannot open %s: %s" % (path, exc)


def port_users(path: str, proc_root: str = "/proc") -> List[Tuple[int, str]]:
    """Processes that currently hold *path* open (pid, command line).

    Klipper, Klarity's predecessor `xindi`/`makerbase-client` and friends keep
    the MCU UARTs open; talking to an MCU underneath them corrupts both sides.
    """
    if not os.path.exists(path):
        return []
    users: List[Tuple[int, str]] = []
    me = os.getpid()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    real = os.path.realpath(path)
    for entry in entries:
        if not entry.isdigit() or int(entry) == me:
            continue
        fd_dir = os.path.join(proc_root, entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target == real or target == path:
                users.append((int(entry), _cmdline(proc_root, entry)))
                break
    return sorted(users)


def _cmdline(proc_root: str, pid: str) -> str:
    try:
        with open(os.path.join(proc_root, pid, "cmdline"), "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return "?"


def open_transport(path: str, baud: int) -> Transport:
    return PosixSerial(path, baud)


class TransportFactory:
    """Indirection so tests can inject fake buses: factory(path, baud) -> Transport."""

    def __init__(self, opener=open_transport) -> None:
        self._opener = opener

    def __call__(self, path: str, baud: int) -> Transport:
        return self._opener(path, baud)
