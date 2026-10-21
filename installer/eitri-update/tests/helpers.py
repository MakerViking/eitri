# SPDX-License-Identifier: Apache-2.0
"""Test scaffolding: a signed file:// channel, a fake rauc, a boot dir and a command dispatcher."""
import base64
import hashlib
import json
import os
import subprocess
import tarfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from eitri_mcuflash.events import EventSink
from eitri_mcuflash.proc import RunResult, run
from eitri_update.bootenv import BootControl, serialize
from eitri_update.client import Config, Updater
from eitri_update.rauc import RaucClient
from eitri_update.state import State


class Signer:
    def __init__(self, tmp, name="release"):
        self.sk = ed25519.Ed25519PrivateKey.generate()
        self.pk = self.sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.key_id = os.urandom(8)
        self.dir = os.path.join(str(tmp), "keys-" + name)
        os.makedirs(self.dir, exist_ok=True)
        self.pub = os.path.join(self.dir, name + ".pub")
        blob = base64.b64encode(b"Ed" + self.key_id + self.pk).decode()
        with open(self.pub, "w") as fh:
            fh.write("untrusted comment: minisign public key TEST\n%s\n" % blob)

    def sign(self, data, comment="test release", legacy=False):
        msg = data if legacy else hashlib.blake2b(data, digest_size=64).digest()
        sig = self.sk.sign(msg)
        glob = self.sk.sign(sig + comment.encode())
        alg = b"Ed" if legacy else b"ED"
        return ("untrusted comment: signature from test key\n%s\ntrusted comment: %s\n%s\n"
                % (base64.b64encode(alg + self.key_id + sig).decode(), comment, base64.b64encode(glob).decode()))


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class Channel:
    """A directory acting as the update server; URLs are file:// so no network is involved."""

    def __init__(self, tmp, signer, versions=("2026.10.0",), sequence=1, flavors=("klipper", "kalico")):
        self.tmp = str(tmp)
        self.dir = os.path.join(self.tmp, "server")
        os.makedirs(self.dir, exist_ok=True)
        self.signer = signer
        self.sequence = sequence
        self.releases = []
        self.addons = {}
        for v in versions:
            self.add_release(v, flavors)
        self.publish()

    def _file(self, name, size, seed):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write((hashlib.sha256(seed.encode()).digest() * (size // 32 + 1))[:size])
        return path

    def add_release(self, version, flavors=("klipper", "kalico"), **extra):
        osb = self._file("os-%s.raucb" % version, 4096, "os" + version)
        bundles = [{"kind": "os", "url": "file://" + osb, "sha256": sha(osb), "size": os.path.getsize(osb), "reboot": True}]
        for fl in flavors:
            d = self._file("data-%s-%s.raucb" % (fl, version), 2048, fl + version)
            bundles.append({"kind": "data", "flavor": fl, "url": "file://" + d, "sha256": sha(d), "size": os.path.getsize(d),
                            "restart_services": ["klipper", "moonraker"]})
        rel = {"version": version, "bundles": bundles, "notes_url": "https://example.test/notes/" + version}
        rel.update(extra)
        self.releases.append(rel)

    def add_toolchain(self, archive, version="13.3-2026.10"):
        self.addons["toolchain"] = {"version": version, "url": "file://" + archive, "sha256": sha(archive),
                                    "size": os.path.getsize(archive), "installed_size": 2 * os.path.getsize(archive)}
        self.publish()

    def publish(self, tamper=False, sign_with=None):
        doc = {"schema": 1, "channel": "stable", "sequence": self.sequence, "generated": "2026-10-21T00:00:00Z",
               "expires": "2030-01-01T00:00:00Z", "releases": self.releases, "addons": self.addons}
        data = json.dumps(doc, indent=1).encode()
        path = os.path.join(self.dir, "manifest.json")
        signer = sign_with or self.signer
        with open(path, "wb") as fh:
            fh.write(data)
        with open(path + ".minisig", "w") as fh:
            fh.write(signer.sign(data))
        if tamper:
            with open(path, "wb") as fh:
                fh.write(data.replace(b"2026.10.0", b"2026.10.9"))
        return "file://" + path


class Dispatcher:
    """Fake command runner: answers rauc/systemctl/mcuflash, forwards everything else to the real thing."""

    def __init__(self):
        self.calls = []
        self.rauc_slots = {"A": {"state": "booted", "boot_status": "good", "version": "2026.09.0"},
                           "B": {"state": "inactive", "boot_status": "bad", "version": ""}}
        self.rauc_fail_install = False
        self.installs = []
        self.mcu_boards = {"mainboard": True, "toolhead": True}      # board -> needs_flash
        self.mcu_fail = False
        self.mcu_done_before_fail = []       # boards that were flashed before the failing one
        self.mcu_rollbacks = []
        self.passthrough = ("tar", "git")

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        a0 = argv[0]
        if a0 == "rauc":
            return self._rauc(argv, kw)
        if a0 == "systemctl":
            return RunResult(0, "")
        if a0 == "mount" or a0 == "sync":
            return RunResult(0, "")
        if "eitri_mcuflash" in argv:
            return self._mcuflash(argv, kw)
        if a0 in self.passthrough:
            return run(argv, **kw)
        return RunResult(127, "unexpected command %r" % (argv,))

    def _rauc(self, argv, kw):
        if argv[1] == "status" and argv[2].startswith("--output-format"):
            slots = []
            for i, (b, s) in enumerate(sorted(self.rauc_slots.items())):
                slots.append({"rootfs.%d" % i: {"bootname": b, "state": s["state"], "boot_status": s["boot_status"],
                                                "slot_status": {"bundle": {"version": s["version"]}}}})
            booted = [b for b, s in self.rauc_slots.items() if s["state"] == "booted"][0]
            return RunResult(0, json.dumps({"compatible": "eitri-plus4", "booted": booted, "slots": slots}))
        if argv[1] == "status" and argv[2].startswith("mark-"):
            return RunResult(0, "")
        if argv[1] == "install":
            if self.rauc_fail_install:
                return RunResult(1, "rauc: signature verification failed")
            self.installs.append(argv[2])
            on_line = kw.get("on_line")
            if on_line:
                on_line("  10% Checking bundle")
                on_line(" 100% Installing done.")
            return RunResult(0, "installing done")
        return RunResult(1, "unknown rauc call")

    def _mcuflash(self, argv, kw):
        cmd = argv[argv.index("eitri_mcuflash") + 1]
        on_line = kw.get("on_line")
        if cmd == "plan":
            data = {"boards": {b: {"needs_flash": nf} for b, nf in self.mcu_boards.items()}}
            ev = {"type": "result", "ok": True, "data": data}
        elif cmd == "flash":
            if self.mcu_fail:
                ev = {"type": "result", "ok": False, "msg": "toolhead did not come back",
                      "data": {"results": {b: {} for b in self.mcu_done_before_fail}}}
            else:
                ev = {"type": "result", "ok": True, "data": {}}
                self.mcu_boards = {b: False for b in self.mcu_boards}
        elif cmd == "rollback":
            self.mcu_rollbacks.extend(a for a in argv if a in ("mainboard", "toolhead"))
            ev = {"type": "result", "ok": True}
        else:
            return RunResult(2, "bad")
        if on_line:
            on_line(json.dumps(ev))
        return RunResult(0 if ev["ok"] else 1, json.dumps(ev))


class Rig:
    def __init__(self, tmp, versions=("2026.10.0",), installed="2026.09.0", flavor="klipper", slot="A", sequence=1,
                 health_ok=True, tries="3"):
        tmp = str(tmp)
        self.tmp = tmp
        self.signer = Signer(tmp)
        self.channel = Channel(tmp, self.signer, versions, sequence)
        self.data = os.path.join(tmp, "data")
        os.makedirs(os.path.join(self.data, "eitri", "state"))
        for name, value in (("flavor", flavor), ("channel", "eitri")):
            with open(os.path.join(self.data, "eitri", name), "w") as fh:
                fh.write(value + "\n")
        self.boot = os.path.join(tmp, "boot")
        os.makedirs(os.path.join(self.boot, "eitri"))
        with open(os.path.join(self.boot, "eitri", "boot.env"), "w") as fh:
            fh.write(serialize({"eitri_mode": "eitri", "eitri_slot": slot, "eitri_tries": tries,
                                "eitri_state_A": "good", "eitri_state_B": "bad"}))
        self.cmdline = os.path.join(tmp, "cmdline")
        with open(self.cmdline, "w") as fh:
            fh.write("root=PARTUUID=x ro eitri.slot=%s quiet\n" % slot)
        self.run = Dispatcher()
        self.events = EventSink(keep=True, run_id="t")
        self.cfg = Config(manifest_url=self.channel_url(), key_paths=[self.signer.pub], data_root=self.data,
                          mcu_firmware_root=os.path.join(self.data, "eitri", "mcu", "firmware"),
                          mcu_state_dir=os.path.join(self.data, "eitri", "mcu"), health_timeout=1)
        self.state = State(os.path.join(self.data, "eitri"))
        if installed:
            self.state.update(installed_version=installed)
        self.bootctl = BootControl(self.boot, runner=self.run, cmdline_path=self.cmdline, lock_path=os.path.join(tmp, "lock"))
        self.rebooted = 0
        self.health = lambda url, **kw: type("H", (), {"ok": health_ok, "detail": "ok" if health_ok else "klippy not connected"})()
        self.updater = Updater(self.cfg, self.events, runner=self.run, rauc=RaucClient(self.run), bootctl=self.bootctl, state=self.state,
                               reboot=self._reboot, health_check=self.health, sleep=lambda s: None)

    def channel_url(self):
        return "file://" + os.path.join(self.channel.dir, "manifest.json")

    def _reboot(self):
        self.rebooted += 1

    def boot_env(self):
        return self.bootctl.read()

    def results(self):
        return [e for e in self.events.events if e["type"] == "result"]


def make_toolchain_archive(path, files=None, bad_member=None):
    """A .tar.zst with a top-level 'toolchain/' directory, like the CI add-on."""
    files = files if files is not None else {"toolchain/bin/arm-none-eabi-gcc": b"#!/bin/sh\necho gcc\n", "toolchain/bin/gcc": b"x"}
    raw = path[: -len(".zst")]
    with tarfile.open(raw, "w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            import io
            tf.addfile(info, io.BytesIO(data))
        if bad_member:
            info = tarfile.TarInfo(bad_member)
            info.size = 1
            import io
            tf.addfile(info, io.BytesIO(b"x"))
    subprocess.run(["zstd", "-q", "-f", raw, "-o", path], check=True)
    os.unlink(raw)
    return path
