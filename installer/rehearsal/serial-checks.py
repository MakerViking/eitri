#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Drive a root shell on a QEMU serial socket (systemd.debug_shell=ttyAMA0 on the booted Eitri system): wait for the boot
to finish, run the checks, print everything, power off.   serial-checks.py SOCKET LOGFILE"""
import socket
import sys
import time

sock_path, logfile = sys.argv[1], sys.argv[2]
CHECKS = [
    ("boot finished", "systemctl is-system-running --wait; systemd-analyze 2>&1 | head -3"),
    ("kernel command line", "cat /proc/cmdline"),
    ("mounts", "findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS / ; findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS /data; findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS /boot/firmware; findmnt -no SOURCE,TARGET,FSTYPE /etc"),
    ("block devices", "lsblk -o NAME,SIZE,FSTYPE,LABEL,PARTUUID,MOUNTPOINTS"),
    ("targets", "systemctl list-units --type=target --state=active --no-legend --no-pager | awk '{print $1}' | tr '\\n' ' '"),
    ("failed units", "systemctl --failed --no-legend --no-pager"),
    ("eitri units", "systemctl list-units 'eitri*' 'klipper*' 'moonraker*' 'klarity*' 'nginx*' --all --no-legend --no-pager"),
    ("seeded /data", "cat /data/eitri/state/install.env; echo; cat /data/eitri/state/seed-installed.env | grep -v '^#'; cat /data/eitri/flavor /data/eitri/release; ls /data /data/firmware /data/venv /data/eitri"),
    ("klipper clone", "git -C /data/firmware/klipper log -1 --format='%H %cd' 2>&1; git -C /data/firmware/klipper status --porcelain 2>&1 | head -5; echo porcelain-end"),
    ("venvs run", "/data/venv/klippy/bin/python -c 'import sys; print(sys.version)' 2>&1; /data/venv/moonraker/bin/python -c 'import tornado; print(\"tornado\", tornado.version)' 2>&1"),
    ("configurator", "systemctl status eitri-configurator --no-pager 2>&1 | head -6; ls -la /data/eitri/configurator 2>&1 | head; cat /data/eitri/configurator/pairing.json 2>/dev/null | head -c 120; echo"),
    ("boot selection", "cat /boot/firmware/eitri/boot.env; cat /boot/firmware/eitri/layout.env"),
    ("rauc", "cat /etc/rauc/system.conf 2>&1 | grep -E '^device=|bootloader|^\\[slot'; rauc status 2>&1 | head -12"),
    ("eitri-update / verify", "journalctl -b --no-pager -u eitri-prepare -u eitri-verify -u eitri-seed -u eitri-data-init 2>&1 | tail -25"),
    ("printer.cfg as the configurator renders it here (stock toolhead)",
     "cd /tmp && /usr/bin/python3 -c 'import sys; sys.path.insert(0, \"/usr/lib/eitri/configurator\"); "
     "from eitri_configurator import render, options as o; from eitri_configurator.releaselock import load; "
     "from eitri_configurator.settings import Settings; s = Settings(); "
     "ctx = render.build_context(flavor=\"klipper\", options=o.validate({}), detect=None, lock=load(s.release_lock)); "
     "c = render.render_all(s, ctx)[\"printer.cfg\"]; "
     "print(\"layer:\", s.data_dir, \"| [adxl345]:\", \"[adxl345]\" in c, \"| [resonance_tester]:\", \"[resonance_tester]\" in c, "
     "\"| [mcu]:\", \"[mcu]\" in c, \"| [mcu U_1]:\", \"[mcu U_1]\" in c, \"| lines:\", len(c.splitlines()))' 2>&1 | tail -3"),
    ("memory", "free -m"),
]


def main():
    deadline = time.time() + 120
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            break
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(1)
    s.settimeout(1.0)
    log = open(logfile, "a", encoding="utf-8", errors="replace")
    buf = b""

    def read_until(marker, timeout):
        nonlocal buf
        end = time.time() + timeout
        while marker not in buf:
            if time.time() > end:
                return False
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return False
            buf += chunk
            log.write(chunk.decode("utf-8", "replace"))
            log.flush()
        return True

    # the debug shell comes up early in the boot; poke it until it answers
    for _ in range(180):
        s.sendall(b"\necho __REA\"\"DY__\n")          # the typed line is echoed back; only the output reads __READY__
        if read_until(b"__READY__\r\n", 5) or read_until(b"__READY__\n", 1):
            break
    buf = b""
    for i, (title, cmd) in enumerate(CHECKS):
        start, end = "__S%d__" % i, "__E%d__" % i
        s.sendall(("echo %s\"\"_; %s; echo %s\"\"_\n" % (start[:-1], cmd, end[:-1])).encode())
        ok = read_until((end + "\r\n").encode(), 1000) or read_until((end + "\n").encode(), 5)
        text = buf.decode("utf-8", "replace")
        seg = text[text.rfind(start) + len(start):text.rfind(end)] if ok else text[-2000:]
        print("===== %s" % title)
        print("\n".join(l for l in seg.replace("\r", "").splitlines() if l.strip() and not l.startswith("echo __")))
        sys.stdout.flush()
        buf = b""
    s.sendall(b"systemctl poweroff\n")
    read_until(b"reboot: Power down", 180)
    log.close()


if __name__ == "__main__":
    main()
