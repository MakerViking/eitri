#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Boot smoke test, first-run setup test and performance measurement of the Eitri OS on QEMU.

    python3 -I os/qemu/smoke.py [--out DIR] [--no-second-boot] [--seed-flavor klipper|kalico] [--expect-failed UNIT]...

Boot 1 starts run-qemu.sh on a /data disk built from the data seed exactly like the flash-once image's
data partition (os/board/eitri-plus4/mkdata.sh + seed/bin/eitri-seed-install), with the serial port on a
unix socket. It logs in as the QEMU-only "bench" user, sends guest-collect.sh, collects the state before any
setup, and then does what a person does on the first run, against the real services: reads the pairing
code (what Klarity shows), pairs, detects, applies (the configurator renders printer.cfg, klarity.cfg and
moonraker.conf and links the qd_* extras), checks Klipper, Moonraker, nginx and Klarity, adds an SSH key
through the optional root-access step and logs in with it, finishes setup, waits for the configurator to
retire, unlocks it the way Klarity's button does, locks it again and powers the guest off (timed).
Boot 2 boots the same /data: Klipper, Moonraker and Klarity must come up by themselves and the configurator
must stay retired; timing and per-service memory are collected there.

Writes into DIR/<timestamp>/ : summary.md, summary.json, guest-*.txt, serial-boot*.log, apply-log.txt.
Exit status 0 when every check passes.

QEMU runs the guest in software (TCG): absolute times are not the printer's.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BUILD_ROOT = Path(os.environ.get("BUILD_ROOT", REPO.parent / "build"))
OUT = Path(os.environ.get("OUT", BUILD_ROOT / "br-qemu"))

# What QEMU cannot give the guest; reported in the summary, never a failure.
EXPECTED_SKIPPED = {
    "eitri-camera": "needs the UVC camera (/dev/eitri-cam); there is none in QEMU",
    "klippy": "has no MCU: /dev/ttyS2 and /dev/ttyS0 do not exist, so Klipper stays in its 'cannot connect' state (it keeps running)",
    "klarity": "has no touchscreen UART (/dev/ttyS1): it waits for the port",
    "eitri-prepare / eitri-verify": "no boot slot on the kernel command line (QEMU boots the kernel directly)",
}


# Terminal control sequences on the console: systemd's OSC 3008 shell integration, DCS terminal queries, CSI.
ESCAPE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1bP[^\x1b]*\x1b\\|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>78c\\]")


def load_known_failures() -> dict[str, tuple[str, str]]:
    known: dict[str, tuple[str, str]] = {}
    for line in (HERE / "known-failures.txt").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            unit, kind, reason = (line.split(None, 2) + ["", ""])[:3]
            known[unit] = (kind, reason.strip())
    return known


class Boot:
    """One QEMU run with its serial port on a unix socket."""

    def __init__(self, tag: str, run_dir: Path, fresh: bool, ports: dict[str, int], log_dir: Path, mem: str, smp: str, seed_flavor: str = "klipper"):
        self.tag = tag
        self.sock_path = run_dir / "serial.sock"
        self.log_dir = log_dir
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None
        self.t0 = time.monotonic()
        self.t_first_byte: float | None = None
        self.t_login: float | None = None
        self.seq = 0
        self.proc: subprocess.Popen | None = None
        env = dict(os.environ, RUN_DIR=str(run_dir), MEM=mem, SMP=smp, SEED_FLAVOR=seed_flavor, **{k: str(v) for k, v in ports.items()})
        cmd = [str(HERE / "run-qemu.sh"), "--socket", str(self.sock_path)]
        if fresh:
            cmd.insert(1, "--fresh")
        self.qemu_out = open(log_dir / f"qemu-{tag}.out", "wb")
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=self.qemu_out, stderr=subprocess.STDOUT)
        self._connect()
        threading.Thread(target=self._reader, daemon=True).start()

    def _connect(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"qemu exited early with status {self.proc.returncode}; see qemu-{self.tag}.out")
            if self.sock_path.exists():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.connect(str(self.sock_path))
                    self.sock = s
                    return
                except OSError:
                    s.close()
            time.sleep(0.05)
        raise RuntimeError("serial socket never appeared")

    def _reader(self) -> None:
        assert self.sock
        pending = b""
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                if self.t_first_byte is None:
                    self.t_first_byte = time.monotonic() - self.t0
                data = pending + data
                # Hold back an escape sequence that is not complete yet; it is finished by the next chunk.
                pending = b""
                cut = max(data.rfind(x) for x in (b"\x1b]", b"\x1bP", b"\x1b["))
                if cut != -1 and not ESCAPE.match(data, cut) and len(data) - cut < 4096:
                    data, pending = data[:cut], data[cut:]
                elif data.endswith(b"\x1b"):
                    data, pending = data[:-1], b"\x1b"
                self.buf += ESCAPE.sub(b"", data).replace(b"\r", b"").replace(b"\x1b", b"")
                if self.t_login is None and b"login:" in self.buf:
                    self.t_login = time.monotonic() - self.t0

    def text(self) -> str:
        with self.lock:
            return self.buf.decode("utf-8", "replace")

    def send(self, s: str) -> None:
        assert self.sock
        self.sock.sendall(s.encode())

    def expect(self, pattern: str, timeout: float, start: int = 0, flags: int = re.M, nudge: str | None = None):
        """Wait for a regex in the serial text after offset `start`; returns (match, text offset after it)."""
        rx = re.compile(pattern, flags)
        deadline = time.monotonic() + timeout
        last_nudge = time.monotonic()
        while True:
            t = self.text()
            m = rx.search(t, start)
            if m:
                return m, m.end()
            if self.proc.poll() is not None:
                raise RuntimeError(f"qemu exited (status {self.proc.returncode}) while waiting for {pattern!r}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"timeout after {timeout:.0f}s waiting for {pattern!r}; tail:\n{t[-1500:]}")
            if nudge and time.monotonic() - last_nudge > 30:
                self.send(nudge)
                last_nudge = time.monotonic()
            time.sleep(0.1)

    def login(self, user: str, password: str, timeout: float) -> None:
        self.expect(r"login: ?$", timeout, nudge="\r")
        pos = len(self.text())
        self.send(user + "\r")
        self.expect(r"[Pp]assword: ?", 60, pos)
        pos = len(self.text())
        self.send(password + "\r")
        m, _ = self.expect(r"[$#] ?$|Login incorrect|bad salt", 120, pos)
        if not re.search(r"[$#] ?$", m.group(0)):
            raise RuntimeError(f"login failed: {m.group(0)!r}; serial tail:\n{self.text()[-400:]}")
        self.cmd("stty -echo; export PS1= PS2= TERM=dumb HISTFILE=; echo ready", 60, first=True)

    def cmd(self, command: str, timeout: float, first: bool = False) -> tuple[str, int]:
        """Run a shell command in the guest; returns (output, exit status)."""
        self.seq += 1
        marker = f"__SMOKE_{self.seq}__"
        pos = len(self.text())
        self.send(f"{command}; __rc=$?; echo; echo {marker} rc=$__rc\r")      # the bare echo: output without a final newline must not hide the marker
        m, end = self.expect(rf"^{marker} rc=(\d+)\s*$", timeout, pos)
        out = self.text()[pos:m.start()]
        if first:
            out = out.split("\n", 1)[-1]
        return out, int(m.group(1))

    def put_file(self, path: Path, dest: str) -> None:
        data = base64.b64encode(path.read_bytes()).decode()
        self.cmd(f": > {dest}.b64", 30)
        for i in range(0, len(data), 800):
            self.cmd(f"printf %s '{data[i:i + 800]}' >> {dest}.b64", 30)
        out, rc = self.cmd(f"base64 -d {dest}.b64 > {dest} && wc -c < {dest}", 30)
        if rc != 0:
            raise RuntimeError(f"upload of {path.name} failed: {out}")

    def wait_exit(self, timeout: float) -> float | None:
        t = time.monotonic()
        try:
            self.proc.wait(timeout=timeout)
            return time.monotonic() - t
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.sock:
            self.sock.close()
        self.qemu_out.close()


# --- parsing -------------------------------------------------------------------------------

def parse_sections(text: str) -> dict[str, dict]:
    sections: dict[str, dict] = {}
    cur = None
    lines: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        m = re.match(r"^@@SECTION (\S+)$", line)
        if m:
            cur, lines = m.group(1), []
            continue
        m = re.match(r"^@@END (\S+) rc=(\d+)$", line)
        if m and cur == m.group(1):
            sections[cur] = {"text": "\n".join(lines).strip("\n"), "rc": int(m.group(2))}
            cur = None
            continue
        if cur is not None:
            lines.append(line)
    return sections


def seconds(s: str) -> float:
    """'1min 3.2s', '850ms', '4.1s' -> seconds."""
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)\s*(min|ms|us|s|h)\b", s):
        total += float(num) * {"h": 3600, "min": 60, "s": 1, "ms": 1e-3, "us": 1e-6}[unit]
    return total


