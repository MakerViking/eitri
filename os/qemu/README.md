# Eitri OS on QEMU

A boot smoke test and a measuring rig for the OS image, without a printer. The RK3328 board cannot be
emulated, but everything above the kernel can run on `qemu-system-aarch64` (machine `virt`): the same Buildroot
tree, the same rootfs overlay, the same packages, the same systemd units. Only the boot chain (TF-A, U-Boot,
genimage) is left out, and the kernel gets virtio, PL011 and the GICv3 on top of the printer's
`linux.fragment`.

```sh
make -C os buildroot          # once: the pinned Buildroot checkout in ../build/buildroot
make seed                     # once: the data seeds in ../build/seed/out (os/qemu needs the klipper one)
os/qemu/build.sh              # Image + rootfs.ext4 in ../build/br-qemu/images (shares br-dl and br-ccache)
os/qemu/smoke-test.sh         # boot twice, run the first-run setup, measure, report in ../build/br-qemu/smoke/<time>/
os/qemu/run-qemu.sh           # boot with the console on this terminal (Ctrl-A X quits)
```

A cold build took about 30 minutes on 24 cores with a warm download cache and ccache (toolchain, kernel and
all packages). A rebuild after a change to the overlay or a script takes seconds.

## What is the same, what is different

| | Printer image | QEMU image |
|---|---|---|
| Buildroot, `BR2_EXTERNAL`, packages, rootfs overlay, post-build, users, units, presets | `configs/eitri_plus4_defconfig` | the same file, run through `mkdefconfig.py` with `qemu.fragment` |
| Kernel | 6.18.57, `linux.fragment` | the same fragment + `linux-qemu.fragment` (virtio-mmio, PL011, PL031, GICv3) |
| Boot chain, disk image, RAUC bundle | TF-A, U-Boot, genimage | none; QEMU loads the kernel, the rootfs is a raw ext4 file |
| `/data` | partition 4 of the eMMC, populated from the data seed when the image is built (`mkdata.sh`) | a second virtio disk, ext4 labelled `data`, built by the same `mkdata.sh` from the same data seed (`SEED_FLAVOR`, default `klipper`; `none` = blank), named by `eitri.data=/dev/vdb` |
| Console | none (ttyS2 is wired to an MCU) | PL011, with a QEMU-only `bench` user (passwordless sudo) and a bench password for `eitri` (`post-fakeroot.sh`); `eitri` keeps the image's own scoped sudoers rules so the smoke test exercises them |
| MCUs, touchscreen | `/dev/ttyS0`, `/dev/ttyS2`, `/dev/ttyS1` | none: Klipper cannot connect and Klarity waits for its port |
| Network | gmac2phy, 100 Mbit | virtio-net behind user-mode NAT with `restrict=on`: the guest reaches nothing outside the host |
| CPU | 4 x Cortex-A53 | 4 x emulated Cortex-A53 under TCG (no KVM on an x86 host) |

`qemu.fragment` is the only place that lists what is removed from the printer's defconfig (`#DROP` lines) and
what is added. `build.sh` also checks that every line of both kernel fragments survived Kconfig.

## What the numbers mean

QEMU runs the guest in software. A `.service` start that takes 2 s here does not take 2 s on the board, and
the board adds U-Boot and real device probing in front of the kernel. Use the numbers to compare one change with
the next, to find what a boot waits for (the critical chain, the blame list, services that block
`multi-user.target`) and to size memory: the resident sizes of the processes and the kernel's memory use do not
depend on the CPU. They say nothing about the camera, the screen, the UARTs, the MCUs, Ethernet or eMMC.

## The smoke test

`smoke.py` starts `run-qemu.sh` with a `/data` disk built from the data seed (the flash-once image's path:
`seed/bin/eitri-seed-install` into a scratch tree, then `mke2fs -d`), drives the serial console through a unix
socket, logs in as `bench`, sends `guest-collect.sh` and parses what it prints. Boot 1 then does what a person does on the
first run, **against the real services, over the configurator's HTTP API**:

