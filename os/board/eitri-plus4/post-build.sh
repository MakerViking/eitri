#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Buildroot post-build hook for the Eitri image. Runs inside fakeroot, after
# the rootfs overlay has been applied, with the target tree as $1.
#
# Environment (all optional):
#   EITRI_VERSION            version stamp (default: derived from git)
#   EITRI_RELEASE=1          fail instead of falling back to development
#                            defaults (no RAUC keyring, development keys)
#   EITRI_RAUC_KEYRING       CA certificate (PEM) the image trusts for updates
#   EITRI_DEV_PASSWORD_HASH  crypt(3) hash set as the "eitri" password, for a
#                            bench image only; never for a release
set -euo pipefail

TARGET_DIR=${1:?usage: post-build.sh TARGET_DIR}
BOARD_DIR=${BR2_EXTERNAL_EITRI_PATH:?run through Buildroot with BR2_EXTERNAL=os/}/board/eitri-plus4
REPO_ROOT=$(cd "$BR2_EXTERNAL_EITRI_PATH/.." && pwd)
VERSION=$("$BR2_EXTERNAL_EITRI_PATH/scripts/version.sh")
RELEASE=${EITRI_RELEASE:-0}

log() { echo "eitri post-build: $*"; }
warn() { echo "eitri post-build: WARNING: $*" >&2; }
fail() { echo "eitri post-build: ERROR: $*" >&2; exit 1; }

# --- Kernel and device tree must be in /boot of the slot ----------------------
# U-Boot loads /boot/Image and /boot/rk3328-eitri-plus4.dtb from the slot's own
# filesystem, see uboot.env.
[ -f "$TARGET_DIR/boot/Image" ] || fail "no /boot/Image in the rootfs (BR2_LINUX_KERNEL_INSTALL_TARGET)"
[ -f "$TARGET_DIR/boot/rk3328-eitri-plus4.dtb" ] || fail "no /boot/rk3328-eitri-plus4.dtb in the rootfs"

# The in-place installer boots slots with the stock U-Boot, which asks for
# /boot/dtb/${fdtfile} and has fdtfile=rockchip/rk3328-roc-cc.dtb. Offer our
# device tree under that name too; the board is not a plain ROC-CC.
install -d -m 0755 "$TARGET_DIR/boot/dtb/rockchip"
ln -sf ../../rk3328-eitri-plus4.dtb "$TARGET_DIR/boot/dtb/rockchip/rk3328-roc-cc.dtb"
ln -sf ../rk3328-eitri-plus4.dtb "$TARGET_DIR/boot/dtb/rk3328-eitri-plus4.dtb"

# --- Mount points -------------------------------------------------------------
# /data and /boot/firmware are mounted by units generated at boot. The
# directories have to exist in the read-only image.
install -d -m 0755 "$TARGET_DIR/data" "$TARGET_DIR/boot/firmware"

# --- Identity -----------------------------------------------------------------
cat >"$TARGET_DIR/usr/lib/os-release" <<OSREL
NAME="Eitri OS"
ID=eitri
VERSION_ID="$VERSION"
VERSION="$VERSION"
PRETTY_NAME="Eitri OS $VERSION"
VARIANT_ID=plus4
OSREL
ln -sf ../usr/lib/os-release "$TARGET_DIR/etc/os-release"
printf '%s\n' "$VERSION" >"$TARGET_DIR/etc/eitri-release"

# --- Accounts -----------------------------------------------------------------
# Buildroot leaves root with an empty password. Nothing needs a root password:
# the eitri user has sudo, and there is no console.
sed -i 's/^root:[^:]*:/root:*:/' "$TARGET_DIR/etc/shadow"

if [ -n "${EITRI_DEV_PASSWORD_HASH:-}" ]; then
	[ "$RELEASE" != 1 ] || fail "EITRI_DEV_PASSWORD_HASH must not be set for a release"
	warn "setting a development password for the eitri user"
	hash=${EITRI_DEV_PASSWORD_HASH//\\/\\\\}
	hash=${hash//&/\\&}
	hash=${hash//|/\\|}
	sed -i "s|^eitri:[^:]*:|eitri:${hash}:|" "$TARGET_DIR/etc/shadow"
fi

# sudo ignores a drop-in that is not mode 0440, and git does not keep that mode.
chmod 0440 "$TARGET_DIR/etc/sudoers.d/eitri"

# NetworkManager writes the resolver file; systemd-resolved is not built.
ln -sfn ../run/NetworkManager/resolv.conf "$TARGET_DIR/etc/resolv.conf"

# --- Application layer entry points ---------------------------------------
install -D -m 0644 "$REPO_ROOT/release.lock" "$TARGET_DIR/usr/lib/eitri/release.lock"

# Component-owned units win over the defaults in the rootfs overlay, so a
# component can change its own unit without touching the OS.
for component in klarity configurator; do
	for unit in "$REPO_ROOT/$component/systemd/"*.service; do
		[ -f "$unit" ] || continue
		install -D -m 0644 "$unit" "$TARGET_DIR/usr/lib/systemd/system/$(basename "$unit")"
		log "using $component/systemd/$(basename "$unit")"
	done
done

# eitri-update, eitri-bootctl, their units and the trusted release keys (installer family).
if [ -x "$REPO_ROOT/installer/tools/install-into-rootfs.sh" ]; then
	"$REPO_ROOT/installer/tools/install-into-rootfs.sh" "$TARGET_DIR"
	log "installed eitri-update and eitri-bootctl"
fi

# The configurator runs from a read-only tree, so Python cannot write byte
# code next to it. Precompile it with the same interpreter version, unchecked
# so the files stay valid whatever their timestamps are.
configurator="$TARGET_DIR/usr/lib/eitri/configurator"
if [ -d "$configurator" ]; then
	if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 14) else 1)'; then
		python3 -m compileall -q --invalidation-mode unchecked-hash "$configurator"
	else
		warn "host python is not 3.14: the configurator starts without precompiled byte code"
	fi
fi

# --- Documentation shipped on the device ------------------------------------
install -D -m 0644 "$BR2_EXTERNAL_EITRI_PATH/README.md" "$TARGET_DIR/usr/share/doc/eitri/os/README.md"
install -D -m 0644 "$BR2_EXTERNAL_EITRI_PATH/docs/boot-chain.md" "$TARGET_DIR/usr/share/doc/eitri/os/boot-chain.md"

# --- RAUC trust anchor ----------------------------------------------------
keyring_dest="$TARGET_DIR/etc/rauc/keyring.pem"
keyring_src=${EITRI_RAUC_KEYRING:-}
[ -n "$keyring_src" ] || { [ -f "$BOARD_DIR/rauc/keyring.crt" ] && keyring_src="$BOARD_DIR/rauc/keyring.crt"; } || true

if [ -n "$keyring_src" ]; then
	[ -f "$keyring_src" ] || fail "RAUC keyring $keyring_src not found"
	install -D -m 0644 "$keyring_src" "$keyring_dest"
	log "RAUC keyring: $keyring_src"
elif [ "$RELEASE" = 1 ]; then
	fail "no RAUC keyring: set EITRI_RAUC_KEYRING or commit board/eitri-plus4/rauc/keyring.crt"
else
	pki="${BASE_DIR:?}/eitri-dev-pki"
	"$BR2_EXTERNAL_EITRI_PATH/scripts/gen-dev-pki.sh" "$pki"
	install -D -m 0644 "$pki/ca.cert.pem" "$keyring_dest"
	warn "development image: RAUC trusts the throwaway CA in $pki, not the Eitri release key"
fi

log "Eitri OS $VERSION"
