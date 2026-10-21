# SPDX-License-Identifier: Apache-2.0
"""post-build.sh against a synthetic target tree."""
from __future__ import annotations

import os
import shutil
import stat

import pytest
from conftest import BOARD, OS_DIR, OVERLAY, REPO_ROOT, run


@pytest.fixture
def tree(tmp_path):
    """A target tree: the rootfs overlay plus the files Buildroot would have produced."""
    target = tmp_path / "target"
    shutil.copytree(OVERLAY, target)
    (target / "boot").mkdir(exist_ok=True)
    (target / "boot/Image").write_bytes(b"kernel")
    (target / "boot/rk3328-eitri-plus4.dtb").write_bytes(b"dtb")
    (target / "usr/lib/python3.14/site-packages").mkdir(parents=True, exist_ok=True)
    (target / "etc/shadow").write_text("root::0:0:99999:7:::\neitri:*:20000:0:99999:7:::\n")
    (target / "etc/sudoers.d/eitri").chmod(0o644)  # git does not keep 0440
    base = tmp_path / "base"
    base.mkdir()
    return target, base


def post_build(target, base, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("EITRI_")}
    env.update({"BR2_EXTERNAL_EITRI_PATH": str(OS_DIR), "BASE_DIR": str(base), **extra})
    return run(["bash", BOARD / "post-build.sh", target], env=env)


def test_development_build(tree):
    target, base = tree
    result = post_build(target, base, EITRI_VERSION="9.9.9")
    assert result.returncode == 0, result.stderr
    osrel = (target / "usr/lib/os-release").read_text()
    assert 'NAME="Eitri OS"' in osrel and 'VERSION_ID="9.9.9"' in osrel
    assert (target / "etc/os-release").is_symlink()
    assert (target / "etc/eitri-release").read_text().strip() == "9.9.9"
    shadow = (target / "etc/shadow").read_text().splitlines()
    assert shadow[0].startswith("root:*:"), "root must not have an empty password"
    assert stat.S_IMODE((target / "etc/sudoers.d/eitri").stat().st_mode) == 0o440
    assert (target / "data").is_dir() and (target / "boot/firmware").is_dir()
    assert (target / "usr/lib/eitri/release.lock").read_bytes() == (REPO_ROOT / "release.lock").read_bytes()
    assert (target / "usr/share/doc/eitri/os/boot-chain.md").is_file()
    # The installer family's on-device half comes with the image.
    assert (target / "usr/libexec/eitri/eitri-bootctl").is_file() and (target / "usr/bin/eitri-update").is_file()
    assert (target / "usr/lib/python3.14/site-packages/eitri_update/cli.py").is_file()
    assert (target / "usr/lib/systemd/system/eitri-prepare.service").is_file()
    # The data seed's installer, the root helpers and the configurator command line.
    installer = target / "usr/libexec/eitri/eitri-seed-install"
    assert installer.read_bytes() == (REPO_ROOT / "seed/bin/eitri-seed-install").read_bytes()
    assert (target / "usr/share/doc/eitri/seed/SPEC.md").is_file()
    for tool in ("usr/libexec/eitri/eitri-access", "usr/libexec/eitri/seed-first-boot", "usr/bin/eitri-configurator",
                 "usr/libexec/eitri/eitri-seed-install"):
        assert stat.S_IMODE((target / tool).stat().st_mode) == 0o755, tool
    # No release key: the image trusts the throwaway CA, loudly.
    assert (target / "etc/rauc/keyring.pem").read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert "development image" in result.stderr


def test_device_tree_is_reachable_under_the_stock_name(tree):
    target, base = tree
    post_build(target, base)
    link = target / "boot/dtb/rockchip/rk3328-roc-cc.dtb"
    assert link.is_symlink() and link.resolve() == (target / "boot/rk3328-eitri-plus4.dtb").resolve()


def test_missing_kernel_fails_the_build(tree):
    target, base = tree
    (target / "boot/Image").unlink()
    result = post_build(target, base)
    assert result.returncode != 0 and "/boot/Image" in result.stderr


def test_release_build_refuses_to_fall_back(tree):
    target, base = tree
    result = post_build(target, base, EITRI_RELEASE="1")
    assert result.returncode != 0 and "no RAUC keyring" in result.stderr


def test_release_build_with_a_keyring(tree, tmp_path):
    target, base = tree
    ca = tmp_path / "ca.crt"
    run([OS_DIR / "scripts/gen-dev-pki.sh", tmp_path / "pki"], check=True)
    shutil.copy(tmp_path / "pki/ca.cert.pem", ca)
    result = post_build(target, base, EITRI_RELEASE="1", EITRI_RAUC_KEYRING=str(ca), EITRI_VERSION="1.0.0")
    assert result.returncode == 0, result.stderr
    assert (target / "etc/rauc/keyring.pem").read_bytes() == ca.read_bytes()
    assert not (base / "eitri-dev-pki").exists(), "a release build must not create development keys"


def test_development_password_hash(tree):
    target, base = tree
    result = post_build(target, base, EITRI_DEV_PASSWORD_HASH="$6$salt$a&b|c\\d")
    assert result.returncode == 0, result.stderr
    assert (target / "etc/shadow").read_text().splitlines()[1].startswith("eitri:$6$salt$a&b|c\\d:")
    # A bench image gets passwordless sudo in its own drop-in; the shipped rule stays scoped.
    dev = target / "etc/sudoers.d/eitri-dev"
    assert "NOPASSWD: ALL" in dev.read_text() and stat.S_IMODE(dev.stat().st_mode) == 0o440


def test_a_normal_build_has_no_passwordless_root(tree):
    target, base = tree
    post_build(target, base)
    assert not (target / "etc/sudoers.d/eitri-dev").exists()


def test_development_password_is_refused_in_a_release(tree, tmp_path):
    target, base = tree
    result = post_build(target, base, EITRI_RELEASE="1", EITRI_DEV_PASSWORD_HASH="$6$x$y")
    assert result.returncode != 0 and "must not be set for a release" in result.stderr


def test_component_units_are_installed(tree):
    target, base = tree
    post_build(target, base)
    for component, unit in (("klarity", "klarity.service"), ("configurator", "eitri-configurator.service")):
        source = REPO_ROOT / component / "systemd" / unit
        assert (target / "usr/lib/systemd/system" / unit).read_bytes() == source.read_bytes()


def test_a_stale_klipper_layer_is_removed_from_the_target_tree(tree):
    # Buildroot keeps the files of a package that left the configuration; the layer lives on /data now.
    target, base = tree
    stale = target / "usr/lib/eitri/klipper-layer/extras"
    stale.mkdir(parents=True)
    (stale / "qd_old.py").write_text("")
    result = post_build(target, base)
    assert result.returncode == 0, result.stderr
    assert not (target / "usr/lib/eitri/klipper-layer").exists() and "stale" in result.stderr


def test_the_image_does_not_ship_the_layer(tree):
    target, base = tree
    post_build(target, base)
    assert not (target / "usr/lib/eitri/klipper-layer").exists()


def test_missing_root_helper_fails_the_build(tree):
    target, base = tree
    (target / "usr/libexec/eitri/eitri-access").unlink()
    result = post_build(target, base)
    assert result.returncode != 0 and "eitri-access" in result.stderr
