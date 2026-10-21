# SPDX-License-Identifier: Apache-2.0
"""The RAUC bootloader backend and the sector format U-Boot shares with it."""
from __future__ import annotations

import os

import pytest
from conftest import LIBEXEC, need, run

BACKEND = LIBEXEC / "rauc-bootloader"
SECTOR = 17  # any sector; the real one is 8128


@pytest.fixture
def state(tmp_path, cmdline):
    """A file standing in for the eMMC, plus the environment pointing at it."""
    need("dd", "tr")
    dev = tmp_path / "emmc.img"
    dev.write_bytes(b"\xaa" * 512 * 32)  # non-zero so stale bytes would show
    cl = cmdline("root=/dev/mmcblk1p2 ro rauc.slot=A eitri.disk=" + str(dev))
    env = {
        **os.environ,
        "EITRI_STATE_DEV": str(dev),
        "EITRI_STATE_SECTOR": str(SECTOR),
        "EITRI_CMDLINE": str(cl),
        "EITRI_LOCK": str(tmp_path / "lock"),
    }

    class State:
        path = dev

        def call(self, *args):
            return run([BACKEND, *args], env=env)

        def sector(self) -> bytes:
            return dev.read_bytes()[SECTOR * 512 : (SECTOR + 1) * 512]

        def write_sector(self, text: str) -> None:
            data = bytearray(dev.read_bytes())
            data[SECTOR * 512 : (SECTOR + 1) * 512] = text.encode().ljust(512, b"\0")
            dev.write_bytes(bytes(data))

    return State()


def test_blank_sector_means_default_order(state):
    assert state.call("get-primary").stdout == "A\n"
    assert state.call("get-state", "A").stdout == "good\n"
    assert state.call("get-state", "B").stdout == "good\n"


def test_get_current_reads_the_kernel_command_line(state):
    assert state.call("get-current").stdout == "A\n"


def test_set_primary_puts_the_slot_first_with_full_attempts(state):
    assert state.call("set-primary", "B").returncode == 0
    assert state.call("get-primary").stdout == "B\n"
    text = state.sector().rstrip(b"\0").decode()
    assert text.splitlines() == ["BOOT_ORDER=B A", "BOOT_A_LEFT=3", "BOOT_B_LEFT=3", "EITRI_STATE=1"]


def test_the_sector_is_padded_with_zeros_and_other_sectors_are_untouched(state):
    before = state.path.read_bytes()
    state.call("set-primary", "B")
    after = state.path.read_bytes()
    lo, hi = SECTOR * 512, (SECTOR + 1) * 512
    assert after[:lo] == before[:lo] and after[hi:] == before[hi:]
    body = after[lo:hi].rstrip(b"\0")
    assert b"\0" not in body and len(after[lo:hi]) == 512


def test_mark_bad_skips_the_slot_and_good_restores_it(state):
    state.call("set-primary", "B")
    state.call("set-state", "B", "bad")
    assert state.call("get-state", "B").stdout == "bad\n"
    assert state.call("get-primary").stdout == "A\n", "a slot without attempts is not the primary"
    state.call("set-state", "B", "good")
    assert state.call("get-primary").stdout == "B\n"


def test_reads_what_uboot_writes(state):
    # Exactly what "env export -t" produces in U-Boot: sorted lines, NUL padded.
    state.write_sector("BOOT_A_LEFT=1\nBOOT_B_LEFT=0\nBOOT_ORDER=B A\nEITRI_STATE=1\n")
    assert state.call("get-primary").stdout == "A\n"
    assert state.call("get-state", "A").stdout == "good\n"
    assert state.call("get-state", "B").stdout == "bad\n"


def test_every_slot_out_of_attempts_falls_back_to_the_first_in_order(state):
    state.write_sector("BOOT_A_LEFT=0\nBOOT_B_LEFT=0\nBOOT_ORDER=B A\nEITRI_STATE=1\n")
    assert state.call("get-primary").stdout == "B\n"


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "\xff\xfe junk",
        "BOOT_ORDER=B A\nBOOT_B_LEFT=0\n",  # no EITRI_STATE marker: not trusted
        "BOOT_ORDER=C D\nBOOT_A_LEFT=x\nEITRI_STATE=1\n",
    ],
)
def test_an_invalid_sector_means_defaults(state, garbage):
    state.write_sector(garbage)
    assert state.call("get-primary").stdout == "A\n"
    assert state.call("get-state", "B").stdout == "good\n"


def test_refuses_to_write_when_booted_from_another_disk(state, cmdline, tmp_path):
    other = cmdline("root=/dev/mmcblk0p2 rauc.slot=A eitri.disk=/dev/mmcblk0")
    env = {**os.environ, "EITRI_STATE_DEV": str(state.path), "EITRI_STATE_SECTOR": str(SECTOR),
           "EITRI_CMDLINE": str(other), "EITRI_LOCK": str(tmp_path / "lock")}
    before = state.path.read_bytes()
    result = run([BACKEND, "set-primary", "B"], env=env)
    assert result.returncode != 0 and "refusing" in result.stderr
    assert state.path.read_bytes() == before
    # Reading stays allowed.
    assert run([BACKEND, "get-primary"], env=env).returncode == 0


@pytest.mark.parametrize("line", [
    # the in-place boot.scr (stock U-Boot), e.g. a bench microSD card next to a stock eMMC
    "root=PARTUUID=e17a5d01-05 ro rootwait eitri.slot=A eitri.data=PARTUUID=e17a5d01-07",
    "root=/dev/mmcblk1p2 rauc.slot=A",
])
def test_refuses_to_write_without_proof_that_the_eitri_uboot_started_this_disk(state, cmdline, tmp_path, line):
    """No eitri.disk= means another boot manager: the raw sector on the eMMC (the stock U-Boot's area) is not ours."""
    env = {**os.environ, "EITRI_STATE_DEV": str(state.path), "EITRI_STATE_SECTOR": str(SECTOR),
           "EITRI_CMDLINE": str(cmdline(line)), "EITRI_LOCK": str(tmp_path / "lock")}
    before = state.path.read_bytes()
    for args in (("set-primary", "B"), ("set-state", "A", "good"), ("set-state", "B", "bad")):
        result = run([BACKEND, *args], env=env)
        assert result.returncode != 0 and "refusing" in result.stderr, args
    assert state.path.read_bytes() == before


@pytest.mark.parametrize("args", [(), ("bogus",), ("set-primary",), ("set-primary", "C"), ("set-state", "A", "meh"),
                                  ("get-state", "Z")])
def test_bad_usage_fails(state, args):
    assert state.call(*args).returncode != 0