def parse_timing(text: str) -> dict:
    out: dict = {}
    m = re.search(r"Startup finished in (.*?)(?:\n|$)", text)
    if m:
        line = m.group(1)
        out["startup_line"] = line
        for key, rx in (("firmware", r"([\d.]+\w*(?:\s[\d.]+\w+)*)\s*\(firmware\)"), ("loader", r"([\d.]+\w*(?:\s[\d.]+\w+)*)\s*\(loader\)"),
                        ("kernel", r"([\d.]+\w*(?:\s[\d.]+\w+)*)\s*\(kernel\)"), ("initrd", r"([\d.]+\w*(?:\s[\d.]+\w+)*)\s*\(initrd\)"),
                        ("userspace", r"([\d.]+\w*(?:\s[\d.]+\w+)*)\s*\(userspace\)")):
            mm = re.search(rx, line)
            if mm:
                out[key + "_s"] = round(seconds(mm.group(1)), 3)
        mm = re.search(r"=\s*(.+)$", line)
        if mm:
            out["total_s"] = round(seconds(mm.group(1)), 3)
    m = re.search(r"(\S+\.target) reached after (.*?) in userspace", text)
    if m:
        out["target"] = m.group(1)
        out["target_reached_userspace_s"] = round(seconds(m.group(2)), 3)
    return out


def parse_ready_times(text: str) -> dict:
    """Service activation times (seconds since the kernel started); the latest one that is not the camera."""
    times: dict[str, float] = {}
    for block in text.split("\n\n"):
        kv = dict(l.split("=", 1) for l in block.split("\n") if "=" in l)
        if kv.get("ActiveState") == "active" and kv.get("ActiveEnterTimestampMonotonic", "0") != "0":
            times[kv["Id"]] = int(kv["ActiveEnterTimestampMonotonic"]) / 1e6
    out: dict = {}
    if times:
        rest = {k: v for k, v in times.items() if k != "eitri-camera.service"}
        last = max(rest, key=rest.get)
        out["last_service"] = last
        out["last_service_active_s"] = round(rest[last], 2)
        out["services_active"] = len(times)
    return out


def parse_free(text: str) -> dict:
    out: dict = {}
    hdr = None
    for line in text.split("\n"):
        parts = line.split()
        if parts and parts[0] == "total":
            hdr = parts
        elif parts and parts[0].startswith("Mem:") and hdr:
            vals = [int(x) for x in parts[1:] if x.isdigit()]
            out.update(dict(zip(hdr, vals)))
        elif parts and parts[0].startswith("Swap:"):
            vals = [int(x) for x in parts[1:] if x.isdigit()]
            if vals:
                out["swap_total"] = vals[0]
    return out


def parse_meminfo(text: str) -> dict:
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"^(\w+):\s+(\d+) kB", text, re.M)}


