#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Boot the Eitri OS userspace (built by build.sh) on qemu-system-aarch64, "virt" machine.
#
#   os/qemu/run-qemu.sh [--fresh] [--socket PATH] [-- extra qemu arguments]
#
# The machine mimics the printer's board where QEMU can: 4 x Cortex-A53, 1 GiB of RAM (the board
# reports 976 MB; set MEM=976 to match), the root filesystem on a read-only virtio disk, a second
# virtio disk for /data (a blank ext4 file system on the first run, so the first boot creates the
# /data layout), user-mode networking that cannot reach anything outside the host (restrict=on:
# no NTP, no DNS, no Internet), and forwarded host ports for SSH, the web UI and the configurator.
#
# What QEMU cannot mimic: the RK3328 SoC, its boot chain (U-Boot, TF-A), the UARTs, the camera
# and the screen, eMMC timing, and the speed of the CPU. The guest runs under TCG (software
# emulation), so absolute times are not the printer's. Compare runs with each other, not with a board.
#
# Default console: this terminal (Ctrl-A X quits QEMU). With --socket PATH the serial port is a
# unix socket the smoke test drives, and nothing is read from the terminal.
#
# Environment (all optional):
#   OUT           Buildroot output directory     (default: <repo>/../build/br-qemu)
#   RUN_DIR       disks, logs, sockets           (default: $OUT/qemu)
#   MEM           guest RAM in MiB               (default: 1024)
#   SMP           guest CPUs                     (default: 4)
#   DATA_SIZE     size of the blank /data disk   (default: 512M; a sparse file)
#   SSH_PORT, HTTP_PORT, CONFIGURATOR_PORT, MOONRAKER_PORT   host ports on 127.0.0.1
#                 (defaults 2222, 8088, 8099, 7125; set to 0 to forward nothing)
#   APPEND        extra kernel command line words
#   VERBOSE=1     show kernel messages and systemd's unit status on the console (slower boot)
#   QEMU          qemu binary                    (default: qemu-system-aarch64)
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
BUILD_ROOT=${BUILD_ROOT:-$(cd "$REPO_ROOT/.." && pwd)/build}
OUT=${OUT:-$BUILD_ROOT/br-qemu}
RUN_DIR=${RUN_DIR:-$OUT/qemu}
IMAGES=$OUT/images
MEM=${MEM:-1024}
SMP=${SMP:-4}
DATA_SIZE=${DATA_SIZE:-512M}
SSH_PORT=${SSH_PORT:-2222}
HTTP_PORT=${HTTP_PORT:-8088}
CONFIGURATOR_PORT=${CONFIGURATOR_PORT:-8099}
MOONRAKER_PORT=${MOONRAKER_PORT:-7125}
QEMU=${QEMU:-qemu-system-aarch64}

fresh=0
socket=
while [ $# -gt 0 ]; do
	case "$1" in
	--fresh) fresh=1 ;;
	--socket) socket=${2:?--socket needs a path}; shift ;;
	--) shift; break ;;
	-h | --help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
	*) echo "run-qemu.sh: unknown argument $1" >&2; exit 2 ;;
	esac
	shift
done

for f in "$IMAGES/Image" "$IMAGES/rootfs.ext4"; do
	[ -f "$f" ] || { echo "run-qemu.sh: $f is missing; run os/qemu/build.sh" >&2; exit 1; }
done
command -v "$QEMU" >/dev/null || { echo "run-qemu.sh: $QEMU not found" >&2; exit 1; }

mkdir -p "$RUN_DIR"
data=$RUN_DIR/data.img
if [ "$fresh" = 1 ] || [ ! -f "$data" ]; then
	rm -f "$data"
	truncate -s "$DATA_SIZE" "$data"
	# What grow-data and the image's genimage config give the printer: one ext4 file system, labelled "data".
	mke2fs -q -t ext4 -L data -m 0 "$data"
	echo "run-qemu.sh: new blank /data disk $data ($DATA_SIZE)" >&2
fi

fwd=
add_fwd() { # host-port guest-port
	[ "$1" != 0 ] || return 0
	fwd="$fwd,hostfwd=tcp:127.0.0.1:$1-:$2"
}
add_fwd "$SSH_PORT" 22
add_fwd "$HTTP_PORT" 80
add_fwd "$CONFIGURATOR_PORT" 8099
add_fwd "$MOONRAKER_PORT" 7125

# The printer has no console; here the PL011 serial port is it. "quiet" and a hidden systemd status
# keep the console from slowing the boot being measured; the journal and dmesg have everything.
cmdline="root=/dev/vda ro rootwait rootfstype=ext4 console=ttyAMA0 eitri.data=/dev/vdb"
if [ "${VERBOSE:-0}" = 1 ]; then
	cmdline="$cmdline systemd.show_status=1 ${APPEND:-}"
else
	cmdline="$cmdline quiet loglevel=4 systemd.show_status=0 ${APPEND:-}"
fi

args=(
	-M virt,gic-version=3 -accel tcg,thread=multi
	-cpu cortex-a53 -smp "$SMP" -m "$MEM"
	-display none -monitor none
	-kernel "$IMAGES/Image" -append "$cmdline"
	# virtio-mmio slots are probed in reverse order of the command line: the LAST disk below is
	# /dev/vda (the root filesystem) and the first is /dev/vdb (/data).
	-drive "file=$data,if=none,id=datadisk,format=raw,cache=writeback"
	-device virtio-blk-device,drive=datadisk
	-drive "file=$IMAGES/rootfs.ext4,if=none,id=rootdisk,format=raw,readonly=on"
	-device virtio-blk-device,drive=rootdisk
	-netdev "user,id=net0,restrict=on$fwd"
	-device virtio-net-device,netdev=net0
	-object rng-random,filename=/dev/urandom,id=rng0
	-device virtio-rng-device,rng=rng0
	-rtc base=utc
	-no-reboot
)

log=$RUN_DIR/serial.log
if [ -n "$socket" ]; then
	rm -f "$socket"
	args+=(-chardev "socket,id=ser0,path=$socket,server=on,wait=off,logfile=$log"
		-serial chardev:ser0)
else
	# Console on this terminal, logged to a file. Ctrl-A X quits.
	args+=(-chardev "stdio,id=ser0,mux=on,signal=off,logfile=$log"
		-serial chardev:ser0 -mon chardev=ser0,mode=readline)
fi

echo "run-qemu.sh: $QEMU ${args[*]} $*" >&2
exec "$QEMU" "${args[@]}" "$@"
