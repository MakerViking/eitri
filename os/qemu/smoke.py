#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Boot smoke test and performance measurement of the Eitri OS userspace on QEMU.

    python3 -I os/qemu/smoke.py [--out DIR] [--no-second-boot] [--expect-failed UNIT]...

Starts run-qemu.sh on a blank /data disk with the serial port on a unix socket, waits for the
login prompt, logs in as the bench user, sends guest-collect.sh over the console, and collects
systemd-analyze, memory, per-service RSS/PSS, the state of the Eitri units and the layout that the
first boot created on /data. Then it powers the guest off cleanly (timing the shutdown), boots it
again on the same /data (second boot: nothing to create) and collects the timing again.

Writes into DIR/<timestamp>/ : summary.md, summary.json, guest-boot1.txt, guest-boot2.txt,
serial-boot1.log, serial-boot2.log. Exit status 0 when every check passes.

QEMU runs the guest in software (TCG): absolute times are not the printer's.
"""
from __future__ import annotations

import argparse
import base64
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

# Units that the image enables but that cannot start in QEMU or on a printer without the data seed.
# They are reported, not failures: the unit file's ConditionPathExists skips them.
EXPECTED_SKIPPED = {
    "klipper": "needs /data/firmware/klipper, /data/venv/klippy and printer.cfg (data seed, configurator)",
    "moonraker": "needs /data/firmware/moonraker, /data/venv/moonraker and moonraker.conf (data seed, configurator)",
    "klarity": "needs /data/eitri/klarity/venv (data seed) and klarity.cfg",
    "eitri-camera": "needs the UVC camera (/dev/eitri-cam); there is none in QEMU",
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

    def __init__(self, tag: str, run_dir: Path, fresh: bool, ports: dict[str, int], log_dir: Path, mem: str, smp: str):
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
        env = dict(os.environ, RUN_DIR=str(run_dir), MEM=mem, SMP=smp, **{k: str(v) for k, v in ports.items()})
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
        self.send(f"{command}; echo {marker} rc=$?\r")
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


def free_ports(n: int) -> list[int]:
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


# --- the run -------------------------------------------------------------------------------

def one_boot(tag: str, args, run_dir: Path, report_dir: Path, fresh: bool, mode: str, ports: dict[str, int]) -> dict:
    result: dict = {"tag": tag, "fresh_data": fresh}
    boot = Boot(tag, run_dir, fresh, ports, report_dir, args.mem, args.smp)
    try:
        boot.login(args.user, args.password, args.boot_timeout)
        result["host_first_serial_byte_s"] = round(boot.t_first_byte or -1, 2)
        result["host_login_prompt_s"] = round(boot.t_login or -1, 2)
        boot.put_file(HERE / "guest-collect.sh", "/tmp/gc.sh")
        t = time.monotonic()
        out, rc = boot.cmd(f"sudo bash /tmp/gc.sh {mode}", args.collect_timeout)
        result["collect_s"] = round(time.monotonic() - t, 1)
        (report_dir / f"guest-{tag}.txt").write_text(out)
        sections = parse_sections(out)
        result["sections"] = {k: v["text"] for k, v in sections.items() if k in ("analyze_blame", "analyze_critical_chain", "analyze_critical_chain_multi-user", "failed", "free")}
        s = sections
        result["system_state"] = s.get("system_state", {}).get("text", "").split("\n")[0]
        result["timing"] = parse_timing(s.get("analyze_time", {}).get("text", ""))
        result["ready"] = parse_ready_times(s.get("ready_times", {}).get("text", ""))
        result["free_mb"] = parse_free(s.get("free", {}).get("text", ""))
        result["meminfo_kb"] = parse_meminfo(s.get("meminfo", {}).get("text", ""))
        result["memory_by_unit"] = parse_units_mem(s.get("memory_by_unit", {}).get("text", ""))
        mh = re.search(r"^HARNESS_RSS_KB (\d+)", s.get("memory_by_unit", {}).get("text", ""), re.M)
        result["harness_rss_kb"] = int(mh.group(1)) if mh else 0
        result["unit_state"] = parse_unit_state(s.get("unit_state", {}).get("text", ""))
        result["failed_units"] = [l.split()[1] if l.startswith("●") else l.split()[0] for l in s.get("failed", {}).get("text", "").split("\n") if l.strip()]
        result["sections_missing"] = [k for k in ("analyze_time", "free", "memory_by_unit", "failed", "unit_state") if k not in s]
        if mode == "full":
            result["host_http"] = {
                "configurator_8099": http_probe(ports["CONFIGURATOR_PORT"]),
                "nginx_80": http_probe(ports["HTTP_PORT"]),
                "moonraker_7125": http_probe(ports["MOONRAKER_PORT"], "/server/info"),
            }
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


def checks(first: dict, second: dict | None, known: dict[str, tuple[str, str]]) -> list[tuple[bool, str]]:
    c: list[tuple[bool, str]] = []
    c.append((first.get("host_login_prompt_s", -1) > 0, "login prompt reached on the serial console"))
    c.append((first.get("system_state") in ("running", "degraded"), f"systemd finished booting (state: {first.get('system_state')!r})"))
    c.append((not first.get("sections_missing"), f"all measurement sections collected (missing: {first.get('sections_missing')})"))
    us = first.get("unit_state", {})
    di = us.get("eitri-data-init", {})
    c.append((di.get("ActiveState") == "active" and di.get("Result") == "success", f"eitri-data-init ran ({di.get('ActiveState')}/{di.get('Result')})"))
    for b in (first, second):
        if not b:
            continue
        unexpected = [u for u in b.get("failed_units", []) if u not in known]
        c.append((not unexpected, f"{b['tag']}: no failed units except the known ones (unexpected: {unexpected}; known and failed: "
                                  f"{[u for u in b.get('failed_units', []) if u in known]})"))
    for unit in sorted(known):
        if unit.split(".")[0] in us and unit not in first.get("failed_units", []):
            st = us[unit.split(".")[0]]
            c.append((True, f"WARNING known-failures.txt lists {unit}, but it is {st.get('ActiveState')}/{st.get('SubState')} (fixed? remove the line)"))
    for name in ("klipper", "moonraker", "klarity"):
        st = us.get(name, {})
        skipped = st.get("ActiveState") == "inactive" and st.get("ConditionResult") == "no"
        c.append((skipped or st.get("ActiveState") == "active",
                  f"{name}: skipped by its ConditionPathExists (no data seed) or running (state {st.get('ActiveState')}/{st.get('SubState')}, condition {st.get('ConditionResult')})"))
    cam = us.get("eitri-camera", {})
    c.append((cam.get("ActiveState") != "active", f"eitri-camera is not running, as there is no camera (state {cam.get('ActiveState')}/{cam.get('SubState')})"))
    cf = us.get("eitri-configurator", {})
    c.append((cf.get("ActiveState") == "active", f"eitri-configurator is running ({cf.get('ActiveState')}/{cf.get('SubState')})"))
    http = first.get("host_http", {})
    c.append((http.get("configurator_8099", "").startswith("2"), f"configurator answers on the forwarded port ({http.get('configurator_8099')})"))
    c.append((http.get("nginx_80", "").startswith("2"), f"nginx answers on the forwarded port ({http.get('nginx_80')})"))
    c.append((first.get("clean_poweroff", False), "guest powered off cleanly"))
    if second:
        c.append((second.get("system_state") in ("running", "degraded"), f"second boot finished (state: {second.get('system_state')!r})"))
    return c


def report(first: dict, second: dict | None, results: list[tuple[bool, str]]) -> str:
    L: list[str] = []
    L.append("# Eitri OS QEMU smoke test\n")
    L.append("QEMU virt, 4 x Cortex-A53 (TCG, software emulation), see os/qemu/README.md for what this does and does not say about the printer.\n")
    L.append("## Checks\n")
    for ok, msg in results:
        L.append(f"- {'WARN' if msg.startswith('WARNING') else 'PASS' if ok else 'FAIL'}  {msg}")
    for b in (first, second):
        if not b:
            continue
        L.append(f"\n## Boot `{b['tag']}` ({'blank /data' if b['fresh_data'] else 'existing /data'})\n")
        t = b.get("timing", {})
        L.append(f"- {t.get('startup_line', '?')}")
        rd = b.get("ready", {})
        if rd:
            L.append(f"- last service other than the camera became active at {rd.get('last_service_active_s')} s after kernel start ({rd.get('last_service')}); "
                     f"{rd.get('services_active')} services active")
        L.append(f"- {t.get('target', 'target')} reached {t.get('target_reached_userspace_s', '?')} s into userspace")
        L.append(f"- host clock: first serial byte {b.get('host_first_serial_byte_s')} s, login prompt {b.get('host_login_prompt_s')} s after QEMU start; shutdown {b.get('shutdown_s')} s")
        f = b.get("free_mb", {})
        L.append(f"- free -m: {f}")
        mi = b.get("meminfo_kb", {})
        if mi:
            used = mi.get("MemTotal", 0) - mi.get("MemAvailable", 0)
            hr = b.get("harness_rss_kb", 0)
            L.append(f"- MemTotal {mi.get('MemTotal', 0) // 1024} MiB, MemAvailable {mi.get('MemAvailable', 0) // 1024} MiB, used (total - available) {used // 1024} MiB, "
                     f"of which the measuring tool itself about {hr // 1024} MiB (idle: about {(used - hr) // 1024} MiB), Slab {mi.get('Slab', 0) // 1024} MiB")
        L.append(f"- system state `{b.get('system_state')}`, failed units: {b.get('failed_units')}")
        L.append("\nPer-unit memory (kB), largest PSS first:\n")
        L.append("| unit | RSS | PSS | procs | commands |\n|---|---:|---:|---:|---|")
        for r in b.get("memory_by_unit", []):
            L.append(f"| {r['unit']} | {r['rss_kb']} | {r['pss_kb']} | {r.get('procs', '')} | {r.get('commands', '')} |")
        L.append("\nEitri units:\n")
        L.append("| unit | active | sub | result | condition | status |\n|---|---|---|---|---|---:|")
        for name, st in b.get("unit_state", {}).items():
            L.append(f"| {name} | {st.get('ActiveState')} | {st.get('SubState')} | {st.get('Result')} | {st.get('ConditionResult')} | {st.get('ExecMainStatus')} |")
        for key in ("analyze_blame", "analyze_critical_chain_multi-user"):
            if key in b.get("sections", {}):
                L.append(f"\n```\n{b['sections'][key]}\n```")
        if b.get("host_http"):
            L.append(f"\nHost-side probes through the forwarded ports: {b['host_http']}")
    L.append("\nKnown failures (os/qemu/known-failures.txt): " + "; ".join(f"{u} [{k}] {r}" for u, (k, r) in load_known_failures().items()))
    L.append("\nExpected skips (no data seed, no camera): " + "; ".join(f"{k}: {v}" for k, v in EXPECTED_SKIPPED.items()))
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT / "smoke", help="report directory (default: %(default)s)")
    ap.add_argument("--no-second-boot", action="store_true", help="skip the second boot on the existing /data")
    ap.add_argument("--expect-failed", action="append", default=[], metavar="UNIT", help="another unit that may be failed without failing the test (known-failures.txt lists the standing ones)")
    ap.add_argument("--user", default="eitri")
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
    p = free_ports(4)
    ports = {"SSH_PORT": p[0], "HTTP_PORT": p[1], "CONFIGURATOR_PORT": p[2], "MOONRAKER_PORT": p[3]}

    print(f"smoke.py: report in {report_dir}", flush=True)
    first = one_boot("boot1", args, run_dir, report_dir, True, "full", ports)
    second = None
    if not args.no_second_boot and first.get("clean_poweroff"):
        second = one_boot("boot2", args, run_dir, report_dir, False, "brief", ports)
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