def parse_units_mem(text: str) -> list[dict]:
    rows = []
    for line in text.split("\n"):
        m = re.match(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", line)
        if m and m.group(1) != "UNIT":
            rows.append({"unit": m.group(1), "rss_kb": int(m.group(2)), "pss_kb": int(m.group(3)), "procs": int(m.group(4)), "commands": m.group(5)})
        else:
            m = re.match(r"^TOTAL \(userspace processes\)\s+(\d+)\s+(\d+)", line)
            if m:
                rows.append({"unit": "TOTAL", "rss_kb": int(m.group(1)), "pss_kb": int(m.group(2))})
    return rows


def parse_unit_state(text: str) -> dict[str, dict]:
    out = {}
    for line in text.split("\n"):
        name, _, rest = line.partition(" ")
        rest = rest.strip()
        if not name or not rest:
            continue
        out[name] = dict(kv.split("=", 1) for kv in rest.split() if "=" in kv)
    return out


def http_probe(port: int, path: str = "/") -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}"


def http_status(port: int, method: str, path: str, headers: dict[str, str]) -> int:
    """Status of one request with exactly these headers (Host included), the way a browser or a slicer sends it."""
    return http_request(port, method, path, headers)[0]


def http_request(port: int, method: str, path: str, headers: dict[str, str]) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        if method == "POST":
            conn.putheader("Content-Length", "0")
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read().decode(errors="replace")
    except OSError:
        return 0, ""
    finally:
        conn.close()


def free_ports(n: int) -> list[int]:
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


# --- the configurator, driven over HTTP as a browser would -------------------------------------

