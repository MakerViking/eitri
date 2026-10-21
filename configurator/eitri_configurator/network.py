"""Network facts and Wi-Fi onboarding.

Wired is the lifeline (the board's Ethernet is in-kernel); Wi-Fi uses NetworkManager
when it is present on the image and is otherwise reported as unsupported rather
than half-working.  All subprocesses use argv lists - no shell, so SSIDs and
passwords cannot inject anything.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .options import validate_hostname
from .proc import run
from .settings import Settings
from .state import atomic_write

SYS_NET = Path("/sys/class/net")
WIFI_PROFILE = "eitri-wifi"


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def _last_line(out: str) -> str:
    lines = out.strip().splitlines()
    return lines[-1][:200] if lines else ""


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _parse_ip_addr(text: str) -> dict[str, list[str]]:
    """Parse `ip -4 -o addr show` (works with busybox ip, which lacks -j)."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
        if m:
            out.setdefault(m.group(1), []).append(m.group(2).split("/")[0])
    return out


MOCK_WIFI = [
    {"ssid": "Forge-5G", "signal": 88, "security": "WPA2", "in_use": False},
    {"ssid": "Workshop", "signal": 71, "security": "WPA2", "in_use": False},
    {"ssid": "Anvil Guest", "signal": 54, "security": "WPA2", "in_use": False},
    {"ssid": "OpenBench", "signal": 33, "security": "", "in_use": False},
]


class Network:
    def __init__(self, settings: Settings):
        self.s = settings
        self.mock_hostname = "eitri"
        self.mock_wifi: str | None = None

    async def summary(self) -> dict[str, Any]:
        if self.s.mock:
            return {
                "hostname": self.mock_hostname,
                "interfaces": [
                    {"name": "eth0", "kind": "ethernet", "up": True, "speed": 100,
                     "mac": "02:42:ac:11:00:02", "addresses": ["192.168.0.211"]},
                    {"name": "wlan0", "kind": "wifi", "up": bool(self.mock_wifi), "speed": None,
                     "mac": "02:42:ac:11:00:03", "addresses": ["192.168.0.212"] if self.mock_wifi else [],
                     "ssid": self.mock_wifi},
                ],
                "wifi_supported": True,
                "online": True,
            }
        addrs: dict[str, list[str]] = {}
        if shutil.which("ip"):
            try:
                _, text = await run("ip", "-4", "-o", "addr", "show")
                addrs = _parse_ip_addr(text)
            except (asyncio.TimeoutError, OSError):
                pass
        ifaces = []
        for d in sorted(SYS_NET.glob("*")):
            if d.name == "lo":
                continue
            kind = "wifi" if (d / "wireless").exists() else "ethernet" if d.name.startswith(("eth", "en")) else "other"
            if kind == "other":
                continue
            speed = _read(d / "speed")
            ifaces.append({
                "name": d.name, "kind": kind,
                "up": _read(d / "operstate") == "up" or bool(addrs.get(d.name)),
                "speed": int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None,
                "mac": _read(d / "address"), "addresses": addrs.get(d.name, []),
            })
        return {
            "hostname": _read(Path("/etc/hostname")) or "eitri",
            "interfaces": ifaces,
            "wifi_supported": bool(shutil.which("nmcli")) and any(i["kind"] == "wifi" for i in ifaces),
            "online": any(i["addresses"] for i in ifaces),
        }

    async def scan(self) -> dict[str, Any]:
        if self.s.mock:
            await asyncio.sleep(0.4 if self.s.mock_speed else 0)
            nets = [dict(n, in_use=n["ssid"] == self.mock_wifi) for n in MOCK_WIFI]
            return {"supported": True, "networks": nets}
        if not shutil.which("nmcli"):
            return {"supported": False, "networks": [], "reason": "This image has no Wi-Fi manager; use the Ethernet port."}
        rc, text = await run("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes", timeout=45)
        if rc != 0:
            return {"supported": False, "networks": [], "reason": text.strip()[:200]}
        seen: dict[str, dict] = {}
        for line in text.splitlines():
            parts = re.split(r"(?<!\\):", line)
            if len(parts) < 4 or not parts[1]:
                continue
            ssid = parts[1].replace("\\:", ":")
            net = {"ssid": ssid, "signal": int(parts[2] or 0), "security": parts[3], "in_use": parts[0] == "*"}
            if ssid not in seen or net["signal"] > seen[ssid]["signal"]:
                seen[ssid] = net
        return {"supported": True, "networks": sorted(seen.values(), key=lambda n: -n["signal"])}

    async def connect(self, ssid: str, password: str | None) -> dict[str, Any]:
        if not ssid or len(ssid.encode()) > 32 or _has_control(ssid) or (password and (not 8 <= len(password) <= 63 or _has_control(password))):
            return {"ok": False, "error": "SSID or password is not valid (WPA passwords are 8-63 printable characters)."}
        if self.s.mock:
            await asyncio.sleep(0.8 if self.s.mock_speed else 0)
            if password == "wrongpass1":
                return {"ok": False, "error": "Authentication failed - check the password."}
            self.mock_wifi = ssid
            return {"ok": True, "ssid": ssid}
        # One saved profile, rebuilt on every attempt.  Keyword arguments (`ssid <name>`) cannot be taken for
        # options even when an SSID starts with "-", and the passphrase never appears on a command line, where
        # every local user could read it from /proc/<pid>/cmdline: it travels in a 0600 file that nmcli reads.
        await run("nmcli", "connection", "delete", "id", WIFI_PROFILE, timeout=20)       # absent profile: ignore the error
        add = ["nmcli", "connection", "add", "type", "wifi", "con-name", WIFI_PROFILE, "ssid", ssid]
        if password:
            add += ["wifi-sec.key-mgmt", "wpa-psk"]
        rc, out = await run(*add, timeout=30)
        if rc != 0:
            return {"ok": False, "ssid": ssid, "error": _last_line(out) or "could not create the connection"}
        up = ["nmcli", "connection", "up", "id", WIFI_PROFILE]
        secret_path: str | None = None
        try:
            if password:
                fd, secret_path = tempfile.mkstemp(prefix="eitri-wifi-")        # mode 0600, in the unit's private /tmp
                with os.fdopen(fd, "w") as fh:
                    fh.write(f"802-11-wireless-security.psk:{password}\n")
                up += ["passwd-file", secret_path]
            rc, out = await run(*up, timeout=60)
        finally:
            if secret_path:
                Path(secret_path).unlink(missing_ok=True)
        if rc != 0:
            await run("nmcli", "connection", "delete", "id", WIFI_PROFILE, timeout=20)   # do not keep a profile that cannot connect
            return {"ok": False, "ssid": ssid, "error": _last_line(out) or "connection failed"}
        return {"ok": True, "ssid": ssid}

    async def set_hostname(self, name: str) -> dict[str, Any]:
        name = validate_hostname(name)
        # Persist on /data (the rootfs is read-only); the OS applies it at boot.
        atomic_write(self.s.hostname_file, name + "\n")
        if self.s.mock:
            self.mock_hostname = name
            return {"ok": True, "hostname": name, "applied": "mock"}
        applied = "on-next-boot"
        if shutil.which("hostnamectl"):
            rc, _ = await run("hostnamectl", "set-hostname", name)
            applied = "now" if rc == 0 else applied
        return {"ok": True, "hostname": name, "applied": applied}
