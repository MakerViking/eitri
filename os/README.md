# Eitri OS

The base image of Eitri: a Buildroot appliance for the QIDI X-Plus 4 with a
read-only A/B root filesystem, mainline Linux 6.18 LTS and systemd. It is a
Buildroot `BR2_EXTERNAL` tree; Buildroot itself is fetched at the version pinned
in [`release.lock`](../release.lock).

Nothing in this tree has been booted on a printer yet. Section
[Status](#status) lists what has been checked and what has not.

## Build

```sh
make -C os image          # needs a Linux host with the usual Buildroot prerequisites
```

This clones Buildroot 2026.02.3 into `../build/buildroot` (outside the
repository), configures `../build/br-out` from `configs/eitri_plus4_defconfig`
and builds. A cold build downloads the sources into `../build/br-dl`. Outputs:

| File in `br-out/images/` | What |
|---|---|
| `eitri-plus4.img` | raw disk image, about 1.7 GiB; write it to the start of the eMMC or a microSD card |
| `eitri-plus4-<version>.raucb` | signed update bundle for an installed printer |
| `idbloader.img`, `u-boot.itb`, `Image`, `rk3328-eitri-plus4.dtb`, `rootfs.ext4` | the parts |

Host prerequisites beyond Buildroot's own list: `python3` 3.11 or newer, `rsync`,
`unzip`, `git`, and for the RAUC bundle `openssl`. `make -C os help` lists every target.

### Development and release builds

A plain build is a **development build**. It has no release signing key, so it
creates a throwaway CA in `br-out/eitri-dev-pki`, trusts only that CA for
updates, and signs its bundle with it. Such an image accepts bundles from the
same build tree and nothing else.

A release build supplies the real keys and refuses to fall back:

```sh
EITRI_RELEASE=1 EITRI_VERSION=0.1.0 \
EITRI_RAUC_KEYRING=ca.crt EITRI_RAUC_CERT=release.crt EITRI_RAUC_KEY=release.key \
make -C os image
```

`EITRI_RAUC_KEYRING` may instead be committed as
`board/eitri-plus4/rauc/keyring.crt` (a CA certificate; never a private key).
For a bench image only, `EITRI_DEV_PASSWORD_HASH` (a `crypt(3)` hash, for
example from `openssl passwd -6`) sets a password for the `eitri` user.

### Checks that need no full build

```sh
make -C os check          # pins, defconfig, fragments, unit tests
```

| Target | What it proves |
|---|---|
| `check-pins` | versions and digests in the packages, defconfig and hash files equal `release.lock` |
| `check-defconfig` | every option in the defconfig took effect (Buildroot silently drops unmet ones) |
| `check-fragments` | with `KERNEL_SRC=` and `UBOOT_SRC=` pointing at extracted trees, every line of `linux.fragment`, `uboot.fragment` and the DRAM-blob fragment survives Kconfig |
| `test` | the shell tools that ship in the image, `uboot.env`, the partition layout (needs `genimage`; set `EITRI_GENIMAGE`) and, with `EITRI_UBOOT_SANDBOX=`, the boot script in a U-Boot sandbox |

## Disk layout

```
sector 64     idbloader.img        TPL + SPL
sector 8128   A/B boot state       one text sector (U-Boot and rauc-bootloader)
sector 16384  u-boot.itb           U-Boot proper + TF-A BL31
 1  boot      FAT   32 MiB   boot options and an authorized_keys drop; /boot/firmware, read-only
 2  rootfs-a  ext4  768 MiB  read-only; kernel and device tree in /boot
 3  rootfs-b  ext4  768 MiB  read-only; the other slot
 4  data      ext4  rest     /data, writable; grown to fill the eMMC on first boot
```

The image holds a 128 MiB data partition. On first boot `eitri-grow-data`
rewrites the GPT so partition 4 reaches the end of the disk (and the backup GPT
moves there), and `eitri-data-init` grows the file system online. The reasoning
behind the raw offsets, the A/B state sector and the open boot-chain questions
are in [`docs/boot-chain.md`](docs/boot-chain.md).

**Deviation from the shared contract:** the contract lists a `/boot` partition.
Here each root filesystem slot carries its own kernel and device tree in its
`/boot` directory, so one RAUC image updates kernel and rootfs atomically and a
FAT file system is never rewritten by an update. The FAT partition is still
there, mounted read-only at `/boot/firmware`, and holds the user-editable boot
options.

## What is writable

The root filesystem is read-only, and almost nothing below it needs to change
at run time:

| Path | What | Where it really lives |
|---|---|---|
| `/data` | the application layer and all printer data | ext4 partition 4 |
| `/etc` | hostname, passwords, SSH host keys, Wi-Fi profiles, time zone | overlay: the read-only `/etc` below, `/data/eitri/state/etc/upper` above |
| `/var`, `/tmp`, `/run` | runtime state | tmpfs (journald is volatile) |
| `/boot/firmware` | boot options | FAT partition 1, mounted read-only |

`/etc` is a real overlay so that `hostnamectl`, `passwd`, `ssh-keygen -A`,
`nmcli` and `timedatectl` work unchanged, and what they write survives a reboot
and an OS update. If `/data` is missing or damaged, `eitri-etc` falls back to a
volatile `/etc` and the system still boots, so it can be reached and repaired;
the reason is written to `/run/eitri/degraded*`.

The mounts of `/data` and `/boot/firmware` follow the disk that was booted. U-Boot
passes `eitri.disk=/dev/mmcblkN`, and `eitri-generator` turns that into mount
units, so a bench boot from a microSD card uses that card's own data partition.

### Kernel command line interface

The boot script (`board/eitri-plus4/uboot.env`) and anything else that boots this
rootfs talk to userspace through these parameters:

| Parameter | Meaning |
|---|---|
| `eitri.disk=/dev/mmcblkN` | the disk that was booted; `/data` is its partition 4 and `/boot/firmware` its partition 1 |
| `eitri.data=SPEC`, `eitri.bootfs=SPEC` | name those partitions explicitly (`/dev/...`, `PARTUUID=`, `UUID=`, `LABEL=`); they win over `eitri.disk` |
| `eitri.bootmount=PATH` | where the FAT partition is mounted (default `/boot/firmware`) |
| `rauc.slot=A\|B` | the running slot, read by `rauc-bootloader` |

A boot manager with a different partition map, such as the in-place installer
with the stock U-Boot, passes `eitri.data=`, `eitri.bootfs=` and its own RAUC
configuration instead of `eitri.disk=`. It can ship that configuration by
writing `rauc/system.conf` into `/data/eitri/state/etc/upper/` (the persistent
`/etc` layer) before the first boot. For the stock U-Boot's benefit the rootfs
also offers the device tree as `/boot/dtb/rockchip/rk3328-roc-cc.dtb`.

### First boot

`eitri-data-init` runs on every boot after `/data` is mounted and only creates
what is missing: the whole tree from the shared contract (`firmware/`, `venv/`,
`printer_data/{config,logs,gcodes,database,comms}`, `eitri/{extras,klarity,
toolchain,configurator,home}`), the default `moonraker.asvc`, the camera
defaults, the `eitri/ui` link to Fluidd, and SSH keys found in
`/boot/firmware/authorized_keys`. It never overwrites a file. The `eitri` user
(uid 1000, groups `dialout`, `video`, `plugdev`, `tty`) is part of the image,
with its home in `/data/eitri/home`; it has no password, so login is by key.

## Services

| Unit | Source | Port |
|---|---|---|
| `klipper.service`, `moonraker.service` | this tree; start only when `/data` holds a Klipper clone, venv and config | 7125 |
| `klarity.service` | this tree, replaced by `klarity/systemd/klarity.service` when that exists | - |
| `eitri-camera.service`, `go2rtc.service` | `camera/systemd` through the `eitri-camera` package; go2rtc is disabled by default | 8080 (loopback), 1984, 8554 |
| `eitri-configurator.service` | this tree, replaced by `configurator/systemd/` when that exists | 8099 |
| `nginx.service` | Buildroot; `/etc/nginx/nginx.conf` here | 80: UI, `/configure/`, `/webcam/`, Moonraker proxy |
| `NetworkManager`, `sshd`, `avahi-daemon`, `chrony`, `rauc`, `polkit` | Buildroot packages | 22 for ssh |
| `eitri-zram`, `eitri-mark-good`, `eitri-grow-data`, `eitri-data-init`, `eitri-etc` | this tree | - |

The `eitri` user may start and stop the Eitri services, reboot, change the host
name and manage network profiles through polkit
(`usr/share/polkit-1/rules.d/50-eitri.rules`); it also has passwordless sudo.
Moonraker's `machine` component therefore works over D-Bus without extra setup.

The web UIs are installed under `/usr/share/eitri/ui/{fluidd,mainsail}`;
`/data/eitri/ui` selects the one nginx serves.

### Updates

Releases arrive as RAUC bundles: signed, `verity` format, one `rootfs.ext4`
image that contains the kernel. RAUC writes the inactive slot and makes it the
primary one. U-Boot gives each slot three boot attempts; `eitri-mark-good` marks
the running slot good only after D-Bus, NetworkManager, sshd, nginx and the
configurator are active and `/data` is mounted, so an image that cannot even
bring up the network rolls back by itself. Klipper and Moonraker are not part of
that check: the application layer on `/data` has its own rollback.

Switching to the other slot by hand, from a PC: put `eitri_force_slot=A` or `B`
into `eitri-boot.env` on the FAT partition.

## Which parts come from where

| In the image | From |
|---|---|
| kernel, device tree, bootloader | this tree: `board/eitri-plus4/{linux.fragment,uboot.*,dts/}` |
| ustreamer, go2rtc, uv, Katapult scripts, Fluidd, Mainsail | `package/`, pinned by SHA-256 or commit in the `.hash` files and `.mk` |
| camera units, udev rule (`/dev/eitri-cam`), nginx `/webcam/`, defaults | `camera/` through `package/eitri-camera` |
| configurator | `configurator/` through `package/eitri-configurator` (source tree in `/usr/lib/eitri/configurator`) |
| `eitri-mcuflash` | `installer/mcuflash` through `package/eitri-mcuflash` |
| Klipper, Moonraker, Klarity, the `qd_*` extras | not in the image: installed to `/data` by the installer and configurator |

The configurator also calls `/usr/libexec/eitri/switch-flavor` and
`/usr/libexec/eitri/toolchain`; those belong to the installer component and are
not provided here.

## Status

Checked here, without a printer:

- the defconfig resolves against Buildroot 2026.02.3 and every option applies
- the pins agree with `release.lock`, and every download is verified against
  the digest in its `.hash` file
- TF-A v2.15.0 and U-Boot v2026.10 build for the board, with and without the
  Rockchip DRAM blob; the boot script runs correctly in the U-Boot sandbox
- the kernel fragment survives Kconfig on 6.18.57, the kernel builds with it,
  and the device tree compiles without warnings
- RAUC accepts the custom backend (`rauc status`, `mark-active`), and a bundle
  built by `post-bundle.sh` carries the right manifest, is signed, and is
  rejected by a keyring from another build
- the partition layout, partition growth, `/data` setup, health check and the
  systemd generator behave as intended in the unit tests
- a complete `make -C os image` finishes: a 1.7 GiB disk image and a signed RAUC
  bundle. The root filesystem is 640 MiB with about 325 MiB used and passes
  `e2fsck`. The build uses Buildroot's own glibc toolchain with Linux 6.18
  headers; Buildroot's external Bootlin toolchain was tried first and could not
  build the host-side tools against its older headers
- systemd's network, resolver and time daemons are not built: NetworkManager
  and chrony own those jobs, and `/etc/resolv.conf` points to NetworkManager's file

Not done: everything that needs the board: DRAM
initialisation, the device tree (Ethernet PHY, USB power, SD regulators),
Wi-Fi, camera, MCU UARTs, the first boot, an update and a rollback. The open
questions are in [`docs/boot-chain.md`](docs/boot-chain.md).
