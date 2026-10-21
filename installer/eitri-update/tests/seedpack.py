# SPDX-License-Identifier: Apache-2.0
"""Synthetic data seeds and MCU firmware archives for tests (the layout of seed/SPEC.md, a few bytes each)."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile

COMPONENT_FILES = {
    "firmware/klipper/klippy/klippy.py": "# {flavor} {version}\n",
    "firmware/moonraker/moonraker/moonraker.py": "# moonraker {version}\n",
    "venv/klippy/bin/python": "#!/bin/sh\n# klippy venv {flavor} {version}\n",
    "venv/moonraker/bin/python": "#!/bin/sh\n# moonraker venv {version}\n",
    "eitri/klarity/venv/bin/klarity": "#!/bin/sh\n# klarity {version}\n",
    "eitri/extras/qd_dummy.py": "# extras {version}\n",
    "eitri/klipper-layer/config/printer.cfg.tmpl": "# template {version}\n",
    "eitri/klipper-layer/scripts/overlay-install.sh": "#!/bin/sh\n# {version}\nexit 0\n",
}
PARENT_DIRS = ("eitri", "eitri/klarity", "firmware", "venv")


def commit_for(flavor, version):
    return hashlib.sha1(("%s-%s" % (flavor, version)).encode()).hexdigest()


def describe_for(flavor, version):
    return "v0.13.0-813-g%s" % commit_for(flavor, version)[:9] if flavor == "klipper" else "v2026.10.00-0-g%s" % commit_for(flavor, version)[:9]


def release_flavors(version, flavors=("klipper", "kalico")):
    """The `flavors` entry of a release / install.json: the host firmware each MCU image set is built from."""
    return {f: {"commit": commit_for(f, version), "describe": describe_for(f, version)} for f in flavors}


def _zstd(src, dst):
    subprocess.run(["zstd", "-q", "-f", src, "-o", dst], check=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_seed(outdir, version, flavor, *, abi=None, extra_members=None, commit=None, describe=None, mutate_manifest=None,
              uncompressed_size=None, name=None, flavor_inside=None):
    """Write data-seed-<version>-<flavor>.tar.zst and its manifest into *outdir*.  Returns a dict of paths/digests.
    *extra_members* = [(name, bytes_or_TarInfo)] are added verbatim (hostile archives); *mutate_manifest* edits the
    manifest dict before it is written."""
    os.makedirs(outdir, exist_ok=True)
    commit = commit or commit_for(flavor, version)
    describe = describe or describe_for(flavor, version)
    abi = abi or "cp%d%d" % (sys.version_info[0], sys.version_info[1])
    name = name or "data-seed-%s-%s.tar.zst" % (version, flavor)
    raw = os.path.join(outdir, name[: -len(".zst")])
    files = {}
    for path, text in COMPONENT_FILES.items():
        files[path] = text.format(flavor=flavor, version=version).encode()
    files["eitri/flavor"] = (flavor_inside or flavor).encode() + b"\n"
    env = ("# seed\nSEED_KIND=eitri-data-seed\nSEED_FLAVOR=%s\nSEED_LAYOUT=1\nSEED_VERSION=%s\nSEED_TREE_SHA256=%s\n"
           "SEED_FIRMWARE_COMMIT=%s\nSEED_MOONRAKER_COMMIT=%s\n"
           % (flavor, version, hashlib.sha256((flavor + version).encode()).hexdigest(), commit, "b" * 40))
    files["eitri/seed/seed-info.env"] = env.encode()
    files["eitri/seed/seed-info.json"] = json.dumps({"version": version, "flavor": flavor}).encode()
    files["eitri/seed/locks/klippy.txt"] = b"# lock\n"
    with tarfile.open(raw, "w", format=tarfile.GNU_FORMAT) as tf:
        def add(info, data=None):
            info.uid = info.gid = 1000
            info.uname = info.gname = ""
            info.mtime = 1700000000
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
        dirs = set(PARENT_DIRS)
        for path in files:
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        entries = [(d, None) for d in dirs] + [(p, c) for p, c in files.items()]
        for path, data in sorted(entries):
            if data is None:
                info = tarfile.TarInfo(path)
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                add(info)
            else:
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mode = 0o755 if ("/bin/" in path or path.endswith(".sh")) else 0o644
                add(info, data)
        for member, payload in extra_members or []:
            if isinstance(member, tarfile.TarInfo):
                add(member, payload)
            else:
                info = tarfile.TarInfo(member)
                info.size = len(payload)
                info.mode = 0o644
                add(info, payload)
    archive = os.path.join(outdir, name)
    _zstd(raw, archive)
    os.unlink(raw)
    unpacked = uncompressed_size or sum(len(v) for v in files.values())
    doc = {
        "schema": 1, "kind": "eitri-data-seed", "version": version, "flavor": flavor, "layout": 1,
        "archive": {"name": name, "sha256": sha256_file(archive), "size": os.path.getsize(archive), "uncompressed_size": unpacked,
                    "compression": "zstd", "tar_format": "gnu"},
        "created": "2026-10-21T00:00:00Z", "source_date_epoch": 1700000000,
        "target": {"arch": "aarch64", "python": "3.14.5", "python_abi": abi, "interpreter": "/usr/bin/python3",
                   "wheel_platform": "aarch64-manylinux_2_40", "glibc_max": "2.42", "glibc_needed": "2.34", "image_libraries": ["libc.so.6"]},
        "sources": {"firmware": {"name": flavor, "version": describe.split("-")[0], "commit": commit, "tag": describe.split("-")[0],
                                 "describe": describe, "origin": "https://example.test/%s.git" % flavor, "branch": "master", "shallow": True},
                    "moonraker": {"name": "moonraker", "commit": "b" * 40, "describe": "v0.11.0-0-gbbbbbbbbb"}},
    }
    if mutate_manifest:
        mutate_manifest(doc)
    manifest = os.path.join(outdir, "data-seed-%s-%s.manifest.json" % (version, flavor))
    with open(manifest, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return {"name": name, "path": archive, "sha256": sha256_file(archive), "size": os.path.getsize(archive),
            "manifest_path": manifest, "manifest_sha256": sha256_file(manifest), "manifest_size": os.path.getsize(manifest),
            "doc": doc, "commit": commit, "describe": describe, "unpacked": unpacked}


def make_mcu_archive(path, flavor, version="x", boards=("mainboard", "toolhead"), other_flavors=(), extra=None):
    """mcu-firmware tar.zst: <flavor>/manifest.json + <flavor>/<board>/klipper.bin (and, optionally, other flavors' trees)."""
    raw = path[: -len(".zst")]
    with tarfile.open(raw, "w") as tf:
        def put(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
        for fl in (flavor,) + tuple(other_flavors):
            doc = {"schema": 1, "flavor": fl, "source": {"commit": commit_for(fl, version)},
                   "boards": {b: {"file": "%s/klipper.bin" % b, "version": describe_for(fl, version)} for b in boards}}
            put("%s/manifest.json" % fl, json.dumps(doc).encode())
            for b in boards:
                put("%s/%s/klipper.bin" % (fl, b), (b + fl + version).encode() * 8)
        for name, data in (extra or []):
            if isinstance(name, tarfile.TarInfo):
                tf.addfile(name, io.BytesIO(data) if data is not None else None)
            else:
                put(name, data)
    _zstd(raw, path)
    os.unlink(raw)
    return path
