# SPDX-License-Identifier: Apache-2.0
#
# Eitri boot manager for the QIDI X-Plus 4 (Firefly ROC-RK3328-CC).
#
# Installed as /boot/boot.scr on the vfat boot partition.  It only runs because the
# installer renamed extlinux/extlinux.conf to extlinux.conf.stock: the stock
# U-Boot (2021.07-armbian) tries extlinux first, then boot.scr.  Everything it
# needs is in the distro-boot environment that stock U-Boot already provides
# (devtype, devnum, distro_bootpart, *_addr_r) and a handful of standard commands
# (load, ext4load, env import/export, fatwrite, booti, sysboot; fdt on the bench card).
#
# Two key=value text files on the vfat partition:
#   /eitri/layout.env  written once by the installer, never rewritten by U-Boot:
#       eitri_part_A/B, eitri_partuuid_A/B  partition number and PARTUUID of each slot
#       eitri_cmdline                       extra kernel arguments
#       eitri_s2_kernel/fdt/initrd/args     the one-shot RAM installer
#       eitri_hide_emmc=1                   only on the bench microSD card (os/bench/mk-sd-bench.sh): the eMMC
#                                           controller is disabled in the device tree handed to Linux
#   /eitri/boot.env    the mutable state, rewritten by U-Boot and by eitri-bootctl:
#       eitri_mode     eitri | stage2 | stock
#       eitri_slot     A | B               rootfs slot to boot
#       eitri_tries    3 2 1 0             boot attempts left for that slot
#       eitri_state_A/B  good | bad | unknown
#       eitri_s2_tries 3 2 1 0     RAM-installer starts left while eitri_mode=stage2 (absent otherwise)
#   (Only these six state variables are exported: U-Boot's command line is limited
#   to 16 arguments, and `env export -t ADDR` plus a longer list would exceed it.)
#   Both files are imported with an explicit variable list: nothing else in them is taken over, so a hand-edited
#   file cannot set eitri_dev, eitri_save, eitri_keys or anything of U-Boot's own (where fatwrite goes, what runs).
#   (Without -d, a listed variable that the file lacks keeps the value it had before the import.)
#
# Safety rules:
#  * Every path that does not end in a successful `booti` falls through to the
#    last lines, which boot the stock system.
#  * The boot counter / installer start counter is written BEFORE the kernel is started.  If
#    that write fails we do not boot Eitri at all - otherwise a kernel that hangs
#    could never be escaped.
#  * The RAM installer is armed with a start counter, not a one-shot flag: a power cut in the middle of the
#    conversion (or of a restore) must re-enter the installer, which resumes from its phase markers, instead
#    of dropping to a half-converted stock disk.  The counter bounds that: each start spends one, and when it
#    is gone the mode falls back to stock.
#  * Userspace (eitri-bootctl mark-good) resets the counter once the system is up.

# The stock U-Boot's distro environment normally sets fdtfile; if it does not, this board's name is the right default.
if test "x${fdtfile}" = "x"; then
    setenv fdtfile rockchip/rk3328-roc-cc.dtb
fi
setenv eitri_buf ${pxefile_addr_r}
setenv eitri_dev ${devtype} ${devnum}:${distro_bootpart}
setenv eitri_mode stock
setenv eitri_keys "eitri_mode eitri_slot eitri_tries eitri_state_A eitri_state_B eitri_s2_tries"
setenv eitri_save 'env export -t ${eitri_buf} ${eitri_keys} && fatwrite ${eitri_dev} ${eitri_buf} /eitri/boot.env ${filesize}'

# Diagnostics only: /eitri/runs.env gains one "x" every time this script runs. Read from Linux after a boot that never
# reached Linux (a hung warm reboot), it tells whether that boot still got as far as this script. Nothing reads it here.
setenv eitri_runs
if load ${eitri_dev} ${eitri_buf} /eitri/runs.env; then
    env import -t ${eitri_buf} ${filesize} eitri_runs
fi
setenv eitri_runs "${eitri_runs}x"
env export -t ${eitri_buf} eitri_runs && fatwrite ${eitri_dev} ${eitri_buf} /eitri/runs.env ${filesize}

# Only layout.env may switch the eMMC hiding on.
setenv eitri_hide_emmc
# Two imports for layout.env so that no command line exceeds 16 arguments.
if load ${eitri_dev} ${eitri_buf} /eitri/layout.env; then
    env import -t ${eitri_buf} ${filesize} eitri_part_A eitri_part_B eitri_partuuid_A eitri_partuuid_B eitri_cmdline eitri_hide_emmc
    env import -t ${eitri_buf} ${filesize} eitri_s2_kernel eitri_s2_fdt eitri_s2_initrd eitri_s2_args
fi
if load ${eitri_dev} ${eitri_buf} /eitri/boot.env; then
    env import -t ${eitri_buf} ${filesize} ${eitri_keys}
fi

if test "${eitri_hide_emmc}" = "1"; then
    if test "${eitri_mode}" = "stage2"; then
        # The RAM installer converts the eMMC; a card that hides the eMMC never starts it.
        echo "Eitri: the installer environment is not started from a card that hides the eMMC"
        setenv eitri_mode stock
    fi
fi

