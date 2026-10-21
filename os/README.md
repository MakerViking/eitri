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
| `eitri-plus4.img` | raw disk image, about 2.3 GiB with the data seed (1.7 GiB without); write it to the start of the eMMC or a microSD card |
| `eitri-plus4-<version>.raucb` | signed update bundle for an installed printer |
| `../bench/eitri-sd-bench.img` | not built by `make image`: `os/bench/mk-sd-bench.sh` makes a microSD card that the printer's **stock** U-Boot boots, leaving the eMMC untouched ([`bench/README.md`](bench/README.md)) |
| `idbloader.img`, `u-boot.itb`, `Image`, `rk3328-eitri-plus4.dtb`, `rootfs.ext4` | the parts |

Host prerequisites beyond Buildroot's own list: `python3` 3.11 or newer, `rsync`,
`unzip`, `git`, `zstd`, and for the RAUC bundle `openssl`. `make -C os help` lists every target.

### The data seed in the image

The disk image's `/data` partition is **populated when the image is built**: `post-image.sh` installs the
data seed (pinned Klipper, Moonraker and Klarity with their Python environments and the `qd_*` extras, see
[`../seed/SPEC.md`](../seed/SPEC.md)) into the partition with `board/eitri-plus4/mkdata.sh`, so a printer
flashed with it reaches Klipper, Moonraker and Klarity on its first boot with no network. Build the seed
first (`make -C seed build`, output in `../build/seed/out`); then:

| Variable | Meaning |
|---|---|
| `EITRI_SEED_DIR` | where the seed was built (default `<build root>/seed/out`; exactly one `data-seed-*-<flavor>.tar.zst` there) |
| `EITRI_SEED_ARCHIVE` | the seed to use, when there are several; its `.manifest.json` must sit next to it |
| `EITRI_SEED_FLAVOR` | `klipper` (default) or `kalico`: the flavor the image starts with. The other one is installed later by the configurator or `eitri-update` |
| `EITRI_NO_SEED=1` | an empty `/data` for OS work (the three services stay off); refused with `EITRI_RELEASE=1`, and a release without a seed fails |

Why at build time and not at the first boot (SPEC 11.1 offers both): an archive in the root file system would
sit in **both** RAUC slots (about 55 MiB each of a 640 MiB file system that is already half full) and in every
update bundle, and the first boot would spend its first minute unpacking about 380 MiB on a Cortex-A53 before
Klipper could start. The cost here is the larger `data` partition of the flash-once image, which a RAUC
bundle never contains (seeds follow Klipper through data bundles, not the OS). The files must belong to
uid/gid 1000, so `post-image.sh` runs `mkdata.sh` under `fakeroot` (the one from Buildroot's host tools, else
`PATH`), which installs the seed and builds the ext4 file system in one session.

Two other ways in exist and are not used together with this one: the in-place installer's stage 2 installs the
seed onto the new `/data` itself (DOS boot backend, the user default), and `eitri-seed.service` unpacks an
archive that an image carries in `/usr/share/eitri/seed/` (`data-seed-<version>-<flavor>.tar.zst` plus its
`.manifest.json`; bench and QEMU images that opt in). Both use the same tool, `eitri-seed-install`, which is
in the image as `/usr/libexec/eitri/eitri-seed-install`.

`/data/eitri/extras` and `/data/eitri/klipper-layer` come from the seed and follow Klipper and Kalico
(decision 2). The image no longer contains `/usr/lib/eitri/klipper-layer` (package `eitri-klipper-layer`
is gone); the configurator finds the layer on `/data`, and a build that still has the old directory in its
target tree removes it.

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

The image holds a data partition of the seed's size plus 128 MiB (a seedless bench image: 128 MiB). On first boot `eitri-grow-data`
rewrites the GPT so partition 4 reaches the end of the disk (and the backup GPT
moves there), and `eitri-data-init` grows the file system online. `eitri-grow-data`
only modifies the disk that holds the running root file system; on a DOS disk
booted by the in-place `boot.scr` it acts only with `eitri.growdata=1` (the bench
microSD card) and then enlarges the extended partition and the last logical one. The reasoning
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
| `eitri.growdata=1` | on a DOS disk: let `eitri-grow-data` enlarge the `eitri.data=PARTUUID=` partition (the bench microSD card) |

A boot manager with a different partition map, such as the in-place installer
with the stock U-Boot, passes `eitri.data=`, `eitri.bootfs=` and its own RAUC
configuration instead of `eitri.disk=`. It can ship that configuration by
writing `rauc/system.conf` into `/data/eitri/state/etc/upper/` (the persistent
`/etc` layer) before the first boot. For the stock U-Boot's benefit the rootfs
also offers the device tree as `/boot/dtb/rockchip/rk3328-roc-cc.dtb`.
Whatever the slots say, `rauc-pre-install` stops any installation into a slot
that is not on the disk holding the running root file system, and
`rauc-bootloader` only writes its raw state sector when `eitri.disk=` names the
disk it lives on.

