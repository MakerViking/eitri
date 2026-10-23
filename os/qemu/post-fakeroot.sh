#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Buildroot post-fakeroot hook for the QEMU image: give the smoke test a way to log in on the serial
# console without weakening the account that the product runs as.
#
#   * "bench" is a QEMU-only account (uid 1990) with a development password and passwordless sudo. The
#     smoke test logs in as bench, runs its collection script as root and powers the guest off.
#   * "eitri" keeps the image's own, scoped sudoers rules (etc/sudoers.d/eitri) so that the smoke test can
#     exercise them for real: `sudo -u eitri sudo -n /usr/bin/eitri-configurator unlock` is what Klarity runs,
#     and the configurator calls `sudo -n /usr/libexec/eitri/eitri-access`. It also gets the same password,
#     so a person can log in as eitri on the console.
#
# This has to run here and not in board/eitri-plus4/post-build.sh: Buildroot creates the users of
# users.table after the post-build scripts, so the "eitri" line of /etc/shadow does not exist yet when
# post-build.sh edits it (its EITRI_DEV_PASSWORD_HASH then does nothing).
#
# The root password stays locked, as in the printer image.
set -euo pipefail
TARGET_DIR=${1:?usage: post-fakeroot.sh TARGET_DIR}
hash=${EITRI_QEMU_PASSWORD_HASH:?EITRI_QEMU_PASSWORD_HASH is not set; build through os/qemu/build.sh}
grep -q '^eitri:' "$TARGET_DIR/etc/shadow" || { echo "post-fakeroot: no eitri user in $TARGET_DIR/etc/shadow" >&2; exit 1; }
# Escape what sed treats specially in the replacement.
esc=${hash//\\/\\\\}
esc=${esc//&/\\&}
esc=${esc//|/\\|}
sed -i "s|^eitri:[^:]*:|eitri:${esc}:|" "$TARGET_DIR/etc/shadow"

if ! grep -q '^bench:' "$TARGET_DIR/etc/passwd"; then
	printf 'bench:x:1990:1990:QEMU bench:/tmp:/bin/bash\n' >>"$TARGET_DIR/etc/passwd"
	printf 'bench:x:1990:\n' >>"$TARGET_DIR/etc/group"
	printf 'bench:%s:20000:0:99999:7:::\n' "$hash" >>"$TARGET_DIR/etc/shadow"
fi
echo "eitri qemu post-fakeroot: WARNING: bench image, the eitri and bench users have a development password" >&2
# QEMU bench image only: the smoke test runs "sudo bash" and "sudo systemctl poweroff" non-interactively as bench.
printf '# QEMU bench image only.\nbench ALL=(ALL) NOPASSWD: ALL\n' >"$TARGET_DIR/etc/sudoers.d/eitri-qemu"
chmod 0440 "$TARGET_DIR/etc/sudoers.d/eitri-qemu"

# QEMU's virt board has no thermal sensor, so /sys/class/thermal is empty and Klipper's
# [temperature_sensor host] (temperature_host, thermal_zone0) stops the whole config with "Unable to open
# temperature file". On the printer the RK3328 provides that zone. Give the bench image a stand-in zone
# (a tmpfs over the empty class directory, before Klipper starts) so the smoke test still checks that every
# later config section and extra loads.
mkdir -p "$TARGET_DIR/etc/systemd/system/multi-user.target.wants"
cat >"$TARGET_DIR/etc/systemd/system/eitri-qemu-thermal.service" <<'UNIT'
[Unit]
Description=QEMU bench image only: a stand-in thermal zone for Klipper's host temperature sensor
DefaultDependencies=no
After=sysinit.target
Before=klipper.service
ConditionPathExists=!/sys/class/thermal/thermal_zone0

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'mount -t tmpfs -o size=16k,mode=0755 eitri-qemu-thermal /sys/class/thermal && mkdir /sys/class/thermal/thermal_zone0 && echo 42000 >/sys/class/thermal/thermal_zone0/temp'

[Install]
WantedBy=multi-user.target
UNIT
ln -sf ../eitri-qemu-thermal.service "$TARGET_DIR/etc/systemd/system/multi-user.target.wants/eitri-qemu-thermal.service"