class Wizard:
    """The setup wizard (port 8099, forwarded to the host), used with a bearer token like a script would."""

    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.token: str | None = None

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 90) -> tuple[int, dict]:
        data = json.dumps(body if body is not None else {}).encode() if method in ("POST", "PUT") else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return 0, {"error": f"{type(e).__name__}: {e}"}
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {"raw": raw[:200].decode("utf-8", "replace")}

    def job(self, started: tuple[int, dict], timeout: float = 300) -> dict:
        status, body = started
        if status != 202:
            return {"status": "refused", "http": status, "body": body}
        jid = body["job"]["id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, j = self.call("GET", f"/api/jobs/{jid}")
            if j.get("status") not in (None, "running"):
                return j
            time.sleep(1)
        return {"status": "timeout", "id": jid}


def wait_for(boot: Boot, command: str, pattern: str, timeout: float, interval: float = 3.0) -> tuple[bool, str]:
    """Run a guest command until its output matches a regex."""
    deadline = time.monotonic() + timeout
    out = ""
    while time.monotonic() < deadline:
        out, _ = boot.cmd(command, 60)
        if re.search(pattern, out, re.M):
            return True, out
        time.sleep(interval)
    return False, out


def setup_scenario(boot: Boot, ports: dict[str, int], run_dir: Path, report_dir: Path, flavor: str) -> list[dict]:
    """What a person does on the first run, with the real services: pair, detect, apply (the configurator renders
    printer.cfg / klarity.cfg / moonraker.conf and links the extras), add and use an SSH key, finish, let the
    configurator retire and unlock it again the way Klarity's button does. Returns [{name, ok, detail}, ...]."""
    steps: list[dict] = []

    def step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append({"name": name, "ok": bool(ok), "detail": detail[:600]})
        print(f"smoke.py:   [{'ok' if ok else 'FAIL'}] {name}" + (f": {detail[:200]}" if (detail and not ok) else ""), flush=True)
        return bool(ok)

    w = Wizard(ports["CONFIGURATOR_PORT"])
    out, _ = boot.cmd("sudo cat /data/eitri/configurator/pairing.json", 30)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        pairing = json.loads(m.group(0)) if m else {}
    except ValueError:
        pairing = {}
    if not step("pairing.json offers a code (what Klarity shows)", pairing.get("state") == "pairing" and re.fullmatch(r"\d{4}-\d{4}", str(pairing.get("code"))), out):
        return steps

    st, body = w.call("POST", "/api/pair", {"code": pairing["code"]})
    w.token = body.get("token")
    if not step("pairing with the code from the screen", st == 200 and w.token, f"{st} {body}"):
        return steps
    st, body = w.call("PUT", "/api/state", {"flavor": flavor})
    step(f"choose the {flavor} flavor", st == 200 and body.get("flavor") == flavor, f"{st} {body}")

    st, body = w.call("GET", "/api/access")
    step("remote access starts off (sudo eitri-access status works inside the configurator's sandbox)",
         st == 200 and body.get("available") is True and body.get("keys") == 0 and not body.get("password"), f"{st} {body}")

    det = w.job(w.call("POST", "/api/detect", {}))
    step("detect runs (no MCUs or camera in QEMU: it must finish and say so, not hang)", det.get("status") in ("done", "failed") and det.get("id"), str(det)[:400])
    st, state = w.call("GET", "/api/state")
    step("detection result is stored", st == 200 and state.get("detect") is not None, f"{st}")

    ap = w.job(w.call("POST", "/api/apply", {"confirm": True}), timeout=300)
    written = (ap.get("result") or {}).get("written")
    step("apply writes printer.cfg, klarity.cfg and the access config", ap.get("status") == "done" and written and "printer.cfg" in written, str(ap)[:500])
    ev = [e for e in (w.call("GET", f"/api/jobs/{ap['id']}")[1].get("events") or []) if e.get("type") == "log"] if ap.get("id") else []
    (report_dir / "apply-log.txt").write_text("\n".join(f"{e.get('level')}: {e.get('message')}" for e in ev))
    step("apply linked the extras (overlay-install.sh from /data/eitri/klipper-layer)",
         not any("overlay" in str(e.get("message", "")).lower() and "fail" in str(e.get("message", "")).lower() for e in ev) and ap.get("status") == "done", "")

    ok, out = wait_for(boot, "systemctl is-active klipper moonraker klarity", r"^active\nactive\nactive$", 180)
    step("klipper, moonraker and klarity are active after Apply (they were skipped before)", ok, out)
    time.sleep(40)          # klippy connects, moonraker finds it, klarity opens (or waits for) its port

    # --- root never runs the service user's files, and the eitri account's sudo rule cannot widen eitri-update ---
    out, _ = boot.cmd("sudo -u eitri sudo -n /usr/bin/eitri-update --data-root /tmp/fake status; echo rc=$?", 60)
    step("eitri-update refuses trust-affecting options through the eitri account's sudo rule",
         re.search(r"rc=2\b", out) is not None and "refusing --data-root through sudo" in out, out)
    out, _ = boot.cmd("sudo python3 -I -c 'import subprocess as s; from eitri_mcuflash import privs as p; "
                      "r = lambda a: s.run(a, stdout=s.PIPE, stderr=s.STDOUT, universal_newlines=True); "
                      "print(r(p.as_service_user([\"id\"])).stdout); "
                      "g = r(p.git_argv(\"/data/firmware/klipper\", \"status\", \"--porcelain\")); print(\"git-rc=%d\" % g.returncode)'", 60)
    step("root's helpers run /data commands as eitri (uid, gid, dialout) and git in the clone as its owner",
         re.search(r"uid=1000\(eitri\) gid=1000\(eitri\)", out) is not None and "dialout" in out and "git-rc=0" in out, out)

    # --- optional root access, off by default: add a key, log in with it over SSH, take it away again ---
    key = run_dir / "id_smoke"
    for f in (key, key.with_suffix(".pub")):
        f.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "eitri-smoke", "-f", str(key)], check=True)
    pub = key.with_suffix(".pub").read_text().strip()
    st, body = w.call("POST", "/api/access/key", {"key": pub})
    step("add an SSH key (configurator -> sudo -n eitri-access set-key)", st == 200 and body.get("ok"), f"{st} {body}")
    ok, out = wait_for(boot, "sudo cat /data/eitri/state/access/authorized_keys 2>&1; sudo cat /data/eitri/state/access/applied 2>&1", r"ssh-ed25519", 60)
    step("eitri-access.path/.service applied it (root-owned authorized_keys under /data/eitri/state/access)", ok, out)
    ssh = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
           "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=60", "-i", str(key), "-p", str(ports["SSH_PORT"]), "eitri@127.0.0.1"]
    try:
        r = subprocess.run([*ssh, "id -un; systemctl is-active klipper"], capture_output=True, text=True, timeout=240)
        step("SSH login with that key works (sshd StrictModes accepts the key file)", r.returncode == 0 and r.stdout.split() == ["eitri", "active"], f"rc={r.returncode} {r.stdout!r} {r.stderr[-300:]!r}")
    except subprocess.TimeoutExpired:
        step("SSH login with that key works (sshd StrictModes accepts the key file)", False, "timeout")
    st, body = w.call("POST", "/api/access/disable", {"confirm": True})
    step("take access away again", st == 200 and body.get("ok"), f"{st} {body}")
    ok, out = wait_for(boot, "sudo test -s /data/eitri/state/access/authorized_keys && echo present || echo gone", r"^gone$", 60)
    step("the key is gone from the guest", ok, out)
    try:
        r = subprocess.run([*ssh, "true"], capture_output=True, text=True, timeout=240)
        step("SSH login is refused after 'disable'", r.returncode != 0, f"rc={r.returncode}")
    except subprocess.TimeoutExpired:
        step("SSH login is refused after 'disable'", False, "timeout")

    # --- the administrator (root): a key during first setup only, then SSH as admin with sudo ---
    st, body = w.call("GET", "/api/access")
    step("first setup: the administrator-key window is open (data-init opened it on the first boot)",
         st == 200 and (body.get("admin") or {}).get("window_open") is True, f"{st} {body}")
    akey = run_dir / "id_admin"
    for f in (akey, akey.with_suffix(".pub")):
        f.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "eitri-smoke-admin", "-f", str(akey)], check=True)
    apub = akey.with_suffix(".pub").read_text().strip()
    st, body = w.call("POST", "/api/access/key", {"key": apub, "admin": True})
    step("add an administrator key (configurator -> sudo -n eitri-access admin-set-key)",
         st == 200 and (body.get("added") or {}).get("user") == "admin", f"{st} {body}")
    assh = [*ssh[:-1], "admin@127.0.0.1"]
    assh[assh.index(str(key))] = str(akey)
    try:
        r = subprocess.run([*assh, "id -un; sudo -n true && echo root-ok"], capture_output=True, text=True, timeout=240)
        step("SSH as admin with that key works, and admin is root through sudo", r.returncode == 0 and r.stdout.split() == ["admin", "root-ok"],
             f"rc={r.returncode} {r.stdout!r} {r.stderr[-300:]!r}")
    except subprocess.TimeoutExpired:
        step("SSH as admin with that key works, and admin is root through sudo", False, "timeout")

    # --- the web interface on port 80: only names the printer is reached by, and no cross-site writes ---
    hp = ports["HTTP_PORT"]
    host = {"Host": f"127.0.0.1:{hp}"}
    step("nginx refuses a host name it does not know (DNS rebinding)", http_status(hp, "GET", "/server/info", {"Host": "rebind.example.com"}) == 403)
    step("nginx refuses a cross-site write to Moonraker (Sec-Fetch-Site)",
         http_status(hp, "POST", "/printer/gcode/script?script=M115", {**host, "Sec-Fetch-Site": "cross-site"}) == 403)
    step("nginx refuses a write from a page of another origin (Origin)",
         http_status(hp, "POST", "/printer/gcode/script?script=M115", {**host, "Origin": "http://evil.example.com"}) == 403)
    step("a slicer-style request (no browser headers) still reaches Moonraker", http_status(hp, "GET", "/server/info", host) == 200)
    step("the printer's own page (same origin) still reaches Moonraker",
         http_status(hp, "GET", "/server/info", {**host, "Origin": f"http://127.0.0.1:{hp}", "Sec-Fetch-Site": "same-origin"}) == 200)
    boot.cmd("sudo -u eitri sh -c 'echo printer.home.arpa > /data/printer_data/config/eitri-hosts.txt'", 30)
    deadline = time.monotonic() + 60
    while http_status(hp, "GET", "/server/info", {"Host": "printer.home.arpa"}) != 200 and time.monotonic() < deadline:
        time.sleep(2)
    out, _ = boot.cmd("cat /etc/nginx/eitri-hosts.map; systemctl show -p Result eitri-hosts.service", 30)
    step("a name listed in eitri-hosts.txt is served (eitri-hosts.path -> validated map -> nginx reload)",
         http_status(hp, "GET", "/server/info", {"Host": "printer.home.arpa"}) == 200, out)
    st, body = http_request(hp, "GET", "/configure/api/auth", host)
    step("the setup wizard is not proxied on port 80 any more (its own origin: port 8099 only)", "pairing_open" not in body, f"{st} {body[:120]!r}")

    # --- finish: the configurator locks and exits, Klarity's 'Unlock setup' brings it back ---
    st, body = w.call("POST", "/api/complete", {})
    step("finish setup (the configurator locks)", st == 200, f"{st} {body}")
    out, _ = boot.cmd("sudo /usr/libexec/eitri/eitri-access status", 30)
    step("finishing setup closed the administrator-key window", '"window_open": false' in out, out)
    out, _ = boot.cmd(f"echo '{apub}' | sudo -u eitri sudo -n /usr/libexec/eitri/eitri-access admin-set-key; echo rc=$?", 30)
    step("after setup the service account cannot add an administrator key", re.search(r"rc=3\b", out) is not None, out)
    st, body = w.call("POST", "/api/logout", {})
    step("the finishing browser logs out", st == 200, f"{st} {body}")
    ok, out = wait_for(boot, "systemctl show -p ActiveState,SubState,Result,NRestarts eitri-configurator", r"ActiveState=inactive", 120, 5)
    step("the configurator retires by itself (--retire-when-locked)", ok and "Result=success" in out, out)
    boot.cmd("sleep 1", 10)
    # What Klarity's button runs, in Klarity's own sandbox: a transient unit with the same user, groups and mount/device/cgroup
    # options as klarity.service (taken from the installed unit file), running the exact command from klarity.cfg. Under the
    # image's own sudoers rule (the bench user's passwordless sudo is only used to start the transient unit).
    cmd = ('props=(); while IFS= read -r l; do props+=(-p "$l"); done < <(grep -E "^(User|Group|SupplementaryGroups|NoNewPrivileges|ProtectSystem|ReadWritePaths|'
           'ProtectHome|PrivateTmp|PrivateDevices|DevicePolicy|DeviceAllow|ProtectControlGroups|UMask)=" /usr/lib/systemd/system/klarity.service); '
           'echo "properties: ${props[*]}"; sudo systemd-run -q -P --wait "${props[@]}" -- /usr/bin/sudo -n /usr/bin/eitri-configurator unlock; echo rc=$?')
    out, rc = boot.cmd(cmd, 180)
    step("Klarity's sandbox runs 'sudo -n /usr/bin/eitri-configurator unlock' under the image's own sudoers rule (what the button runs)", re.search(r"rc=0\b", out) is not None and "properties:" in out, out)
    ok, out = wait_for(boot, "sudo cat /data/eitri/configurator/pairing.json; systemctl is-active eitri-configurator", r'"state": ?"pairing"[\s\S]*(?<![a-z])active\s*$', 90)
    step("unlock restarted the configurator and published a new code", ok, out)
    st, _ = w.call("GET", "/api/auth")
    step("the wizard answers on port 8099 again", st == 200, str(st))
    out, rc = boot.cmd("sudo -u eitri /usr/bin/eitri-configurator lock; echo rc=$?", 60)
    step("lock again", "rc=0" in out, out)
    ok, out = wait_for(boot, "systemctl show -p ActiveState,Result eitri-configurator", r"ActiveState=inactive", 120, 5)
    step("and it retires again", ok and "Result=success" in out, out)
    return steps