if test "${eitri_mode}" = "stage2"; then
    # Spend one start of the installer environment.  Unlike the Eitri boot counter it is NOT cleared by a start
    # alone: stage 2 itself ends the mode (commit, or a handled failure that leaves a bootable stock system).
    # So an installer that hangs or loses power re-enters here, up to the counter; then stock boots.
    setenv eitri_s2_ok 0
    if test "${eitri_s2_tries}" = "3"; then
        setenv eitri_s2_tries 2; setenv eitri_s2_ok 1
    elif test "${eitri_s2_tries}" = "2"; then
        setenv eitri_s2_tries 1; setenv eitri_s2_ok 1
    elif test "${eitri_s2_tries}" = "1"; then
        setenv eitri_s2_tries 0; setenv eitri_s2_ok 1
    fi
    if test "${eitri_s2_ok}" = "1"; then
        echo "Eitri: starting the installer environment (${eitri_s2_tries} start(s) left after this one)"
        if run eitri_save; then
            if load ${eitri_dev} ${kernel_addr_r} ${eitri_s2_kernel}; then
                if load ${eitri_dev} ${fdt_addr_r} ${eitri_s2_fdt}; then
                    if load ${eitri_dev} ${ramdisk_addr_r} ${eitri_s2_initrd}; then
                        setenv bootargs "${eitri_s2_args}"
                        booti ${kernel_addr_r} ${ramdisk_addr_r}:${filesize} ${fdt_addr_r}
                    fi
                fi
            fi
        fi
        echo "Eitri: installer environment could not be started"
        # Files that will not load will not load next time either: stop retrying.
        setenv eitri_mode stock
        run eitri_save
    else
        echo "Eitri: installer environment is out of starts, booting the stock system"
        setenv eitri_mode stock
        run eitri_save
    fi
elif test "${eitri_mode}" = "eitri"; then
    # Spend one boot attempt on the selected slot.
    setenv eitri_boot_slot ${eitri_slot}
    setenv eitri_try_ok 0
    if test "${eitri_tries}" = "3"; then
        setenv eitri_tries 2; setenv eitri_try_ok 1
    elif test "${eitri_tries}" = "2"; then
        setenv eitri_tries 1; setenv eitri_try_ok 1
    elif test "${eitri_tries}" = "1"; then
        setenv eitri_tries 0; setenv eitri_try_ok 1
    fi
    if test "${eitri_try_ok}" = "0"; then
        # This slot is out of attempts: mark it bad and switch to the other one if it is known good.
        if test "${eitri_slot}" = "A"; then
            setenv eitri_state_A bad
            if test "${eitri_state_B}" = "good"; then
                setenv eitri_slot B; setenv eitri_tries 2; setenv eitri_try_ok 1
            fi
        else
            setenv eitri_state_B bad
            if test "${eitri_state_A}" = "good"; then
                setenv eitri_slot A; setenv eitri_tries 2; setenv eitri_try_ok 1
            fi
        fi
        if test "${eitri_try_ok}" = "1"; then
            echo "Eitri: slot ${eitri_boot_slot} failed, falling back to slot ${eitri_slot}"
        else
            run eitri_save
            echo "Eitri: no bootable slot left"
        fi
    fi
    if test "${eitri_try_ok}" = "1"; then
        if test "${eitri_slot}" = "B"; then
            setenv eitri_part ${eitri_part_B}; setenv eitri_partuuid ${eitri_partuuid_B}
        else
            setenv eitri_part ${eitri_part_A}; setenv eitri_partuuid ${eitri_partuuid_A}
        fi
        if run eitri_save; then
            echo "Eitri: booting slot ${eitri_slot} (partition ${eitri_part}, ${eitri_tries} attempt(s) left after this one)"
            if ext4load ${devtype} ${devnum}:${eitri_part} ${kernel_addr_r} /boot/Image; then
                if ext4load ${devtype} ${devnum}:${eitri_part} ${fdt_addr_r} /boot/dtb/${fdtfile}; then
                    setenv eitri_fdt_ok 1
                    if test "${eitri_hide_emmc}" = "1"; then
                        # Bench card: Linux must not see the eMMC at all. Disable its controller (mmc1 alias,
                        # /mmc@ff520000) in the loaded device tree; if that fails, do not start this kernel.
                        setenv eitri_fdt_ok 0
                        if fdt addr ${fdt_addr_r}; then
                            if fdt resize; then
                                if fdt set /mmc@ff520000 status disabled; then
                                    setenv eitri_fdt_ok 1
                                    echo "Eitri: eMMC controller /mmc@ff520000 disabled for Linux"
                                fi
                            fi
                        fi
                    fi
                    if test "${eitri_fdt_ok}" = "1"; then
                        setenv bootargs "root=PARTUUID=${eitri_partuuid} ro rootwait rootfstype=ext4 eitri.slot=${eitri_slot} ${eitri_cmdline}"
                        booti ${kernel_addr_r} - ${fdt_addr_r}
                    else
                        echo "Eitri: cannot hide the eMMC from Linux, not booting Eitri"
                    fi
                fi
            fi
            echo "Eitri: slot ${eitri_slot} could not be loaded"
        else
            echo "Eitri: cannot update the boot counter, not booting Eitri"
        fi
    fi
fi

echo "Eitri: booting the stock system"
sysboot ${devtype} ${devnum}:${distro_bootpart} any ${pxefile_addr_r} /extlinux/extlinux.conf.stock