### First boot

`eitri-data-init` runs on every boot after `/data` is mounted and only creates
what is missing: the whole tree from the shared contract (`firmware/`, `venv/`,
`printer_data/{config,logs,gcodes,database,comms}`, `eitri/{extras,klarity,
toolchain,configurator,home}`), the default `moonraker.asvc`, the camera
defaults, `eitri/ui/current` (the link to Fluidd that nginx serves) and the service user's `eitri/flavor` and `eitri/channel`, and SSH keys found in
`/boot/firmware/authorized_keys`. It never overwrites a file and adopts a
`/data` that was populated with the data seed (it only chowns the seed's parent
directories to `eitri` if a root-run installer made them). The `eitri` user
(uid 1000, groups `dialout`, `video`, `plugdev`, `tty`) is part of the image,
with its home in `/data/eitri/home`; it has no password, so login is by key.

The data seed is not unpacked by `eitri-data-init` (see above). `eitri-seed.service` runs right after it
and before `eitri-prepare`, Klipper, Moonraker, Klarity and the configurator: it logs that a seed is
installed (`/data/eitri/state/seed-installed.env`), installs the archive of the image if there is one and
none is installed yet, or notes in `/run/eitri/no-seed` why Klipper, Moonraker and Klarity stay off (their
`ConditionPathExists=` are the seed's own files, so a missing seed shows as "start condition unmet", and
now also as this line in the journal). A failed installation fails the unit and shows in `systemctl --failed`.
Klipper and Moonraker start when the seed and the configurator's `printer.cfg` / `moonraker.conf` exist;
Klarity when its environment and `klarity.cfg` exist.

### Optional shell access

Off by default (decision 4): a fresh image has no password and takes only the keys dropped on the boot
partition. The configurator's optional step calls `sudo -n /usr/libexec/eitri/eitri-access`:

| Command | Does |
|---|---|
| `set-key` | reads one OpenSSH public key on stdin (bare key, a known type, strict parse; no authorized_keys options) and adds it for `eitri`, up to 8 |
| `set-password [--ssh-login]` | reads the password on stdin (8 to 128 characters), sets it for `eitri` (yescrypt, else SHA-512). It gives general `sudo` and a console login. SSH password login is a separate choice: only with `--ssh-login` |
| `disable` | removes the added keys and the password and turns SSH password login off |
| `status` | JSON: keys (type, fingerprint, comment), password set / SSH login, `generation`, `applied`, `pending` (no privileges needed) |

Each is one fixed sudoers rule (`etc/sudoers.d/eitri`; sudo matches the arguments exactly). The callers
(configurator, Klarity) run in a sandbox whose `/etc` is read-only, and a sudo child inherits that, so the
tool only records the wanted state in `/data/eitri/state/access/` and bumps `generation`; `eitri-access.path`
starts `eitri-access.service`, which runs `eitri-access apply` with a normal view of `/etc` (writes the hash into
`/etc/shadow`, reloads sshd) and publishes `applied`. The unit also runs at every boot before sshd. The tool waits
up to 20 s for it and exits 4 ("saved, not applied yet") otherwise. Added keys live in the root-owned
`/data/eitri/state/access/authorized_keys`, which `sshd_config` reads next to `~/.ssh/authorized_keys`, so
`disable` never touches the keys from the boot partition; `sshd_config` includes
`/data/eitri/state/access/sshd.conf` first (sshd uses the first value it sees), which says
`PasswordAuthentication yes` only when asked. sshd's `StrictModes` needs `/data` itself to be owned by root and not
writable by others (the image sets that).

