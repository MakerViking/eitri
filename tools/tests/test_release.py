# SPDX-License-Identifier: Apache-2.0
"""Tests for tools/release: metadata generation, the preflight gate and the signing script.

Nothing here needs the network or a printer.  The tests that sign use the real ``minisign`` and, where it exists,
the real host ``rauc`` of the Buildroot tree; they skip with a reason when the tool is missing.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE_PY = ROOT / "tools" / "release" / "release.py"
SIGN_SH = ROOT / "tools" / "release" / "sign-release.sh"
VERSION = "2026.10.0"
GENERATED = "2026-10-22T09:00:00Z"
NOW = dt.datetime(2026, 10, 22, 10, 0, tzinfo=dt.timezone.utc)
BASE_URL = f"https://example.invalid/OWNER/REPO/releases/download/v{VERSION}/"


def _load():
    spec = importlib.util.spec_from_file_location("eitri_release_under_test", RELEASE_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


release = _load()


# --- fixtures ------------------------------------------------------------------------------------------
def host_rauc() -> str | None:
    for cand in (os.environ.get("EITRI_TEST_RAUC"), shutil.which("rauc"), str(ROOT.parent / "build" / "br-out" / "host" / "bin" / "rauc")):
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def blob(seed: str, size: int = 4096) -> bytes:
    out = b""
    n = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}{n}".encode()).digest()
        n += 1
    return out[:size]


def fake_pub(path: Path, seed: str = "k") -> Path:
    """A syntactically valid minisign public key (the preflight only parses it)."""
    raw = b"Ed" + hashlib.sha256(seed.encode()).digest()[:8] + hashlib.sha256(b"pk" + seed.encode()).digest()
    return write(path, "untrusted comment: minisign public key TEST\n" + base64.b64encode(raw).decode() + "\n")


def seed_manifest(lock: dict, flavor: str, archive: Path, version: str = VERSION) -> dict:
    sl = release.seedlib(ROOT)
    fw, mr = lock["firmware"][flavor], lock["moonraker"]
    origin = {"klipper": ("https://github.com/Klipper3d/klipper.git", "master"),
              "kalico": ("https://github.com/KalicoCrew/kalico.git", "main")}[flavor]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    h = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
    return {
        "schema": 1, "kind": "eitri-data-seed", "version": version, "flavor": flavor, "layout": 1,
        "archive": {"name": archive.name, "sha256": digest, "size": archive.stat().st_size, "uncompressed_size": 123456,
                    "compression": "zstd", "tar_format": "gnu"},
        "created": "2026-10-21T14:28:58Z", "source_date_epoch": 1792535588,
        "target": {"arch": "aarch64", "python": lock["python"]["version"], "python_abi": "cp314", "interpreter": "/usr/bin/python3",
                   "wheel_platform": "aarch64-manylinux_2_40", "glibc_max": "2.42", "glibc_needed": "2.34",
                   "image_libraries": ["libc.so.6", "libz.so.1"]},
        "sources": {
            "firmware": {"name": flavor, "commit": fw["commit"], "describe": "v0.13.0-813-ga08fc79fc", "origin": origin[0], "branch": origin[1],
                         "tag": "v0.13.0", "version": fw["version"], "shallow": True},
            "moonraker": {"name": "moonraker", "commit": mr["commit"], "describe": "v0.11.0-0-g985c1d0bb",
                          "origin": "https://github.com/Arksine/moonraker.git", "branch": "master"},
            "klarity": {"version": "0.1.0", "wheel": "klarity-0.1.0-py3-none-any.whl", "wheel_sha256": h("wheel")},
            "klipper_layer": {"version": "0.0.0.dev0", "tree_sha256": h("layer")},
        },
        "venvs": {n: {"lock": f"seed/requirements/{n}.txt", "lock_sha256": h(n), "packages": [{"name": "x", "version": "1"}]}
                  for n in ("klippy", "moonraker", "klarity")},
        "chelper": {"path": "firmware/klipper/klippy/chelper/c_helper.so", "sha256": h("chelper"), "size": 50936},
        "tree": {"algorithm": "sha256", "digest": h("tree"), "entries": 9, "bytes": 1, "components": list(sl.COMPONENTS)},
        "info_sha256": h("info"),
    }


class Fixture:
    """A release directory like the one the collect job assembles, plus the files release.py needs next to it."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.dir = tmp / "unsigned"
        self.dir.mkdir()
        self.keys = tmp / "keys"
        fake_pub(self.keys / "eitri-release.pub")
        self.keyring = write(tmp / "keyring.crt", "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
        self.lock_path = tmp / "release.lock"
        shutil.copy(ROOT / "release.lock", self.lock_path)
        self.lock = release.load_lock(self.lock_path)

    def add(self, name: str, data: bytes | None = None) -> Path:
        return write(self.dir / name, data if data is not None else blob(name))

    def add_seed(self, flavor: str, version: str = VERSION) -> None:
        arc = self.add(f"data-seed-{version}-{flavor}.tar.zst")
        write(self.dir / f"data-seed-{version}-{flavor}.manifest.json", json.dumps(seed_manifest(self.lock, flavor, arc, version)))

    def add_rootfs(self) -> None:
        zstd = shutil.which("zstd")
        if not zstd:
            pytest.skip("zstd is not installed")
        raw = write(self.tmp / "rootfs.raw", blob("rootfs", 65536))
        subprocess.run([zstd, "-q", "-f", str(raw), "-o", str(self.dir / f"rootfs-{VERSION}.img.zst")], check=True)

    def populate(self, *, flavors=("klipper", "kalico"), stage2=True, mcu=True, rootfs=True) -> "Fixture":
        self.add(f"eitri-plus4-{VERSION}.raucb")
        self.add(f"eitri-plus4-{VERSION}.img.zst")
        self.add("buildroot.config", b"BR2_x=y\n")
        for f in flavors:
            self.add_seed(f)
            if mcu:
                self.add(f"mcu-firmware-{VERSION}-{f}.tar.zst")
        if stage2:
            self.add(f"stage2-initramfs-{VERSION}.cpio.gz")
        if rootfs:
            self.add_rootfs()
        self.write_sums()
        return self

    def write_sums(self) -> None:
        names = sorted(p.name for p in self.dir.iterdir() if p.is_file() and p.name not in release.GENERATED_FILES and p.name != release.INPUTS_SUMS)
        text = "".join(f"{hashlib.sha256((self.dir / n).read_bytes()).hexdigest()}  {n}\n" for n in names)
        (self.dir / release.INPUTS_SUMS).write_text(text)

    def metadata(self, **kw):
        args = dict(root=ROOT, lock_path=self.lock_path, version=VERSION, base_url=BASE_URL, generated=GENERATED)
        args.update(kw)
        return release.build_metadata(self.dir, self.dir, **args)

    def preflight(self, stage="inputs", tag=f"v{VERSION}", now=NOW):
        return release.preflight(self.dir, stage=stage, root=ROOT, lock_path=self.lock_path, tag=tag, keys_dir=self.keys,
                                 keyring=self.keyring, now=now)

    def set_lock(self, old: str, new: str) -> None:
        text = self.lock_path.read_text()
        assert old in text, old
        self.lock_path.write_text(text.replace(old, new, 1))
        self.lock = release.load_lock(self.lock_path)


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


# --- release.lock ------------------------------------------------------------------------------------
def test_real_lock_has_valid_release_tables():
    cfg = release.release_settings(release.load_lock(ROOT / "release.lock"))
    assert cfg["min_manifest_sequence"] <= cfg["manifest_sequence"]
    assert "console=ttyS" not in cfg["install"]["kernel_cmdline"]   # ttyS0/ttyS2 are the MCU UARTs


def test_real_lock_clock_floor_matches_the_client():
    _, mf, _ = release.client(ROOT)
    assert release.load_lock(ROOT / "release.lock")["release"]["clock_floor"] == mf.CLOCK_FLOOR


@pytest.mark.parametrize("old,new,why", [
    ("manifest_sequence = 1", "manifest_sequence = 0", "at least 1"),
    ("min_manifest_sequence = 1", "min_manifest_sequence = 5", "above manifest_sequence"),
    ('clock_floor = "2026-10-01T00:00:00Z"', 'clock_floor = "2026-10-01"', "bad timestamp"),
    ("manifest_validity_days = 90", "manifest_validity_days = 3", "manifest_validity_days"),
    ("manifest_validity_days = 90", "manifest_validity_days = 4000", "manifest_validity_days"),
    ('channel = "stable"', 'channel = "Stable Channel"', "channel"),
    ("root_mib = 2048", "root_mib = 0", "root_mib"),
    ('stock_soc = ["1.7.1"]', "stock_soc = []", "stock_soc"),
])
def test_release_settings_rejects(fx, old, new, why):
    fx.set_lock(old, new)
    with pytest.raises(release.ReleaseError, match=why):
        release.release_settings(fx.lock)


# --- metadata ----------------------------------------------------------------------------------------
def test_metadata_complete_release_is_accepted_by_the_printers_own_parsers(fx):
    fx.populate()
    plan = fx.metadata()
    assert plan["documents"] == {"install.json": True, "manifest.json": True}
    _, mf, pp = release.client(ROOT)
    install = json.loads((fx.dir / "install.json").read_text())
    for flavor in ("klipper", "kalico"):
        pp.validate(install, flavor)   # the same function install.sh runs after verifying the signature
        assert install["files"]["data_seed"][flavor]["url"] == f"data-seed-{VERSION}-{flavor}.tar.zst"
        assert install["files"]["data_seed"][flavor]["manifest"]["url"] == f"data-seed-{VERSION}-{flavor}.manifest.json"
        assert install["flavors"][flavor]["commit"] == fx.lock["firmware"][flavor]["commit"]
    assert install["files"]["rootfs"]["raw_size"] == 65536
    assert install["files"]["rootfs"]["raw_sha256"] == hashlib.sha256(blob("rootfs", 65536)).hexdigest()
    man = mf.parse_manifest((fx.dir / "manifest.json").read_bytes())
    release_entry = man.latest()
    assert release_entry.version == VERSION
    assert man.sequence == fx.lock["release"]["manifest_sequence"]
    kinds = sorted((b.kind, b.flavor) for b in release_entry.bundles)
    assert kinds == [("mcu", "kalico"), ("mcu", "klipper"), ("os", None), ("seed", "kalico"), ("seed", "klipper")]
    seed_bundle = next(b for b in release_entry.bundles if b.kind == "seed" and b.flavor == "klipper")
    assert seed_bundle.manifest.url == BASE_URL + f"data-seed-{VERSION}-klipper.manifest.json"
    assert all(b.url.startswith(BASE_URL) for b in release_entry.bundles)
    assert man.generated == GENERATED and not man.expired(NOW)
    assert (man.upstream["klipper"]["origin"], man.upstream["moonraker"]["branch"]) == ("https://github.com/Klipper3d/klipper.git", "master")
    assert fx.preflight("metadata") == []


def test_metadata_is_deterministic(fx):
    fx.populate()
    fx.metadata()
    first = {n: (fx.dir / n).read_bytes() for n in release.GENERATED_FILES}
    fx.metadata()
    assert first == {n: (fx.dir / n).read_bytes() for n in release.GENERATED_FILES}


def test_install_json_is_not_written_while_the_stage2_initramfs_is_missing(fx):
    fx.populate(stage2=False)
    plan = fx.metadata()
    assert plan["documents"]["install.json"] is False
    assert f"stage2-initramfs-{VERSION}.cpio.gz" in plan["missing"]["install.json"]
    assert not (fx.dir / "install.json").exists()
    assert (fx.dir / "manifest.json").exists()   # the channel manifest does not need it


def test_install_json_offers_only_flavors_with_seed_and_mcu_firmware(fx):
    fx.populate()
    (fx.dir / f"mcu-firmware-{VERSION}-kalico.tar.zst").unlink()
    fx.write_sums()
    fx.metadata()
    install = json.loads((fx.dir / "install.json").read_text())
    assert list(install["flavors"]) == ["klipper"]
    assert list(install["files"]["mcu_firmware"]) == ["klipper"]


def test_metadata_without_an_os_bundle_has_no_manifest(fx):
    fx.populate()
    (fx.dir / f"eitri-plus4-{VERSION}.raucb").unlink()
    plan = fx.metadata()
    assert plan["documents"]["manifest.json"] is False and not (fx.dir / "manifest.json").exists()


def test_metadata_require_fails_when_incomplete(fx, capsys):
    fx.populate(stage2=False)
    argv = ["--root", str(ROOT), "--lock", str(fx.lock_path), "metadata", "--artifacts", str(fx.dir), "--out", str(fx.dir),
            "--version", VERSION, "--base-url", BASE_URL, "--generated", GENERATED]
    assert release.main(argv + ["--require", "manifest"]) == 0
    assert release.main(argv + ["--require", "install"]) == 1
    assert "install.json is required but incomplete" in capsys.readouterr().err


def test_metadata_carries_earlier_releases_and_demands_a_higher_sequence(fx):
    fx.populate()
    fx.metadata()
    previous = fx.tmp / "previous.json"
    old = json.loads((fx.dir / "manifest.json").read_text())
    old["sequence"] = 0
    old["releases"][0]["version"] = "2026.09.0"
    previous.write_text(json.dumps(old))
    fx.metadata(previous=previous)
    new = json.loads((fx.dir / "manifest.json").read_text())
    assert [r["version"] for r in new["releases"]] == [VERSION, "2026.09.0"]
    old["sequence"] = 1
    previous.write_text(json.dumps(old))
    with pytest.raises(release.ReleaseError, match="must be above the previous"):
        fx.metadata(previous=previous)


@pytest.mark.parametrize("mutate,match", [
    (lambda fx: fx.add("eitri-plus4-2026.09.0.raucb"), "another version"),
    (lambda fx: fx.add("data-seed-2026.09.0-klipper.tar.zst"), "another version"),
    (lambda fx: (fx.dir / f"data-seed-{VERSION}-klipper.manifest.json").unlink(), "needs both its archive and its manifest"),
    (lambda fx: (fx.dir / f"data-seed-{VERSION}-klipper.tar.zst").write_bytes(b"tampered"), "does not match its manifest"),
])
def test_metadata_refuses_inconsistent_artifacts(fx, mutate, match):
    fx.populate()
    mutate(fx)
    with pytest.raises(release.ReleaseError, match=match):
        fx.metadata()


def test_metadata_refuses_a_seed_built_from_other_commits_than_release_lock(fx):
    fx.populate()
    fx.set_lock(fx.lock["firmware"]["klipper"]["commit"], "0" * 40)
    with pytest.raises(release.ReleaseError, match="release.lock pins"):
        fx.metadata()


@pytest.mark.parametrize("version", ["v2026.10.0", "1.0", "2026.10"])
def test_metadata_refuses_a_version_the_client_would_reject(fx, version):
    fx.populate()
    with pytest.raises(release.ReleaseError, match="YYYY.MM.N"):
        fx.metadata(version=version)


@pytest.mark.parametrize("url", ["http://example.invalid/x/", "https://example.invalid/x"])
def test_metadata_refuses_a_bad_base_url(fx, url):
    fx.populate()
    with pytest.raises(release.ReleaseError, match="base-url"):
        fx.metadata(base_url=url)


# --- preflight: inputs -------------------------------------------------------------------------------
def test_preflight_accepts_a_clean_input_set(fx):
    fx.populate()
    assert fx.preflight() == []


def test_preflight_rejects_tampering_with_a_pinned_file(fx):
    fx.populate()
    (fx.dir / f"eitri-plus4-{VERSION}.img.zst").write_bytes(b"swapped after the build")
    assert any("does not match INPUTS.sha256" in e for e in fx.preflight())


def test_preflight_rejects_a_file_nobody_pinned(fx):
    fx.populate()
    fx.add("extra.bin")
    assert any("extra.bin is in the directory but not in INPUTS.sha256" in e for e in fx.preflight())


def test_preflight_rejects_a_missing_pinned_file(fx):
    fx.populate()
    (fx.dir / "buildroot.config").unlink()
    assert any("buildroot.config is in INPUTS.sha256 but missing" in e for e in fx.preflight())


def test_preflight_needs_the_pinned_digests_at_all(fx):
    fx.populate()
    (fx.dir / release.INPUTS_SUMS).unlink()
    assert any("INPUTS.sha256 is missing" in e for e in fx.preflight())


def test_preflight_rejects_a_tag_that_is_not_the_artifact_version(fx):
    fx.populate()
    assert any("the tag is v2026.10.1" in e for e in fx.preflight(tag="v2026.10.1"))


@pytest.mark.parametrize("name,data", [
    ("deploy.key", b"x"),
    ("notes.txt", b"-----BEGIN PRIVATE KEY-----\nabc\n"),
    ("notes2.txt", b"-----BEGIN EC PRIVATE KEY-----\nabc\n"),
    ("minisign.txt", b"untrusted comment: minisign encrypted secret key\nRWQ\n"),
    ("ca.pem", b"anything"),
])
def test_preflight_rejects_key_material_in_the_release_directory(fx, name, data):
    fx.populate()
    fx.add(name, data)
    fx.write_sums()
    assert any(e.startswith("SECRET?") for e in fx.preflight())


def test_preflight_rejects_a_seed_for_other_commits(fx):
    fx.populate()
    m = json.loads((fx.dir / f"data-seed-{VERSION}-klipper.manifest.json").read_text())
    m["sources"]["moonraker"]["commit"] = "1" * 40
    (fx.dir / f"data-seed-{VERSION}-klipper.manifest.json").write_text(json.dumps(m))
    fx.write_sums()
    assert any("Moonraker" in e and "release.lock pins" in e for e in fx.preflight())


def test_preflight_needs_a_trusted_public_key(fx):
    fx.populate()
    (fx.keys / "eitri-release.pub").unlink()
    assert any("no trusted public key" in e for e in fx.preflight())


def test_preflight_rejects_a_key_set_that_revocation_emptied(fx):
    fx.populate()
    ms, _, _ = release.client(ROOT)
    key = ms.load_keys([str(fx.keys)])[0]
    (fx.keys / "revoked-keys.txt").write_text(f"# retired\n{key.key_id_hex}\n")
    assert any("no trusted public keys" in e for e in fx.preflight())


def test_preflight_needs_the_committed_rauc_keyring(fx):
    fx.populate()
    fx.keyring.unlink()
    assert any("RAUC CA certificate" in e for e in fx.preflight())


def test_preflight_requires_clock_floor_to_move_with_the_client(fx):
    fx.populate()
    fx.set_lock('clock_floor = "2026-10-01T00:00:00Z"', 'clock_floor = "2026-12-01T00:00:00Z"')
    assert any("CLOCK_FLOOR" in e for e in fx.preflight())


def test_preflight_wants_os_bundle_image_and_a_seed(fx):
    fx.populate()
    for n in (f"eitri-plus4-{VERSION}.raucb", f"eitri-plus4-{VERSION}.img.zst"):
        (fx.dir / n).unlink()
    fx.write_sums()
    errs = fx.preflight()
    assert any("raucb is missing" in e for e in errs) and any("img.zst is missing" in e for e in errs)


# --- preflight: metadata -----------------------------------------------------------------------------
def test_preflight_metadata_requires_the_plan(fx):
    fx.populate()
    assert any("release-plan.json is missing" in e for e in fx.preflight("metadata"))


@pytest.mark.parametrize("expires,match", [
    ("2026-10-30T09:00:00Z", "valid for 8 days"),
    ("2028-10-22T09:00:00Z", "valid for 731 days"),
])
def test_preflight_metadata_bounds_the_manifest_lifetime(fx, expires, match):
    fx.populate()
    fx.metadata(expires=expires)
    assert any(match in e for e in fx.preflight("metadata"))


def test_preflight_metadata_rejects_generated_before_the_clock_floor(fx):
    fx.populate()
    fx.metadata(generated="2026-09-01T00:00:00Z")
    assert any("before CLOCK_FLOOR" in e for e in fx.preflight("metadata"))


@pytest.mark.parametrize("generated,match", [
    ("2026-10-25T00:00:00Z", "in the future"),
    ("2026-08-01T00:00:00Z", "before CLOCK_FLOOR"),
])
def test_preflight_metadata_rejects_implausible_generation_times(fx, generated, match):
    fx.populate()
    fx.metadata(generated=generated)
    assert any(match in e for e in fx.preflight("metadata"))


def test_preflight_metadata_rejects_a_stale_approval(fx):
    fx.populate()
    fx.metadata()
    late = NOW + dt.timedelta(days=45)
    assert any("old" in e for e in fx.preflight("metadata", now=late))


def test_preflight_metadata_rejects_a_sequence_below_the_image_floor_or_the_lock(fx):
    fx.populate()
    fx.set_lock("min_manifest_sequence = 1", "min_manifest_sequence = 2")
    fx.set_lock("manifest_sequence = 1", "manifest_sequence = 2")
    fx.metadata(sequence=1)
    errs = fx.preflight("metadata")
    assert any("below the floor" in e for e in errs) and any("below release.lock" in e for e in errs)


def test_preflight_metadata_detects_a_document_that_disagrees_with_the_files(fx):
    fx.populate()
    fx.metadata()
    doc = json.loads((fx.dir / "manifest.json").read_text())
    doc["releases"][0]["bundles"][0]["sha256"] = "f" * 64
    (fx.dir / "manifest.json").write_text(json.dumps(doc))
    assert any("size or SHA-256 does not match" in e for e in fx.preflight("metadata"))


def test_preflight_metadata_detects_a_swapped_install_json_seed(fx):
    fx.populate()
    fx.metadata()
    doc = json.loads((fx.dir / "install.json").read_text())
    doc["files"]["data_seed"]["klipper"]["sha256"] = "e" * 64
    (fx.dir / "install.json").write_text(json.dumps(doc))
    assert any("data_seed.klipper" in e for e in fx.preflight("metadata"))


def test_preflight_metadata_detects_a_console_on_an_mcu_uart(fx):
    fx.populate()
    fx.set_lock("console=tty1", "console=ttyS2,115200")
    fx.metadata()
    assert any("kernel_cmdline" in e for e in fx.preflight("metadata"))


def test_preflight_metadata_ignores_resigned_bundles_but_pins_them_in_the_plan(fx):
    fx.populate()
    fx.metadata()
    bundle = fx.dir / f"eitri-plus4-{VERSION}.raucb"
    bundle.write_bytes(bundle.read_bytes() + b"resigned")
    errs = fx.preflight("metadata")
    assert any("changed after release-plan.json" in e for e in errs)   # the plan, not INPUTS.sha256, pins the new bytes
    assert not any("INPUTS.sha256" in e and "raucb" in e for e in errs)


# --- command line ------------------------------------------------------------------------------------
def test_cli_preflight_exit_codes(fx, capsys):
    fx.populate()
    argv = ["--root", str(ROOT), "--lock", str(fx.lock_path), "preflight", "--dir", str(fx.dir), "--stage", "inputs",
            "--keys-dir", str(fx.keys), "--keyring", str(fx.keyring), "--tag", f"v{VERSION}"]
    assert release.main(argv) == 0
    (fx.dir / "buildroot.config").write_bytes(b"changed")
    assert release.main(argv) == 1
    assert "PREFLIGHT FAIL" in capsys.readouterr().err


# --- signing -----------------------------------------------------------------------------------------
MINISIGN = shutil.which("minisign")
OPENSSL = shutil.which("openssl")


def minisign_keypair(tmp: Path, name: str = "release", password: str | None = None) -> tuple[Path, Path]:
    pub, sec = tmp / f"{name}.pub", tmp / f"{name}.sec"
    cmd = [MINISIGN, "-G", "-f", "-p", str(pub), "-s", str(sec)]
    if password is None:
        cmd.append("-W")
        subprocess.run(cmd, check=True, capture_output=True)
    else:
        subprocess.run(cmd, check=True, capture_output=True, input=f"{password}\n{password}\n".encode())
    return pub, sec


def openssl(*args: str, cwd: Path) -> None:
    subprocess.run([OPENSSL, *args], cwd=cwd, check=True, capture_output=True)


def make_pki(directory: Path) -> None:
    """A CA and two code-signing leaves (build = the throwaway key of the build job, rel = the release signer)."""
    directory.mkdir(parents=True, exist_ok=True)
    openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "ca.key", "-out", "ca.crt",
            "-days", "3650", "-subj", "/O=Eitri test/CN=ca", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=directory)
    (directory / "ext.cnf").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning\n")
    for n in ("build", "rel"):
        openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", f"{n}.key", "-out", f"{n}.csr",
                "-subj", f"/O=Eitri test/CN={n}", cwd=directory)
        openssl("x509", "-req", "-in", f"{n}.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", f"{n}.crt", "-days", "825",
                "-extfile", "ext.cnf", cwd=directory)


