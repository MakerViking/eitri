# SPDX-License-Identifier: Apache-2.0
"""The pieces of eitri-seed-install that depend on the OS image's BusyBox: its `tar -tv` listing format (what the
layout policy parses) and its extraction (modes, symlinks and, crucially, mtimes). BusyBox runs under qemu-user as
a top-level process (qemu-user cannot exec children, so the whole script cannot run there). Skipped without a
Buildroot image root and qemu-aarch64."""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import seedfix
from conftest import make_sysroot, needs_git, needs_zstd

pytestmark = [needs_zstd, needs_git]

INSTALLER = Path(__file__).resolve().parent.parent / "bin" / "eitri-seed-install"


@pytest.fixture(scope="module")
def sysroot(scratch_next_to_image, image_root, qemu):
    root = scratch_next_to_image / "installer-sr"
    make_sysroot(image_root, root)
    if not (root / "bin/busybox").exists():
        pytest.skip("the image has no /bin/busybox")
    (root / "opt/seedtest").mkdir(parents=True, exist_ok=True)
    return root


def busybox_tar(sysroot, qemu, *args, stdin=None):
    cmd = [qemu, "-L", str(sysroot), str(sysroot / "bin/busybox"), "tar", *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=300)


def raw_tar(arc: Path) -> bytes:
    return subprocess.run(["zstd", "-dc", str(arc)], check=True, capture_output=True).stdout


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("synthetic")
    stage = tmp / "stage"
    seedfix.make_stage(stage, marker="one", version="1.0.0")
    arc, sha = seedfix.pack(stage, tmp, "1.0.0")
    return arc


def test_busybox_listing_is_accepted_by_the_policy(sysroot, qemu, synthetic, tmp_path):
    r = busybox_tar(sysroot, qemu, "-tvf", "-", stdin=raw_tar(synthetic))
    assert r.returncode == 0, r.stderr
    listing = tmp_path / "listing"
    listing.write_bytes(r.stdout)
    lines = r.stdout.decode().splitlines()
    assert any(ln.startswith("l") and " -> " in ln for ln in lines), "symlinks must appear as 'name -> target'"
    assert any(ln.startswith("d") for ln in lines)
    p = subprocess.run(["sh", str(INSTALLER), "--check-listing", str(listing)], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.strip().splitlines()[-1].startswith("bytes ")


@pytest.mark.parametrize("member,refused", [
    (seedfix.tinfo("../evil", size=0), True),
    (seedfix.tinfo("printer_data/config/printer.cfg"), True),
    (seedfix.tinfo("firmware/klipper/lnk", tarfile.SYMTYPE, 0o777, "/root/.ssh/authorized_keys"), True),
    (seedfix.tinfo("eitri/extras/su", mode=0o4755), True),
    (seedfix.tinfo("eitri/extras/h", tarfile.LNKTYPE, 0o644, "eitri/flavor"), True),
    (seedfix.tinfo("eitri/extras/ok.py"), False),
])
def test_busybox_listing_of_hostile_members(sysroot, qemu, tmp_path, member, refused):
    arc, _ = seedfix.raw_archive(tmp_path / "x.tar.zst", [member])
    r = busybox_tar(sysroot, qemu, "-tvf", "-", stdin=raw_tar(arc))
    # BusyBox may itself refuse a member (absolute/.. names); either way the policy must not pass it
    listing = tmp_path / "listing"
    listing.write_bytes(r.stdout)
    p = subprocess.run(["sh", str(INSTALLER), "--check-listing", str(listing)], capture_output=True, text=True)
    assert (p.returncode != 0) is refused, (r.stdout, r.stderr, p.stdout, p.stderr)


def test_busybox_extraction_keeps_modes_symlinks_and_mtimes(sysroot, qemu, synthetic):
    dest = sysroot / "opt/seedtest/x"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir()
    r = busybox_tar(sysroot, qemu, "-xf", "-", "-C", str(dest), stdin=raw_tar(synthetic))
    assert r.returncode == 0, r.stderr
    chdir = dest / "firmware/klipper/klippy/chelper"
    # klippy rebuilds c_helper.so when a source is newer than it: both mtimes must be exactly as archived
    assert int((chdir / "c_helper.so").stat().st_mtime) == seedfix.EPOCH + 1
    assert int((chdir / "a.c").stat().st_mtime) == seedfix.EPOCH
    assert os.readlink(dest / "venv/klippy/bin/python") == "/usr/bin/python3"
    assert os.readlink(dest / "eitri/klipper-layer/extras") == "../extras"
    assert os.readlink(dest / "firmware/klipper/qd_link.py") == "/data/eitri/extras/qd_x.py"
    assert (dest / "eitri/klipper-layer/config").stat().st_mode & 0o777 == 0o755
    assert (dest / "eitri/flavor").read_text() == "klipper\n"
    assert (dest / "firmware/klipper/.git/HEAD").exists()
    shutil.rmtree(dest)
