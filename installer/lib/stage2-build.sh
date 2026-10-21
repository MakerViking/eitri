# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
#
# Building the one-shot RAM installer (stage 2).  Shared by install.sh and revert.sh.
# Expects: R (test root prefix), STATE_DIR, INSTALLER_DIR, and the helpers from eitri-common.sh.

P() { printf '%s%s' "$R" "$1"; }
py() { python3 -I "$@"; }
read_stock_boot() {
  # kernel / fdt / append of the stock extlinux entry
  local bootdir="${BOOT:-$(P /boot)}"            # revert.sh sets BOOT (/boot/firmware when running on Eitri)
  local conf="$bootdir/extlinux/extlinux.conf"
  [[ -r "$conf" ]] || conf="$bootdir/extlinux/extlinux.conf.stock"
  [[ -r "$conf" ]] || die 10 "no extlinux.conf on /boot - this is not the stock boot layout"
  STOCK_KERNEL="$(awk '$1 == "kernel" {print $2; exit}' "$conf")"
  STOCK_FDT="$(awk '$1 == "fdt" {print $2; exit}' "$conf")"
  STOCK_APPEND="$(awk '$1 == "append" {$1 = ""; sub(/^ /, ""); print; exit}' "$conf")"
  [[ -n "$STOCK_KERNEL" && -n "$STOCK_FDT" ]] || die 10 "cannot find the kernel/fdt lines in $conf"
  [[ -r "$bootdir$STOCK_KERNEL" && -r "$bootdir$STOCK_FDT" ]] || die 10 "the stock kernel ($STOCK_KERNEL) or device tree ($STOCK_FDT) is not on /boot"
  STOCK_FS_UUID="$(sed -n 's/.*root=UUID=\([^ ]*\).*/\1/p' <<<"$STOCK_APPEND")"
  if [[ -z "$STOCK_FS_UUID" ]]; then STOCK_FS_UUID="$(blkid -s UUID -o value "$(part_dev 2)" 2>/dev/null || true)"; fi
}

# Where eitri-seed-install lives: next to this script (a release tarball), or in a repository checkout.
seed_tool() {
  local c
  for c in "${EITRI_SEED_INSTALL:-}" "$INSTALLER_DIR/bin/eitri-seed-install" "$INSTALLER_DIR/../seed/bin/eitri-seed-install"; do
    if [[ -n "$c" && -x "$c" ]]; then printf '%s' "$c"; return 0; fi
  done
  return 1
}

collect_modules() {
  # Modules of the RUNNING stock kernel that stage 2 needs to read vfat / USB sticks; ordered for insmod.
  local m out
  : >"$STATE_DIR/modules.list"
  for m in fat vfat nls_cp437 nls_iso8859_1 nls_utf8 usb_storage uas sd_mod; do
    out="$(modprobe --show-depends "$m" 2>/dev/null | awk '$1 == "insmod" {print $2}' || true)"
    if [[ -n "$out" ]]; then printf '%s\n' "$out" >>"$STATE_DIR/modules.list"; fi
  done
  awk '!seen[$0]++' "$STATE_DIR/modules.list" >"$STATE_DIR/modules.list.u" && mv -f "$STATE_DIR/modules.list.u" "$STATE_DIR/modules.list"
}

# build_stage2_initramfs OUT BASE_ARCHIVE [WITH_MODULES]
# OUT = BASE (CI-built system) + a small archive with our scripts, $STATE_DIR/stage2.conf and, for the
# first run, the stock kernel's vfat/USB modules.  Linux unpacks concatenated archives in order, so later
# files win - which is also how revert.sh re-targets an existing installer image.
build_stage2_initramfs() {
  local out="$1" base="$2" with_modules="${3:-1}" extra="$STATE_DIR/stage2-extra.cpio.gz" args=() ko
  args+=(--file "etc/eitri/stage2.conf=$STATE_DIR/stage2.conf:0644"
         --file "usr/lib/eitri/stage2.sh=$INSTALLER_DIR/stage2/stage2.sh:0755"
         --file "usr/lib/eitri/eitri-common.sh=$INSTALLER_DIR/lib/eitri-common.sh:0644"
         --file "usr/lib/eitri/layout.sh=$INSTALLER_DIR/lib/layout.sh:0644"
         --file "usr/lib/eitri/www/index.html=$INSTALLER_DIR/stage2/www/index.html:0644")
  # The data seed installer (convert only; a restore does not need it): stage 2 unpacks /data with it.
  local seedtool; seedtool="$(seed_tool || true)"
  if [[ -n "$seedtool" ]]; then args+=(--file "usr/libexec/eitri/eitri-seed-install=$seedtool:0755"); fi
  if [[ "$with_modules" == 1 ]]; then
    collect_modules
    args+=(--file "etc/eitri/modules.list=$STATE_DIR/modules.list:0644")
    while IFS= read -r ko; do
      if [[ -n "$ko" && -r "$(P "$ko")" ]]; then args+=(--file "${ko#/}=$(P "$ko"):0644"); fi
    done <"$STATE_DIR/modules.list"
  fi
  py "$INSTALLER_DIR/tools/mkcpio.py" "$extra" "${args[@]}"
  cat "$base" "$extra" >"$out"
}
