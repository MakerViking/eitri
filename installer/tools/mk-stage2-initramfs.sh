#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the base archive of the stage-2 RAM installer (stage2-initramfs.cpio.gz, docs/BUNDLE.md) from a Buildroot output
# directory, and the static aarch64 zstd that stage 1 needs on the stock system (Debian 10 there has no zstd).
#
#   installer/tools/mk-stage2-initramfs.sh --br-out ../build/br-out --out DIR
#
# Writes DIR/stage2-initramfs.cpio.gz and DIR/zstd-aarch64 (statically linked) and prints their SHA-256.
#
# The archive is only the userland: it runs on the printer's STOCK kernel (5.16.20-rockchip64), which U-Boot loads from
# /boot together with this archive; stage 1 appends stage2.sh, its libraries, stage2.conf, eitri-seed-install and the
# stock kernel's vfat/USB modules as a second archive (lib/stage2-build.sh).  Everything here comes from the Buildroot
# target tree (bash, BusyBox with all its applets, util-linux, e2fsprogs, zstd, glibc), plus two programs built from the
# same Buildroot source trees with its cross compiler: debugfs (Buildroot does not install it; stage 2 reads RAUC's
# system.conf out of the new rootfs with it) and the static zstd.  The Buildroot build directories are never written:
# the sources are copied into a scratch directory first.
#
# Contents are checked before the archive is written: every binary and every shared library it needs is present, and
# nothing in the tree is an absolute symlink pointing outside it.  The archive is reproducible for a given input tree
# (sorted entries, fixed mtime, owner root).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BR_OUT=""; OUT=""; KEEP=0
while (($#)); do
  case "$1" in
    --br-out) BR_OUT="${2:?}"; shift ;;
    --out) OUT="${2:?}"; shift ;;
    --keep-work) KEEP=1 ;;
    -h | --help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "mk-stage2-initramfs: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$BR_OUT" && -n "$OUT" ]] || { echo "usage: $0 --br-out BUILDROOT_OUTPUT --out DIR" >&2; exit 2; }
BR_OUT="$(cd "$BR_OUT" && pwd)"
TARGET="$BR_OUT/target"
CC="$BR_OUT/host/bin/aarch64-buildroot-linux-gnu-gcc"
STRIP="$BR_OUT/host/bin/aarch64-buildroot-linux-gnu-strip"
READELF="$BR_OUT/host/bin/aarch64-buildroot-linux-gnu-readelf"
[[ -x "$CC" && -x "$READELF" && -d "$TARGET/usr/lib" ]] || { echo "mk-stage2-initramfs: $BR_OUT is not a built Buildroot output (no cross gcc / target tree)" >&2; exit 1; }
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/eitri-s2.XXXXXX")"
cleanup() { if ((KEEP)); then echo "work dir kept: $WORK" >&2; else rm -rf "$WORK"; fi; }
trap cleanup EXIT
ROOT="$WORK/root"
log() { printf 'mk-stage2-initramfs: %s\n' "$*" >&2; }

src_dir() {   # src_dir NAME -> the one Buildroot build directory NAME-<version>
  local d; d="$(find "$BR_OUT/build" -maxdepth 1 -type d -name "$1-[0-9]*" | sort | tail -1)"
  [[ -n "$d" ]] || { log "no $1 source tree in $BR_OUT/build"; exit 1; }
  printf '%s' "$d"
}

# --- 1. programs Buildroot does not install -------------------------------------------------------------------
log "building debugfs (e2fsprogs) and a static zstd with the Buildroot cross compiler"
E2="$(src_dir e2fsprogs)"; ZS="$(src_dir zstd)"
cp -a "$E2" "$WORK/e2fsprogs"
# the copied tree was configured by Buildroot (cross compiler, --prefix=/usr); only the debugfs program is built
make -s -C "$WORK/e2fsprogs/debugfs" debugfs >"$WORK/debugfs.log" 2>&1 || { tail -30 "$WORK/debugfs.log" >&2; exit 1; }
cp -a "$ZS" "$WORK/zstd"
make -s -C "$WORK/zstd/programs" zstd CC="$CC" LDFLAGS="-static" HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0 HAVE_THREAD=0 \
  ZSTD_LEGACY_SUPPORT=0 >"$WORK/zstd.log" 2>&1 || { tail -30 "$WORK/zstd.log" >&2; exit 1; }
