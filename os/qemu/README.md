# Eitri OS on QEMU

A boot smoke test and a measuring rig for the OS image, without a printer. The RK3328 board cannot be
emulated, but everything above the kernel can run on `qemu-system-aarch64` (machine `virt`): the same Buildroot
tree, the same rootfs overlay, the same packages, the same systemd units. Only the boot chain (TF-A, U-Boot,
genimage) is left out, and the kernel gets virtio, PL011 and the GICv3 on top of the printer's
`linux.fragment`.

```sh
make -C os buildroot          # once: the pinned Buildroot checkout in ../build/buildroot
os/qemu/build.sh              # Image + rootfs.ext4 in ../build/br-qemu/images (shares br-dl and br-ccache)
os/qemu/smoke-test.sh         # boot twice, measure, report in ../build/br-qemu/smoke/<time>/
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
| `/data` | partition 4 of the eMMC | a second virtio disk, blank ext4 labelled `data`, named by `eitri.data=/dev/vdb` |
| Console | none (ttyS2 is wired to an MCU) | PL011, with a bench password for the `eitri` user (`post-fakeroot.sh`) |
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

`smoke.py` starts `run-qemu.sh` with a blank `/data`, drives the serial console through a unix socket, logs
in, sends `guest-collect.sh` and parses what it prints. It then powers the guest off (timed), boots again on the
same `/data` and collects the timing and the failures a second time. Output in `<report>/`:

| File | Content |
|---|---|
| `summary.md`, `summary.json` | checks, `systemd-analyze time/blame/critical-chain`, `free -m`, resident (RSS) and proportional (PSS) memory per unit, the state of every Eitri unit |
| `guest-boot1.txt`, `guest-boot2.txt` | everything the guest printed: unit status and journals of the failed units, the `/data` layout, listening ports, HTTP probes, `dmesg` |
| `serial-boot*.log`, `qemu-boot*.out` | the raw console and QEMU's own output |

Exit status 0 means every check passed. Units that are allowed to be failed are listed with a reason in
`known-failures.txt`; the file is how a defect in a shared file is carried until it is fixed.

The units that cannot start here are reported, not failed:

* `klipper`, `moonraker`, `klarity`: skipped by `ConditionPathExists=` because nothing builds the data seed
  yet (STATUS.md, "Not built by anyone yet", item 1). `systemctl status` shows `inactive (dead)`,
  `start condition unmet`; no process is ever created and nothing is logged as an error. Their paths:
  `/data/firmware/{klipper,moonraker}`, `/data/venv/{klippy,moonraker}`, `/data/eitri/klarity/venv`,
  `printer.cfg`, `moonraker.conf`, `klarity.cfg`.
* `eitri-camera`: there is no UVC camera. The unit waits 20 s for `/dev/eitri-cam`, fails, restarts, and so on.
  `go2rtc` is disabled in the preset.
* The screen (`klarity`'s UART) and the MCUs have no counterpart, so the configurator's detection step finds nothing.

## Options

`run-qemu.sh`: `MEM` (default 1024; the board reports 976), `SMP`, `DATA_SIZE`, `APPEND`, `VERBOSE=1` (kernel
and systemd output on the console), `SSH_PORT`, `HTTP_PORT`, `CONFIGURATOR_PORT`, `MOONRAKER_PORT` (forwarded on
127.0.0.1; defaults 2222, 8088, 8099, 7125), `--fresh` (new blank `/data`), `--socket PATH`.
`build.sh`: `BUILD_ROOT`, `OUT`, `BR2_DL_DIR`, `BR2_CCACHE_DIR`, `EITRI_QEMU_PASSWORD`.
`smoke.py`: `--no-second-boot`, `--expect-failed UNIT`, `--mem`, `--smp`, `--out`, timeouts.

Log in by hand on the console as `eitri` (password `eitri` unless changed). The image is a bench image and must
never be written to a printer. SSH is forwarded to port 2222; password login there was not exercised.
