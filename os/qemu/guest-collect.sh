#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Runs inside the guest (as root, through sudo): prints the measurements of one boot.
# smoke.py sends this file over the serial console and parses the output, which is a series of
#
#   @@SECTION <name>
#   ...text...
#   @@END <name> rc=<exit status>
#
# blocks. Usage: guest-collect.sh [full|brief]   (brief = timing, memory and failures only)
mode=${1:-full}
UNITS="eitri-grow-data eitri-data-init eitri-etc eitri-zram eitri-mark-good klipper moonraker klarity eitri-configurator eitri-camera go2rtc nginx NetworkManager sshd avahi-daemon chrony rauc dbus polkit systemd-journald systemd-logind systemd-udevd"

sec() { # name command...
	local name=$1
	shift
	echo "@@SECTION $name"
	"$@" 2>&1
	echo "@@END $name rc=$?"
}

sh_sec() { # name 'shell text'
	local name=$1
	echo "@@SECTION $name"
	bash -c "$2" 2>&1
	echo "@@END $name rc=$?"
}

# --- state ---------------------------------------------------------------------------------
sh_sec identity 'uname -a; echo; cat /proc/cmdline; echo; cat /etc/os-release; echo; nproc; grep -E "model name|CPU part" /proc/cpuinfo | sort -u'
# Wait for the boot to finish: jobs still queued make is-system-running say "starting".
sh_sec system_state 'for i in $(seq 1 60); do s=$(systemctl is-system-running); [ "$s" != starting ] && [ "$s" != initializing ] && break; sleep 2; done; echo "$s"; echo; systemctl list-jobs --no-pager'

# --- timing --------------------------------------------------------------------------------
sec analyze_time systemd-analyze time --no-pager
sh_sec analyze_blame 'systemd-analyze blame --no-pager | head -40'
sec analyze_critical_chain systemd-analyze critical-chain --no-pager
sec analyze_critical_chain_multi-user systemd-analyze critical-chain multi-user.target --no-pager
sh_sec dmesg_milestones "dmesg | grep -E 'Linux version|Kernel command line|Freeing unused kernel|Run /sbin/init|EXT4-fs \(vda\)|EXT4-fs \(vdb\)|systemd\[1\]: (systemd|Hostname)' | head -20"

# When each service became active, in microseconds since the kernel started (monotonic clock).
sh_sec ready_times "systemctl show '*.service' -p Id -p ActiveState -p ActiveEnterTimestampMonotonic --no-pager"

# --- memory --------------------------------------------------------------------------------
sec free free -m
sh_sec meminfo "grep -E 'MemTotal|MemFree|MemAvailable|Buffers|^Cached|SwapTotal|SwapFree|Shmem:|Slab|SReclaimable|SUnreclaim|KernelStack|PageTables|Percpu|Zswap|AnonPages|Mapped' /proc/meminfo; echo; cat /proc/swaps"
# RSS and PSS per systemd unit (processes grouped by the unit in their cgroup). PSS shares
# library pages fairly between processes, so the sum is the real footprint; RSS counts them twice.
echo "@@SECTION memory_by_unit"
python3 - <<'PY' 2>&1
import os, re, collections
units = collections.defaultdict(lambda: [0, 0, 0, []])
total_rss = total_pss = 0

def comm_of(pid):
    return open(f"/proc/{pid}/comm").read().strip()

def ppid_of(pid):
    return int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])

# The measuring tool itself (this interpreter, gc.sh, sudo) is not part of the idle system: leave it out
# and report it separately. The console login shell above it stays, as it is part of the console session.
own = set()
pid = os.getpid()
while pid > 1 and comm_of(ppid_of(pid)) not in ("login", "agetty", "systemd"):
    own.add(str(pid))
    pid = ppid_of(pid)
harness_rss = 0
for pid in filter(str.isdigit, os.listdir("/proc")):
    if pid in own:
        try:
            harness_rss += int(re.search(r"VmRSS:\s+(\d+) kB", open(f"/proc/{pid}/status").read()).group(1))
        except Exception:
            pass
        continue
    try:
        status = open(f"/proc/{pid}/status").read()
        m = re.search(r"VmRSS:\s+(\d+) kB", status)
        if not m:
            continue                     # kernel thread
        rss = int(m.group(1))
        try:
            pss = int(re.search(r"^Pss:\s+(\d+) kB", open(f"/proc/{pid}/smaps_rollup").read(), re.M).group(1))
        except Exception:
            pss = rss
        cg = open(f"/proc/{pid}/cgroup").read().strip().splitlines()[-1].split(":", 2)[-1]
        unit = [p for p in cg.split("/") if p.endswith((".service", ".scope", ".slice"))]
        unit = (unit[-1] if unit else cg) or "(root)"
        if "/user.slice/" in cg or cg.startswith("/user.slice"):
            unit = "user-session"
        comm = open(f"/proc/{pid}/comm").read().strip()
    except Exception:
        continue
    u = units[unit]
    u[0] += rss; u[1] += pss; u[2] += 1; u[3].append(comm)
    total_rss += rss; total_pss += pss
print(f"{'UNIT':42} {'RSS_kB':>8} {'PSS_kB':>8} {'PROCS':>5}  COMMANDS")
for unit, (rss, pss, n, comms) in sorted(units.items(), key=lambda kv: -kv[1][1]):
    print(f"{unit:42} {rss:8d} {pss:8d} {n:5d}  {','.join(sorted(set(comms)))}")
