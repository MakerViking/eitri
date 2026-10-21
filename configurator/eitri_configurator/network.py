"""Network facts and Wi-Fi onboarding.

Wired is the lifeline (the board's Ethernet is in-kernel); Wi-Fi uses NetworkManager
when it is present on the image and is otherwise reported as unsupported rather
than half-working.  All subprocesses use argv lists - no shell, so SSIDs and
passwords cannot inject anything.
"""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from .options import validate_hostname
from .settings import Settings
from .state import atomic_write

SYS_NET = Path("/sys/class/net")


async def _run(*argv: str, timeout: float = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


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
                _, text = await _run("ip", "-4", "-o", "addr", "show")
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
        rc, text = await _run("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes", timeout=45)
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
        if not ssid or len(ssid) > 32 or "\x00" in ssid or (password and not 8 <= len(password) <= 63):
            return {"ok": False, "error": "SSID or password has an invalid length (WPA passwords are 8-63 characters)."}
        if self.s.mock:
            await asyncio.sleep(0.8 if self.s.mock_speed else 0)
            if password == "wrongpass1":
                return {"ok": False, "error": "Authentication failed - check the password."}
            self.mock_wifi = ssid
            return {"ok": True, "ssid": ssid}
        argv = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            argv += ["password", password]
        rc, out = await _run(*argv, timeout=60)
        return {"ok": rc == 0, "ssid": ssid, **({} if rc == 0 else {"error": out.strip().splitlines()[-1][:200] if out.strip() else "connection failed"})}

    async def set_hostname(self, name: str) -> dict[str, Any]:
        name = validate_hostname(name)
        # Persist on /data (the rootfs is read-only); the OS applies it at boot.
        atomic_write(self.s.eitri_dir / "hostname", name + "\n")
        if self.s.mock:
            self.mock_hostname = name
            return {"ok": True, "hostname": name, "applied": "mock"}
        applied = "on-next-boot"
        if shutil.which("hostnamectl"):
            rc, _ = await _run("hostnamectl", "set-hostname", name)
            applied = "now" if rc == 0 else applied
        return {"ok": True, "hostname": name, "applied": applied}
