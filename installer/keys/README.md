# Trusted release keys

`install.sh` verifies `install.json` against every `*.pub` file in this directory (minisign public key format);
the installed system keeps its copy in `/usr/share/eitri/keys/` for `eitri-update`.

**No key is committed yet.** The release job must add `eitri-release.pub` (and, when rotating, the next key next
to it: both are accepted, so a signed manifest can switch keys). Until then use `--pubkey FILE` or
`--key-dir FILE` explicitly. Never commit a private key; the CI signs with `minisign -S` from a protected secret.
`tools/install-into-rootfs.sh` refuses to build an image when `EITRI_REQUIRE_UPDATE_KEY=1` and no `*.pub` is present here;
the tag workflow must set it (and, once the key is committed, `EITRI_RELEASE=1` should imply it).

Generate a key pair for a release channel:

    minisign -G -p eitri-release.pub -s eitri-release.key

## What is trusted, and how that can change

* **The trust root is the OS image.** `eitri-update` trusts only `/usr/share/eitri/keys/` (read-only, part of the
  RAUC-verified rootfs). `/etc/eitri/keys` is not consulted: `/data` is writable by every service on the printer, so
  a key directory there would be a way to add a signer. To use another directory on purpose (a private channel),
  name it with `key_dir=` in `/etc/eitri/update.conf`.
* **Delegation chain.** Release root key (`eitri-release.pub`, minisign) signs `manifest.json` and `install.json`.
  The RAUC code-signing certificate that signs the `.raucb` bundles is a separate trust root, shipped in the image's
  RAUC keyring; the manifest only names the bundles and their SHA-256, RAUC checks the bundle signature itself.
  Compromise of either does not by itself let an attacker install anything: a bundle needs both a manifest entry
  signed by the minisign key and a RAUC signature.
* **Revocation.** Put the 16-hex-digit key ids (as `minisign` prints them) of keys that must stop being trusted into
  `revoked-keys.txt` here, one per line (`#` starts a comment). The file is installed next to the keys and
  `load_keys` drops those ids, so a revocation ships inside the next OS image, which is verified by RAUC and not by
  the key being retired. Revoke a key and publish the replacement `*.pub` in the same release.
* **Rollback of the channel.** A manifest must carry `expires`. `eitri-update` refuses a lower `sequence` than it
  has seen, and than `/usr/share/eitri/min-manifest-sequence` (written into the image from
  `EITRI_MIN_MANIFEST_SEQUENCE`, so a reset of `/data` does not reopen old manifests). Expiry is judged against the
  later of the system clock, the manifest's own `generated` time and a floor that moves with releases
  (`CLOCK_FLOOR` in `manifest.py`), because the printer has no battery-backed clock.