# --- the run -------------------------------------------------------------------------------

def collect(boot: Boot, mode: str, tag: str, report_dir: Path, args) -> dict:
    t = time.monotonic()
    out, rc = boot.cmd(f"sudo bash /tmp/gc.sh {mode}", args.collect_timeout)
    (report_dir / f"guest-{tag}.txt").write_text(out)
    s = parse_sections(out)
    r: dict = {"tag": tag, "mode": mode, "collect_s": round(time.monotonic() - t, 1)}
    r["sections"] = {k: v["text"] for k, v in s.items() if k in ("analyze_blame", "analyze_critical_chain", "analyze_critical_chain_multi-user", "failed", "free")}
    r["app"] = {k: s[k]["text"] for k in ("app_units", "seed_record", "extras_links", "klippy_log", "moonraker_probes", "app_journals", "klarity_map") if k in s}
    r["system_state"] = s.get("system_state", {}).get("text", "").split("\n")[0]
    r["timing"] = parse_timing(s.get("analyze_time", {}).get("text", ""))
    r["ready"] = parse_ready_times(s.get("ready_times", {}).get("text", ""))
    r["free_mb"] = parse_free(s.get("free", {}).get("text", ""))
    r["meminfo_kb"] = parse_meminfo(s.get("meminfo", {}).get("text", ""))
    r["memory_by_unit"] = parse_units_mem(s.get("memory_by_unit", {}).get("text", ""))
    mh = re.search(r"^HARNESS_RSS_KB (\d+)", s.get("memory_by_unit", {}).get("text", ""), re.M)
    r["harness_rss_kb"] = int(mh.group(1)) if mh else 0
    r["unit_state"] = parse_unit_state(s.get("unit_state", {}).get("text", ""))
    r["failed_units"] = [l.split()[1] if l.startswith("●") else l.split()[0] for l in s.get("failed", {}).get("text", "").split("\n") if l.strip()]
    r["sections_missing"] = [k for k in ("analyze_time", "free", "memory_by_unit", "failed", "unit_state") if k not in s]
    return r


