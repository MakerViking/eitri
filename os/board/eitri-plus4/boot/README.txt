Eitri boot partition
====================

This FAT partition is mounted read-only at /boot/firmware on the printer.
It is not used to boot the kernel; each root filesystem slot carries its own
kernel. It holds two optional things:

  eitri-boot.env     boot options read by U-Boot (see the comments in the file)
  authorized_keys    SSH public keys for the "eitri" user, one per line. The
                     printer installs them on the next boot. Create this file
                     when the partition is mounted on a PC, for example from a
                     microSD card used for bench testing.

Changing either file does not need any special tool, but remount the partition
read-write on the printer first:  sudo mount -o remount,rw /boot/firmware
