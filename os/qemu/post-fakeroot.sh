#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Buildroot post-fakeroot hook for the QEMU image: give the "eitri" user a password so that the
# smoke test can log in on the serial console.
#
# This has to run here and not in board/eitri-plus4/post-build.sh: Buildroot creates the users of
# users.table after the post-build scripts, so the "eitri" line of /etc/shadow does not exist yet
# when post-build.sh edits it (its EITRI_DEV_PASSWORD_HASH then does nothing).
#
# The root password stays locked, as in the printer image.
set -euo pipefail
TARGET_DIR=${1:?usage: post-fakeroot.sh TARGET_DIR}
hash=${EITRI_QEMU_PASSWORD_HASH:?EITRI_QEMU_PASSWORD_HASH is not set; build through os/qemu/build.sh}
grep -q '^eitri:' "$TARGET_DIR/etc/shadow" || { echo "post-fakeroot: no eitri user in $TARGET_DIR/etc/shadow" >&2; exit 1; }
# Escape what sed treats specially in the replacement.
hash=${hash//\\/\\\\}
hash=${hash//&/\\&}
hash=${hash//|/\\|}
sed -i "s|^eitri:[^:]*:|eitri:${hash}:|" "$TARGET_DIR/etc/shadow"
echo "eitri qemu post-fakeroot: WARNING: bench image, the eitri user has a development password" >&2
# The image ships scoped sudo rules; the smoke test runs "sudo bash" and "sudo systemctl poweroff"
# non-interactively, so this bench image also gets passwordless sudo.
printf '# QEMU bench image only.\neitri ALL=(ALL) NOPASSWD: ALL\n' >"$TARGET_DIR/etc/sudoers.d/eitri-qemu"
chmod 0440 "$TARGET_DIR/etc/sudoers.d/eitri-qemu"