# BusyBox: the image's configuration plus httpd (progress page on :8099), nc (revert.sh --full --from-host), stat, and
# xz-compressed modules for insmod (the stock kernel ships .ko.xz and cannot decompress them itself before 5.17)
BB="$(src_dir busybox)"
cp -a "$BB" "$WORK/busybox"
make -s -C "$WORK/busybox" clean >/dev/null 2>&1 || true
sed -i -e 's/^# CONFIG_HTTPD is not set/CONFIG_HTTPD=y/' -e 's/^# CONFIG_NC is not set/CONFIG_NC=y/' -e 's/^# CONFIG_STAT is not set/CONFIG_STAT=y/' \
  -e 's/^# CONFIG_FEATURE_STAT_FORMAT is not set/CONFIG_FEATURE_STAT_FORMAT=y/' \
  -e 's/^# CONFIG_FEATURE_SEAMLESS_XZ is not set/CONFIG_FEATURE_SEAMLESS_XZ=y/' "$WORK/busybox/.config"
yes '' | make -s -C "$WORK/busybox" oldconfig CROSS_COMPILE="${CC%gcc}" >"$WORK/busybox.log" 2>&1 || true
make -s -C "$WORK/busybox" -j"$(nproc)" busybox CROSS_COMPILE="${CC%gcc}" >>"$WORK/busybox.log" 2>&1 || { tail -30 "$WORK/busybox.log" >&2; exit 1; }
"$STRIP" "$WORK/zstd/programs/zstd" "$WORK/e2fsprogs/debugfs/debugfs" "$WORK/busybox/busybox"
"$READELF" -d "$WORK/zstd/programs/zstd" | grep -q NEEDED && { log "the stage-1 zstd is not static"; exit 1; }

# --- 2. the tree ---------------------------------------------------------------------------------------------
mkdir -p "$ROOT"/{usr/bin,usr/lib,etc,dev,proc,sys,run,tmp,mnt,root,usr/share/udhcpc}
ln -s usr/bin "$ROOT/bin"; ln -s usr/bin "$ROOT/sbin"; ln -s usr/lib "$ROOT/lib"; ln -s usr/lib "$ROOT/lib64"; ln -s bin "$ROOT/usr/sbin"

BINS=(bash zstd sfdisk blockdev blkid partx e2fsck resize2fs dumpe2fs mke2fs)
for b in "${BINS[@]}"; do
  f="$TARGET/usr/bin/$b"; [[ -e "$f" ]] || f="$TARGET/usr/sbin/$b"
  [[ -e "$f" ]] || { log "missing in the Buildroot target: $b"; exit 1; }
  cp -L "$f" "$ROOT/usr/bin/$b"
done
cp "$WORK/e2fsprogs/debugfs/debugfs" "$ROOT/usr/bin/debugfs"
cp "$WORK/busybox/busybox" "$ROOT/usr/bin/busybox"
# BusyBox provides everything else stage 2 and eitri-seed-install use (tar, gzip, sha256sum, mount, dd, httpd, udhcpc...)
for a in $(qemu-aarch64 -L "$TARGET" "$ROOT/usr/bin/busybox" --list 2>/dev/null || true); do
  [[ -e "$ROOT/usr/bin/$a" ]] || ln -s busybox "$ROOT/usr/bin/$a"
done
[[ -L "$ROOT/usr/bin/tar" && -L "$ROOT/usr/bin/httpd" && -L "$ROOT/usr/bin/nc" ]] || {
  log "could not list the BusyBox applets (needs qemu-aarch64 user emulation on this host)"; exit 1; }

