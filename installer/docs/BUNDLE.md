# Bundle formats (contract with CI and the OS image)

The installer consumes three kinds of artifact. All metadata is JSON, signed with **minisign** (Ed25519; both the
default pre-hashed `ED` and legacy `Ed` signatures are accepted, trusted comments are verified). Trusted public
keys live in `installer/keys/*.pub` (installer) and `/usr/share/eitri/keys/` (installed system). Who signs what, and how
the offline-key / protected-CI-environment arrangement maps onto this: section 3.

These bundles are for the **in-place conversion** (the DOS boot backend that keeps QIDI's U-Boot), the path users take. The
flash-once GPT disk image built by `os/` is a different artifact for bench boards, microSD cards and CI; it does not use
`install.json`.

## 1. Install bundle (first conversion): `install.json` + `install.json.minisig` + files

A directory on an https server (or a USB stick: `--bundle-dir`). File URLs are relative to `install.json`.

```jsonc
{
  "schema": 1,
  "version": "2026.10.0",
  "channel": "stable",
  "target": { "compatible": "firefly,roc-rk3328-cc",          // must match the device tree
              "python_abi": "cp314" },                        // optional: the image's interpreter ABI; a seed built for another is refused
  "stock":  { "soc": ["1.7.1"] },                             // tested stock firmware; others only warn
  "layout": { "root_mib": 2048, "data_min_mib": 4096 },       // rootfs slot size, minimum /data
  "kernel_cmdline": "console=tty1 loglevel=4 panic=10 net.ifnames=0",
  "flavors": { "klipper": {"describe": "v0.13.0-813-ga08fc79fc", "commit": "<40 hex>"},     // the host firmware the MCU images
               "kalico":  {"describe": "v2026.10.00-0-g3526de3bf", "commit": "<40 hex>"} },  // are built from; the seed must carry it
  "files": {
    "rootfs":           { "url": "rootfs.img.zst", "sha256": "…", "size": 0, "raw_size": 0, "raw_sha256": "…" },
    "stage2_initramfs": { "url": "stage2-initramfs.cpio.gz", "sha256": "…", "size": 0 },
    "data_seed":    { "klipper": { "url": "data-seed-2026.10.0-klipper.tar.zst", "sha256": "…", "size": 0,
                                   "manifest": { "url": "data-seed-2026.10.0-klipper.manifest.json", "sha256": "…", "size": 0 } },
                      "kalico":  { … } },
    "mcu_firmware": { "klipper": { "url": "…", "sha256": "…", "size": 0 }, "kalico": { … } }
  }
}
```

`kernel_cmdline` **must not** put a console on `ttyS2` (or `ttyS0`): those are the MCU UARTs.

### `rootfs.img.zst`
Raw ext4 image (zstd), written verbatim to partition 5 (slot A) and mounted **read-only** at runtime.
* Must contain `/boot/Image` (uncompressed arm64 kernel) and `/boot/dtb/rockchip/rk3328-roc-cc.dtb`:
  `boot.scr` loads both with U-Boot's `ext4load`, so create the filesystem conservatively
  (`mke2fs -t ext4 -b 4096 -O ^64bit,^orphan_file,^metadata_csum_seed`; U-Boot 2021.07 predates newer features).
* Mounts `/data` by label `eitri-data` and never refers to partition numbers (the flash-once image and the
  in-place layout number partitions differently). Root is given as `root=PARTUUID=… ro` by `boot.scr`; the
  booted slot is `eitri.slot=A|B` on the kernel command line.
* Image size ≤ `layout.root_mib`; `raw_sha256`/`raw_size` describe the decompressed bytes.

### `stage2-initramfs.cpio.gz`
The RAM installer's userland, run on the **stock 5.16 kernel** (so the kernel is a known quantity). Requirements:
* `/init` mounts nothing it does not need and ends with `exec /usr/lib/eitri/stage2.sh` (the installer appends
  that script, `eitri-common.sh`, `layout.sh`, `stage2.conf` and the stock vfat/USB modules as a second archive).
* Tools: `bash` (not just ash), busybox with `mount umount dd sync awk sed tr tee mktemp seq sleep udhcpc httpd
  mountpoint insmod reboot nc head cat find grep date df mv rm rmdir mkdir` and `dd iflag=fullblock` (`find grep date df mv rm rmdir mkdir
  cat tr`, with `tar`, `zstd` and `sha256sum`, are what `eitri-seed-install` needs; the installer appends that script itself as
  `/usr/libexec/eitri/eitri-seed-install`); util-linux `sfdisk blockdev blkid partx`;
  e2fsprogs `e2fsck resize2fs dumpe2fs mke2fs debugfs` (debugfs is used to read `/etc/rauc/system.conf` out of the new rootfs); `sha256sum zstd tar gzip`. `stage2.sh` refuses to start (before
  touching anything) when one is missing.
* Ethernet (`eth0`, DHCP) is only for the progress page on port 8099 and `revert.sh --full --from-host`.

`installer/tools/install-into-rootfs.sh DESTDIR` installs `eitri-update`, `/usr/libexec/eitri/eitri-bootctl`, the
`eitri-prepare`/`eitri-verify`/`eitri-update-check` units (enabled) and `keys/*.pub` into an image tree.

### RAUC in the rootfs
The image's `/etc/rauc/system.conf` is written for the flash-once disk (GPT, slots in partitions 2/3). For an in-place
conversion stage 2 copies that file into the `/etc` overlay's upper layer (`/data/eitri/state/etc/upper/rauc/system.conf`)
with exactly two edits: each `[slot.rootfs.N]` `device=` becomes partition 5 (bootname A) or 6 (bootname B), and
`bootloader-custom-backend=` becomes `/usr/libexec/eitri/eitri-bootctl` (this repo's backend over `/boot/eitri/boot.env`;
the image must ship it, the flash-once backend `rauc-bootloader` works on a different state store). `eitri-update upgrade`
refuses to run RAUC when its slot devices disagree with `/boot/eitri/layout.env`: p3 on this layout is the extended
partition container, and a bundle written to it would destroy the partition table.

### The data seed: `data-seed-<version>-<flavor>.tar.zst` + `data-seed-<version>-<flavor>.manifest.json`
The application layer of `/data` for one flavor, built by CI per release (`seed/`, specification in
[`seed/SPEC.md`](../../seed/SPEC.md)): pinned Klipper *or* Kalico and Moonraker clones, their Python environments, Klarity and
its environment, the `qd_*` extras and the klipper-layer config, and a prebuilt Klipper C helper. With it the first boot needs
no network and no compiler. `<version>` is the release version (`install.json` `version`); one release has exactly one seed per
flavor.

Stage 1 stages **both files** under exactly these names and trusts nothing the seed says about itself:
the archive's and the manifest's SHA-256 and size come from the signed `install.json`; the manifest must then agree with the
signed document (same version, the flavor asked for, this archive's name/digest/size, `layout` 1, aarch64, the interpreter ABI
if `target.python_abi` is given, the firmware commit and `git describe` of `flavors.<flavor>` so host and MCU firmware match);
the archive must say the same about itself (`eitri/flavor`, `eitri/seed/seed-info.env`); its unpacked size plus 10 % and 64 MiB
must fit `layout.data_min_mib`; and `eitri-seed-install --mode install --dry-run` rehearses the member policy (only the seed
layout, no traversal, devices, setuid files or escaping links). All of that happens before the CONVERT prompt; a failure exits 12
with nothing changed. Stage 2 then runs `eitri-seed-install --mode install --owner 1000:1000` on the freshly created `/data`
(it checks the digest again before reading the archive, places every component with one rename and writes
`eitri/state/seed-installed.env` last). `eitri/flavor` comes from the seed; stage 2 only checks that it is the flavor chosen.
Stage 2 adds `eitri/release`, `eitri/migrate/` (stock printer config *without* `config.mksini*`, Wi-Fi config, authorized_keys),
`eitri/state/{install.env,backup.env}` and `eitri/mcu/{firmware,backup,stock}`; those are outside the seed.

Offline: `install.sh --bundle-dir /media/usb/eitri-2026.10.0` (everything from a USB stick or a directory copied from your PC,
`file://` URLs only) installs without any network access; `--seed-file F --seed-manifest M` takes just the seed from local
files. The signed `install.json` still decides, so such files must match its digests.

### `mcu-firmware.tar.zst`
Unpacks to `<flavor>/manifest.json` and `<flavor>/<board>/klipper.bin`, extracted to `/data/eitri/mcu/firmware`:

```jsonc
{ "schema": 1, "flavor": "klipper", "source": {"commit": "…"},
  "boards": { "mainboard": { "file": "mainboard/klipper.bin", "sha256": "…", "mcu": "stm32f401xc",
                             "version": "v0.13.0-813-ga08fc79fc", "app_address": "0x08008000" },
              "toolhead": { … } } }
```
`version` must equal the version embedded in the image's data dictionary, and equal the host Klipper's
`git describe` (checked before every flash: that is the host/MCU version lock). `app_address` must equal the
application start the board's Katapult reports; a mismatch aborts before erasing.

## 2. Channel manifest (updates): `manifest.json` + `manifest.json.minisig`

```jsonc
{ "schema": 1, "channel": "stable", "sequence": 42,            // sequence only ever grows (replay protection)
  "expires": "2026-12-01T00:00:00Z",
  "releases": [ { "version": "2026.10.0", "notes_url": "…",
                  "requires": { "min_installed": "2026.09.0" }, "rollback_floor": "2026.08.0",
                  "flavors": { "klipper": {"describe": "…", "commit": "<40 hex>"}, "kalico": { … } },     // host firmware pin per flavor
                  "bundles": [ { "kind": "os",   "url": "https://…/os.raucb", "sha256": "…", "size": 0, "reboot": true },
                               { "kind": "mcu",  "flavor": "klipper", "url": "…/mcu-firmware-2026.10.0-klipper.tar.zst", "sha256": "…", "size": 0 },
                               { "kind": "seed", "flavor": "klipper", "url": "…/data-seed-2026.10.0-klipper.tar.zst", "sha256": "…", "size": 0,
                                 "manifest": { "url": "…/data-seed-2026.10.0-klipper.manifest.json", "sha256": "…", "size": 0 },
                                 "restart_services": ["klipper", "moonraker", "klarity"] },
                               { "kind": "mcu", "flavor": "kalico", … }, { "kind": "seed", "flavor": "kalico", … } ] } ],
  "addons": { "toolchain": { "version": "13.3-2026.10", "url": "…tar.zst", "sha256": "…", "size": 0, "installed_size": 0 } },
  "upstream": { "klipper": {"origin": "https://github.com/Klipper3d/klipper.git", "branch": "master"},
                "kalico": {…}, "moonraker": {…} } }
```
* `os` bundles are RAUC bundles installing the inactive rootfs slot. The client installs `os`, reboots, and on the new
  system, in `eitri-prepare.service` (before Klipper, Moonraker and Klarity start), applies the release's data layer:
  1. `mcu` (one per flavor): `mcu-firmware-<version>-<flavor>.tar.zst` unpacks to `<flavor>/manifest.json` and
     `<flavor>/<board>/klipper.bin`. Members are validated (regular files and directories only, nothing outside `<flavor>/`,
     64 MiB cap) and the tree replaces `/data/eitri/mcu/firmware/<flavor>` with two renames, the old one kept until the verdict.
  2. `seed` (one per flavor, needs the matching `mcu`): the data seed above, with its manifest listed under `manifest`.
     `eitri-update` downloads and checks the manifest *before* anything else is downloaded or the OS slot is written (same
     rules as stage 1, plus this system's interpreter ABI and glibc, and the unpacked size counts towards the free space it
     asks for), then runs `eitri-seed-install --mode update --keep-old` after the reboot, re-runs the layer's
     `overlay-install.sh`, and flashes MCU firmware that does not match (the release's `flavors.<flavor>.commit` is the pin).
  3. `data` (older releases): a RAUC bundle whose hooks update `/data`. Still installed, in this position, if present.
  The slot is committed only after Klipper and Moonraker are healthy; then the replaced trees are deleted
  (`eitri-seed-install --mode finish`). If anything fails, or the health check does, `eitri-seed-install --mode rollback`
  puts the previous trees, flavor flag and seed record back, the MCU firmware tree is restored, the MCUs are flashed back, and the
  previous OS slot is selected. A seed is never mixed across releases: it must carry the version of the release it ships in.
* **Flavor switch** (`eitri-update switch-flavor kalico`, also the configurator's `/usr/libexec/eitri/switch-flavor`): the
  `seed` and `mcu` bundles of the *installed* release for the other flavor are fetched from the channel (the release must still
  be listed; otherwise upgrade first), the services are stopped, the trees are swapped (`--switch-flavor`), both MCUs are flashed
  with that flavor's firmware, the services restart and health is checked. No reboot and no OS change. A failure restores the
  previous flavor completely (the same rollback as above); a switch that is interrupted by a power cut is undone at the next
  boot. Refused while printing, in upstream mode and while an earlier update is unverified.
* `toolchain` is a `.tar.zst` (or `.xz`) with one top-level directory containing `bin/arm-none-eabi-gcc`.
* The manifest is trusted only through its signature; bundle SHA-256s come from it. RAUC additionally verifies
  its own CMS signature against the keyring in the OS image.

## 3. Trust model: who signs what

* `install.json` and `manifest.json` are the only documents the clients trust (minisign/Ed25519, key from
  `/usr/share/eitri/keys/` in the RAUC-verified image, or `installer/keys/`). They carry the SHA-256 and size of every other
  artifact: root image, stage-2 initramfs, MCU firmware, **data seeds and their manifests**, RAUC bundles. Nothing downloaded is
  looked at before its digest matched a signed value; the seed's own manifest is only an extra consistency check.
* The private release keys (minisign and the RAUC CA) are held offline by the maintainer. CI builds every artifact unsigned
  (the seed workflow has no secrets and signs nothing), writes the digests into unsigned `install.json`/`manifest.json`
  drafts, and signs only in a separate job that runs in the protected GitHub `release` environment with the maintainer as
  required reviewer (deferred signing). The reviewer approves against the digest list in the job summary. The clients cannot tell
  how a signature was produced; they only know the public key. Consequences for the signing job: `sequence` must be assigned
  at signing time (clients refuse a lower one than they have seen, so two drafts approved out of order would lock the
  older one out), `expires` counts from the day of signing, and a draft that sat in review long should be rebuilt rather than
  signed. See `keys/README.md` for revocation, expiry and rollback rules.
  A seed is therefore exactly as trustworthy as the signed `install.json`/`manifest.json` that names its digest.

## 4. Release lock
`release.lock` at the repo root pins the same versions the manifest points at; CI is expected to generate both
from one source so they cannot drift.
