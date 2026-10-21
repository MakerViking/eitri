# SPDX-License-Identifier: Apache-2.0
"""uboot.env: format, structure and (when a sandbox build is available) behaviour."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from conftest import BOARD

ENV_FILE = BOARD / "uboot.env"
ENV_SIZE = 0x8000  # CONFIG_ENV_SIZE in the built U-Boot


def compile_env(text: str) -> dict[str, str]:
    """Turn the file into variables the way U-Boot's Makefile does.

    The filechk_defaultenv.h recipe drops comment and blank lines, makes every
    newline a NUL, removes a backslash-NUL pair together with the following
    whitespace, and treats each remaining NUL-terminated string as one variable.
    """
    lines = [ln for ln in text.split("\n") if not ln.startswith("#") and ln != ""]
    joined = "\0".join(lines) + "\0"
    joined = re.sub(r"\\\0\s*", "", joined)
    entries = [e for e in joined.split("\0") if e]
    out = {}
    for entry in entries:
        name, sep, value = entry.partition("=")
        assert sep, f"line without '=' became a variable: {entry!r}"
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name), f"stray continuation line became a variable: {entry!r}"
        out[name] = value
    return out


@pytest.fixture(scope="module")
def env():
    return compile_env(ENV_FILE.read_text())


def test_every_line_is_a_variable_or_a_continuation(env):
    # compile_env asserts this; the test documents it and gives the failure a name.
    assert "bootcmd" in env


def test_fits_the_environment_size(env):
    size = sum(len(f"{k}={v}") + 1 for k, v in env.items()) + 1
    assert size < ENV_SIZE // 2, f"{size} bytes leaves too little headroom in a {ENV_SIZE}-byte environment"


def test_is_silent_and_boots_through_the_eitri_script(env):
    assert env["silent"] == "1"
    assert env["bootcmd"] == "run eitri_boot"


def test_state_sector_matches_the_userspace_backend(env):
    backend = (BOARD / "rootfs-overlay/usr/libexec/eitri/rauc-bootloader").read_text()
    sector = int(env["eitri_state_blk"], 16)
    assert sector == 0x3F8000 // 512 == 8128
    assert f"SECTOR=${{EITRI_STATE_SECTOR:-{sector}}}" in backend


def test_partition_numbers_match_genimage_and_rauc(env):
    cfg = (BOARD / "genimage.cfg").read_text()
    order = re.findall(r"^\tpartition (\S+) \{", cfg, re.M)
    in_table = [p for p in order if p not in ("idbloader", "u-boot")]
    assert in_table == ["boot", "rootfs-a", "rootfs-b", "data"]
    assert (int(env["eitri_part_boot"]), int(env["eitri_part_A"]), int(env["eitri_part_B"])) == (1, 2, 3)
    system_conf = (BOARD / "rootfs-overlay/etc/rauc/system.conf").read_text()
    assert "device=/dev/mmcblk1p2" in system_conf and "device=/dev/mmcblk1p3" in system_conf


def test_hush_blocks_balance(env):
    for name, value in env.items():
        words = re.findall(r"(?<![\w$])(if|fi|for|done)(?![\w=])", re.sub(r'"[^"]*"', '""', value))
        assert words.count("if") == words.count("fi"), name
        assert words.count("for") == words.count("done"), name


def test_boot_args_never_enable_a_console(env):
    args = env["eitri_boot_slot"]
    assert "console=" not in args.replace("eitri_extra_bootargs", ""), "uart2 is wired to an MCU"
    assert "rauc.slot=${eitri_slot}" in args and "eitri.disk=/dev/mmcblk" in args


# --- Behaviour in the U-Boot sandbox ----------------------------------------
# Build it with the same environment file (sandbox_defconfig +
# CONFIG_ENV_USE_DEFAULT_ENV_TEXT_FILE) and point EITRI_UBOOT_SANDBOX at the binary.

SANDBOX = os.environ.get("EITRI_UBOOT_SANDBOX")


def select_slot(order: str, a_left: int, b_left: int, force: str = "") -> dict[str, str]:
    stub_load = f'setenv BOOT_ORDER "{order}"; setenv BOOT_A_LEFT {a_left}; setenv BOOT_B_LEFT {b_left}'
    script = (
        f"setenv silent; setenv eitri_state_load '{stub_load}'; "
        "setenv eitri_state_save 'echo SAVE ${BOOT_ORDER}/${BOOT_A_LEFT}/${BOOT_B_LEFT}'; "
        f"setenv eitri_force_slot '{force}'; run eitri_select_slot; echo RESULT ${{eitri_slot}}"
    )
    out = subprocess.run([SANDBOX, "-T", "-c", script], capture_output=True, text=True, timeout=30,
                         cwd=Path(SANDBOX).parent).stdout
    result = re.search(r"^RESULT (\w*)$", out, re.M)
    save = re.search(r"^SAVE (.*)$", out, re.M)
    return {"slot": result.group(1) if result else "", "saved": save.group(1) if save else ""}


@pytest.mark.skipif(not SANDBOX, reason="set EITRI_UBOOT_SANDBOX to a sandbox U-Boot built with uboot.env")
class TestSandbox:
    def test_first_boot_uses_a_and_spends_an_attempt(self):
        assert select_slot("A B", 3, 3) == {"slot": "A", "saved": "A B/2/3"}

    def test_exhausted_slot_is_skipped(self):
        assert select_slot("A B", 0, 3) == {"slot": "B", "saved": "A B/0/2"}

    def test_order_decides_which_slot_is_tried_first(self):
        assert select_slot("B A", 3, 1) == {"slot": "B", "saved": "B A/3/0"}

    def test_force_slot_ignores_the_counters(self):
        assert select_slot("A B", 0, 0, force="B") == {"slot": "B", "saved": ""}
