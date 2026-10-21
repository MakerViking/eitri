# eitri-update

Client for the Eitri release channel. Python >= 3.7, standard library plus `eitri-mcuflash`.

```sh
eitri-update check                     # fetch + verify the signed manifest, say whether something newer exists
eitri-update status
eitri-update upgrade --dry-run         # the plan; downloads nothing
eitri-update upgrade --yes             # download, verify, rauc install (OS), reboot; seed + MCU firmware follow after the reboot
eitri-update switch-flavor kalico      # run the other flavor: its seed + MCU firmware from the installed release, no reboot
eitri-update post-boot --stage prepare|verify     # systemd units, every boot (see systemd/)
eitri-update rollback                  # boot the previous system next time
eitri-update upstream enable|disable|status
```

* The manifest is trusted only if its **minisign signature** verifies against a key in `/usr/share/eitri/keys` (the
  read-only, RAUC-verified image; `key_dir=` in `/etc/eitri/update.conf` names another directory on purpose; ids in
  `revoked-keys.txt` next to the keys are dropped). `sequence` may never decrease (nor go below
  `/usr/share/eitri/min-manifest-sequence`); a manifest must carry `expires` and an expired one is refused;
  only `https://` (and `file://` for offline updates) URLs are fetched; every bundle must match the signed SHA-256
  and size (resumable downloads). `upgrade` refuses while an earlier update is unverified or the running system has
  not been confirmed, because it overwrites the only known-good slot. See `../keys/README.md`.
* **The data layer travels with the release.** A release lists, per flavor, a `seed` bundle (the data seed of `seed/SPEC.md`
  plus its manifest) and an `mcu` bundle (the matching MCU firmware). `upgrade` downloads the seed manifest first and refuses a
  seed that is not this release's (version, flavor, pinned firmware commit, this image's interpreter ABI and glibc, unpacked size
  vs free space) before it touches the OS slot. After the reboot `eitri-prepare` (before Klipper, Moonraker and Klarity) unpacks
  the MCU firmware tree, runs `eitri-seed-install --mode update --keep-old`, re-links the klipper-layer extras and flashes MCUs
  that do not match. The slot is committed only when Klipper and Moonraker are healthy (then the replaced trees are deleted); any
  failure restores the previous trees, flavor flag, seed record, MCU firmware files and MCU images, and selects the previous
  slot. A power cut mid-way is recoverable: progress is recorded, the seed record decides whether the swap already happened, and
  leftovers of a half-done swap are undone before another attempt. The tool is `/usr/libexec/eitri/eitri-seed-install`.
* **`switch-flavor klipper|kalico`** does the same for the other flavor without an OS update (details in `../docs/BUNDLE.md`):
  it needs the installed release to still be in the channel, refuses during a print, in upstream mode and while the system is
  unconfirmed, stops the services that were running, swaps the trees, flashes both MCUs and restarts and health-checks; failure
  puts the previous flavor back and restarts the services. `/usr/libexec/eitri/switch-flavor` (`bin/eitri-switch-flavor`) is the
  wrapper the configurator calls. The installed flavor is read from the seed record, not from `/data/eitri/flavor`, because the
  configurator writes that file before it asks for the switch.
* `os` and `data` bundles are installed by **RAUC** into the inactive A/B slot. Two boot-state layouts are supported
  behind `BootControl`: the in-place conversion (the layout users get; `eitri-bootctl` is RAUC's custom backend and writes the
  `/boot/eitri/boot.env` that `boot.scr` reads) and the flash-once GPT image (bench boards, microSD cards and CI only;
  `rauc-bootloader` over U-Boot's raw state sector; a slot only counts as good once a healthy boot was recorded). See `../docs/BOOT.md`.
* **Post-boot** (two units around Klipper): `prepare` installs the queued data bundle and flashes MCUs whose
  firmware does not match this host (a blocked or incomplete `eitri-mcuflash plan` is an error, never "already matches"); `verify` health-checks Moonraker/Klippy and only then `mark-good` (a printer whose Klipper/Moonraker are not configured yet is committed on OS health alone). On failure
  the MCUs are rolled back and the previous slot is selected again. After the in-place conversion the same path
  confirms the very first boot (and flashes the MCUs when `install.env` says `mcu_flash=auto`).
* **Upstream mode** (`upstream enable`): installs the signed toolchain add-on into `/data/eitri/toolchain`
  (archive paths validated, installed atomically), writes a Moonraker `update_manager` fragment for Klipper *or*
  Kalico (the flavor chosen at install) and Moonraker, flips the `printer_data/config/eitri-update-manager.conf`
  symlink, restarts Moonraker. Klipper/Kalico stays a pristine clone. The fragment names the data seed's hash-pinned lock files
  (`/data/eitri/seed/locks/{klippy,moonraker}.txt`) as `requirements:` when they exist, instead of the repositories' own
  requirement files, which pin versions the seed deliberately replaced and would downgrade the venvs (Moonraker only
  installs requirements that changed during an update, and a fixed lock file never changes). `upstream disable` flips it back and removes
  the toolchain; the next release bundle re-pins Klipper and the MCU firmware. While in upstream mode release
  bundles are refused. After a Klipper update, `eitri-mcuflash flash --build --apply --yes` rebuilds and flashes.
* Configuration: `/etc/eitri/update.conf` (`manifest_url=`, `key_dir=`). The built-in default manifest URL is the
  GitHub release asset of the project; set the final one at release time.
