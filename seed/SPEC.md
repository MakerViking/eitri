# Eitri data seed: specification

The **data seed** is the initial, ready-to-run application layer of `/data`: pinned Klipper *or* Kalico and
Moonraker as git clones, their Python environments, Klarity and its environment, the `qd_*` extras and the
klipper-layer config set, and a prebuilt Klipper C helper. CI builds it for aarch64 and ships it with every release.
With it a printer reaches a running Klipper, Moonraker and Klarity on its first boot with **no network and no
compiler**. This file is the contract between the seed build (`seed/`), the OS image (`os/`), the in-place installer
(`installer/`), `eitri-update` and the configurator. If you change the layout, change this file, `tools/seedlib.py`
(`COMPONENTS`) and `bin/eitri-seed-install` together; the tests fail otherwise.

Decisions this implements (STATUS.md, "Decisions"): **2** extras and layer config live on `/data` and follow
Klipper/Kalico; **3** the seed is built by CI per release; **13** `.py` source stays in the image and in the seed.

Contents: [1 Artifacts](#1-artifacts) | [2 Archive](#2-archive) | [3 Layout](#3-layout-of-data) |
[4 Python environments](#4-python-environments) | [5 Firmware clones](#5-firmware-clones) |
[6 The C helper](#6-the-prebuilt-c-helper-chelper) | [7 Extras and layer](#7-extras-and-the-klipper-layer) |
[8 Manifest](#8-manifest) | [9 Versioning](#9-versioning-and-compatibility) | [10 Installing](#10-installing-a-seed) |
[11 Consumers](#11-who-uses-it-and-how) | [12 Reproducibility and CI](#12-building-reproducibility-and-ci) |
[13 Image prerequisites](#13-what-the-os-image-must-provide) | [14 Adding a dependency](#14-adding-a-dependency) |
[15 Open items](#15-open-items-and-limits)

## 1. Artifacts

One seed per flavor, per release:

| File | Content |
|---|---|
| `data-seed-<version>-<flavor>.tar.zst` | the archive (section 2) |
| `data-seed-<version>-<flavor>.manifest.json` | metadata, including the archive's SHA-256 (section 8) |

* `<flavor>` is `klipper` or `kalico`. `<version>` is the Eitri release version (git tag without the `v`, for example
  `2026.10.0`); development builds use `0.0.0.dev0` or `0.0.0.dev0+g<sha7>`. Both match
  `[0-9A-Za-z][0-9A-Za-z.+_~-]*`; the file names match `data-seed-<version>-(klipper|kalico).tar.zst`.
* The manifest is **not** trusted by itself. Its SHA-256 for the archive is only as good as the channel that delivered
  it: `install.json` / the channel `manifest.json` (both minisign-signed, `installer/docs/BUNDLE.md`) carry the same
  digest, and *that* is what consumers pass to `eitri-seed-install --sha256`. The seed workflow signs nothing and has no
  secrets (decision 5: deferred signing behind the protected `release` environment).
* `install.json` `files.data_seed.<flavor>` points at the archive; a `data` update bundle carries it as payload.

Current sizes (Buildroot 2026.02.3 toolchain, `0.0.0.dev0`): klipper 55 MiB compressed / 383 MiB unpacked,
kalico 94 MiB / 472 MiB. Most of the weight is upstream, not ours: Klipper's `lib/` (vendor MCU libraries, 206 MiB,
needed for on-device MCU builds in upstream mode), Moonraker's venv (104 MiB: Pillow, uvloop, apprise) and, for Kalico,
numpy (about 60 MiB). `/data` needs `layout.data_min_mib` (4096) free anyway.

## 2. Archive

* **Tar, GNU format** (`ustar  \0` magic), **zstd** compressed (level 19, content checksum on, no dictionary, no
  `--long`, so any zstd >= 1.4 decodes it with the default window).
* Members sorted by byte order of the path, **no `./` prefix**, directories before their contents. Only regular files,
  directories and symlinks. **No hard links**, no devices, no set-uid/set-gid/sticky bits, no extended attributes,
  no ACLs. Names match `[A-Za-z0-9._+@=,:~/-]+` (so shell tools never see spaces or quotes).
* **Owner `1000:1000`** (the image's `eitri` user and group) on every member, numeric, empty user/group names.
* **mtimes are meaningful** (section 6): every member has the build's *epoch*, which is the newest commit time of the
  pinned Klipper/Kalico and Moonraker commits; `c_helper.so` is epoch + 1 s. Extract with the stored times
  (`tar -x`, never `-m`/`--touch`). GNU tar and the image's BusyBox tar both do this by default.
* **Symlinks** are relative inside the seed, or absolute and pointing into `/data/`, `/usr/`, `/etc/`, `/lib/` or
  `/run/` (these two classes exist: `klippy/extras/qd_*.py -> /data/eitri/extras/qd_*.py` and the venvs'
  `bin/python -> /usr/bin/python3`). Every `/data/...` link resolves inside the seed (checked at build time).
* The builder is deterministic: the same inputs (release.lock, requirement locks, repository content, toolchain)
  give byte-identical archives, also across hosts, work directories and uv versions (section 12).

**Extraction rules** every consumer enforces (`tools/seedlib.py: check_member`, `bin/eitri-seed-install`):
verify the archive's SHA-256 **before** reading it; list it first and refuse the whole archive if any member is a
non-file/dir/symlink type, has a set-uid/gid/sticky bit, has an absolute, `..`, `.`, empty or otherwise non-normalised
path, lies outside the layout of section 3, or is a symlink that leaves the seed (an absolute link outside the five
prefixes above, or a relative one that climbs out). Extract into a staging directory on the **same file system** as
the destination, never into `/data` directly.

## 3. Layout of `/data`

The archive unpacks to this tree (paths relative to `/data`). Nothing else is allowed in it.

| Path | What | Written by | Changes with |
|---|---|---|---|
| `firmware/klipper/` | pristine git clone of Klipper **or** Kalico (always named `klipper`), plus the extras symlinks and the prebuilt `klippy/chelper/c_helper.so` | seed | seed / upstream mode |
| `firmware/moonraker/` | pristine git clone of Moonraker | seed | seed / upstream mode |
| `venv/klippy/` | Python environment of Klippy (flavor-specific) | seed | seed |
| `venv/moonraker/` | Python environment of Moonraker | seed | seed |
| `eitri/klarity/venv/` | Python environment of Klarity, including the `klarity` console script | seed | seed |
| `eitri/extras/` | the `qd_*.py` extras and `gcode_shell_command.py`, flat, plus `LICENSE` | seed | seed |
| `eitri/klipper-layer/` | `config/` (printer.cfg template, macros, flavor files, variables), `scripts/` (`overlay-install.sh`, `render-config.py`), `VERSION`, and `extras -> ../extras` | seed | seed |
| `eitri/seed/` | `seed-info.json`, `seed-info.env`, `locks/{klippy,moonraker,klarity}.txt` (the exact hash-pinned requirement sets) | seed | seed |
| `eitri/flavor` | `klipper\n` or `kalico\n` | seed (if absent) | flavor switch |

Parent directories (`firmware`, `venv`, `eitri`, `eitri/klarity`) are in the archive as `0755 1000:1000` entries only so
that a plain `tar -x` works; consumers place **components** (the rows above except `eitri/flavor`) with one rename each.

**Never in the seed**, never touched by installing it: `printer_data/` (config, logs, gcodes, database, comms: user
data, rendered by the configurator), `eitri/state/` (OS state, written by `data-init`, RAUC, `eitri-update`; the
seed's own record `seed-installed.env` is written there by the installer), `eitri/mcu/` (MCU firmware: separate
`mcu-firmware.tar.zst`), `eitri/configurator/`, `eitri/toolchain/`, `eitri/home/`, `eitri/ui`.

`printer_data/config/eitri` (a symlink to `eitri/klipper-layer/config`) and `eitri-flavor.cfg` are created by
`eitri/klipper-layer/scripts/overlay-install.sh` when the configurator renders `printer.cfg`; the seed does not ship
`printer_data/` so that "never clobber an existing `printer_data/config`" holds by construction.

## 4. Python environments

Three environments, all for **CPython 3.14 (`cp314`), aarch64, glibc**, none depending on the image's site-packages:

| venv | Interpreter for | Lock | Notes |
|---|---|---|---|
| `venv/klippy` | `klippy.py` | `requirements/klippy-<flavor>.txt` | cffi, greenlet, Jinja2, markupsafe, pyserial 3.4, python-can 4.6, pip; Klipper adds msgspec, Kalico numpy 2.3 |
| `venv/moonraker` | `moonraker.py` | `requirements/moonraker.txt` | tornado, Pillow, zeroconf, dbus-fast, uvloop, msgspec, apprise, ldap3, ... (36 packages) and `streaming-form-data` built from source |
| `eitri/klarity/venv` | `klarity` | `requirements/klarity.txt` | pyserial-asyncio-fast, websockets, PyYAML, Pillow (camera preview), and Klarity itself |

**Relocation.** A venv is a plain directory tree that is correct *at its final path*, because it is built under
`/data/...` names, not moved:

* `pyvenv.cfg`: `home = /usr/bin`, `include-system-site-packages = false`, `version_info = 3.14.5` (release.lock).
* `bin/python -> /usr/bin/python3` (absolute; the image's Python, `/usr/bin/python3 -> python3.14`), `bin/python3` and
  `bin/python3.14 -> python`. The interpreter is **not** copied; the image's CPython 3.14.x is the only interpreter.
  The unit files already call `/data/venv/klippy/bin/python`.
* `lib/python3.14/site-packages/` with the installed wheels; `lib64 -> lib`.
* Console scripts in `bin/` have the shebang `#!/data/<venv path>/bin/python` (rewritten from whatever the installer
  wrote). `pip3.14`-style names are normalised to the target's Python minor version.
* No `direct_url.json` / `uv_cache.json` and no build-host path anywhere (checked: symlink targets, shebangs, dist-info).
* Byte-code is **pre-compiled** (timestamp mode, source mtime = the archive's mtime, `co_filename` = the `/data/...`
  path), so the first start does not spend minutes compiling on a Cortex-A53. The same is done for `klippy/` and
  `moonraker/` in the clones (all ignored by git). Python recompiles anything whose source changed.

**Wheels.** Everything comes from PyPI as **binary wheels only**, resolved for `--python-platform
aarch64-manylinux_2_40 --python-version 3.14` and installed with `--require-hashes`. `manylinux_2_40` is the highest
baseline uv knows that does not exceed the image's glibc 2.42, so no wheel can need a newer C library than the image
has. The build then reads the symbol versions and `DT_NEEDED` entries of **every ELF object** in the seed
(`tools/seedtool.py check-elf`): all must be AArch64, need at most the image's glibc (highest actually needed today: 2.34),
and every external library must be listed in `requirements/image-libs.txt` (section 13).

**Locks.** `requirements/*.in` are hand-written inputs; `requirements/*.txt` are `uv pip compile --generate-hashes`
output (`make -C seed lock`), committed, reviewed and the only thing the build installs from. Their SHA-256 is recorded
in the manifest (`venvs.<name>.lock_sha256`) and a copy is shipped in `eitri/seed/locks/` so that a later
`uv pip sync --require-hashes` reproduces the environment.

**Deviations from the upstream requirement files**, each forced by "no compiler on the printer, Python 3.14, binary
wheels":

| Upstream pin | Seed | Why |
|---|---|---|
| Klipper `Jinja2==2.11.3`, `markupsafe==1.1.1` | Jinja2 3.1.6, markupsafe 3.x | 2.x does not support 3.14, no 3.14 wheel; Moonraker and Kalico already use 3.1.6 |
| Klipper `python-can==3.3.4`, `setuptools` | python-can 4.6.1 | 3.3.4 predates 3.14; same as Kalico's pin |
| Klipper `cffi==2.1.1`, `greenlet==3.3.2` | as upstream | |
| Kalico `cffi==1.17.1` | cffi 2.1.x | no cp314 wheel of 1.17.1; the image itself carries cffi 2.0 |
| Kalico `markupsafe==2.1.5` | markupsafe 3.x | no cp314 wheel |
| Kalico `numpy~=2.3.0`, `greenlet 3.3`, `pyserial==3.4`, `python-can~=4.6` | as upstream | |
| Moonraker caps in release.lock | all respected (a test enforces them) | `tornado<=6.5.8`, `pillow<=12.3.0`, `dbus-fast<=5.0.22`, `zeroconf<=0.150.0`, `importlib_metadata<=9.0.0`, `pyserial==3.4` |
| (new) `pip==25.3` in the klippy and moonraker venvs | | Moonraker's update manager runs `<venv>/bin/python -m pip`; same version as the image's |

**No aarch64 wheel: cross-compile.** `streaming-form-data` (Moonraker's multipart upload parser) publishes x86-64
wheels and an sdist only. `requirements/sdists.toml` pins its sdist by URL and SHA-256; `build-seed.sh` downloads it,
compiles its (shipped, pre-generated) C file with the **Buildroot cross gcc** against **the image's own Python headers**
(`$BR_OUT/staging/usr/include/python3.14`), using a copy of the image's `_sysconfigdata__linux_aarch64-linux-gnu.py`
with the compiler and include paths replaced (`_PYTHON_SYSCONFIGDATA_NAME`, `_PYTHON_HOST_PLATFORM=linux-aarch64`,
`SETUPTOOLS_EXT_SUFFIX`), producing `streaming_form_data-2.1.0-cp314-cp314-linux_aarch64.whl`; generated C is pruned from
the wheel. Its dependencies (`smart-open`, `aiofiles`) are in the Moonraker lock. The source SHA-256 and the built
wheel's SHA-256 are recorded in the manifest (`venvs.moonraker.built_from_source`). Section 14 says how to add another.
Not used: running the image's Python under `qemu-aarch64` to build (qemu-user cannot start the aarch64 compiler
child processes without binfmt), and podman arm64 images.

## 5. Firmware clones

`firmware/klipper` and `firmware/moonraker` are real git repositories, **unmodified** (invariant I1): HEAD is the
pinned commit of release.lock (`[firmware.klipper]` / `[firmware.kalico]` / `[moonraker]`).

* **Shallow, with exactly the history `git describe` needs.** Moonraker and Kalico: depth 1 plus the tag. Klipper has
  no tag since v0.13.0, so the history since the day before that tag is fetched (836 commits): `git describe --tags
  --long` gives `v0.13.0-813-ga08fc79fc`, which klippy reports, Moonraker's update manager compares and the MCU
  firmware version lock expects. `core.abbrev = 9` is set in each clone so the hash length is stable (git would pick
  7-9 by object count). **The MCU firmware must be built from this tree (or with `git -c core.abbrev=9 describe`) to
  get the same version string.** The manifest records the string (`sources.firmware.describe`).
* **Attached to a branch that tracks upstream**: local branch `master` (Klipper, Moonraker) or `main` (Kalico) at the
  pinned commit, `refs/remotes/origin/<branch>` = the same commit, `branch.<b>.remote=origin`,
  `remote.origin.url` = `https://github.com/Klipper3d/klipper.git` / `KalicoCrew/kalico.git` /
  `Arksine/moonraker.git` (the same origins and branches `eitri-update upstream enable` writes into
  `moonraker.conf`; a test compares them). A later `git pull` in upstream mode fast-forwards from the pinned commit.
* **Clean**: `git status --porcelain --untracked-files=all` is empty and `git describe --dirty` has no `-dirty`. The
  overlay symlinks are hidden by `.git/info/exclude` (`overlay-install.sh`); `c_helper.so` and byte-code by upstream's
  own `.gitignore`. Klippy itself still appends `-dirty` to the version it **logs**, because `klippy.py` counts untracked
  or ignored `.py` files and symlinks under `klippy/extras/` (ARCHITECTURE section 6.2): expected, harmless, and the
  reason to compare versions by commit (or `git describe` without `--dirty`).
* **One pack, packed the same way** (`repack -a -d -f --window=10 --depth=50`, single thread), no reflogs, no
  `FETCH_HEAD`, no hooks (the repository is created with an empty template).
* **Index without stat data** (`git read-tree HEAD`), because index entries with inode/ctime/mtime would make every
  build different. Consequence: the **first** `git status` on the printer re-hashes the work tree (Klipper: about 2,300 tracked files,
  Kalico about 2,500, Moonraker 180; a few seconds on the A53) and writes a normal index. Moonraker's update manager does this on its first
  refresh. A consumer that wants that cost at install time runs `git -C /data/firmware/klipper status` once as `eitri`.

## 6. The prebuilt C helper (chelper)

Klippy needs `klippy/chelper/c_helper.so`. Upstream builds it on first start with `gcc` (and the printer has none).
`klippy/chelper/__init__.py` (`check_build_c_library` / `get_ffi`; identical rule in Klipper and Kalico) rebuilds when

```
not exists(c_helper.so)  or  max(mtime of SOURCE_FILES + OTHER_FILES + chelper/__init__.py) > mtime(c_helper.so)
```

and otherwise `dlopen`s the library. Nothing else is checked: no hash, no version, no compiler flags. So the seed ships
a `c_helper.so` that is **not older than any source it was built from**:

* `build-seed.sh` extracts `GCC_CMD`, `COMPILE_ARGS`, `SOURCE_FILES` and `DEST_LIB` from the clone's own
  `chelper/__init__.py` (with `ast`, without importing it) and runs that command with the Buildroot cross gcc:
  `-Wall -O2 -shared -fPIC -flto -fwhole-program -fno-use-linker-plugin`, plus `-mcpu=cortex-a53` (instead of
  `-march=native`, which a cross compiler cannot use), without `-g`, with `-frandom-seed`, no build-id and
  `-ffile-prefix-map` for reproducibility, then `strip --strip-unneeded`. Klipper: 50,936 bytes.
* All sources have the archive epoch as mtime and `c_helper.so` has epoch + 1 s, so the rule above says "up to date"
  **as long as the archive's mtimes are kept** (section 2).
* Verified three ways: `verify-seed.sh` re-checks the rule after extraction; the smoke test runs the real
  `chelper.get_ffi()` under qemu with `do_build_code` replaced by a function that raises, then calls into the library;
  the installer tests repeat the rule on a real archive.
* Consequence for **upstream mode**: when a `git pull` changes a chelper source, its mtime becomes newer than the
  library and klippy wants `gcc`. That is what the optional toolchain add-on (`eitri/toolchain`, decision: opt-in) is
  for; without it Klippy logs `Unable to build C code module` and stops. The release channel never has this problem:
  the next seed ships a matching helper. Klippy does not verify the `.so`, so a stale library after a source change
  would be silently wrong; the mtime rule is the only protection.

## 7. Extras and the klipper-layer

The seed makes `/data` the source of truth for the extras and the layer (decision 2): `/usr/lib/eitri/klipper-layer`
is gone from the image (package `eitri-klipper-layer` was removed; `post-build.sh` deletes a stale copy): `/data` is the
only place the extras and the layer live.

* `eitri/extras/*.py` are copied from `klipper-layer/extras/` (flat; `__pycache__` and tests excluded).
* `eitri/klipper-layer/{config,scripts}` are copied from `klipper-layer/`; `extras -> ../extras` makes the layout
  `overlay-install.sh` expects (`LAYER/extras`, `LAYER/config`, `LAYER/scripts`) valid with the flat extras at the
  contract path `/data/eitri/extras`.
* The build **runs `overlay-install.sh`** against the staged clone, so the seed already contains
  `firmware/klipper/klippy/extras/qd_*.py -> /data/eitri/extras/qd_*.py` and the `.git/info/exclude` block. For Kalico
  `gcode_shell_command.py` is not linked (the script never shadows a name upstream tracks). It requires `git status` to
  be unchanged (the build fails otherwise).
* After **any** change to `eitri/extras` without a matching change to `firmware/klipper` (an update that kept a
  moved-forward clone), run `eitri/klipper-layer/scripts/overlay-install.sh --klipper-dir /data/firmware/klipper
  --extras-dir /data/eitri/extras --config-dir /data/eitri/klipper-layer/config --printer-data /data/printer_data
  --owner eitri` to add and remove links.
* The licence split is unchanged: extras GPL-3.0-or-later, the rest of the layer as in `release.lock`.

## 8. Manifest

`data-seed-<version>-<flavor>.manifest.json`, UTF-8, sorted keys, 2-space indent, trailing newline. Schema 1.
`eitri/seed/seed-info.json` inside the archive has the same content **minus** `archive`, `created`, `info_sha256` and
`builder` (they cannot be known before the archive exists). `tools/seedlib.py: validate_manifest` is the executable
definition; the tests mutate every field.

| Field | Type / rule |
|---|---|
| `schema` | `1` |
| `kind` | `"eitri-data-seed"` |
| `version`, `flavor` | as in the file name; `flavor` is `klipper` or `kalico` |
| `layout` | `1`: the layout of section 3 (data-init's `LAYOUT_VERSION`). A consumer refuses a seed whose layout it does not know |
| `archive` | `name`, `sha256` (64 lowercase hex), `size` (bytes), `uncompressed_size` (sum of regular file sizes), `compression` `"zstd"`, `tar_format` `"gnu"` |
| `created` | UTC `YYYY-MM-DDTHH:MM:SSZ` of the build (`SOURCE_DATE_EPOCH` if set); not part of the archive |
| `source_date_epoch` | the epoch used for every mtime (newest pinned commit time) |
| `info_sha256` | SHA-256 of `eitri/seed/seed-info.json` |
| `target` | `arch` `aarch64`; `python` (image CPython, `x.y.z`, from release.lock); `python_abi` `cp314`; `interpreter` `/usr/bin/python3`; `wheel_platform`; `glibc_max` (the image's) and `glibc_needed` (highest used); `image_libraries` (sonames expected from the image) |
| `sources.firmware` | `name`, `version`, `commit` (40 hex), `tag`, `describe`, `origin`, `branch`, `shallow` |
| `sources.moonraker` | same fields |
| `sources.klarity` | `version`, `wheel`, `wheel_sha256` (built from this repository's `klarity/`) |
| `sources.klipper_layer` | `version`, `tree_sha256` (digest of `eitri/extras` + `eitri/klipper-layer` as installed) |
| `venvs.{klippy,moonraker,klarity}` | `lock` (path in this repository), `lock_sha256`, `packages` (name, version, wheel tags of what was installed), `built_from_source` (name, version, `sdist_sha256`, `wheel`, `wheel_sha256`) |
| `chelper` | `path`, `sha256`, `size`, `command` (without build-host paths), `compiler` |
| `tree` | `algorithm`, `digest`, `entries`, `bytes`, `components` (the list of section 3 in contract order) |
| `builder` | `script`, `uv`, `zstd`, `git`, `host_python`, `python_headers`, `cross_compiler`: provenance only, not part of the archive |

**Tree digest.** SHA-256 over the lines `<type> <mode:04o> <size|-> <sha256|-> <path>\n` for every entry in byte order of
the path (type `d`, `f` or `l`; for a symlink the digest is of the link target), **excluding**
`eitri/seed/seed-info.json` and `eitri/seed/seed-info.env`. Owners and mtimes are not part of it (they are fixed by the
archive format and checked separately). `seedlib.tree_digest(dir)` computes it for any extracted tree.

Example (shortened):

```json
{
  "schema": 1, "kind": "eitri-data-seed", "version": "2026.10.0", "flavor": "klipper", "layout": 1,
  "archive": {"name": "data-seed-2026.10.0-klipper.tar.zst", "sha256": "...", "size": 57879093,
              "uncompressed_size": 401470209, "compression": "zstd", "tar_format": "gnu"},
  "created": "2026-10-21T14:28:58Z", "source_date_epoch": 1792535588,
  "target": {"arch": "aarch64", "python": "3.14.5", "python_abi": "cp314", "interpreter": "/usr/bin/python3",
             "wheel_platform": "aarch64-manylinux_2_40", "glibc_max": "2.42", "glibc_needed": "2.34",
             "image_libraries": ["ld-linux-aarch64.so.1", "libc.so.6", "libgcc_s.so.1", "libstdc++.so.6", "libz.so.1"]},
  "sources": {"firmware": {"name": "klipper", "commit": "a08fc79fcfa342e5b95ac02a9a8b271f453523e9",
                           "describe": "v0.13.0-813-ga08fc79fc", "branch": "master", "tag": "v0.13.0", "...": "..."},
              "moonraker": {"commit": "985c1d0bbeb90bc057d34a232c9dc3b05e0c6c8d", "describe": "v0.11.0-0-g985c1d0bb", "...": "..."}},
  "venvs": {"moonraker": {"lock_sha256": "...", "packages": [{"name": "tornado", "version": "6.5.8", "tags": ["..."]}],
                          "built_from_source": [{"name": "streaming-form-data", "version": "2.1.0",
                                                 "sdist_sha256": "...", "wheel_sha256": "..."}]}, "...": "..."},
  "chelper": {"path": "firmware/klipper/klippy/chelper/c_helper.so", "sha256": "...", "size": 50936},
  "tree": {"digest": "...", "entries": 9272, "components": ["firmware/klipper", "..."]}
}
```

## 9. Versioning and compatibility

* **Seed version = Eitri release version.** One release has exactly two seeds (one per flavor), built from one
  release.lock. The `data_seed` entry of `install.json` and the `data` bundle of the channel manifest name them.
* **`layout`** changes only when section 3 changes incompatibly. `eitri-seed-install` and `data-init` know a layout
  number and refuse others. Adding an optional component is a layout change.
* **Interpreter ABI.** A seed is valid for images whose CPython is `3.14.x` (`cp314`) on aarch64 with glibc >= the
  seed's `glibc_needed` (2.34). An image with another minor Python needs a new seed; the manifest says which
  (`target.python`, `target.python_abi`).
* **Flavor** is part of the seed (`eitri/flavor`). A seed never changes a printer's flavor except through an explicit
  `--switch-flavor` update, and switching also needs the MCU firmware of the other flavor flashed (ARCHITECTURE section 5).
* **Host/MCU lock.** `sources.firmware.describe` is the string the matching MCU firmware must report
  (`installer/docs/BUNDLE.md`, "mcu-firmware.tar.zst"). The release must therefore build both from the same commit.
* **Rollback.** An OS rollback (RAUC) does not roll `/data` back. A seed update therefore keeps the replaced trees until
  the caller declares success (`--keep-old`, then `--mode finish` or `--mode rollback`, section 10).

## 10. Installing a seed

`seed/bin/eitri-seed-install` is the reference implementation (POSIX sh; tested under bash, the system `sh`, and with
the image's BusyBox `tar -tv`/`tar -x` under qemu-user). It ships in the OS image as
`/usr/libexec/eitri/eitri-seed-install` and in the stage-2 payload. Other implementations must behave identically.

```
eitri-seed-install --archive FILE --sha256 HEX --flavor klipper|kalico [--data /data] [--mode install|update]
                   [--switch-flavor] [--force] [--keep-old] [--owner UID:GID] [--manifest FILE] [--dry-run] [-q]
eitri-seed-install --mode rollback|finish [--data /data]
```

Exit codes: `0` ok or nothing to do; `2` usage; `3` integrity or policy failure, **nothing was changed**; `4` conflict
(flavor mismatch, moved-forward tree without `--force`); `5` not enough space; `6` other failure (an update that had
started is rolled back; or another run holds the lock).

**Common steps**

1. Take the lock `$DATA/.eitri-seed.lock` (a directory holding the pid; a lock of a dead process is taken over).
2. SHA-256 of the archive == `--sha256`, otherwise exit 3, before anything is read. The file name must be
   `data-seed-*-<flavor>.tar.zst`. With `--manifest` the manifest must agree (name, digest, flavor).
3. List the archive (`zstd -dc | tar -tv`) and apply the member policy of section 2 and the layout of section 3; any
   violation: exit 3. The listing also yields the unpacked size.
4. If `eitri/flavor` exists it must equal `--flavor` (exit 4; `update --switch-flavor` is the only way to change it).
5. Free space on `$DATA` >= unpacked size x 1.1 + 64 MiB (exit 5). (Test hook: `EITRI_SEED_FAKE_FREE_KIB`.)
6. Remove a stale `$DATA/.eitri-seed-staging`, extract into it (`zstd -dc | tar -xf - -C staging`, times preserved),
   read `eitri/seed/seed-info.env` (only `KEY=value` lines are accepted), require kind `eitri-data-seed`, layout `1`, and
   the same flavor in the archive name, the env file and `eitri/flavor`; require all eight components.
7. Place components (below), write `eitri/flavor` if absent, write the record
   `$DATA/eitri/state/seed-installed.env` **last** (`SEED_VERSION`, `SEED_FLAVOR`, `SEED_TREE_SHA256`,
   `SEED_ARCHIVE_SHA256`, `SEED_FIRMWARE_COMMIT`, `SEED_MOONRAKER_COMMIT`, `SEED_MODE`, `SEED_INSTALLED_AT`, `SEED_PLACED`,
   `SEED_KEPT`; atomically via a temporary file), `sync`, remove the staging directory.

**Install mode** (blank or pre-created `/data`; first boot): for each component, if the target does not exist or is an
**empty directory** (`data-init` pre-creates `venv/klippy`, `eitri/klarity`, `eitri/extras`, ...), it is replaced by a
single `rename` of the staged tree. A target with content is **kept**: never merged, never overwritten, reported in
`SEED_KEPT`. Running it again after success changes nothing (`already installed from this seed`). A crash at any point
leaves each component either completely absent or completely in place; running again finishes the rest. Existing
`printer_data/` is never read or written.

**Update mode** (a newer seed of the same flavor; `eitri-update`): services `klipper`,
`moonraker`, `klarity` must be stopped by the caller. For each component the target is renamed to
`.<name>.seed-old` (same directory), the staged tree is renamed into its place; if any step fails every swapped
component is put back (exit 6). A **moved-forward tree is not replaced**: if the installed `firmware/klipper` (or
`firmware/moonraker`) has a HEAD different from the commit recorded for the installed seed (upstream mode,
a manual `git pull`), that clone and its venv are kept and reported, unless `--force`. After success the old trees are
deleted, or with `--keep-old` kept together with `seed-installed.env.prev` and the previous flavor until the caller runs
`--mode finish` (delete) or `--mode rollback` (restore trees, record and flavor). A leftover `.seed-old` next to a missing
tree (a crash between the two renames) is restored at the start of the next run. `printer_data/`, `eitri/state/` and
everything else outside the components stay as they are. Afterwards the caller re-runs `overlay-install.sh` if
`firmware/klipper` was kept (section 7).

## 11. Who uses it and how

The integration points below are *specified* here; the OS lane (data-init, image), the installer lane (stage 2,
`eitri-update`) and the configurator lane (flavor switch) implement them.

### 11.1 GPT flash-once image (bench, microSD, CI/QEMU)

Implemented as the **pre-populated `data` partition**: `os/board/eitri-plus4/mkdata.sh` (called by `post-image.sh`)
installs the klipper-flavor seed with `eitri-seed-install --mode install --owner 1000:1000` into a scratch tree under
fakeroot and turns that tree into `data.ext4` with `mke2fs -d` (without `SOURCE_DATE_EPOCH`, which would flatten the
mtimes the chelper rule depends on). `data-init` finds everything present and only repairs parent ownership; `grow-data`
enlarges the file system at first boot. `os/qemu/run-qemu.sh` builds the QEMU `/data` disk with the same script, so the
smoke test boots exactly this layout.

The alternative, for an image that carries an archive (`/usr/share/eitri/seed/data-seed-<version>-<flavor>.tar.zst`
plus its manifest), is `eitri-seed.service` (`usr/libexec/eitri/seed-first-boot`): it installs the seed once on the first
boot when `/data/eitri/state/seed-installed.env` is missing, before `eitri-prepare`, Klipper, Moonraker and Klarity
start. It costs the archive size in both RAUC root slots, so the shipped image does not carry one.

### 11.2 In-place installer (DOS boot backend, decision 1)

`install.json` lists `files.data_seed.<flavor>` (`url`, `sha256`, `size`; section 8 of `BUNDLE.md`). The installer fetches the
chosen flavor (or reads it from a USB `--bundle-dir`), checks the digest from the **signed** `install.json`, and stage 2
runs `eitri-seed-install --archive <file> --sha256 <digest> --flavor <chosen> --data /mnt/data --mode install` against
the freshly created data file system before it switches the boot to the new system. Stage 2 adds `eitri/migrate/`,
`eitri/state/{install.env,backup.env}`, `eitri/mcu/...` afterwards; those are outside the seed. `eitri/flavor` is
shipped by the seed; the installer does not need to write it. `layout.data_min_mib` (4096) covers the unpacked size
with room to spare. The tool needs `zstd`, `tar` (GNU or BusyBox), `sha256sum`, `awk`, `df` (all in the stage-2 list).

### 11.3 eitri-update (release channel) and flavor switching

Implemented by the installer lane (`installer/docs/BUNDLE.md` section 2, `eitri_update/seed.py`, `mcufw.py`): the channel
manifest lists first-class bundle kinds `seed` (with a signed companion manifest) and `mcu` (per flavor; a seed needs its
MCU firmware), not a RAUC bundle with hooks. `eitri-update upgrade` checks the seed manifest (version, flavor, pinned
commit, image ABI, glibc, unpacked size) before any big download or the OS slot write. After the reboot into the new OS,
`eitri-prepare` unpacks the MCU firmware, runs `eitri-seed-install --mode update --keep-old`, re-runs the layer's
`overlay-install.sh` (from `/data/eitri/klipper-layer/scripts`), flashes both MCUs and starts the services; the commit
after the health check runs `--mode finish`, a failure restores the replaced trees (`--mode rollback`) before the slot
rollback. A **flavor switch** (`eitri-update switch-flavor klipper|kalico`, the configurator's `switch-flavor` helper) is
the same with the other flavor's seed from the installed release and no reboot.

### 11.4 Upstream mode

Opt-in. The clones are ordinary git repositories on the upstream branch, so Moonraker's update manager can `git pull`
them; its `requirements:` files (`scripts/klippy-requirements.txt`, `scripts/moonraker-requirements.txt`) are then
installed into the venvs **by pip on the printer, from the network**, with whatever those files say at that commit. Those
files pin versions the seed deliberately replaced (section 4), and Klipper's `klippy-requirements.txt` still says
`Jinja2==2.11.3`. See section 15.

## 12. Building, reproducibility and CI

```
make -C seed build            # both flavors into $(BUILD)/seed/out (1 to 3 minutes)
make -C seed verify           # archive checks + the seed's venvs under qemu-aarch64
make -C seed repro            # two builds from empty work directories, byte-compared
make -C seed seed             # the test suite (offline; heavy parts skip when seeds/image/qemu are absent)
make -C seed lock             # regenerate requirements/*.txt (needs network; review the diff)
```

`seed/build-seed.sh` does, per flavor: fetch the pinned clones (`release.lock`) -> stage them (branch, remote, repack,
stat-free index) -> copy extras/layer and run `overlay-install.sh` -> cross-compile `c_helper.so` -> build the venvs from
the locks (+ cross-compiled sdists, + the Klarity wheel built from `klarity/`) -> check every ELF object -> normalise
mtimes and modes, pre-compile byte-code -> write `eitri/flavor`, `eitri/seed/*` -> tar+zstd -> manifest -> verify.

* **Inputs**: release.lock (commits, Python version, uv version), `seed/requirements/*`, `klarity/`, `klipper-layer/`,
  the cross toolchain, the image's Python headers and sysconfig data. **Nothing is taken from the network without a
  digest**: git objects are verified by commit id and tag, wheels by `--require-hashes`, sdists by `sdists.toml`.
* **Reproducible**: two builds from empty work directories, and builds with uv 0.11.19 and 0.13.4 (the release.lock pin),
  produced byte-identical archives for both flavors. What is fixed: file order, owners, modes (normalised), mtimes,
  zstd parameters (the zstd *version* is an input too: level-19 output can differ between releases),
  git pack layout and config, index contents, wheel RECORD files (installer bookkeeping removed),
  byte-code (compiled with the build host's Python 3.14: **the minor version must be 3.14; the patch version of the
  build host's Python can change the byte-code**), compiler flags (`-frandom-seed`, no build id, relative paths). The
  toolchain is an input: a different gcc gives a different `c_helper.so` and `streaming_form_data` object, hence a
  different archive; the shipped seeds are the ones CI built, and the manifest records compiler and header versions.
* **CI** (`.github/workflows/data-seed.yml`): pull requests build both flavors and run everything; a `v*` tag also
  rebuilds from scratch and requires identical archives, then attaches the archives, manifests and `SHA256SUMS-<flavor>`
  to the draft release. CI has no Buildroot tree: it uses Ubuntu's `gcc-aarch64-linux-gnu`, a pinned
  python-build-standalone aarch64 CPython for the headers and sysconfig data and for the qemu-user smoke test
  (`requirements/ci-toolchain.toml`, SHA-256 checked, `tools/ci-toolchain.sh`), and `EITRI_GLIBC_MAX=2.42`.
  Third-party actions are pinned by commit SHA.
* **Environment variables** are listed at the top of `build-seed.sh` (`EITRI_BR_OUT`, `EITRI_CROSS_CC`,
  `EITRI_PY_INCLUDE`, `EITRI_SYSCONFIGDATA`, `EITRI_SYSROOT`, `EITRI_GLIBC_MAX`, `EITRI_SEED_EPOCH`, `UV`, ...).
* **Verification under emulation** (`verify-seed.sh`): the archive is extracted into a hard-linked copy of the image
  root and `smoke_klippy.py`, `smoke_moonraker.py` and `smoke_klarity.py` (in `tools/guest/`) run with the *image's*
  `python3.14` and glibc under `qemu-aarch64 -L`, with `argv[0]` set to the venv's `bin/python` so `pyvenv.cfg` applies.
  They check `sys.prefix`, all imports, that pre-compiled byte-code matches its source, that the extras are linked and
  import (153 klippy modules for Klipper, 164 for Kalico), that `chelper.get_ffi()` loads the prebuilt library **without
  calling a compiler** and that the library works, that the cross-compiled `streaming_form_data` parses a multipart
  body, and that Klarity and its console script are intact. qemu-user limits: it cannot exec child processes (no
  binfmt), does not follow absolute symlinks into the emulated root (the throw-away copy gets relative links) and does
  not translate `chdir`. It proves the code loads and runs; it does not replace a run on the printer.

## 13. What the OS image must provide

* `/usr/bin/python3` -> CPython **3.14.x**, built with `ctypes`, `ssl`, `sqlite3`, `zlib`, `lzma`, `bz2`, `_ctypes`
  (Buildroot's python3 does; the seed's `target.python` is the release.lock value).
* glibc >= 2.34 and the shared libraries in `requirements/image-libs.txt` (`libc`, `libm`, `libdl`, `libpthread`, `librt`,
  `libgcc_s`, `libstdc++`, `libz`, the loader): everything else is inside the wheels. `check-elf` fails the build when a
  new wheel needs another one. A test compares the list with the Buildroot target.
* **`libsodium` (`libsodium.so.26`): missing in the current image configuration, and Moonraker cannot start without it.**
  `moonraker/components/authorization.py` imports `libnacl.sign` at module level and `libnacl` loads libsodium through
  `ctypes` at import time. Add `BR2_PACKAGE_LIBSODIUM=y`. The seed test `test_image_provides_libsodium_for_moonraker` is
  an expected failure until then.
* `git` (Moonraker's update manager and klippy's version string run it), `/dev/ttyS*` access for the `eitri` user (not a
  seed matter), and the `eitri` user with uid/gid 1000.
* No compiler, no `pip` on the system path: the venvs bring their own `pip`.

## 14. Adding a dependency

1. Add it to the right `requirements/<venv>.in` and run `make -C seed lock`. If uv says "no usable wheels", the package
   has no aarch64/cp314 wheel: go to 2. Otherwise commit the new `.txt`.
2. Add an `[[sdist]]` entry to `requirements/sdists.toml` (`name`, `version`, `venv`, `url`, `sha256`, optional `prune`),
   put the package's *dependencies* (not the package) in the `.in`, rebuild. Works for setuptools-style sdists whose
   extensions build with plain `gcc`; Rust (maturin) or CMake projects need their own recipe in `tools/seedtool.py`.
   The manifest records the sdist and wheel digests automatically.
3. If `check-elf` complains about a library, add it to the image (Buildroot) first, then to `requirements/image-libs.txt`.
4. `make -C seed build verify`; add an import (or better, a behaviour check) for the package to the matching
   `tools/guest/smoke_*.py`.

## 15. Open items and limits

* **libsodium missing from the image** (section 13): Moonraker will crash at start-up until the OS lane adds it.
* **Not run on a printer.** Everything above is proven by static checks and by `qemu-aarch64` user-mode emulation of the
  image's own Python; real Cortex-A53 timing (first `git status`, first start) and the klippy + MCU handshake are not.
* **Upstream mode and pinned requirements** (11.4): Moonraker's `pip install -r scripts/*-requirements.txt` after a
  `git pull` can downgrade `Jinja2`/`python-can` to versions that do not work on 3.14, or need a compiler. The
  `eitri-update-manager.conf` that `upstream enable` writes should drop the `requirements:` lines or point them at
  `/data/eitri/seed/locks/` (installer lane).
* **Size.** 55 and 94 MiB compressed. Candidates if it matters: drop `apprise` (9 MiB, only for notifications), `uvloop`
  (17 MiB), Pillow's bundled libraries (16 MiB, needed for thumbnails). Not done: they are upstream's requirements.
* **Stat-free git index** (section 5): one slow `git status` after install.
* **Pre-compiled byte-code depends on the build host's Python 3.14 patch level** being compatible with the image's
  3.14.5 magic number (it is, within a minor version); a wrong magic number would only cost start-up time, not
  correctness, because Python recompiles.
* The seed ships `.py` sources (decision 13) next to the byte-code.