print(f"{'TOTAL (userspace processes)':42} {total_rss:8d} {total_pss:8d}")
print(f"HARNESS_RSS_KB {harness_rss}  (the measuring tool itself, in neither the rows nor the total, but in free -m)")
PY
echo "@@END memory_by_unit rc=$?"
sh_sec processes 'ps -o pid,ppid,user,rss,vsz,comm,args | awk "NR==1 || \$4>0" | sort -k4 -n -r | head -50'
sh_sec cgroup_memory 'for u in klipper moonraker klarity eitri-configurator nginx NetworkManager; do f=/sys/fs/cgroup/system.slice/$u.service/memory.current; [ -r $f ] && echo "$u $(( $(cat $f) / 1024 )) kB"; done; echo; cat /sys/fs/cgroup/cgroup.controllers; cat /sys/fs/cgroup/system.slice/cgroup.controllers 2>&1'

# --- services ------------------------------------------------------------------------------
sec failed systemctl --failed --no-pager --no-legend
sh_sec failed_unit_journals 'for u in $(systemctl --failed --no-pager --no-legend --plain | awk "{print \$1}"); do echo "=== $u"; systemctl show -p Result,ExecMainStatus,ExecMainCode,NRestarts $u | tr "\n" " "; echo; journalctl -b -u $u --no-pager -o short-monotonic -n 25; done'
sh_sec unit_state "for u in $UNITS; do printf '%-26s' \$u; systemctl show -p LoadState,ActiveState,SubState,Result,ConditionResult,ExecMainStatus,NRestarts \$u.service | tr '\n' ' '; echo; done"
if [ "$mode" = full ]; then
	for u in eitri-data-init klarity moonraker klipper eitri-configurator eitri-camera eitri-prepare eitri-verify eitri-mark-good; do
		sec "status_$u" systemctl status "$u.service" --no-pager -l -n 20
	done
	sh_sec condition_paths 'for p in /data/firmware/klipper/klippy/klippy.py /data/venv/klippy/bin/python /data/printer_data/config/printer.cfg /data/firmware/moonraker/moonraker/moonraker.py /data/venv/moonraker/bin/python /data/printer_data/config/moonraker.conf /data/eitri/klarity/venv/bin/klarity /data/printer_data/config/klarity.cfg /data/eitri/configurator/venv/bin/python /dev/eitri-cam /dev/video0 /dev/ttyS1; do if [ -e "$p" ]; then echo "present  $p"; else echo "MISSING  $p"; fi; done'
	sh_sec unit_files 'systemctl list-unit-files --no-pager --state=enabled | head -60'
	sec preset_check systemctl list-unit-files --no-pager --state=disabled,masked,static,indirect,generated,transient --type=service

	# --- /data on the first boot -----------------------------------------------------------
	sh_sec data_mount 'findmnt -n /data; findmnt -n /; findmnt -n /etc; df -h / /data /etc /run /var 2>&1; ls -la /run/eitri 2>&1'
	sh_sec data_layout 'find /data -maxdepth 3 -exec ls -ldn {} + 2>&1 | sort -k9'
	sh_sec data_state 'for f in /data/eitri/state/first-boot /data/eitri/state/layout-version /data/eitri/flavor; do echo "== $f"; cat $f 2>&1; done; echo "== machine-id"; cat /etc/machine-id; echo "== hostname"; hostname; echo "== ui"; readlink /data/eitri/ui; echo "== configurator venv"; ls -la /data/eitri/configurator/venv/bin 2>&1 | head; cat /data/eitri/configurator/venv/lib/python3*/site-packages/eitri-image.pth 2>&1'
	sec journal_data-init journalctl -b -u eitri-data-init -u eitri-etc -u eitri-grow-data -u eitri-zram --no-pager -o short-monotonic
	sh_sec fsck_root 'journalctl -b -u systemd-fsck-root -u "systemd-fsck@*" --no-pager -o short-monotonic; findmnt -n -o SOURCE,FSTYPE,OPTIONS /; grep -v "^#" /etc/fstab'
	sec journal_warnings journalctl -b -p warning --no-pager -o short-monotonic -n 150
	sec dmesg_errors dmesg --level=err,warn
	sh_sec listening 'ss -ltnup'
	sh_sec http_checks 'for url in http://127.0.0.1:8099/ http://127.0.0.1:8099/api/health http://127.0.0.1/ http://127.0.0.1:7125/server/info http://127.0.0.1:8080/snapshot; do printf "%-44s " $url; curl -s -m 8 -o /dev/null -w "%{http_code}\n" $url || echo "curl rc=$?"; done'
	sh_sec configurator_body 'curl -s -m 8 http://127.0.0.1:8099/ | head -c 600'
	sh_sec zram 'cat /run/eitri/zram.dev 2>&1; zramctl 2>&1'
	sh_sec rauc 'systemctl status rauc.service --no-pager -n 5 2>&1 | head -12; rauc status 2>&1 | head -20'
	sh_sec time_sync 'chronyc tracking 2>&1 | head -5; timedatectl 2>&1 | head -8'
	sh_sec network 'ip -br addr; nmcli -t -f DEVICE,STATE,TYPE device 2>&1; nmcli general status 2>&1'
fi
echo "@@DONE"