def one_boot(tag: str, args, run_dir: Path, report_dir: Path, fresh: bool, scenario: str, ports: dict[str, int]) -> dict:
    """scenario "setup": first boot of a seeded, unconfigured /data; collect, run the setup, collect again.
    scenario "steady": boot on the configured /data; collect everything."""
    result: dict = {"tag": tag, "fresh_data": fresh, "scenario": scenario}
    boot = Boot(tag, run_dir, fresh, ports, report_dir, args.mem, args.smp, args.seed_flavor)
    try:
        boot.login(args.user, args.password, args.boot_timeout)
        result["host_first_serial_byte_s"] = round(boot.t_first_byte or -1, 2)
        result["host_login_prompt_s"] = round(boot.t_login or -1, 2)
        boot.put_file(HERE / "guest-collect.sh", "/tmp/gc.sh")
        if scenario == "setup":
            result["pre"] = collect(boot, "full", f"{tag}-pre-setup", report_dir, args)
            result["host_http"] = {"configurator_8099": http_probe(ports["CONFIGURATOR_PORT"]), "nginx_80": http_probe(ports["HTTP_PORT"])}
            print(f"smoke.py: {tag}: running the first-run setup against the real services", flush=True)
            result["setup"] = setup_scenario(boot, ports, run_dir, report_dir, args.seed_flavor)
            result["post"] = collect(boot, "running", f"{tag}-after-setup", report_dir, args)
            result["host_http_after"] = {"nginx_server_info": http_probe(ports["HTTP_PORT"], "/server/info"), "nginx_printer_info": http_probe(ports["HTTP_PORT"], "/printer/info")}
        else:
            result["steady"] = collect(boot, "full", f"{tag}-steady", report_dir, args)
            result["host_http"] = {"configurator_8099": http_probe(ports["CONFIGURATOR_PORT"]), "nginx_80": http_probe(ports["HTTP_PORT"]),
                                   "nginx_server_info": http_probe(ports["HTTP_PORT"], "/server/info")}
        # Orderly shutdown, timed on the host.
        t = time.monotonic()
        boot.send("sudo systemctl poweroff\r")
        took = boot.wait_exit(args.shutdown_timeout)
        result["shutdown_s"] = round(time.monotonic() - t, 1) if took is not None else None
        result["clean_poweroff"] = took is not None
    finally:
        boot.kill()
        shutil.copy(run_dir / "serial.log", report_dir / f"serial-{tag}.log") if (run_dir / "serial.log").exists() else None
    return result


def mem_summary(b: dict) -> dict:
    mi = b.get("meminfo_kb", {})
    used = mi.get("MemTotal", 0) - mi.get("MemAvailable", 0)
    return {"used_mib": used // 1024, "idle_used_mib": (used - b.get("harness_rss_kb", 0)) // 1024, "slab_mib": mi.get("Slab", 0) // 1024,
            "rss_total_kb": next((r["rss_kb"] for r in b.get("memory_by_unit", []) if r["unit"] == "TOTAL"), 0),
            "pss_total_kb": next((r["pss_kb"] for r in b.get("memory_by_unit", []) if r["unit"] == "TOTAL"), 0)}


