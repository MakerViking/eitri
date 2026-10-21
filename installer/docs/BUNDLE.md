# Bundle formats (contract with CI and the OS image)

The installer consumes three kinds of artifact. All metadata is JSON, signed with **minisign** (Ed25519; both the
default pre-hashed `ED` and legacy `Ed` signatures are accepted, trusted comments are verified). Trusted public
keys live in `installer/keys/*.pub` (installer) and `/usr/share/eitri/keys/` (installed system).

## 1. Install bundle (first conversion): `install.json` + `install.json.minisig` + files

A directory on an https server (or a USB stick: `--bundle-dir`). File URLs are relative to `install.json`.

```jsonc
{
  "schema": 1,
  "version": "2026.10.0",
  "channel": "stable",
  "target": { "compatible": "firefly,roc-rk3328-cc" },       // must match the device tree
  "stock":  { "soc": ["1.7.1"] },                             // tested stock firmware; others only warn
  "layout": { "root_mib": 2048, "data_min_mib": 4096 },       // rootfs slot size, minimum /data
  "kernel_cmdline": "console=tty1 loglevel=4 panic=10 net.ifnames=0",
  "flavors": { "klipper": {"describe": "v0.13.0-813-ga08fc79fc", "commit": "…"},
               "kalico":  {"describe": "v2026.10.00",             "commit": "…"} },
  "files": {
    "rootfs":           { "url": "rootfs.img.zst", "sha256": "…", "size": 0, "raw_size": 0, "raw_sha256": "…" },
    "stage2_initramfs": { "url": "stage2-initramfs.cpio.gz", "sha256": "…", "size": 0 },
    "data_seed":    { "klipper": { "url": "…", "sha256": "…", "size": 0 }, "kalico": { … } },
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
  mountpoint insmod reboot nc head cat` and `dd iflag=fullblock`; util-linux `sfdisk blockdev blkid partx`;
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

### `data-seed.tar.zst`
Tar of the initial `/data` (owner uid/gid 1000 preserved, `--numeric-owner`): `eitri/`, `firmware/klipper`
(pristine clone of the chosen flavor, **not** patched; `eitri/flavor` is written by the installer),
`firmware/moonraker`, `venv/*`, `printer_data/`, `eitri/extras`, `eitri/klarity`. Stage 2 adds
`eitri/migrate/` (stock printer config *without* `config.mksini*`, Wi-Fi config, authorized_keys),
`eitri/state/{install.env,backup.env}`, `eitri/mcu/{firmware,backup,stock}`.

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
                  "bundles": [ { "kind": "os",   "url": "https://…/os.raucb", "sha256": "…", "size": 0, "reboot": true },
                               { "kind": "data", "flavor": "klipper", "url": "…", "sha256": "…", "size": 0,
                                 "restart_services": ["klipper", "moonraker", "klarity"] } ] } ],
  "addons": { "toolchain": { "version": "13.3-2026.10", "url": "…tar.zst", "sha256": "…", "size": 0, "installed_size": 0 } },
  "upstream": { "klipper": {"origin": "https://github.com/Klipper3d/klipper.git", "branch": "master"},
                "kalico": {…}, "moonraker": {…} } }
```
* `os` bundles are RAUC bundles installing the inactive rootfs slot; `data` bundles are RAUC bundles that update
  `/data` (hooks do the work). The client installs `os`, reboots, then installs `data` on the new system and
  flashes matching MCU firmware (`/data/eitri/mcu/firmware/<flavor>`) *before* Klipper starts.
* `toolchain` is a `.tar.zst` (or `.xz`) with one top-level directory containing `bin/arm-none-eabi-gcc`.
* The manifest is trusted only through its signature; bundle SHA-256s come from it. RAUC additionally verifies
  its own CMS signature against the keyring in the OS image.

## 3. Release lock
`release.lock` at the repo root pins the same versions the manifest points at; CI is expected to generate both
from one source so they cannot drift.
