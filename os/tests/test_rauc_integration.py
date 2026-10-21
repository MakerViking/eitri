# SPDX-License-Identifier: Apache-2.0
"""The backend and the bundle script against a real RAUC (skipped without one).

Point EITRI_RAUC at a rauc binary, or put rauc on PATH. The host-rauc package of
a Buildroot build provides one: output/host/bin/rauc.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from conftest import BOARD, LIBEXEC, OS_DIR, OVERLAY, run

RAUC = os.environ.get("EITRI_RAUC") or shutil.which("rauc")
pytestmark = pytest.mark.skipif(not RAUC, reason="needs rauc (set EITRI_RAUC)")

SECTOR = 17


def system_conf(tmp_path: Path, keyring: Path | None = None) -> Path:
    """The image's system.conf with device paths pointed at files."""
    for name in ("slotA.img", "slotB.img", "emmc.img"):
        (tmp_path / name).write_bytes(b"\0" * (1 << 20))
    text = (OVERLAY / "etc/rauc/system.conf").read_text()
    text = text.replace("/usr/libexec/eitri/rauc-bootloader", str(LIBEXEC / "rauc-bootloader"))
    text = text.replace("/usr/libexec/eitri/rauc-pre-install", str(LIBEXEC / "rauc-pre-install"))
    text = text.replace("/dev/mmcblk1p2", str(tmp_path / "slotA.img"))
    text = text.replace("/dev/mmcblk1p3", str(tmp_path / "slotB.img"))
    text = text.replace("/data/eitri/state/rauc.status", str(tmp_path / "rauc.status"))
    text = text.replace("/run/rauc", str(tmp_path / "mnt"))
    if keyring:
        text = text.replace("/etc/rauc/keyring.pem", str(keyring))
    conf = tmp_path / "system.conf"
    conf.write_text(text)
    return conf


def test_rauc_drives_the_backend(tmp_path):
    conf = system_conf(tmp_path)
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(f"ro rauc.slot=A eitri.disk={tmp_path / 'emmc.img'}\n")
    env = {**os.environ, "EITRI_STATE_DEV": str(tmp_path / "emmc.img"), "EITRI_STATE_SECTOR": str(SECTOR),
           "EITRI_CMDLINE": str(cmdline), "EITRI_LOCK": str(tmp_path / "lock")}

    status = run([RAUC, f"--conf={conf}", "status"], env=env)
    assert status.returncode == 0, status.stderr
    assert "Booted from: rootfs.0 (A)" in status.stdout
    assert "Activated: rootfs.0 (A)" in status.stdout

    result = run([RAUC, f"--conf={conf}", "status", "mark-active", "other"], env=env)
    assert result.returncode == 0, result.stderr
    sector = (tmp_path / "emmc.img").read_bytes()[SECTOR * 512 : (SECTOR + 1) * 512].rstrip(b"\0").decode()
    assert sector.splitlines()[0] == "BOOT_ORDER=B A"

    status = run([RAUC, f"--conf={conf}", "status"], env=env)
    assert "Activated: rootfs.1 (B)" in status.stdout


def test_post_bundle_signs_a_bundle_the_image_accepts(tmp_path):
    images, target, base, host_bin = (tmp_path / d for d in ("images", "target", "base", "host/bin"))
    for d in (images, target / "etc/rauc", base, host_bin):
        d.mkdir(parents=True)
    (host_bin / "rauc").symlink_to(RAUC)
    # What post-build.sh does for a development image.
    run([OS_DIR / "scripts/gen-dev-pki.sh", base / "eitri-dev-pki"], check=True)
    shutil.copy(base / "eitri-dev-pki/ca.cert.pem", target / "etc/rauc/keyring.pem")
    # A rootfs image with enough incompressible content for a verity bundle.
    root = tmp_path / "rootfs/boot"
    root.mkdir(parents=True)
    (root / "Image").write_bytes(os.urandom(2_000_000))
    # Buildroot names the file rootfs.ext2 and links rootfs.ext4 to it.
    run(["mke2fs", "-q", "-t", "ext4", "-d", root.parent, images / "rootfs.ext2", "16M"], check=True)
    (images / "rootfs.ext4").symlink_to("rootfs.ext2")
    (images / "eitri-plus4.img").write_bytes(b"")

    env = {**os.environ, "BR2_EXTERNAL_EITRI_PATH": str(OS_DIR), "HOST_DIR": str(tmp_path / "host"),
           "TARGET_DIR": str(target), "BASE_DIR": str(base), "BINARIES_DIR": str(images), "EITRI_VERSION": "1.2.3"}
    result = run(["bash", BOARD / "post-bundle.sh"], env=env)
    assert result.returncode == 0, result.stderr + result.stdout

    bundle = images / "eitri-plus4-1.2.3.raucb"
    assert bundle.is_file() and (images / "eitri-plus4.raucb").is_symlink()
    conf = system_conf(tmp_path, keyring=target / "etc/rauc/keyring.pem")
    info = run([RAUC, f"--conf={conf}", "info", bundle])
    assert info.returncode == 0, info.stderr
    assert "Compatible:     'eitri-plus4'" in info.stdout
    assert "Version:        '1.2.3'" in info.stdout and "Bundle Format:  verity" in info.stdout
    assert "Filename:  rootfs.ext4" in info.stdout

    # Another build's keyring must not accept it.
    other = tmp_path / "other"
    run([OS_DIR / "scripts/gen-dev-pki.sh", other], check=True)
    wrong = system_conf(tmp_path, keyring=other / "ca.cert.pem")
    assert run([RAUC, f"--conf={wrong}", "info", bundle]).returncode != 0


def test_a_release_build_refuses_the_development_key(tmp_path):
    env = {**os.environ, "BR2_EXTERNAL_EITRI_PATH": str(OS_DIR), "HOST_DIR": str(tmp_path / "host"),
           "TARGET_DIR": str(tmp_path), "BASE_DIR": str(tmp_path), "BINARIES_DIR": str(tmp_path), "EITRI_RELEASE": "1"}
    (tmp_path / "host/bin").mkdir(parents=True)
    (tmp_path / "host/bin/rauc").symlink_to(RAUC)
    result = run(["bash", BOARD / "post-bundle.sh"], env=env)
    assert result.returncode != 0 and "EITRI_RAUC_CERT" in result.stderr