def checks(first: dict, second: dict | None, known: dict[str, tuple[str, str]]) -> list[tuple[bool, str]]:
    c: list[tuple[bool, str]] = []
    pre, post = first.get("pre", {}), first.get("post", {})
    c.append((first.get("host_login_prompt_s", -1) > 0, "login prompt reached on the serial console"))
    c.append((pre.get("system_state") in ("running", "degraded"), f"systemd finished booting (state: {pre.get('system_state')!r})"))
    c.append((not pre.get("sections_missing"), f"all measurement sections collected (missing: {pre.get('sections_missing')})"))
    us = pre.get("unit_state", {})
    di = us.get("eitri-data-init", {})
    c.append((di.get("ActiveState") == "active" and di.get("Result") == "success", f"eitri-data-init ran ({di.get('ActiveState')}/{di.get('Result')})"))
    ap = pre.get("app", {})
    c.append((re.search(r"^SEED_FLAVOR=(klipper|kalico)$", ap.get("seed_record", ""), re.M) is not None, "the data seed is on /data from the first boot (eitri/state/seed-installed.env)"))
    c.append(("git status rc=0" in ap.get("seed_record", "") and not re.search(r"^ ?[MA?D]", ap.get("seed_record", ""), re.M), "the Klipper clone from the seed has a clean git status"))
    for name in ("klipper", "moonraker", "klarity"):
        st = us.get(name, {})
        c.append((st.get("ActiveState") == "inactive" and st.get("ConditionResult") == "no",
                  f"before setup, {name} waits for its configuration without an error (state {st.get('ActiveState')}/{st.get('SubState')}, condition {st.get('ConditionResult')})"))
    http = first.get("host_http", {})
    c.append((http.get("configurator_8099", "").startswith("2"), f"configurator answers on the forwarded port ({http.get('configurator_8099')})"))
    c.append((http.get("nginx_80", "").startswith("2"), f"nginx answers on the forwarded port ({http.get('nginx_80')})"))
    for s in first.get("setup", []):
        c.append((s["ok"], f"setup: {s['name']}" + ("" if s["ok"] else f" ({s['detail'][:160]})")))
    if not first.get("setup"):
        c.append((False, "the first-run setup did not run"))

    def running_checks(label: str, b: dict) -> None:
        us2 = b.get("unit_state", {})
        for name in ("klipper", "moonraker", "klarity"):
            st = us2.get(name, {})
            c.append((st.get("ActiveState") == "active" and st.get("NRestarts") in ("0", None, ""),
                      f"{label}: {name} is active and has not been restarted by systemd ({st.get('ActiveState')}/{st.get('SubState')}, NRestarts={st.get('NRestarts')})"))
        app = b.get("app", {})
        probes = app.get("moonraker_probes", "")
        m = re.search(r"== http://127.0.0.1:7125/server/info\n(.*?)\nHTTP (\d+)", probes, re.S)
        c.append((bool(m) and m.group(2) == "200" and '"klippy_state"' in m.group(1), f"{label}: Moonraker answers on 127.0.0.1:7125 /server/info and sees Klipper's socket"))
        m = re.search(r"== http://127.0.0.1/server/info\n(.*?)\nHTTP (\d+)", probes, re.S)
        c.append((bool(m) and m.group(2) == "200", f"{label}: Moonraker answers through nginx on port 80 (/server/info)"))
        c.append((re.search(r"7125 on [\d.]+: HTTP 000", probes) is not None or "ip=\n" in probes or "ip=" not in probes,
                  f"{label}: Moonraker is not reachable on the guest's network address (loopback only)"))
        kl = app.get("klippy_log", "")
        c.append(("--- errors:\n--- tail:" in kl, f"{label}: klippy loaded printer.cfg and every extra it names (no config error, import error or unknown module in klippy.log)"))
        pi = re.search(r"== http://127.0.0.1:7125/printer/info\n(.*?)\nHTTP (\d+)", probes, re.S)
        c.append((bool(pi) and pi.group(2) == "200" and ("Unable to connect" in pi.group(1) or "attempting to connect" in pi.group(1)) and "Config error" not in pi.group(1),
                  f"{label}: the only thing wrong is the missing MCU (Moonraker's /printer/info says: {(pi.group(1)[:140] if pi else '?')!r})"))
        km = app.get("klarity_map", "")
        c.append(("map OK" in km and "unlock.py" in km and re.search(r"^[1-9]\d*$", km.strip().split("\n")[-1]) is not None,
                  f"{label}: the seed's Klarity has the Unlock-setup feature and a valid screen map ({km.strip().splitlines()[0][:80] if km.strip() else '?'})"))
        c.append((re.search(r"^[1-9]\d*\n", kl.split("--- errors:")[0].split("printer.cfg:")[-1]) is not None or "--- qd_ sections in printer.cfg:\n0" not in kl,
                  f"{label}: printer.cfg uses qd_* extras"))
        nnp = dict(re.findall(r"^(\S+) NoNewPrivs=(\d)$", app.get("app_units", ""), re.M))
        for name in ("eitri-configurator", "klarity", "klipper", "moonraker"):
            if name in nnp:
                want = "0" if name in ("eitri-configurator", "klarity") else "1"
                c.append((nnp[name] == want, f"{label}: {name} runs with NoNewPrivs={nnp[name]} (sudo needs 0 in the two units that call it, klipper and moonraker keep 1)"))
        ex = app.get("extras_links", "")
        links = [l for l in ex.split("\n") if "klippy/extras/qd_" in l]
        # Kalico ships its own (tracked) gcode_shell_command.py, which the overlay must not shadow; qd_shell_guard wraps it.
        kalico = re.search(r"^SEED_FLAVOR=kalico$", app.get("seed_record", ""), re.M) is not None
        not_links = [l for l in ex.split("\n") if "NOT A LINK" in l and not (kalico and "gcode_shell_command" in l)]
        c.append((bool(links) and all("-> /data/eitri/extras/" in l for l in links) and not not_links,
                  f"{label}: {len(links)} qd_* extras in the Klipper clone are links to /data/eitri/extras" + ("" if not not_links else f" (not a link: {not_links})")))
        c.append((re.search(r"Unable to open|mcu|Timeout|Lost communication|Unable to connect|serial", kl, re.I) is not None,
                  f"{label}: klippy reports the missing MCU (expected in QEMU)"))

    if post:
        running_checks("after setup", post)
        h = first.get("host_http_after", {})
        c.append((h.get("nginx_server_info", "").startswith("2"), f"after setup: /server/info through the forwarded port 80 ({h.get('nginx_server_info')})"))
    c.append((first.get("clean_poweroff", False), "guest powered off cleanly"))
    for b in (first.get("pre"), first.get("post"), (second or {}).get("steady")):
        if not b or b.get("mode") == "running":
            continue
        unexpected = [u for u in b.get("failed_units", []) if u not in known]
        c.append((not unexpected, f"{b['tag']}: no failed units except the known ones (unexpected: {unexpected}; known and failed: "
                                  f"{[u for u in b.get('failed_units', []) if u in known]})"))
    if post:
        unexpected = [u for u in post.get("failed_units", []) if u not in known]
        c.append((not unexpected, f"{post['tag']}: no failed units except the known ones (unexpected: {unexpected})"))
    for unit in sorted(known):
        if unit.split(".")[0] in us and unit not in pre.get("failed_units", []):
            st = us[unit.split(".")[0]]
            c.append((True, f"WARNING known-failures.txt lists {unit}, but it is {st.get('ActiveState')}/{st.get('SubState')} (fixed? remove the line)"))
    cam = us.get("eitri-camera", {})
    c.append((cam.get("ActiveState") != "active", f"eitri-camera is not running, as there is no camera (state {cam.get('ActiveState')}/{cam.get('SubState')})"))
    if second:
        sd = second.get("steady", {})
        c.append((sd.get("system_state") in ("running", "degraded"), f"second boot finished (state: {sd.get('system_state')!r})"))
        running_checks("second boot", sd)
        us2 = sd.get("unit_state", {})
        cf = us2.get("eitri-configurator", {})
        c.append((cf.get("ActiveState") == "inactive" and cf.get("Result") == "success", f"second boot: the configurator is retired after setup (state {cf.get('ActiveState')}/{cf.get('SubState')}, result {cf.get('Result')})"))
        h = second.get("host_http", {})
        c.append((not h.get("configurator_8099", "").startswith("2"), f"second boot: nothing answers on the configurator port ({h.get('configurator_8099')})"))
        c.append((h.get("nginx_80", "").startswith("2") and h.get("nginx_server_info", "").startswith("2"), f"second boot: nginx and /server/info answer through the forwarded port ({h.get('nginx_80')}, {h.get('nginx_server_info')})"))
        c.append((second.get("clean_poweroff", False), "second boot: guest powered off cleanly"))
    return c


