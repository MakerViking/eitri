#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Put the on-device half of the installer family (eitri-update, eitri-bootctl, eitri-seed-install, the configurator's
# switch-flavor helper, their systemd units and the trusted release keys) into a root filesystem tree.  For the OS build
# (Buildroot post-build step or package) and for CI; the eitri_mcuflash package itself comes from the eitri-mcuflash package.
#
#   install-into-rootfs.sh DESTDIR [SITE_PACKAGES_DIR]
#
# SITE_PACKAGES_DIR defaults to the single DESTDIR/usr/lib/python3*/site-packages.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:?usage: install-into-rootfs.sh DESTDIR [SITE_PACKAGES_DIR]}"
SITE="${2:-}"
if [[ -z "$SITE" ]]; then
  shopt -s nullglob
  cands=("$DEST"/usr/lib/python3*/site-packages)
  ((${#cands[@]} == 1)) || { echo "cannot tell which site-packages to use (found ${#cands[@]}); pass it as the 2nd argument" >&2; exit 2; }
  SITE="${cands[0]}"
fi

install -d "$SITE" "$DEST/usr/bin" "$DEST/usr/libexec/eitri" "$DEST/usr/lib/systemd/system" \
  "$DEST/usr/lib/systemd/system/multi-user.target.wants" "$DEST/usr/lib/systemd/system/timers.target.wants" "$DEST/usr/share/eitri/keys"

rm -rf "${SITE:?}/eitri_update"
cp -r "$HERE/eitri-update/eitri_update" "$SITE/eitri_update"
find "$SITE/eitri_update" -name __pycache__ -prune -exec rm -rf {} +

cat >"$DEST/usr/bin/eitri-update" <<'PY'
#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
import sys

from eitri_update.cli import main

if __name__ == "__main__":
    sys.exit(main())
PY
cat >"$DEST/usr/libexec/eitri/eitri-bootctl" <<'PY'
#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
# RAUC "custom" bootloader backend over /boot/eitri/boot.env (see installer/docs/BOOT.md).
import sys

from eitri_update.bootctl import main

if __name__ == "__main__":
    sys.exit(main())
PY
chmod 0755 "$DEST/usr/bin/eitri-update" "$DEST/usr/libexec/eitri/eitri-bootctl"

# The data seed installer (seed/bin/eitri-seed-install, the reference implementation of seed/SPEC.md "Installing a seed"):
# eitri-update uses it to deliver a new seed with a release and to switch flavor; the OS lane's data-init may use it for a
# first-boot unpack.  EITRI_SEED_INSTALL_SRC overrides where it is taken from (a CI checkout layout without seed/).
SEED_TOOL_SRC="${EITRI_SEED_INSTALL_SRC:-$HERE/../seed/bin/eitri-seed-install}"
[[ -f "$SEED_TOOL_SRC" ]] || { echo "error: eitri-seed-install not found at $SEED_TOOL_SRC (set EITRI_SEED_INSTALL_SRC)" >&2; exit 1; }
install -m 0755 "$SEED_TOOL_SRC" "$DEST/usr/libexec/eitri/eitri-seed-install"
# What the configurator runs when the wizard asks for the other flavor (it calls /usr/libexec/eitri/switch-flavor if present).
install -m 0755 "$HERE/bin/eitri-switch-flavor" "$DEST/usr/libexec/eitri/switch-flavor"

for unit in eitri-prepare.service eitri-verify.service eitri-update-check.service eitri-update-check.timer; do
  install -m 0644 "$HERE/eitri-update/systemd/$unit" "$DEST/usr/lib/systemd/system/$unit"
done
ln -sf ../eitri-prepare.service "$DEST/usr/lib/systemd/system/multi-user.target.wants/eitri-prepare.service"
ln -sf ../eitri-verify.service "$DEST/usr/lib/systemd/system/multi-user.target.wants/eitri-verify.service"
ln -sf ../eitri-update-check.timer "$DEST/usr/lib/systemd/system/timers.target.wants/eitri-update-check.timer"

shopt -s nullglob
for key in "$HERE"/keys/*.pub; do install -m 0644 "$key" "$DEST/usr/share/eitri/keys/"; done
# Keys that must stop being trusted (listed by key id): shipped inside the RAUC-verified image, so retiring a key never
# depends on the key being retired.  See keys/README.md.
if [[ -f "$HERE/keys/revoked-keys.txt" ]]; then install -m 0644 "$HERE/keys/revoked-keys.txt" "$DEST/usr/share/eitri/keys/revoked-keys.txt"; fi
# The lowest channel-manifest sequence this image accepts; survives a reset of /data (which forgets how far the channel got).
if [[ -n "${EITRI_MIN_MANIFEST_SEQUENCE:-}" ]]; then
  [[ "$EITRI_MIN_MANIFEST_SEQUENCE" =~ ^[0-9]+$ ]] || { echo "error: EITRI_MIN_MANIFEST_SEQUENCE must be a number" >&2; exit 2; }
  printf '%s\n' "$EITRI_MIN_MANIFEST_SEQUENCE" >"$DEST/usr/share/eitri/min-manifest-sequence"
  chmod 0644 "$DEST/usr/share/eitri/min-manifest-sequence"
fi
if ! compgen -G "$DEST/usr/share/eitri/keys/*.pub" >/dev/null; then
  if [[ "${EITRI_REQUIRE_UPDATE_KEY:-0}" == 1 ]]; then
    # A tag build without a trust root would ship an updater that refuses every manifest (or, worse, gets one added later
    # from somewhere unverified).  The tag workflow sets EITRI_REQUIRE_UPDATE_KEY=1 so that never gets out.
    echo "error: EITRI_REQUIRE_UPDATE_KEY=1 but installer/keys/ has no *.pub: commit the release public key before tagging" >&2
    exit 1
  fi
  echo "warning: no keys/*.pub in the installer tree: eitri-update will refuse every manifest until a release key is installed" >&2
fi