FAKE_RAUC = """#!/usr/bin/env bash
# Stand-in for rauc: resign appends a marker, info succeeds only when the marker is there.
args=("$@")
for i in "${!args[@]}"; do
	case ${args[$i]} in resign) cmd=resign ;; info) cmd=info ;; esac
done
last=${args[-1]}
case $cmd in
	resign) in=${args[-2]}; { cat "$in"; printf 'RESIGNED'; } > "$last" ;;
	info) tail -c 8 "$last" | grep -q RESIGNED ;;
	*) exit 2 ;;
esac
"""


class SignEnv:
    def __init__(self, tmp: Path, fx: Fixture, rauc: str):
        self.tmp, self.fx = tmp, fx
        pub, self.sec = minisign_keypair(tmp)
        shutil.copy(pub, fx.keys / "eitri-release.pub")
        self.keyfiles = tmp / "secrets"
        self.keyfiles.mkdir()
        self.rauc_key, self.rauc_cert = write(self.keyfiles / "rauc.key", "fake"), write(self.keyfiles / "rauc.crt", "fake")
        self.rauc = rauc
        self.out = tmp / "signed"

    def run(self, **env_over):
        env = {k: v for k, v in os.environ.items() if not k.startswith("EITRI_")}
        env.update({
            "EITRI_MINISIGN_KEY_FILE": str(self.sec), "EITRI_RAUC_KEY_FILE": str(self.rauc_key), "EITRI_RAUC_CERT_FILE": str(self.rauc_cert),
            "EITRI_RAUC_KEYRING": str(self.fx.keyring), "EITRI_KEYS_DIR": str(self.fx.keys), "EITRI_RAUC": self.rauc,
        })
        env.update(env_over)
        # The script judges the timestamp against the real clock (a stale or future "generated" is refused).
        generated = release.fmt_ts(dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
        return subprocess.run(["bash", str(SIGN_SH), "--in", str(self.fx.dir), "--out", str(self.out), "--version", VERSION,
                               "--base-url", BASE_URL, "--generated", generated, "--lock", str(self.fx.lock_path)],
                              env=env, capture_output=True, text=True)


needs_minisign = pytest.mark.skipif(MINISIGN is None, reason="minisign is not installed")


@pytest.fixture
def sign_env(fx, tmp_path):
    if MINISIGN is None:
        pytest.skip("minisign is not installed")
    fake = write(tmp_path / "bin" / "rauc", FAKE_RAUC)
    fake.chmod(0o755)
    fx.populate()
    return SignEnv(tmp_path, fx, str(fake))


@needs_minisign
def test_signing_end_to_end_with_a_stand_in_rauc(sign_env):
    r = sign_env.run()
    assert r.returncode == 0, r.stdout + r.stderr
    out = sign_env.out
    assert (out / "SHA256SUMS.minisig").is_file() and (out / "install.json.minisig").is_file() and (out / "manifest.json.minisig").is_file()
    assert (out / f"eitri-plus4-{VERSION}.raucb").read_bytes().endswith(b"RESIGNED")   # re-signed, not the build's file
    assert not (out / "INPUTS.sha256").exists()
    assert release.verify_signed(out, root=ROOT, keys_dir=sign_env.fx.keys) == []
    # no key material was copied anywhere into the published set
    assert not any(p.suffix in (".sec", ".key") for p in out.iterdir())
    # the CLI used by the publish job agrees
    assert release.main(["--root", str(ROOT), "verify-signed", "--dir", str(out), "--keys-dir", str(sign_env.fx.keys)]) == 0


@needs_minisign
def test_signed_release_is_rejected_after_any_later_change(sign_env):
    assert sign_env.run().returncode == 0
    out = sign_env.out
    manifest = out / "manifest.json"
    original = manifest.read_bytes()
    manifest.write_bytes(original.replace(b'"sequence": 1', b'"sequence": 9'))
    assert any("manifest.json: signature check failed" in e for e in release.verify_signed(out, root=ROOT, keys_dir=sign_env.fx.keys))
    manifest.write_bytes(original)
    (out / f"data-seed-{VERSION}-klipper.tar.zst").write_bytes(b"swapped")
    assert any("does not match SHA256SUMS" in e for e in release.verify_signed(out, root=ROOT, keys_dir=sign_env.fx.keys))
    (out / "extra.bin").write_bytes(b"x")
    assert any("not covered by SHA256SUMS" in e for e in release.verify_signed(out, root=ROOT, keys_dir=sign_env.fx.keys))


@needs_minisign
def test_a_signature_by_an_untrusted_key_does_not_verify(sign_env, tmp_path):
    assert sign_env.run().returncode == 0
    other = tmp_path / "other-keys"
    pub, _ = minisign_keypair(tmp_path, "other")
    other.mkdir()
    shutil.copy(pub, other / "eitri-release.pub")
    assert any("signature check failed" in e for e in release.verify_signed(sign_env.out, root=ROOT, keys_dir=other))


@needs_minisign
def test_a_revoked_signing_key_does_not_verify(sign_env):
    assert sign_env.run().returncode == 0
    ms, _, _ = release.client(ROOT)
    key = ms.load_keys([str(sign_env.fx.keys)])[0]
    revoked = sign_env.tmp / "revoked"
    revoked.mkdir()
    shutil.copy(sign_env.fx.keys / "eitri-release.pub", revoked / "old.pub")
    fake_pub(revoked / "new.pub", "replacement")
    (revoked / "revoked-keys.txt").write_text(key.key_id_hex + "\n")
    assert any("unknown key" in e for e in release.verify_signed(sign_env.out, root=ROOT, keys_dir=revoked))


@needs_minisign
def test_signing_with_a_password_protected_key(sign_env, tmp_path):
    pub, sec = minisign_keypair(tmp_path, "pw", password="hunter2")
    shutil.copy(pub, sign_env.fx.keys / "eitri-release.pub")
    r = sign_env.run(EITRI_MINISIGN_KEY_FILE=str(sec), EITRI_MINISIGN_PASSWORD="hunter2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert release.verify_signed(sign_env.out, root=ROOT, keys_dir=sign_env.fx.keys) == []


@needs_minisign
def test_signing_stops_and_leaves_nothing_behind_when_an_input_was_tampered(sign_env):
    (sign_env.fx.dir / f"eitri-plus4-{VERSION}.img.zst").write_bytes(b"not what the build produced")
    r = sign_env.run()
    assert r.returncode != 0 and "does not match INPUTS.sha256" in r.stderr
    assert not sign_env.out.exists()


@needs_minisign
def test_signing_refuses_key_material_inside_the_artifact_directory(sign_env):
    inside = write(sign_env.fx.dir / "x.sec", "k")
    r = sign_env.run(EITRI_MINISIGN_KEY_FILE=str(inside))
    assert r.returncode != 0 and "keys must never be in the artifact directory" in r.stderr
    assert not sign_env.out.exists()


@needs_minisign
def test_signing_refuses_an_existing_output_directory(sign_env):
    sign_env.out.mkdir()
    (sign_env.out / "old").write_text("x")
    r = sign_env.run()
    assert r.returncode != 0 and "refusing to mix" in r.stderr
    assert (sign_env.out / "old").exists()


@needs_minisign
@pytest.mark.parametrize("var", ["EITRI_MINISIGN_KEY_FILE", "EITRI_RAUC_KEY_FILE", "EITRI_RAUC_CERT_FILE"])
def test_signing_needs_every_key(sign_env, var):
    r = sign_env.run(**{var: str(sign_env.tmp / "does-not-exist")})
    assert r.returncode != 0 and var in r.stderr
    assert not sign_env.out.exists()


@needs_minisign
def test_signing_stops_when_the_resigned_bundle_does_not_verify(sign_env, tmp_path):
    broken = write(tmp_path / "bin2" / "rauc", FAKE_RAUC.replace("RESIGNED'; } >", "NOPE'; } >"))
    broken.chmod(0o755)
    r = sign_env.run(EITRI_RAUC=str(broken))
    assert r.returncode != 0 and "does not verify" in r.stderr
    assert not sign_env.out.exists()


@pytest.mark.skipif(MINISIGN is None or OPENSSL is None or host_rauc() is None, reason="needs minisign, openssl and a rauc binary")
def test_signing_end_to_end_with_real_rauc_and_a_throwaway_build_key(fx, tmp_path):
    """The deferred-signing contract: the build signs with a key the image does not trust, the release step re-signs
    with the leaf that chains to the committed CA, and RAUC then accepts the bundle under the image's configuration."""
    rauc = host_rauc()
    pki = tmp_path / "pki"
    make_pki(pki)
    fx.populate()
    # replace the placeholder OS bundle by a real verity bundle signed by the build-only key
    src = tmp_path / "bundle-src"
    write(src / "rootfs.ext4", blob("ext4", 100000))
    write(src / "manifest.raucm", f"[update]\ncompatible=eitri-plus4\nversion={VERSION}\n[bundle]\nformat=verity\n[image.rootfs]\nfilename=rootfs.ext4\n")
    conf = write(tmp_path / "bundle.conf", f"[system]\ncompatible=eitri-plus4\nbootloader=noop\nstatusfile={tmp_path}/status\nbundle-formats=-plain\n"
                                          f"[keyring]\npath={pki}/ca.crt\ncheck-purpose=codesign\nuse-bundle-signing-time=true\n")
    bundle = fx.dir / f"eitri-plus4-{VERSION}.raucb"
    bundle.unlink()   # rauc will not overwrite the placeholder
    subprocess.run([rauc, f"--conf={conf}", "bundle", f"--cert={pki}/build.crt", f"--key={pki}/build.key", str(src), str(bundle)],
                   check=True, capture_output=True)
    fx.write_sums()
    shutil.copy(pki / "ca.crt", fx.keyring)

    env = SignEnv(tmp_path, fx, rauc)
    env.rauc_key, env.rauc_cert = pki / "rel.key", pki / "rel.crt"
    r = env.run()
    assert r.returncode == 0, r.stdout + r.stderr
    signed = env.out / bundle.name
    info = subprocess.run([rauc, f"--conf={conf}", "info", f"--keyring={pki}/ca.crt", "--output-format=json", str(signed)],
                          capture_output=True, text=True)
    assert info.returncode == 0, info.stderr
    assert json.loads(info.stdout)["version"] == VERSION
    chain = subprocess.run([rauc, f"--conf={conf}", "info", f"--keyring={pki}/ca.crt", "--dump-cert", str(signed)], capture_output=True, text=True)
    assert "CN = rel" in chain.stdout and "CN = build" not in chain.stdout
    # the bundle that came out of the build job is not accepted under the same keyring rules once its signer is not the release leaf
    assert signed.read_bytes() != bundle.read_bytes()
    manifest = json.loads((env.out / "manifest.json").read_text())
    entry = manifest["releases"][0]["bundles"][0]
    assert entry["sha256"] == hashlib.sha256(signed.read_bytes()).hexdigest()   # the manifest names the re-signed bytes


@pytest.mark.skipif(MINISIGN is None or OPENSSL is None or host_rauc() is None, reason="needs minisign, openssl and a rauc binary")
def test_a_bundle_signed_by_a_foreign_ca_is_not_resigned_into_a_trusted_one(fx, tmp_path):
    """Re-signing with a certificate the image's keyring does not chain to must fail before anything is published."""
    rauc = host_rauc()
    pki, other = tmp_path / "pki", tmp_path / "other"
    make_pki(pki)
    make_pki(other)
    fx.populate()
    src = tmp_path / "bundle-src"
    write(src / "rootfs.ext4", blob("ext4", 100000))
    write(src / "manifest.raucm", f"[update]\ncompatible=eitri-plus4\nversion={VERSION}\n[bundle]\nformat=verity\n[image.rootfs]\nfilename=rootfs.ext4\n")
    conf = write(tmp_path / "bundle.conf", f"[system]\ncompatible=eitri-plus4\nbootloader=noop\nstatusfile={tmp_path}/status\nbundle-formats=-plain\n"
                                          f"[keyring]\npath={pki}/ca.crt\ncheck-purpose=codesign\nuse-bundle-signing-time=true\n")
    (fx.dir / f"eitri-plus4-{VERSION}.raucb").unlink()   # rauc will not overwrite the placeholder
    subprocess.run([rauc, f"--conf={conf}", "bundle", f"--cert={pki}/build.crt", f"--key={pki}/build.key", str(src),
                    str(fx.dir / f"eitri-plus4-{VERSION}.raucb")], check=True, capture_output=True)
    fx.write_sums()
    shutil.copy(pki / "ca.crt", fx.keyring)
    env = SignEnv(tmp_path, fx, rauc)
    env.rauc_key, env.rauc_cert = other / "rel.key", other / "rel.crt"   # a leaf of a CA the keyring does not contain
    r = env.run()
    assert r.returncode != 0
    assert not env.out.exists()