# shared libraries, recursively (readelf works for any architecture)
need_libs() { "$READELF" -d "$1" 2>/dev/null | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p'; }
find_lib() { local d; for d in usr/lib lib usr/lib64 lib64; do [[ -e "$TARGET/$d/$1" ]] && { printf '%s' "$TARGET/$d/$1"; return 0; }; done; return 1; }
interp="$("$READELF" -l "$ROOT/usr/bin/bash" | sed -n 's/.*interpreter: \(.*\)\]/\1/p')"
cp -L "$TARGET$interp" "$ROOT/usr/lib/$(basename "$interp")"
queue=("$ROOT"/usr/bin/*)
declare -A seen=()
while ((${#queue[@]})); do
  f="${queue[0]}"; queue=("${queue[@]:1}")
  [[ -L "$f" ]] && continue
  for l in $(need_libs "$f"); do
    [[ -n "${seen[$l]:-}" ]] && continue
    seen[$l]=1
    src="$(find_lib "$l")" || { log "library $l (needed by ${f#"$ROOT"}) is not in the target tree"; exit 1; }
    cp -L "$src" "$ROOT/usr/lib/$l"
    queue+=("$ROOT/usr/lib/$l")
  done
done
[[ -r "$TARGET/etc/mke2fs.conf" ]] && cp "$TARGET/etc/mke2fs.conf" "$ROOT/etc/mke2fs.conf"
printf 'root:x:0:0:root:/root:/bin/sh\neitri:x:1000:1000::/data/eitri/home:/bin/sh\n' >"$ROOT/etc/passwd"
printf 'root:x:0:\neitri:x:1000:\n' >"$ROOT/etc/group"

cat >"$ROOT/usr/share/udhcpc/default.script" <<'EOF'
#!/bin/sh
# busybox udhcpc: configure eth0 for the progress page (stage 2 never needs the network otherwise)
case "$1" in
  deconfig) ip addr flush dev "$interface" 2>/dev/null; ip link set "$interface" up ;;
  bound|renew) ip addr flush dev "$interface" 2>/dev/null; ip addr add "$ip/${mask:-24}" dev "$interface"
               [ -n "$router" ] && ip route replace default via "${router%% *}" dev "$interface" ;;
esac
exit 0
EOF
chmod 0755 "$ROOT/usr/share/udhcpc/default.script"

cat >"$ROOT/init" <<'EOF'
#!/bin/sh
# Eitri stage 2: the one-shot RAM installer.  stage2.sh (appended by install.sh/revert.sh) does the work and reboots.
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null
mount -t devtmpfs devtmpfs /dev 2>/dev/null
mount -t tmpfs -o mode=0755 tmpfs /run 2>/dev/null
mount -t tmpfs tmpfs /tmp 2>/dev/null
export PATH=/usr/bin TERM=linux HOME=/root
if [ -x /usr/lib/eitri/stage2.sh ]; then exec /usr/bin/bash /usr/lib/eitri/stage2.sh; fi
echo "Eitri stage 2: /usr/lib/eitri/stage2.sh is missing (the installer did not append its archive); rebooting in 60 s" >/dev/console
sleep 60; reboot -f
EOF
chmod 0755 "$ROOT/init"

# --- 3. checks ----------------------------------------------------------------------------------------------
bad="$(find "$ROOT" -type l -lname '/*' -printf '%p -> %l\n' || true)"
[[ -z "$bad" ]] || { log "absolute symlinks would point outside the archive:"; printf '%s\n' "$bad" >&2; exit 1; }
for t in sh bash sfdisk blockdev blkid partx e2fsck resize2fs dumpe2fs mke2fs debugfs sha256sum zstd tar gzip mount umount dd sync awk sed \
         tr tee mktemp seq sleep mountpoint insmod reboot head cat find grep date df mv rm rmdir mkdir wc od stat chown install cp udhcpc httpd nc ip; do
  [[ -e "$ROOT/usr/bin/$t" ]] || { log "tool $t is missing from the archive"; exit 1; }
done
# every program starts (user-mode emulation against the archive itself: proves the library set is complete)
for t in bash busybox sfdisk blkid e2fsck resize2fs dumpe2fs mke2fs debugfs zstd; do
  arg=-V; [[ $t == busybox ]] && arg=--help
  out="$(qemu-aarch64 -L "$ROOT" "$ROOT/usr/bin/$t" "$arg" </dev/null 2>&1 || true)"
  if grep -qiE "error while loading shared libraries|cannot open shared object|qemu-aarch64: Could not open" <<<"$out" || [[ -z "$out" ]]; then
    log "$t does not start from the archive: $out"; exit 1
  fi
done

# --- 4. archive ---------------------------------------------------------------------------------------------
find "$ROOT" -exec touch -h -d @1 {} +                     # fixed mtimes: the archive depends only on its contents
( cd "$ROOT" && find . -mindepth 1 | LC_ALL=C sort | sed 's|^\./||' ) >"$WORK/list"
( cd "$ROOT" && fakeroot -- sh -c 'chown -R 0:0 . && cpio --quiet -o -H newc --reproducible -R 0:0' <"$WORK/list" ) |
  gzip -9n >"$OUT/stage2-initramfs.cpio.gz.tmp"
mv -f "$OUT/stage2-initramfs.cpio.gz.tmp" "$OUT/stage2-initramfs.cpio.gz"
install -m 0755 "$WORK/zstd/programs/zstd" "$OUT/zstd-aarch64"
log "files: $(wc -l <"$WORK/list"), unpacked $(du -sh --apparent-size "$ROOT" | cut -f1)"
( cd "$OUT" && sha256sum stage2-initramfs.cpio.gz zstd-aarch64 && ls -l stage2-initramfs.cpio.gz zstd-aarch64 )
