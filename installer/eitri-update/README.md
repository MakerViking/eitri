# eitri-update

Client for the Eitri release channel. Python >= 3.7, standard library plus `eitri-mcuflash`.

```sh
eitri-update check                     # fetch + verify the signed manifest, say whether something newer exists
eitri-update status
eitri-update upgrade --dry-run         # the plan; downloads nothing
eitri-update upgrade --yes             # download, verify, rauc install (OS), reboot
eitri-update post-boot --stage prepare|verify     # systemd units, every boot (see systemd/)
eitri-update rollback                  # boot the previous system next time
eitri-update upstream enable|disable|status
```

* The manifest is trusted only if its **minisign signature** verifies against a key in `/usr/share/eitri/keys` or
  `/etc/eitri/keys` (override with `--key-dir`). `sequence` may never decrease; expired manifests are refused;
  only `https://` (and `file://` for offline updates) URLs are fetched; every bundle must match the signed SHA-256
  and size (resumable downloads).
* `os` and `data` bundles are installed by **RAUC** into the inactive A/B slot; `eitri-bootctl` is RAUC's custom
  bootloader backend and writes the same `/boot/eitri/boot.env` that `boot.scr` reads.
* **Post-boot** (two units around Klipper): `prepare` installs the queued data bundle and flashes MCUs whose
  firmware does not match this host; `verify` health-checks Moonraker/Klippy and only then `mark-good`. On failure
  the MCUs are rolled back and the previous slot is selected again. After the in-place conversion the same path
  confirms the very first boot (and flashes the MCUs when `install.env` says `mcu_flash=auto`).
* **Upstream mode** (`upstream enable`): installs the signed toolchain add-on into `/data/eitri/toolchain`
  (archive paths validated, installed atomically), writes a Moonraker `update_manager` fragment for Klipper *or*
  Kalico (the flavor chosen at install) and Moonraker, flips the `printer_data/config/eitri-update-manager.conf`
  symlink, restarts Moonraker. Klipper/Kalico stays a pristine clone. `upstream disable` flips it back and removes
  the toolchain; the next release bundle re-pins Klipper and the MCU firmware. While in upstream mode release
  bundles are refused. After a Klipper update, `eitri-mcuflash flash --build --apply --yes` rebuilds and flashes.
* Configuration: `/etc/eitri/update.conf` (`manifest_url=`, `key_dir=`). The built-in default manifest URL is the
  GitHub release asset of the project; set the final one at release time.
