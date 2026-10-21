# Eitri data seed

The initial `/data` application layer, built by CI per release and flavor: pinned Klipper or Kalico and Moonraker
clones, their CPython 3.14 / aarch64 environments, Klarity, the `qd_*` extras, the klipper-layer config set and a
prebuilt Klipper C helper. First boot works offline and needs no compiler.

* **[SPEC.md](SPEC.md)** is the contract: archive format, layout, manifest, versioning, how `data-init`, the in-place
  installer and `eitri-update` install it, reproducibility, image prerequisites.
* `build-seed.sh` builds both flavors, `verify-seed.sh` checks them and runs their environments under `qemu-aarch64`,
  `bin/eitri-seed-install` is the reference installer (POSIX sh), `requirements/` holds the hash-pinned locks.
* `make -C seed seed` runs the tests (offline); `make -C seed build verify` builds and proves the seeds
  (needs network, uv, zstd, the Buildroot cross toolchain and qemu-user).

```
make -C seed build VERSION=2026.10.0      # data-seed-2026.10.0-{klipper,kalico}.{tar.zst,manifest.json}
make -C seed verify
```

Licence: Apache-2.0 (build tooling). The seed's contents keep their own licences (Klipper and Kalico GPL-3.0-or-later,
Moonraker GPL-3.0, the wheels as published, the extras GPL-3.0-or-later, Klarity Apache-2.0).