`/usr/bin/eitri-configurator pairing|unlock|lock` is the command Klarity's "Unlock setup" runs
(`sudo -n /usr/bin/eitri-configurator unlock`). Because sudo runs it as root for the `eitri` account, it runs
the configurator's copy in the read-only image (`/usr/lib/eitri/configurator`) under `python3 -I`, never the
virtual environment on `/data`, and accepts exactly one of the three words. (Its own code drops to the `eitri` user before touching files and starts the service through polkit, so the root part is only the `sudo` hop that lets Klarity's sandbox reach it.)

## Services

| Unit | Source | Port |
|---|---|---|
| `klipper.service`, `moonraker.service` | this tree; start only when `/data` holds the seed's Klipper/Moonraker clone and venv and the configurator's config | 7125 (loopback) |
| `klarity.service` | `klarity/systemd`, installed by `post-build.sh` | - |
| `eitri-camera.service`, `go2rtc.service` | `camera/systemd` through the `eitri-camera` package; go2rtc is disabled by default | 8080 (loopback), 1984, 8554 |
| `eitri-configurator.service` | `configurator/systemd`, installed by `post-build.sh`; the OS drop-in `20-eitri-os.conf` adds `--retire-when-locked`: the process exits once setup is complete and stays stopped until `eitri-configurator unlock` and `systemctl start` (Klarity's button) | 8099 |
| `nginx.service` | Buildroot; `/etc/nginx/nginx.conf` here | 80: UI, `/configure/`, `/webcam/`, Moonraker proxy |
| `NetworkManager`, `sshd`, `avahi-daemon`, `chrony`, `rauc`, `polkit` | Buildroot packages | 22 for ssh |
| `eitri-zram`, `eitri-mark-good`, `eitri-grow-data`, `eitri-data-init`, `eitri-etc`, `eitri-seed`, `eitri-access` (+ `.path`) | this tree | - |

The `eitri` user may start and stop the Eitri services, reboot, change the host
name and manage network profiles through polkit
(`usr/share/polkit-1/rules.d/50-eitri.rules`). Its sudo rights are scoped:
`eitri-update`, `eitri-bootctl`, the five fixed `eitri-access` command lines and
`eitri-configurator unlock` without a password, anything else only with
the account's own password, which is locked until its owner sets one
(`etc/sudoers.d/eitri`; sudo applies the *last* matching rule, so the broad rule is first). The
configurator and Klarity units therefore cannot set `NoNewPrivileges` (sudo is setuid) and cannot use any sandbox option that systemd
implements with seccomp (`SystemCallFilter`, `ProtectKernelTunables`, `RestrictNamespaces`, `LockPersonality`, ...): systemd sets
`NoNewPrivs` on such a service anyway, which was measured on the QEMU image and makes sudo refuse to run. Klipper and Moonraker keep
`NoNewPrivileges`. The account runs several network-facing programs, so
it must not be a blanket root; bench images get a separate passwordless drop-in.
Moonraker's `machine` component works over D-Bus without extra setup. sshd takes
public keys only, with no TCP forwarding. nginx answers the local network only
(`etc/nginx/eitri-lan-only.conf`: loopback, private and link-local IPv4; nginx has no IPv6 listener at all, so
IPv6 clients are not served) and accepts bodies above 1 MiB only
on the G-code upload routes.

The web UIs are installed under `/usr/share/eitri/ui/{fluidd,mainsail}`;
`/data/eitri/ui/current` (a symlink in a directory of the service user's, since `/data/eitri` itself is root's) selects the one nginx serves.

### Updates

Releases arrive as RAUC bundles: signed, `verity` format, one `rootfs.ext4`
image that contains the kernel. RAUC writes the inactive slot and makes it the
primary one. U-Boot gives each slot three boot attempts. The slot is committed
in exactly one place, `eitri-verify.service` (installed with `eitri-update`):
it runs after `eitri-prepare` (pending data bundles, first-boot MCU flash),
Klipper and Moonraker, and first requires the OS to be healthy
(`mark-good --check`: D-Bus, NetworkManager, sshd, nginx and the configurator
active, `/data` mounted; a drop-in in `eitri-verify.service.d`). An image that
cannot bring up the network, or whose Klipper cannot reach its MCUs, therefore
never commits and rolls back by itself. `eitri-mark-good.service` commits on its
own only on images without `eitri-update`; with it installed, the unit is
skipped (`ConditionPathExists=!/usr/bin/eitri-update`), because committing any
earlier would refill the boot attempts on every boot and make the fallback
unreachable.

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
| Klipper, Moonraker, Klarity, the `qd_*` extras and the klipper-layer config set | not in the image: the data seed (`seed/`) puts them on `/data` (populated at image build time, by the in-place installer, or by `eitri-seed.service`) |
| `eitri-seed-install`, `eitri-access`, `eitri-configurator` (command), `seed-first-boot` | `seed/bin/`, and this tree's `rootfs-overlay/usr/libexec/eitri` and `usr/bin` |

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
- a complete `make -C os image` with the klipper-flavor data seed finishes (2.2 GiB disk image, signed bundle
  of the same 119 MiB as before): `data.ext4` is 609 MiB, `e2fsck` is clean, the partition table is unchanged,
  `/` and `/eitri` are root's, the application layer is 1000:1000, the C helper is still one second newer than
  its sources, the image has `libsodium.so.26`, the new units are enabled by the preset and
  `systemd-analyze verify` finds no ordering or syntax problem. The first boot of that image is not tested
  (QEMU smoke test: next step); `eitri-access`, the sudoers file (`visudo -c`), `sshd -T` with the new
  `Include`, `mark-good` with a retired configurator and `seed-first-boot` are covered by unit tests
- systemd's network, resolver and time daemons are not built: NetworkManager
  and chrony own those jobs, and `/etc/resolv.conf` points to NetworkManager's file

Not done: everything that needs the board: DRAM
initialisation, the device tree (Ethernet PHY, USB power, SD regulators),
Wi-Fi, camera, MCU UARTs, the first boot, an update and a rollback. The open
questions are in [`docs/boot-chain.md`](docs/boot-chain.md).
