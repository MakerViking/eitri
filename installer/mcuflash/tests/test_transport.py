# SPDX-License-Identifier: Apache-2.0
"""PosixSerial against a pty: real termios code path, simulated MCU on the master side."""
import os
import pty
import select
import threading

import pytest

from fakes import FakeBoard
from eitri_mcuflash import klipper_proto as kl
from eitri_mcuflash.errors import TransportError
from eitri_mcuflash.transport import PosixSerial, port_users


class PtyMcu(threading.Thread):
    def __init__(self, master_fd, board, baud):
        super().__init__(daemon=True)
        self.fd, self.board, self.baud = master_fd, board, baud
        self.stop = False

    def run(self):
        while not self.stop:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                return
            reply = self.board.receive(self.baud, data)
            if reply:
                os.write(self.fd, reply)


@pytest.fixture
def pty_pair():
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    yield master, slave, path
    os.close(master)
    os.close(slave)


def test_identify_over_real_termios(pty_pair):
    master, _slave, path = pty_pair
    board = FakeBoard("b", version="v0.13.0-1-g1234567")
    mcu = PtyMcu(master, board, 500000)
    mcu.start()
    try:
        with PosixSerial(path, 500000) as tp:
            d = kl.read_dictionary(tp, timeout=1.0)
        assert d.version == "v0.13.0-1-g1234567"
    finally:
        mcu.stop = True
        mcu.join(1)


def test_custom_baud_250000_is_accepted_or_clearly_refused(pty_pair):
    _m, _s, path = pty_pair
    try:
        tp = PosixSerial(path, 250000)
    except TransportError as exc:
        assert "250000" in exc.message  # a clear message, not a traceback
        pytest.skip("pty does not support termios2 custom baud on this kernel")
    tp.close()


def test_open_errors_are_actionable(tmp_path):
    with pytest.raises(TransportError, match="does not exist"):
        PosixSerial(str(tmp_path / "nope"), 500000)


def test_port_users_finds_holder(tmp_path):
    f = tmp_path / "dev"
    f.write_text("x")
    fh = open(str(f))
    try:
        users = port_users(str(f))
        # we exclude our own pid, so spawn a child that holds it open
    finally:
        fh.close()
    assert users == []
    import subprocess, sys

    child = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1]); print('r', flush=True); time.sleep(5)", str(f)],
                             stdout=subprocess.PIPE)
    try:
        child.stdout.readline()
        users = port_users(str(f))
        assert [p for p, _ in users] == [child.pid]
    finally:
        child.kill()
        child.wait()
