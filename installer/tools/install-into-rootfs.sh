#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Put the on-device half of the installer family (eitri-update, eitri-bootctl, their systemd units and the
# trusted release keys) into a root filesystem tree.  For the OS build (Buildroot post-build step or package)
# and for CI; the eitri_mcuflash package itself comes from the eitri-mcuflash package.
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

for unit in eitri-prepare.service eitri-verify.service eitri-update-check.service eitri-update-check.timer; do
  install -m 0644 "$HERE/eitri-update/systemd/$unit" "$DEST/usr/lib/systemd/system/$unit"
done
ln -sf ../eitri-prepare.service "$DEST/usr/lib/systemd/system/multi-user.target.wants/eitri-prepare.service"
ln -sf ../eitri-verify.service "$DEST/usr/lib/systemd/system/multi-user.target.wants/eitri-verify.service"
ln -sf ../eitri-update-check.timer "$DEST/usr/lib/systemd/system/timers.target.wants/eitri-update-check.timer"

shopt -s nullglob
for key in "$HERE"/keys/*.pub; do install -m 0644 "$key" "$DEST/usr/share/eitri/keys/"; done
if ! compgen -G "$DEST/usr/share/eitri/keys/*.pub" >/dev/null; then
  echo "warning: no keys/*.pub in the installer tree: eitri-update will refuse every manifest until a release key is installed" >&2
fi