1. reads the pairing code from `/data/eitri/configurator/pairing.json` (what Klarity shows) and pairs;
2. detects (no MCUs exist, so it reports that and finishes), and **applies**: the configurator renders `printer.cfg`,
   `klarity.cfg`, `moonraker.conf` and `eitri-access.conf` from the layer on `/data`, links the `qd_*` extras into the
   Klipper clone with `overlay-install.sh`, and restarts Klipper, Moonraker and Klarity (which were skipped at boot
   because their configuration did not exist);
3. checks that Klipper, Moonraker and Klarity are active and not being restarted; that Moonraker answers
   `/server/info` on `127.0.0.1:7125` and through nginx on port 80, and not on the guest's network address
   (loopback only); that klippy loaded `printer.cfg` (with the `qd_*` extras, all links to `/data/eitri/extras`) and
   reports the missing MCU;
4. adds an SSH key in the optional remote-access step (`sudo -n eitri-access`, `eitri-access.path`), logs in over SSH
   with it from the host, removes it and checks that the login is refused;
5. finishes setup, waits for the configurator to **retire**, runs `sudo -n /usr/bin/eitri-configurator unlock` as the
   `eitri` user under the image's own sudoers rule (what Klarity's "Unlock setup" runs), checks the new pairing code,
   locks again and waits for it to retire again.

It then powers the guest off (timed) and boots again on the same `/data`: Klipper, Moonraker and Klarity must start by
themselves and the configurator must stay retired; timing and per-service memory with the application layer running
are collected on that boot. Output in `<report>/`:

| File | Content |
|---|---|
| `summary.md`, `summary.json` | checks, `systemd-analyze time/blame/critical-chain`, `free -m`, resident (RSS) and proportional (PSS) memory per unit, the state of every Eitri unit, the first-run setup steps |
| `guest-boot1-pre-setup.txt`, `guest-boot1-after-setup.txt`, `guest-boot2-steady.txt` | everything the guest printed: unit status and journals of the failed units, the `/data` layout, listening ports, HTTP probes, the klippy log, `dmesg` |
| `apply-log.txt` | the log of the configurator's Apply job |
| `serial-boot*.log`, `qemu-boot*.out` | the raw console and QEMU's own output |

Exit status 0 means every check passed. Units that are allowed to be failed are listed with a reason in
`known-failures.txt`; the file is how a defect in a shared file is carried until it is fixed.

What QEMU cannot give the guest, and what that looks like:

* `klippy` has no MCU: `/dev/ttyS2` and `/dev/ttyS0` do not exist. Klippy loads the configuration (so every `qd_*`
  module is imported), tries to open the ports, reports it in its log and Moonraker's `/printer/info`, and keeps running.
* `klarity` has no touchscreen UART (`/dev/ttyS1`): it waits for the port.
* `eitri-camera`: there is no UVC camera; `ConditionPathExists=/dev/eitri-cam` skips it. `go2rtc` is disabled in the preset.
* `eitri-prepare` and `eitri-verify` fail because QEMU boots without a bootloader (no `eitri.slot=`); see `known-failures.txt`.

## Options

`run-qemu.sh`: `MEM` (default 1024; the board reports 976), `SMP`, `DATA_SIZE` (only for a blank `/data`), `SEED_FLAVOR`
(`klipper`, `kalico`, `none`), `SEED_DIR`, `APPEND`, `VERBOSE=1` (kernel and systemd output on the console), `SSH_PORT`,
`HTTP_PORT`, `CONFIGURATOR_PORT` (forwarded on 127.0.0.1; defaults 2222, 8088, 8099; Moonraker is loopback-only and reached
through nginx), `--fresh` (new `/data` from the seed), `--socket PATH`.
`build.sh`: `BUILD_ROOT`, `OUT`, `BR2_DL_DIR`, `BR2_CCACHE_DIR`, `EITRI_QEMU_PASSWORD`.
`smoke.py`: `--no-second-boot`, `--seed-flavor klipper|kalico`, `--expect-failed UNIT`, `--mem`, `--smp`, `--out`, timeouts.

Log in by hand on the console as `eitri` or `bench` (password `eitri` unless changed). The image is a bench image and must
never be written to a printer. SSH is forwarded to port 2222; the smoke test logs in over it with a key added through the
configurator's remote-access step.