def report(first: dict, second: dict | None, results: list[tuple[bool, str]]) -> str:
    L: list[str] = []
    L.append("# Eitri OS QEMU smoke test\n")
    L.append("QEMU virt, 4 x Cortex-A53 (TCG, software emulation), see os/qemu/README.md for what this does and does not say about the printer.\n")
    L.append("## Checks\n")
    for ok, msg in results:
        L.append(f"- {'WARN' if msg.startswith('WARNING') else 'PASS' if ok else 'FAIL'}  {msg}")
    blocks = [("pre", first.get("pre"), "first boot, seeded /data, before the setup (Klipper, Moonraker and Klarity wait for their configuration)"),
              ("post", first.get("post"), "first boot, after the setup (the configurator has applied; services running; the configurator still up)"),
              ("steady", (second or {}).get("steady"), "second boot, configured /data (the configurator retired; Klipper, Moonraker, Klarity running)")]
    for _, b, title in blocks:
        if not b:
            continue
        L.append(f"\n## {title}\n")
        t = b.get("timing", {})
        if t:
            L.append(f"- {t.get('startup_line', '?')}")
            L.append(f"- {t.get('target', 'target')} reached {t.get('target_reached_userspace_s', '?')} s into userspace")
        rd = b.get("ready", {})
        if rd:
            L.append(f"- last service other than the camera became active at {rd.get('last_service_active_s')} s after kernel start ({rd.get('last_service')}); {rd.get('services_active')} services active")
        L.append(f"- free -m: {b.get('free_mb', {})}")
        mi = b.get("meminfo_kb", {})
        if mi:
            ms = mem_summary(b)
            hr = b.get("harness_rss_kb", 0)
            L.append(f"- MemTotal {mi.get('MemTotal', 0) // 1024} MiB, MemAvailable {mi.get('MemAvailable', 0) // 1024} MiB, used (total - available) {ms['used_mib']} MiB, "
                     f"of which the measuring tool itself about {hr // 1024} MiB (idle: about {ms['idle_used_mib']} MiB), Slab {ms['slab_mib']} MiB; "
                     f"processes: RSS {ms['rss_total_kb']} kB, PSS {ms['pss_total_kb']} kB")
        L.append(f"- system state `{b.get('system_state')}`, failed units: {b.get('failed_units')}")
        L.append("\nPer-unit memory (kB), largest PSS first:\n")
        L.append("| unit | RSS | PSS | procs | commands |\n|---|---:|---:|---:|---|")
        for r in b.get("memory_by_unit", []):
            L.append(f"| {r['unit']} | {r['rss_kb']} | {r['pss_kb']} | {r.get('procs', '')} | {r.get('commands', '')} |")
        L.append("\nEitri units:\n")
        L.append("| unit | active | sub | result | condition | restarts | status |\n|---|---|---|---|---|---:|---:|")
        for name, st in b.get("unit_state", {}).items():
            L.append(f"| {name} | {st.get('ActiveState')} | {st.get('SubState')} | {st.get('Result')} | {st.get('ConditionResult')} | {st.get('NRestarts')} | {st.get('ExecMainStatus')} |")
        if b.get("app", {}).get("klippy_log"):
            L.append(f"\n```\n{b['app']['klippy_log'][:2500]}\n```")
        for key in ("analyze_blame", "analyze_critical_chain_multi-user"):
            if key in b.get("sections", {}):
                L.append(f"\n```\n{b['sections'][key]}\n```")
    for b in (first, second):
        if b:
            L.append(f"\nBoot `{b['tag']}`: host clock first serial byte {b.get('host_first_serial_byte_s')} s, login prompt {b.get('host_login_prompt_s')} s after QEMU start; shutdown {b.get('shutdown_s')} s")
    L.append("\n## First-run setup (driven over the configurator's HTTP API with the real services)\n")
    for s in first.get("setup", []):
        L.append(f"- {'ok  ' if s['ok'] else 'FAIL'} {s['name']}")
    L.append("\nKnown failures (os/qemu/known-failures.txt): " + "; ".join(f"{u} [{k}] {r}" for u, (k, r) in load_known_failures().items()))
    L.append("\nExpected differences from a printer: " + "; ".join(f"{k}: {v}" for k, v in EXPECTED_SKIPPED.items()))
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT / "smoke", help="report directory (default: %(default)s)")
    ap.add_argument("--no-second-boot", action="store_true", help="skip the second boot on the existing /data")
    ap.add_argument("--expect-failed", action="append", default=[], metavar="UNIT", help="another unit that may be failed without failing the test (known-failures.txt lists the standing ones)")
    ap.add_argument("--user", default="bench", help="the QEMU-only login with passwordless sudo (post-fakeroot.sh)")
    ap.add_argument("--seed-flavor", default="klipper", choices=("klipper", "kalico"), help="which data seed populates /data (needs `make seed`)")
    ap.add_argument("--password", default=os.environ.get("EITRI_QEMU_PASSWORD", "eitri"))
    ap.add_argument("--mem", default="1024", help="guest RAM in MiB")
    ap.add_argument("--smp", default="4")
    ap.add_argument("--boot-timeout", type=float, default=900, help="seconds to the login prompt")
    ap.add_argument("--collect-timeout", type=float, default=900)
    ap.add_argument("--shutdown-timeout", type=float, default=180)
    args = ap.parse_args()

    for f in ("Image", "rootfs.ext4"):
        if not (OUT / "images" / f).exists():
            print(f"smoke.py: {OUT / 'images' / f} is missing; run os/qemu/build.sh", file=sys.stderr)
            return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_dir = args.out / stamp
    report_dir.mkdir(parents=True)
    run_dir = report_dir / "run"
    run_dir.mkdir()
    p = free_ports(3)
    ports = {"SSH_PORT": p[0], "HTTP_PORT": p[1], "CONFIGURATOR_PORT": p[2]}

    print(f"smoke.py: report in {report_dir}", flush=True)
    first = one_boot("boot1", args, run_dir, report_dir, True, "setup", ports)
    second = None
    if not args.no_second_boot and first.get("clean_poweroff"):
        second = one_boot("boot2", args, run_dir, report_dir, False, "steady", ports)
    known = load_known_failures()
    known.update({u: ("cli", "--expect-failed") for u in args.expect_failed})
    results = checks(first, second, known)
    (report_dir / "summary.json").write_text(json.dumps({"first": first, "second": second, "checks": [{"ok": ok, "check": m} for ok, m in results]}, indent=1))
    text = report(first, second, results)
    (report_dir / "summary.md").write_text(text)
    print(text)
    bad = [m for ok, m in results if not ok]
    print(f"smoke.py: {len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
