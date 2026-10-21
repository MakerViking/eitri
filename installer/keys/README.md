# Trusted release keys

`install.sh` verifies `install.json` against every `*.pub` file in this directory (minisign public key format);
the installed system keeps its copy in `/usr/share/eitri/keys/` and `/etc/eitri/keys/` for `eitri-update`.

**No key is committed yet.** The release job must add `eitri-release.pub` (and, when rotating, the next key next
to it: both are accepted, so a signed manifest can switch keys). Until then use `--pubkey FILE` or
`--key-dir FILE` explicitly. Never commit a private key; the CI signs with `minisign -S` from a protected secret.

Generate a key pair for a release channel:

    minisign -G -p eitri-release.pub -s eitri-release.key
