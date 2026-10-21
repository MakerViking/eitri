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
| `eitri_hide_emmc` | bench microSD card only (`os/bench/mk-sd-bench.sh`): `1` = disable the eMMC controller `/mmc@ff520000` in the device tree before `booti` (`fdt addr/resize/set`); no Eitri boot if that fails, and stage 2 is never started. The in-place install never writes it. |

`/eitri/boot.env` - the mutable state, rewritten by U-Boot (`env export` + `fatwrite`) and by `eitri-bootctl`:

| key | values |
|---|---|
| `eitri_mode` | `eitri` boot a slot, `stage2` run the RAM installer (bounded by `eitri_s2_tries`), `stock` boot the stock entry |
| `eitri_slot` | `A` or `B` |
| `eitri_tries` | `3 2 1 0`: boot attempts left for that slot |
| `eitri_state_A`, `eitri_state_B` | `good`, `bad`, `unknown` |
| `eitri_s2_tries` | `3 2 1 0`: starts of the RAM installer left while `eitri_mode=stage2` (absent otherwise) |

Only six variables are exported because U-Boot's command line is limited to 16 arguments (found the hard way in
the sandbox test: a longer `env export` list silently prints usage).

Both files are imported with an explicit variable list (`env import -t ADDR SIZE var...`): `boot.env` with the six
state variables (`${eitri_keys}`), `layout.env` with the table above (two imports, to stay under 16 arguments). A
hand-edited file cannot set anything else, in particular not `eitri_dev` (where `fatwrite` writes) or `eitri_save`
(what `run` executes). The lists are `BOOT_SCR_STATE_KEYS` and `LAYOUT_KEYS` in `eitri-update/eitri_update/bootenv.py`;
`tests/test_boot_script.py` checks that `boot.cmd` and every writer of the files agree with them.

## State machine

```
mode=stage2, s2_tries>0 ──boot.scr──► (s2_tries-- written BEFORE the kernel starts; mode stays stage2) ──► RAM installer
                                  │ installer finishes: mode=eitri slot=A tries=3 (no s2_tries)
                                  │ installer fails in a handled way: mode=stock
                                  │ power cut / hang: mode is still stage2 -> next boot starts it again (resumes), until s2_tries=0 -> mode=stock
mode=eitri, tries>0 ──► tries-- (written first), ext4load slot kernel, booti
                                  │ userspace healthy: eitri-update verify → state_X=good, tries=3
tries=0 (kernel never confirmed) ──► state_X=bad; other slot good? boot it : boot stock
mode=stock / no boot.env / anything fails ──► sysboot extlinux.conf.stock
```

Safety properties (each covered by `tests/test_boot_script.py`, which runs the script in a real U-Boot):
* a hanging kernel can be escaped: the counter is persisted before `booti`, and if the write fails
  the script does not boot Eitri at all;
* an interrupted conversion or restore is not a dead end: the RAM installer is armed with a start counter
  (`install.sh`: 2, `revert.sh --full`: 3), not a one-shot flag, so a power cut re-enters it and it resumes from
  its phase markers; when the starts are spent (or the installer's files will not load) the mode falls back to `stock`;
* a slot that never gets confirmed is retired after three attempts; a confirmed other slot takes over;
* every path that does not reach `booti` ends in the stock boot.

## RAUC

Two layouts exist and `eitri-update` (`bootenv.BootControl`) handles both behind one interface:

* **in-place conversion** (this document; the **default and the only path users are given**): state in
  `/boot/eitri/boot.env`; RAUC calls `eitri-bootctl`. It keeps QIDI's U-Boot and the stock system.
* **flash-once GPT image** (bench boards, microSD cards and CI only; built by `os/`, replaces the boot loader): no `boot.env`; U-Boot keeps `BOOT_ORDER`/`BOOT_<slot>_LEFT` in one raw eMMC sector owned by
  `/usr/libexec/eitri/rauc-bootloader` (the kernel command line carries `rauc.slot=`). That script only knows
  "attempts left", so a never-written or unproven slot looks `good` to it. `BootControl` therefore reports `good` only for
  slots with a recorded healthy boot (`/data/eitri/state/slot-confirmed.json`, written by `eitri-update`'s verify/confirm, cleared
  when a new system is written into the slot), and rollback never targets an unconfirmed slot.

In-place RAUC configuration: RAUC uses the `custom` bootloader backend (`eitri-update/systemd/rauc-system.conf.snippet`):
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
