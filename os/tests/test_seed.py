# SPDX-License-Identifier: Apache-2.0
"""The data seed in the OS image: mkdata.sh (flash-once image), seed-first-boot (images that carry the archive),
post-image.sh, and data-init next to a pre-populated /data.

The seed format is specified in seed/SPEC.md; these tests build small synthetic seeds that satisfy it and, where
the seed lane's real archives exist (build/seed/out), also run against those.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from conftest import BOARD, LIBEXEC, OS_DIR, REPO_ROOT, need, run

MKDATA = BOARD / "mkdata.sh"
POST_IMAGE = BOARD / "post-image.sh"
INSTALLER = REPO_ROOT / "seed/bin/eitri-seed-install"
SEED_OUT = REPO_ROOT.parent / "build/seed/out"

TREE = {
    "firmware/klipper/klippy/klippy.py": b"# klippy\n",
    "firmware/klipper/klippy/chelper/c_helper.so": b"\x7fELF",
    "firmware/moonraker/moonraker/moonraker.py": b"# moonraker\n",
    "venv/klippy/pyvenv.cfg": b"home = /usr/bin\n",
    "venv/klippy/bin/python": None,  # symlink to /usr/bin/python3
    "venv/moonraker/pyvenv.cfg": b"home = /usr/bin\n",
    "eitri/klarity/venv/bin/klarity": b"#!/data/eitri/klarity/venv/bin/python\n",
    "eitri/extras/qd_example.py": b"# extra\n",
    "eitri/klipper-layer/config/printer.cfg.tmpl": b"# template\n",
    "eitri/klipper-layer/scripts/overlay-install.sh": b"#!/bin/sh\n",
    "eitri/seed/seed-info.json": b"{}\n",
}


def make_seed(directory: Path, flavor: str = "klipper", version: str = "0.0.0.test", epoch: int = 1_700_000_000) -> tuple[Path, Path]:
    """Write data-seed-<version>-<flavor>.tar.zst and its manifest the way seed/SPEC.md describes."""
    need("zstd")
    directory.mkdir(parents=True, exist_ok=True)
    tree = {**TREE}
    tree["eitri/flavor"] = f"{flavor}\n".encode()
    tree["eitri/seed/seed-info.env"] = (
        f"SEED_KIND=eitri-data-seed\nSEED_FLAVOR={flavor}\nSEED_LAYOUT=1\nSEED_VERSION={version}\n"
        f"SEED_TREE_SHA256={'ab' * 32}\nSEED_FIRMWARE_COMMIT={'1' * 40}\nSEED_MOONRAKER_COMMIT={'2' * 40}\n").encode()
    tar_path = directory / f"data-seed-{version}-{flavor}.tar"
    dirs = {"firmware", "venv", "eitri", "eitri/klarity"}
    for path in tree:
        parts = path.split("/")
        dirs |= {"/".join(parts[:i]) for i in range(1, len(parts))}
    members = sorted([(d, None) for d in dirs] + list(tree.items()))
    with tarfile.open(tar_path, "w", format=tarfile.GNU_FORMAT) as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 1000
            info.uname = info.gname = ""
            info.mtime = epoch + 1 if name.endswith("c_helper.so") else epoch  # the seed's chelper rule (SPEC section 6)
            if name in dirs:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif content is None:
                info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, "/usr/bin/python3"
                tar.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o755 if name.endswith((".so", ".sh", "klarity", "python")) else 0o644
                import io
                tar.addfile(info, io.BytesIO(content))
    archive = directory / f"{tar_path.name}.zst"
    run(["zstd", "-q", "-f", "-o", archive, tar_path], check=True)
    tar_path.unlink()
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = directory / f"data-seed-{version}-{flavor}.manifest.json"
    manifest.write_text(json.dumps({"schema": 1, "kind": "eitri-data-seed", "version": version, "flavor": flavor,
                                    "archive": {"name": archive.name, "sha256": sha}}, indent=2) + "\n")
    return archive, manifest


def sha_of(archive: Path) -> str:
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def debugfs(image: Path, command: str) -> str:
    need("debugfs")
    return run(["debugfs", "-R", command, image], check=True).stdout


# --- mkdata.sh -----------------------------------------------------------------------------------------------

def test_mkdata_without_a_seed_makes_the_old_empty_partition(tmp_path):
    need("mke2fs", "dumpe2fs")
    out = tmp_path / "data.ext4"
    result = run([MKDATA, "--output", out])
    assert result.returncode == 0, result.stderr
    assert out.stat().st_size == 128 * 1024 * 1024
    header = run(["dumpe2fs", "-h", out], check=True).stdout
    assert re.search(r"Filesystem volume name:\s+data", header)
    assert re.search(r"Errors behavior:\s+Remount read-only", header), "a filesystem error must stop the writes"
    assert "7b1f9f0e-3a5c-4f6e-9c41-0e17a1d20004" in header
    assert "eitri" not in debugfs(out, "ls -l /")


def test_mkdata_with_a_seed_installs_it_as_the_installer_would(tmp_path):
    need("mke2fs", "e2fsck", "debugfs")
    archive, manifest = make_seed(tmp_path / "seed")
    out = tmp_path / "data.ext4"
    result = run([MKDATA, "--output", out, "--seed", archive, "--manifest", manifest, "--owner", "none"])
    assert result.returncode == 0, result.stderr
    assert run(["e2fsck", "-fn", out]).returncode == 0
    tree = debugfs(out, "ls -p /eitri")
    for name in ("flavor", "extras", "klipper-layer", "seed", "state", "klarity"):
        assert f"/{name}/" in tree, name
    assert debugfs(out, "cat /eitri/flavor").strip().splitlines()[-1] == "klipper"
    record = debugfs(out, "cat /eitri/state/seed-installed.env")
    assert "SEED_FLAVOR=klipper" in record and f"SEED_ARCHIVE_SHA256={sha_of(archive)}" in record
    assert "klippy.py" in debugfs(out, "ls -p /firmware/klipper/klippy")
    assert not re.search(r"seed-staging|seed\.lock", debugfs(out, "ls -p /"))


def test_mkdata_keeps_modification_times_even_with_source_date_epoch(tmp_path):
    # klippy rebuilds c_helper.so unless it is newer than its sources, and the printer has no compiler: the seed's
    # mtimes are meaningful. Buildroot exports SOURCE_DATE_EPOCH, which would make mke2fs clamp them all.
    need("mke2fs", "debugfs")
    archive, manifest = make_seed(tmp_path / "seed", epoch=1_700_000_000)
    out = tmp_path / "data.ext4"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1600000000", "E2FSPROGS_FAKE_TIME": "1600000000"}
    run([MKDATA, "--output", out, "--seed", archive, "--manifest", manifest, "--owner", "none"], env=env, check=True)
    assert "mtime: 0x6553f100" in debugfs(out, "stat /firmware/klipper/klippy/klippy.py")        # 1700000000
    assert "mtime: 0x6553f101" in debugfs(out, "stat /firmware/klipper/klippy/chelper/c_helper.so")  # + 1 s


def test_mkdata_makes_files_belong_to_eitri_under_fakeroot(tmp_path):
    need("fakeroot", "mke2fs", "debugfs")
    archive, manifest = make_seed(tmp_path / "seed")
    out = tmp_path / "data.ext4"
    result = run(["fakeroot", "--", MKDATA, "--output", out, "--seed", archive, "--manifest", manifest])
    assert result.returncode == 0, result.stderr
    uids = {}
    for path in ("/", "/eitri", "/firmware", "/firmware/klipper", "/venv", "/eitri/klarity", "/eitri/klarity/venv",
                 "/eitri/extras", "/eitri/flavor", "/eitri/state"):
        stat_out = debugfs(out, f"stat {path}")
        m = re.search(r"User:\s+(\d+)\s+Group:\s+(\d+)", stat_out)
        uids[path] = (int(m.group(1)), int(m.group(2)))
    # The root of /data and /eitri and the OS state are root's (sshd's StrictModes, eitri-access); the
    # application layer is uid/gid 1000 like the user the image creates.
    assert uids["/"] == (0, 0) and uids["/eitri"] == (0, 0) and uids["/eitri/state"] == (0, 0)
    for path in ("/firmware", "/firmware/klipper", "/venv", "/eitri/klarity", "/eitri/klarity/venv", "/eitri/extras", "/eitri/flavor"):
        assert uids[path] == (1000, 1000), path


def test_mkdata_refuses_files_it_could_not_give_to_eitri(tmp_path):
    # Without fakeroot or root the seed's files would belong to the build user: a silently mis-owned image.
    if os.geteuid() == 0:
        pytest.skip("running as root")
    archive, manifest = make_seed(tmp_path / "seed")
    # (an uid that is not the build user's own, which could be 1000 as well)
    result = run([MKDATA, "--output", tmp_path / "data.ext4", "--seed", archive, "--manifest", manifest, "--owner", "4242:4242"])
    assert result.returncode != 0 and "fakeroot" in result.stderr
    assert not (tmp_path / "data.ext4").exists()


def test_mkdata_stops_on_a_wrong_digest_or_flavor(tmp_path):
    archive, manifest = make_seed(tmp_path / "seed")
    bad = json.loads(manifest.read_text())
    bad["archive"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(bad))
    result = run([MKDATA, "--output", tmp_path / "data.ext4", "--seed", archive, "--manifest", manifest, "--owner", "none"])
    assert result.returncode != 0 and not (tmp_path / "data.ext4").exists()
    archive, manifest = make_seed(tmp_path / "seed")
    result = run([MKDATA, "--output", tmp_path / "data.ext4", "--seed", archive, "--manifest", manifest, "--flavor", "kalico",
                  "--owner", "none"])
    assert result.returncode != 0 and not (tmp_path / "data.ext4").exists()


def test_mkdata_sizes_the_partition_for_the_seed(tmp_path):
    need("mke2fs", "dumpe2fs")
    archive, manifest = make_seed(tmp_path / "seed")
    out = tmp_path / "data.ext4"
    run([MKDATA, "--output", out, "--seed", archive, "--manifest", manifest, "--owner", "none", "--headroom-mib", "200"], check=True)
    # A seed of a few KiB rounds up to 1 MiB; the headroom is added on top.
    assert 200 * 1024 * 1024 < out.stat().st_size <= 202 * 1024 * 1024


@pytest.mark.skipif(not (SEED_OUT / "data-seed-0.0.0.dev0-klipper.tar.zst").exists(), reason="build the seed first: make -C seed build")
@pytest.mark.parametrize("flavor", ["klipper", "kalico"])
def test_mkdata_with_the_real_seed(tmp_path, flavor):
    need("fakeroot", "mke2fs", "e2fsck", "debugfs")
    archive = next(SEED_OUT.glob(f"data-seed-*-{flavor}.tar.zst"))
    manifest = archive.with_suffix("").with_suffix(".manifest.json")
    out = tmp_path / "data.ext4"
    result = run(["fakeroot", "--", MKDATA, "--output", out, "--seed", archive, "--manifest", manifest, "--flavor", flavor])
    assert result.returncode == 0, result.stderr
    assert run(["e2fsck", "-fn", out]).returncode == 0
    assert debugfs(out, "cat /eitri/flavor").strip().splitlines()[-1] == flavor
    assert "klippy.py" in debugfs(out, "ls -p /firmware/klipper/klippy")
    assert "klarity" in debugfs(out, "ls -p /eitri/klarity/venv/bin")
    # klippy only trusts the prebuilt C helper while it is newer than its sources.
    helper = re.search(r"mtime: 0x([0-9a-f]+)", debugfs(out, "stat /firmware/klipper/klippy/chelper/c_helper.so")).group(1)
    source = re.search(r"mtime: 0x([0-9a-f]+)", debugfs(out, "stat /firmware/klipper/klippy/chelper/itersolve.c")).group(1)
    assert int(helper, 16) > int(source, 16)


# --- post-image.sh ---------------------------------------------------------------------------------------------

@pytest.fixture
def image_build(tmp_path):
    base = tmp_path / "br-out"
    binaries = base / "images"
    binaries.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EITRI_", "HOST_DIR"))}
    env.update({"BR2_EXTERNAL_EITRI_PATH": str(OS_DIR), "BINARIES_DIR": str(binaries), "BASE_DIR": str(base)})
    return {"env": env, "bin": binaries, "seed_out": tmp_path / "seed/out", "tmp": tmp_path}


def post_image(build, **extra):
    return run(["bash", POST_IMAGE], env={**build["env"], **extra})


def test_post_image_seeds_the_data_partition_from_the_build_tree(image_build):
    need("fakeroot", "mke2fs", "debugfs")
    make_seed(image_build["seed_out"])
    make_seed(image_build["seed_out"], flavor="kalico")
    result = post_image(image_build)
    assert result.returncode == 0, result.stderr
    out = image_build["bin"] / "data.ext4"
    assert debugfs(out, "cat /eitri/flavor").strip().splitlines()[-1] == "klipper", "the default flavor is klipper"
    assert "seeding /data from data-seed-0.0.0.test-klipper.tar.zst" in result.stdout
    # The boot partition files are still staged.
    assert (image_build["bin"] / "eitri-boot.env").is_file() and (image_build["bin"] / "README.txt").is_file()
    assert re.search(r"(?m)^\s*\d+\s+.*\s1000\s+1000\s", debugfs(out, "ls -l /firmware")), "owned by eitri"


def test_post_image_can_start_with_kalico(image_build):
    need("fakeroot", "mke2fs", "debugfs")
    make_seed(image_build["seed_out"])
    make_seed(image_build["seed_out"], flavor="kalico")
    result = post_image(image_build, EITRI_SEED_FLAVOR="kalico")
    assert result.returncode == 0, result.stderr
    assert debugfs(image_build["bin"] / "data.ext4", "cat /eitri/flavor").strip().splitlines()[-1] == "kalico"


def test_post_image_without_a_seed_warns_in_a_development_build(image_build):
    need("mke2fs")
    result = post_image(image_build)
    assert result.returncode == 0 and "no data seed found" in result.stderr
    assert (image_build["bin"] / "data.ext4").stat().st_size == 128 * 1024 * 1024


def test_post_image_refuses_to_build_a_release_without_a_seed(image_build):
    for extra in ({"EITRI_RELEASE": "1"}, {"EITRI_RELEASE": "1", "EITRI_NO_SEED": "1"}):
        result = post_image(image_build, **extra)
        assert result.returncode != 0 and "ERROR" in result.stderr, extra
        assert not (image_build["bin"] / "data.ext4").exists()


def test_post_image_asks_which_seed_when_there_are_several(image_build):
    make_seed(image_build["seed_out"], version="1.0.0")
    make_seed(image_build["seed_out"], version="1.0.1")
    result = post_image(image_build)
    assert result.returncode != 0 and "EITRI_SEED_ARCHIVE" in result.stderr
    need("fakeroot", "mke2fs")
    chosen = image_build["seed_out"] / "data-seed-1.0.0-klipper.tar.zst"
    assert post_image(image_build, EITRI_SEED_ARCHIVE=str(chosen)).returncode == 0


def test_post_image_needs_the_manifest_next_to_the_archive(image_build):
    archive, manifest = make_seed(image_build["seed_out"])
    manifest.unlink()
    result = post_image(image_build)
    assert result.returncode != 0 and "manifest" in result.stderr


def test_genimage_takes_the_prebuilt_data_image():
    cfg = (BOARD / "genimage.cfg").read_text()
    assert "image data.ext4" not in cfg, "post-image.sh builds it (owners 1000:1000 need fakeroot)"
    assert 'image = "data.ext4"' in cfg
    assert 'BR2_ROOTFS_POST_IMAGE_SCRIPT="$(BR2_EXTERNAL_EITRI_PATH)/board/eitri-plus4/post-image.sh' in \
        (OS_DIR / "configs/eitri_plus4_defconfig").read_text()


# --- seed-first-boot ------------------------------------------------------------------------------------------------

@pytest.fixture
def firstboot(tmp_path):
    need("zstd", "tar", "sha256sum", "awk", "df")
    data = tmp_path / "data"
    data.mkdir()
    seeds = tmp_path / "image-seed"
    seeds.mkdir()
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("EITRI_")},
           "EITRI_DATA": str(data), "EITRI_SEED_DIR": str(seeds), "EITRI_SEED_INSTALL": str(INSTALLER),
           "EITRI_OWNER": f"{os.getuid()}:{os.getgid()}", "EITRI_RUN": str(tmp_path / "run")}
    return {"env": env, "data": data, "seeds": seeds, "run": tmp_path / "run"}


def seed_first_boot(fb, **extra):
    return run([LIBEXEC / "seed-first-boot"], env={**fb["env"], **extra})


def test_first_boot_unpacks_the_seed_the_image_carries(firstboot):
    make_seed(firstboot["seeds"])
    result = seed_first_boot(firstboot)
    assert result.returncode == 0, result.stdout + result.stderr
    data = firstboot["data"]
    assert (data / "firmware/klipper/klippy/klippy.py").is_file() and (data / "eitri/klarity/venv/bin/klarity").is_file()
    assert (data / "eitri/flavor").read_text() == "klipper\n"
    record = (data / "eitri/state/seed-installed.env").read_text()
    assert "SEED_VERSION=0.0.0.test" in record and "SEED_MODE=install" in record
    assert os.readlink(data / "venv/klippy/bin/python") == "/usr/bin/python3"
    assert not (firstboot["run"] / "no-seed").exists()


def test_first_boot_is_idempotent_and_does_not_unpack_twice(firstboot):
    make_seed(firstboot["seeds"])
    seed_first_boot(firstboot)
    marker = firstboot["data"] / "firmware/klipper/klippy/user-change"
    marker.write_text("kept")
    again = seed_first_boot(firstboot)
    assert again.returncode == 0 and "is installed" in again.stdout
    assert marker.read_text() == "kept"


def test_first_boot_adopts_precreated_empty_directories(firstboot):
    # data-init creates venv/klippy, eitri/extras, ... empty before the seed arrives.
    data = firstboot["data"]
    for d in ("firmware", "venv/klippy", "venv/moonraker", "eitri/extras", "eitri/klarity", "eitri/state"):
        (data / d).mkdir(parents=True)
    make_seed(firstboot["seeds"])
    assert seed_first_boot(firstboot).returncode == 0
    assert (data / "venv/klippy/pyvenv.cfg").is_file() and (data / "eitri/extras/qd_example.py").is_file()


def test_first_boot_never_overwrites_an_existing_tree(firstboot):
    data = firstboot["data"]
    (data / "firmware/klipper/klippy").mkdir(parents=True)
    (data / "firmware/klipper/klippy/klippy.py").write_text("mine")
    make_seed(firstboot["seeds"])
    assert seed_first_boot(firstboot).returncode == 0
    assert (data / "firmware/klipper/klippy/klippy.py").read_text() == "mine"
    assert "SEED_KEPT=firmware/klipper" in (data / "eitri/state/seed-installed.env").read_text()


def test_first_boot_follows_the_flavor_on_disk(firstboot):
    make_seed(firstboot["seeds"])
    make_seed(firstboot["seeds"], flavor="kalico")
    (firstboot["data"] / "eitri").mkdir()
    (firstboot["data"] / "eitri/flavor").write_text("kalico\n")
    assert seed_first_boot(firstboot).returncode == 0
    assert "SEED_FLAVOR=kalico" in (firstboot["data"] / "eitri/state/seed-installed.env").read_text()
    assert (firstboot["data"] / "eitri/flavor").read_text() == "kalico\n"


def test_first_boot_without_any_seed_says_why_and_does_not_fail_the_boot(firstboot):
    result = seed_first_boot(firstboot)
    assert result.returncode == 0 and "no data seed" in result.stdout
    assert "klipper, moonraker and klarity" in (firstboot["run"] / "no-seed").read_text() or "no data seed" in (firstboot["run"] / "no-seed").read_text()
    # An installation done by the in-place installer (a Klipper tree, no archive in the image) is fine.
    (firstboot["data"] / "firmware/klipper/klippy").mkdir(parents=True)
    (firstboot["data"] / "firmware/klipper/klippy/klippy.py").write_text("")
    result = seed_first_boot(firstboot)
    assert result.returncode == 0 and "already holds a Klipper tree" in result.stdout
    assert not (firstboot["run"] / "no-seed").exists()


def test_first_boot_rejects_a_tampered_archive_and_changes_nothing(firstboot):
    archive, manifest = make_seed(firstboot["seeds"])
    with archive.open("ab") as fh:
        fh.write(b"tampered")
    result = seed_first_boot(firstboot)
    assert result.returncode == 1 and "failed" in result.stdout
    assert not (firstboot["data"] / "firmware").exists() and not (firstboot["data"] / "eitri").exists()


def test_first_boot_refuses_to_choose_between_archives(firstboot):
    make_seed(firstboot["seeds"], version="1.0.0")
    make_seed(firstboot["seeds"], version="1.0.1")
    result = seed_first_boot(firstboot)
    assert result.returncode == 1 and "refusing to choose" in result.stdout
    assert not (firstboot["data"] / "firmware").exists()


def test_first_boot_needs_the_manifest(firstboot):
    archive, manifest = make_seed(firstboot["seeds"])
    manifest.unlink()
    assert seed_first_boot(firstboot).returncode == 1


def test_first_boot_skips_an_unmounted_data_partition(tmp_path):
    need("mountpoint")
    if Path("/data").is_mount():
        pytest.skip("/data is a mount point here")
    env = {k: v for k, v in os.environ.items() if not k.startswith("EITRI_")}
    result = run([LIBEXEC / "seed-first-boot"], env=env)
    assert result.returncode == 0 and "not mounted" in result.stdout


def test_first_boot_rejects_an_unknown_flavor(firstboot):
    (firstboot["data"] / "eitri").mkdir()
    (firstboot["data"] / "eitri/flavor").write_text("../../etc\n")
    result = seed_first_boot(firstboot)
    assert result.returncode == 1 and "unknown flavor" in result.stdout


# --- data-init next to a populated /data -----------------------------------------------------------------------------

@pytest.fixture
def data_env(tmp_path):
    need("install")
    factory = tmp_path / "factory"
    (factory / "camera").mkdir(parents=True)
    (factory / "moonraker.asvc").write_text("klipper\nmoonraker\n")
    (factory / "camera/go2rtc.yaml").write_text("api:\n")
    (factory / "camera/camera.env.example").write_text("#EITRI_CAMERA_DEVICE=\n")
    bootfs = tmp_path / "bootfs"
    bootfs.mkdir()
    return {**os.environ, "EITRI_DATA": str(tmp_path / "data"), "EITRI_FACTORY": str(factory), "EITRI_BOOTFS": str(bootfs),
            "EITRI_OWNER": f"{os.getuid()}:{os.getgid()}", "EITRI_NO_RESIZE": "1",
            "EITRI_CONFIGURATOR_SRC": str(tmp_path / "none")}


def test_data_init_still_stamps_the_first_boot_of_a_prepopulated_data_partition(data_env, tmp_path):
    # mkdata.sh leaves eitri/state/seed-installed.env behind, so eitri/state exists before the first boot.
    data = Path(data_env["EITRI_DATA"])
    data.mkdir()
    archive, manifest = make_seed(tmp_path / "seed")
    run([INSTALLER, "--archive", archive, "--sha256", sha_of(archive), "--flavor", "klipper", "--data", data, "-q"], check=True)
    assert (data / "eitri/state/seed-installed.env").is_file()
    result = run([LIBEXEC / "data-init"], env=data_env)
    assert result.returncode == 0, result.stderr
    assert (data / "eitri/state/first-boot").is_file(), "a populated /data is still a first boot for the OS"
    assert (data / "eitri/state/layout-version").read_text().strip() == "1"
    # The seed is adopted untouched, next to the directories data-init adds.
    assert (data / "firmware/klipper/klippy/klippy.py").read_text() == "# klippy\n"
    assert (data / "eitri/extras/qd_example.py").read_text() == "# extra\n"
    assert (data / "eitri/seed/seed-info.env").is_file() and (data / "printer_data/config").is_dir()
    stamp = (data / "eitri/state/first-boot").read_text()
    run([LIBEXEC / "data-init"], env=data_env, check=True)
    assert (data / "eitri/state/first-boot").read_text() == stamp


def test_data_init_does_not_unpack_or_create_the_seed_trees(data_env):
    data = Path(data_env["EITRI_DATA"])
    data.mkdir()
    assert run([LIBEXEC / "data-init"], env=data_env).returncode == 0
    for absent in ("firmware/klipper", "firmware/moonraker", "eitri/klipper-layer", "eitri/flavor", "eitri/klarity/venv"):
        assert not (data / absent).exists(), absent
    assert not (data / "eitri/state/seed-installed.env").exists()
