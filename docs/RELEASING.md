# Releasing Eitri

How an Eitri release is built, signed and published, who holds which key, and what to do when a key must change. Written for the maintainer who holds the keys (today: Thomas) and for anyone reviewing the release tooling.

> **Pre-alpha.** No key exists yet and no release has been signed. Everything below that says "committed" describes what must be in place before the first tag. The tag workflows refuse to run a release build without it.

Contents: [1](#1-what-is-signed-and-by-what) what is signed, [2](#2-keys-and-custody) keys, [3](#3-one-time-setup) setup, [4](#4-making-a-release) releasing, [5](#5-deferred-signing) deferred signing, [6](#6-the-numbers-that-move-with-every-release) numbers that move, [7](#7-rotation) rotation, [8](#8-revocation-and-compromise) revocation, [9](#9-signing-offline) offline signing, [10](#10-limits-and-open-items) limits.

## 1. What is signed, and by what

A printer trusts a release because of two independent signatures. Compromising one is not enough to make a printer install something.

| What | Signed with | Checked by | Trust anchor on the printer |
|---|---|---|---|
| `install.json` (first conversion) | minisign | `installer/install.sh` before it downloads anything else | `installer/keys/*.pub` |
| `manifest.json` (channel manifest) | minisign | `eitri-update` | `/usr/share/eitri/keys/*.pub` (from the same directory, inside the RAUC-verified rootfs) |
| `*.raucb` (OS bundle) | RAUC code-signing certificate | RAUC, on install | the CA certificate in the image keyring, `os/board/eitri-plus4/rauc/keyring.crt` |
| `SHA256SUMS` (everything attached to the release) | minisign | people, `sha256sum --check` | `installer/keys/*.pub` |
| data seed, MCU firmware, rootfs, stage-2 initramfs | not signed themselves | digest and size in the signed `install.json` / `manifest.json` | (same as above) |
| flash-once image `eitri-plus4-<v>.img.zst` | covered by the signed `SHA256SUMS` only | the person flashing it | `installer/keys/*.pub` |

The signed documents name every other file by SHA-256 ([`installer/docs/BUNDLE.md`](../installer/docs/BUNDLE.md)), so signing three small files protects the whole release.

## 2. Keys and custody

There are two tiers. **Root keys never leave Thomas's offline storage and never enter GitHub.** **Signing keys are what CI uses**, only inside the protected `release` environment (section 5). Each signing key is certified or trusted by a root key, so it can be replaced without touching the images that are already out there.

| Key | Kind | Lives | Public half | Used for |
|---|---|---|---|---|
| `eitri-root` | minisign | offline only | `installer/keys/eitri-root.pub` | emergency manifests and revocations (section 8); signing by hand when CI must not be trusted (section 9) |
| RAUC CA | EC P-256 CA key | offline only | `os/board/eitri-plus4/rauc/keyring.crt` (the image keyring) | issuing and renewing the CI signing certificate |
| `eitri-release` | minisign | `release` environment secret, passphrase in a second secret | `installer/keys/eitri-release.pub` | `install.json`, `manifest.json`, `SHA256SUMS` in CI |
| RAUC signing certificate | EC P-256 leaf issued by the CA, code-signing, valid about 13 months | `release` environment secret (key and certificate) | chains to the CA in the keyring, nothing to publish | re-signing the OS bundle in CI |

Both `*.pub` files are trusted at once (`eitri-update` accepts every key in the directory), so a manifest signed by the root key is accepted by every printer that has the image. That is what makes the offline fallback and the recovery from a compromised CI key possible.

Rules:

* A private key is never committed, never written to a log, never put in an artifact, never in a build job. `tools/check-repo.py` fails when a workflow references a secret outside the signing step or when a build job writes key files; `tools/release/release.py preflight` refuses a release directory that holds key material; `sign-release.sh` refuses a key file inside the artifact directory.
* The passphrase of a key is not stored next to it. CI holds the passphrase of `eitri-release` as its own secret because it has to; the root keys have passphrases that only Thomas knows.
* Keep two encrypted copies of each root key on separate media, and write down where they are. Losing the root keys is survivable (new images with a new root, section 7); losing them *and* having a compromised CI key is not (section 8).

## 3. One-time setup

Do this once, on an offline machine, before the first tag. Nothing here touches a printer.

**1. Generate the keys.**

```sh
# minisign (asks for a passphrase twice; choose one per key)
minisign -G -p installer/keys/eitri-root.pub    -s ~/offline/eitri-root.key
minisign -G -p installer/keys/eitri-release.pub -s ~/offline/eitri-release.key

# RAUC CA. Leave the key encrypted (no -noenc): you will be asked for a passphrase.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout ~/offline/rauc-ca.key -out os/board/eitri-plus4/rauc/keyring.crt -days 7300 \
  -subj "/O=Eitri/CN=Eitri release CA" \
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign

# RAUC signing certificate for CI (see section 7 for renewing it)
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning\n' > ext.cnf
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -noenc -keyout rauc-release.key \
  -out rauc-release.csr -subj "/O=Eitri/CN=Eitri release signer 2026"
openssl x509 -req -in rauc-release.csr -CA os/board/eitri-plus4/rauc/keyring.crt -CAkey ~/offline/rauc-ca.key \
  -CAcreateserial -out rauc-release.crt -days 400 -extfile ext.cnf
```

**2. Commit only the public halves:** `installer/keys/eitri-root.pub`, `installer/keys/eitri-release.pub` and `os/board/eitri-plus4/rauc/keyring.crt`. Check with `git diff --cached --stat` that no `.key`, `.sec` or `.csr` is staged.

**3. Create the GitHub environment** (Settings, Environments, New environment): name it exactly `release`.

* Required reviewers: Thomas. Leave "Prevent self-review" off while he is the only maintainer (otherwise nobody can approve his own run); the gate then protects against a compromised workflow, a malicious pull request or a mistaken run, not against Thomas.
* Deployment branches and tags: *Selected branches and tags*, add the tag rule `v*`. A run from a branch cannot reach the secrets.
* Environment secrets (not repository secrets, which every job could read):

  | Secret | Content |
  |---|---|
  | `EITRI_MINISIGN_KEY` | the file `eitri-release.key` |
  | `EITRI_MINISIGN_PASSWORD` | its passphrase |
  | `EITRI_RAUC_KEY` | `rauc-release.key` |
  | `EITRI_RAUC_CERT` | `rauc-release.crt` |

**4. Protect the tags and the default branch** (Settings, Rules): only Thomas may create `v*` tags, and tags cannot be deleted or moved. Require review for pull requests that touch `.github/`, `tools/release/`, `installer/keys/`, `os/board/eitri-plus4/rauc/` or `release.lock`.

**5. Delete the private files from the working machine** once the secrets are set and the offline copies are checked.

Rehearse the whole flow once in a fork or a scratch repository with throwaway keys (the `collect` summary, the approval prompt and the `sign` job are only visible on GitHub) before the first real tag.

If the `release` environment is missing, GitHub creates an empty, unprotected one on first use. It has no secrets, so the `sign` job fails at "the release environment has no secret for ...". It cannot sign with anything else.

## 4. Making a release

Versions are `YYYY.MM.N` (`2026.10.0`); the tag is `v2026.10.0`.

1. **Prepare in a pull request** (section 6 explains each number):
   * `release.lock`: bump `[release] manifest_sequence`; move `clock_floor` and `CLOCK_FLOOR` in `installer/eitri-update/eitri_update/manifest.py` together; raise `min_manifest_sequence` only if old manifests must stop working; update the pins you intend to ship; set `tested = true` only for pins that ran on a printer.
   * `make test`, `make -C os check`, `make -C seed seed`, and `python3 -I tools/check-repo.py` pass.
2. **Tag the merge commit** and push the tag: `git tag -s v2026.10.0 && git push origin v2026.10.0`. This starts `os-image`, `data-seed` and `legal`. None of them has a key. The tag build fails if the public trust roots from section 3 are missing.
3. **Wait until `os-image` and `data-seed` are green** for the tag. (They take hours; the OS image does a full Buildroot build from empty caches.)
4. **Start the release workflow on the tag:** `gh workflow run release.yml --ref v2026.10.0`.
   * `collect` downloads the artifacts of exactly those runs, checks them against their own `SHA256SUMS`, runs the preflight (digests, version against the tag, seeds against `release.lock`, trust roots, key material) and prints a summary: commit, run URLs, every file with size and SHA-256, the digest of `INPUTS.sha256`.
   * The workflow then waits at the `release` environment.
5. **Review and approve.** Compare the commit with what you tagged and the digests with the build logs. Approving lets the `sign` job re-verify the inputs, build the pinned RAUC, re-sign the bundle, write and sign the documents, and verify the result with the printer's own code. A failure leaves nothing behind.
6. **The `publish` job** verifies the signed set again with public keys only and uploads it to the **draft** release (`gh release upload --clobber`). It never publishes.
7. **Before publishing the draft**: download `SHA256SUMS`, `SHA256SUMS.minisig`, `install.json`, `manifest.json` and their signatures into a clean directory and run
   `python3 -I tools/release/release.py verify-signed --dir <dir>` and `sha256sum --check SHA256SUMS`. Read `manifest.json` once.
8. **Publish the draft** and announce. Updating the hosted channel manifest (if the channel is served from a fixed URL) is the same file.

A release without `install.json` is normal while the in-place installer payload is incomplete: `release.py metadata` writes only the documents it can complete, the release plan says what is missing, and `manifest.json` carries the seeds and MCU firmware only for flavors that have both. The preflight does not allow a half-complete document.

### Artifact names (the contract between the workflows and `release.py`)

| File | Made by |
|---|---|
| `eitri-plus4-<v>.img.zst`, `eitri-plus4-<v>.raucb`, `buildroot.config` | `os-image` |
| `data-seed-<v>-<flavor>.tar.zst` and `.manifest.json` | `data-seed` |
| `mcu-firmware-<v>-<flavor>.tar.zst` | not built yet |
| `rootfs-<v>.img.zst`, `stage2-initramfs-<v>.cpio.gz` | not built yet (in-place installer) |
| `install.json`, `manifest.json`, `release-plan.json`, `*.minisig`, `SHA256SUMS` | the `sign` job (`tools/release/`) |

## 5. Deferred signing

The principle: **a job that compiles code never holds a key, and the job that holds the keys never compiles anything.**

```
 tag v2026.10.0
   |
   +--> os-image   (no secrets)  --> unsigned artifact: img.zst, raucb built with a throwaway key, buildroot.config
   +--> data-seed  (no secrets)  --> seed archives + manifests
   +--> legal      (no secrets)  --> SBOM, licences, sources
   |
 gh workflow run release.yml --ref v2026.10.0
   |
   +--> collect (no secrets)     --> release-unsigned artifact + summary + INPUTS.sha256 digest
   |
   |        === environment "release": Thomas approves ===
   |
   +--> sign (secrets, only in the "Sign" step)
   |      builds the pinned RAUC from source (sha256 from release.lock)
   |      rauc resign   --> bundle signed by the release certificate, verified against the image keyring
   |      release.py metadata --> install.json, manifest.json (names the re-signed bundle's digest)
   |      release.py preflight --> everything agrees
   |      minisign install.json, manifest.json, SHA256SUMS
   |      release.py verify-signed --> the printer's own parser and signature code accept the result
   |
   +--> publish (contents: write, no secrets) --> draft release
```

* The OS build has the **public** material only: the RAUC CA certificate (it becomes the image keyring) and the minisign public keys. The bundle is built with a throwaway key because RAUC cannot build an unsigned bundle (`EITRI_DEFERRED_SIGNING=1`, see section 10); the signature that counts is added by `rauc resign`.
* The throwaway signature is never trusted. `sign-release.sh` re-signs with `--no-verify` precisely because the input has no meaningful signature; its integrity is the `INPUTS.sha256` chain: build job `SHA256SUMS`, `collect` pin, digest printed for the reviewer, digest re-checked by `sign`.
* The RAUC used for re-signing is built in the signing job from the pinned source (`tools/release/build-rauc.sh`), not taken from the distribution and not taken from the build job.
* The signing step is the only place that reads `secrets.*`; keys exist in a private temporary directory for the duration of that step. `tools/check-repo.py` enforces this shape on every change to the workflows.
* **Residual trust, stated plainly:** whoever can start the `release` workflow on a `v*` tag and click approve can make CI sign. That is Thomas. A compromised GitHub account with both abilities, or a compromised runner during the `sign` job, can sign. The root keys limit the damage (section 8); they do not prevent it.

## 6. The numbers that move with every release

All three live in `release.lock` (`[release]`), are checked by `tools/check-repo.py` and by the preflight, and are changed in the release pull request, never by CI.

| Field | Meaning | Rule |
|---|---|---|
| `manifest_sequence` | `sequence` of the channel manifest signed for this release | Strictly higher than every sequence ever signed on the channel: `eitri-update` refuses a lower number than it has seen. Bump by one per signed manifest. An emergency manifest (section 8) takes the next number too |
| `min_manifest_sequence` | the floor baked into the image (`EITRI_MIN_MANIFEST_SEQUENCE`, installed as `/usr/share/eitri/min-manifest-sequence`) | Not above `manifest_sequence`. Raise it to the sequence of the first manifest you still accept when older manifests must die (after a revocation, a bad release, a leaked old key). It only protects printers that run an image carrying it; a reset of `/data` cannot lower it. Never raise it past a manifest you still need |
| `clock_floor` and `CLOCK_FLOOR` in `installer/eitri-update/eitri_update/manifest.py` | the earliest "now" a printer without a battery-backed clock will believe | The two must be equal (checked). Set both to a date before the release is built and after the previous floor, every release. A floor after a manifest's `generated` time, or in the future, makes valid manifests look expired and kills updates; a floor that is too old lets a replayed expired manifest through on a printer whose clock is not set yet |
| `manifest_validity_days` | how long a signed manifest stays valid | 14..400, default 90. Short enough that a stolen old manifest dies, long enough to survive a release gap. When no release is due, re-sign the current one (a new `generated`, a new `sequence`) before it expires |

`tools/release/release.py preflight --stage metadata` checks: `generated` is not before `CLOCK_FLOOR`, not in the future, not older than 30 days (the longest a reviewer may sit on an approval); the validity is within bounds; `sequence` is at least `min_manifest_sequence` and at least `manifest_sequence`; the printer's parser accepts both documents; every URL, size and SHA-256 matches a file.

## 7. Rotation

Rotation is routine. Revocation (section 8) is the emergency version.

**RAUC signing certificate** (before it expires, about every 12 months, or whenever the CI secrets may have been seen): on the offline machine issue a new leaf from the CA (the last block of section 3, new `CN`), replace `EITRI_RAUC_KEY` and `EITRI_RAUC_CERT` in the environment. No image, no commit, no printer is involved: images trust the CA, and `use-bundle-signing-time=true` means bundles signed while the old certificate was valid stay valid.

**minisign signing key** (`eitri-release`): generate `eitri-release-2`, commit its `.pub` next to the old one, ship a release (signed by the old key) so the new image trusts both, then replace the environment secrets, then sign the next release with the new key. After enough users have updated, remove the old `.pub` (and list its id in `revoked-keys.txt` if it was exposed). Both keys are accepted at once, so there is no flag day.

**RAUC CA**: append the new CA certificate to `keyring.crt` (a PEM file may hold several), ship a release signed by the old leaf, issue a new leaf from the new CA, switch the secrets, and remove the old CA in a later release.

**Root keys**: a new root pub is trusted by shipping it in an image signed by the old trust chain. If the root key is lost, nothing can be revoked by the root any more, and a compromise of the CI key would then need a new image flashed by hand (`docs/RECOVERY.md`). Make the copies in section 2.

## 8. Revocation and compromise

**What revocation can do.** `installer/keys/revoked-keys.txt` lists key ids (the 16 hex digits `minisign` prints for the key, one per line, `#` for comments). It is installed next to the keys, so a revocation ships **inside the next OS image**, which RAUC verifies. `load_keys` then drops those ids. Revoke a key and publish its replacement `.pub` in the same release.

**What it cannot do.** It reaches a printer only through an update. If an attacker can sign an OS bundle and a manifest before you publish the revocation, printers that update from the attacker's copy of the channel are theirs. Revocation lists for the RAUC side are not implemented (section 10): replacing a leaf certificate does not distrust the old one for already-installed images.

**If the CI signing secrets may have leaked** (the `release` environment, a runner log, a reviewer account):

1. Remove the secrets from the environment and disable the `release` workflow, now.
2. Decide from the GitHub audit log whether a `sign` job ran that Thomas did not approve. If none did, the keys were never usable for a signature the channel would accept; rotate (section 7) and move on.
3. If one did, assume a malicious manifest or bundle may be circulating. On the offline machine: make a clean release, add the old minisign id to `installer/keys/revoked-keys.txt`, add the new `.pub`, issue a new RAUC leaf, raise `min_manifest_sequence` above any sequence that was signed illegitimately, build it, and sign it **by hand with the root minisign key** (section 9). Publish with a `manifest_sequence` higher than anything the attacker used.
4. Printers that already took a malicious OS bundle cannot be fixed by this: they run attacker code. Say so in the advisory and point to `docs/RECOVERY.md` (flash a clean image over maskrom).

**If the offline root keys are lost or exposed**: treat every key as exposed, build a new root (new `.pub`, new CA), publish it in a release signed with the chain that still works, and tell users to update before anything else.

## 9. Signing offline

The same script signs on Thomas's machine; this is the fallback when CI is unavailable or must not be trusted, and the way to sign an emergency manifest.

```sh
gh run download <collect-run-id> -n release-unsigned -D unsigned    # or take the files from the draft release plan
export EITRI_MINISIGN_KEY_FILE=~/offline/eitri-root.key              # asks for its passphrase
export EITRI_RAUC_KEY_FILE=...   EITRI_RAUC_CERT_FILE=...           # a leaf issued by the CA
export EITRI_RAUC=$(tools/release/build-rauc.sh ~/rauc-pinned)       # or any rauc 1.15 you trust
tools/release/sign-release.sh --in unsigned --out signed --version 2026.10.0 \
  --base-url https://github.com/OWNER/REPO/releases/download/v2026.10.0/
gh release upload v2026.10.0 signed/* --clobber                      # to the draft
```

`--previous manifest.json` carries the releases of an earlier verified manifest over. `release.py metadata --sequence N` overrides `manifest_sequence` for an emergency manifest only.

## 10. Limits and open items

* **Deferred bundle mode in the OS build is not implemented yet.** `os/board/eitri-plus4/post-bundle.sh` has to learn `EITRI_DEFERRED_SIGNING=1`: sign the bundle with a throwaway key (`os/scripts/gen-dev-pki.sh`), skip the keyring verification, and leave `EITRI_RELEASE=1` meaning "the public keyring and verification keys must be committed" instead of "a private key must be supplied". `post-build.sh` must bake the committed CA as the image keyring in that mode and pass `EITRI_MIN_MANIFEST_SEQUENCE` and `EITRI_REQUIRE_UPDATE_KEY` through to `installer/tools/install-into-rootfs.sh`. Until then a tag build of `os-image` fails closed.
* No key, CA certificate or `.pub` is committed. The tag workflows refuse without them.
* `rauc resign` was tested here with RAUC 1.15.2 on a small verity bundle (dev-signed, re-signed by a second leaf of the same CA, verified under the image's `[keyring]` settings, and refused for a leaf of another CA). The pinned-RAUC source build in the signing job (`tools/release/build-rauc.sh`) was not run to completion locally (no `gdbus-codegen` on the development machine); the SHA-256 check and the meson configuration step ran. Nothing has run on GitHub.
* Revocation of a RAUC leaf (a CRL in the keyring, `check-crl`) is not configured. See section 8.
* `data-seed`, `os-image` and `release` use the artifact digests of GitHub's artifact storage plus the `SHA256SUMS` files that the build jobs write. Build provenance attestations (`actions/attest-build-provenance`) would bind the artifacts to the workflow run cryptographically; not added.
* The OS image is not reproducible bit for bit (the data seed is). A reviewer cannot rebuild the image to compare it with the artifact.
* `legal` artifacts are attached by their own workflow and are not part of `SHA256SUMS`.
