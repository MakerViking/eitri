# SPDX-License-Identifier: Apache-2.0
"""Synthetic seeds for the tests: tiny, but with the real layout, the real tar writer and a valid manifest."""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

import seedlib

EPOCH = 1_760_000_000


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.org", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def make_repo(path: Path, text: str) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "master")
    (path / "README").write_text(text)
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", text)
    return git(path, "rev-parse", "HEAD")


def make_stage(stage: Path, flavor: str = "klipper", version: str = "9.9.9", marker: str = "one") -> dict:
    """Write a complete minimal seed tree below stage and return the facts about it."""
    fw = make_repo(stage / "firmware/klipper", f"firmware {marker}")
    mr = make_repo(stage / "firmware/moonraker", f"moonraker {marker}")
    for comp in ("venv/klippy", "venv/moonraker", "eitri/klarity/venv"):
        d = stage / comp
        (d / "bin").mkdir(parents=True)
        os.symlink("/usr/bin/python3", d / "bin/python")
        (d / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (d / "marker").write_text(marker)
    (stage / "eitri/extras").mkdir(parents=True)
    (stage / "eitri/extras/qd_x.py").write_text("# extra\n")
    (stage / "eitri/klipper-layer/config").mkdir(parents=True)
    (stage / "eitri/klipper-layer/config/macros.cfg").write_text("# macros\n")
    os.symlink("../extras", stage / "eitri/klipper-layer/extras")
    os.symlink("/data/eitri/extras/qd_x.py", stage / "firmware/klipper/qd_link.py")
    (stage / "eitri/flavor").write_text(flavor + "\n")
    chdir = stage / "firmware/klipper/klippy/chelper"
    chdir.mkdir(parents=True)
    (chdir / "a.c").write_text("int a;\n")
    (chdir / "c_helper.so").write_bytes(b"\x7fELF" + b"\0" * 60)
    seed = stage / "eitri/seed"
    seed.mkdir(parents=True)
    tree, entries = seedlib.tree_digest(stage)
    env = (
        f"SEED_SCHEMA=1\nSEED_KIND=eitri-data-seed\nSEED_VERSION={version}\nSEED_FLAVOR={flavor}\nSEED_LAYOUT=1\n"
        f"SEED_TREE_SHA256={tree}\nSEED_TREE_BYTES=1\nSEED_FIRMWARE_COMMIT={fw}\nSEED_MOONRAKER_COMMIT={mr}\n"
    )
    (seed / "seed-info.env").write_text(env)
    (seed / "seed-info.json").write_text("{}\n")
    seedlib.normalize_tree(stage, EPOCH)
    os.utime(chdir / "c_helper.so", (EPOCH + 1, EPOCH + 1))
    return {"fw": fw, "mr": mr, "tree": tree, "entries": entries, "version": version, "flavor": flavor}


def pack(stage: Path, out: Path, version: str = "9.9.9", flavor: str = "klipper") -> tuple[Path, str]:
    arc = out / f"data-seed-{version}-{flavor}.tar.zst"
    seedlib.zstd_compress(lambda fh: seedlib.write_tar(stage, fh), arc, level=3, threads=1)
    return arc, hashlib.sha256(arc.read_bytes()).hexdigest()


def raw_archive(path: Path, members: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]]) -> tuple[Path, str]:
    """An arbitrary (possibly hostile) tar.zst built member by member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as t:
        for m in members:
            if isinstance(m, tuple):
                t.addfile(m[0], io.BytesIO(m[1]))
            elif m.isreg() and m.size:
                t.addfile(m, io.BytesIO(b"\0" * m.size))
            else:
                t.addfile(m)
    subprocess.run(["zstd", "-q", "-3", "-f", "-o", str(path)], input=buf.getvalue(), check=True)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def tinfo(name: str, typ=tarfile.REGTYPE, mode=0o644, linkname="", size=0) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type, ti.mode, ti.linkname, ti.size = typ, mode, linkname, size
    ti.uid = ti.gid = 1000
    ti.mtime = EPOCH
    return ti


def good_manifest(version: str = "9.9.9", flavor: str = "klipper") -> dict:
    sha = "a" * 64
    pk = [{"name": "cffi", "version": "2.1.1", "tags": ["cp314-cp314-manylinux_2_17_aarch64"]}]
    venv = {"lock": "requirements/x.txt", "lock_sha256": sha, "packages": pk, "built_from_source": []}
    return {
        "schema": 1, "kind": "eitri-data-seed", "version": version, "flavor": flavor, "layout": 1,
        "archive": {"name": f"data-seed-{version}-{flavor}.tar.zst", "sha256": sha, "size": 10, "uncompressed_size": 20,
                    "compression": "zstd", "tar_format": "gnu"},
        "created": "2026-10-21T12:00:00Z", "source_date_epoch": EPOCH, "info_sha256": sha,
        "target": {"arch": "aarch64", "python": "3.14.5", "python_abi": "cp314", "interpreter": "/usr/bin/python3",
                   "wheel_platform": "aarch64-manylinux_2_40", "glibc_max": "2.42", "glibc_needed": "2.34",
                   "image_libraries": ["libc.so.6", "libm.so.6"]},
        "sources": {
            "firmware": {"name": flavor, "version": "v1", "commit": "1" * 40, "tag": "v1", "describe": "v1-0-g111111111",
                         "origin": "https://example.org/fw.git", "branch": "master", "shallow": True},
            "moonraker": {"name": "moonraker", "version": "v0.11.0", "commit": "2" * 40, "tag": "v0.11.0", "describe": "v0.11.0-0-g222222222",
                          "origin": "https://example.org/mr.git", "branch": "master", "shallow": True},
            "klarity": {"version": "0.1.0", "wheel": "klarity-0.1.0-py3-none-any.whl", "wheel_sha256": sha},
            "klipper_layer": {"version": "0.0.0.dev0", "tree_sha256": sha},
        },
        "venvs": {"klippy": dict(venv), "moonraker": dict(venv), "klarity": dict(venv)},
        "chelper": {"path": "firmware/klipper/klippy/chelper/c_helper.so", "sha256": sha, "size": 1, "command": "gcc", "compiler": "gcc"},
        "tree": {"algorithm": "x", "digest": sha, "entries": 5, "bytes": 20, "components": list(seedlib.COMPONENTS)},
        "builder": {"script": "seed/build-seed.sh"},
    }
