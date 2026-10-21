# Boot selection

The stock U-Boot (2021.07-armbian, in the eMMC's reserved sectors) is **never replaced** by the in-place
installer. It tries `extlinux/extlinux.conf` first and `boot.scr` second (`boot_scripts=boot.scr.uimg boot.scr`,
`boot_prefixes=/ /boot/`). The installer renames the stock `extlinux.conf` to `extlinux.conf.stock` and installs
`boot.scr` (compiled from `boot/boot.cmd` by `boot/mkscr.py`, no `mkimage` needed); from then on `boot.scr`
decides, and its last lines `sysboot` the stock entry. Removing `boot.scr` and renaming the file back is the
complete undo (`revert.sh --hard`).

## Files on the vfat `/boot` partition

`/eitri/layout.env` - written once, U-Boot only reads it:

| key | example |
|---|---|
| `eitri_part_A`, `eitri_part_B` | `5`, `6` (dos logical partitions) |
| `eitri_partuuid_A`, `eitri_partuuid_B` | `6a783df4-05`, `6a783df4-06` |
| `eitri_cmdline` | `console=tty1 loglevel=4 panic=10 net.ifnames=0` |
| `eitri_s2_kernel/fdt/initrd/args` | stock `/Image`, stock dtb, `/eitri/stage2/initramfs.cpio.gz`, `…eitri.op=convert` |

`/eitri/boot.env` - the mutable state, rewritten by U-Boot (`env export` + `fatwrite`) and by `eitri-bootctl`:

| key | values |
|---|---|
| `eitri_mode` | `eitri` boot a slot, `stage2` run the one-shot RAM installer once, `stock` boot the stock entry |
| `eitri_slot` | `A` or `B` |
| `eitri_tries` | `3 2 1 0`: boot attempts left for that slot |
| `eitri_state_A`, `eitri_state_B` | `good`, `bad`, `unknown` |

Only five variables are exported because U-Boot's command line is limited to 16 arguments (found the hard way in
the sandbox test: a longer `env export` list silently prints usage).

## State machine

```
mode=stage2 ──boot.scr──► (flag consumed and written BEFORE the kernel starts) ──► RAM installer
                                  │ installer finishes: mode=eitri slot=A tries=3
mode=eitri, tries>0 ──► tries-- (written first), ext4load slot kernel, booti
                                  │ userspace healthy: eitri-update verify → state_X=good, tries=3
tries=0 (kernel never confirmed) ──► state_X=bad; other slot good? boot it : boot stock
mode=stock / no boot.env / anything fails ──► sysboot extlinux.conf.stock
```

Safety properties (each covered by `tests/test_boot_script.py`, which runs the script in a real U-Boot):
* a hanging kernel can be escaped: the counter/one-shot flag is persisted before `booti`, and if the write fails
  the script does not boot Eitri at all;
* a slot that never gets confirmed is retired after three attempts; a confirmed other slot takes over;
* every path that does not reach `booti` ends in the stock boot.

## RAUC

RAUC uses the `custom` bootloader backend (`eitri-update/systemd/rauc-system.conf.snippet`):
`get-primary`, `set-primary SLOT` (slot + 3 tries + mode eitri; state untouched), `get-state`, `set-state`
(good restores 3 tries; marking the booted slot bad forces the fallback on the next boot), `get-current`
(`eitri.slot=` on the kernel command line). Slot bootnames are `A`/`B`.

## Kernel command line

`boot.scr` passes `root=PARTUUID=<label-id>-0N ro rootwait rootfstype=ext4 eitri.slot=X <eitri_cmdline>`.
No serial console: the DT's stdout-path would be `ttyS2`, which is an MCU UART here; `console=tty1` overrides it
(`install.sh` refuses an `install.json` whose `kernel_cmdline` names a serial console).

`install.sh` appends `eitri.data=LABEL=eitri-data eitri.bootfs=PARTUUID=<label-id>-01` to `eitri_cmdline`: the OS image's
mount generator takes `/data` and the FAT partition from those arguments (a dos disk has no GPT names to derive them
from). The FAT partition is then mounted read-only at `/boot/firmware`; `eitri-bootctl`, `eitri-update` and `revert.sh`
find `eitri/boot.env` there (or in `/boot` on the stock system) and remount it read-write only while they write.
On a conversion, stage 2 also rewrites RAUC's slot devices and backend for this layout, see BUNDLE.md.

## Partition layout after conversion

```
sector 64      idbloader, 16384 u-boot.itb      untouched stock bootloader
p1  /boot      vfat 256 MiB, untouched          + boot.scr, eitri/, stock kernel stays for the RAM installer
p2  stock root ext4, shrunk (resize2fs offline) the way back; its Armbian resize service is masked
p3  extended
p5  eitri-rootA   read-only ext4 slot A
p6  eitri-rootB   slot B (empty until the first OS update)
p7  eitri-data    ext4 /data (LABEL=eitri-data)
```
A mainline U-Boot (`u-boot-rockchip.bin` at sector 64) is a separate, optional, riskier step that the in-place
installer deliberately does not perform; the flash-once image uses it.
