#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Buildroot post-image hook, last of three. Builds the signed RAUC bundle that
# the A/B updater installs, from the rootfs image Buildroot just produced.
#
# Environment (all optional):
#   EITRI_VERSION        version stamp (default: derived from git)
#   EITRI_RELEASE=1      require a real signing key
#   EITRI_RAUC_CERT      signing certificate (PEM)
#   EITRI_RAUC_KEY       its private key (PEM)
#
# Without a certificate and key a development image gets a bundle signed by the
# throwaway key that post-build.sh baked into its keyring. A release build
# without them fails.
set -euo pipefail

BOARD_DIR=${BR2_EXTERNAL_EITRI_PATH:?run through Buildroot with BR2_EXTERNAL=os/}/board/eitri-plus4
VERSION=$("$BR2_EXTERNAL_EITRI_PATH/scripts/version.sh")
RELEASE=${EITRI_RELEASE:-0}
RAUC="${HOST_DIR:?}/bin/rauc"

[ -x "$RAUC" ] || { echo "eitri post-bundle: host rauc is missing (BR2_PACKAGE_HOST_RAUC)" >&2; exit 1; }

cert=${EITRI_RAUC_CERT:-}
key=${EITRI_RAUC_KEY:-}
if [ -z "$cert" ] || [ -z "$key" ]; then
	if [ "$RELEASE" = 1 ]; then
		echo "eitri post-bundle: EITRI_RAUC_CERT and EITRI_RAUC_KEY are required for a release" >&2
		exit 1
	fi
	pki="${BASE_DIR:?}/eitri-dev-pki"
	"$BR2_EXTERNAL_EITRI_PATH/scripts/gen-dev-pki.sh" "$pki"
	cert="$pki/dev.cert.pem"
	key="$pki/dev.key.pem"
	echo "eitri post-bundle: WARNING: signing with the development key in $pki" >&2
fi

# Verify the new signature against the keyring that is baked into the image
# (post-build.sh put it there), with the image's own RAUC configuration. A
# certificate that the image would reject fails the build instead of the update.
keyring="${TARGET_DIR:?}/etc/rauc/keyring.pem"
conf="$BOARD_DIR/rootfs-overlay/etc/rauc/system.conf"

work="$BASE_DIR/rauc-bundle"
rm -rf "$work"
install -d "$work"
# Buildroot's rootfs.ext4 is a symlink to rootfs.ext2. A hard link to the real
# file keeps the 640 MiB image from being copied.
rootfs=$(readlink -f "$BINARIES_DIR/rootfs.ext4")
ln "$rootfs" "$work/rootfs.ext4" 2>/dev/null || cp "$rootfs" "$work/rootfs.ext4"
sed -e "s|@VERSION@|$VERSION|g" -e "s|@BUILD@|$(date -u +%Y-%m-%dT%H:%M:%SZ)|g" \
	"$BOARD_DIR/rauc/manifest.raucm.in" >"$work/manifest.raucm"

bundle="$BINARIES_DIR/eitri-plus4-$VERSION.raucb"
rm -f "$bundle"
"$RAUC" --conf="$conf" --keyring="$keyring" bundle --cert="$cert" --key="$key" "$work" "$bundle"
rm -rf "$work"

# Stable names next to the versioned bundle, for scripts and CI.
ln -sf "eitri-plus4-$VERSION.raucb" "$BINARIES_DIR/eitri-plus4.raucb"
ln -sf "eitri-plus4.img" "$BINARIES_DIR/eitri-plus4-$VERSION.img"
echo "eitri post-bundle: wrote $bundle"
