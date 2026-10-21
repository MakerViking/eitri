# Licence compliance for Eitri OS releases

This directory holds the tooling that lets a release meet the source and notice obligations of
the free software in the image. It does not replace legal advice; the open questions are listed
at the end.

| file | purpose |
|---|---|
| `make-sbom.py` | Builds a CycloneDX 1.5 and an SPDX 2.3 SBOM from Buildroot's `legal-info/manifest.csv` (and Klipper/Kalico/Moonraker from `release.lock`). Standard library only. |
| `package-legal.sh` | Runs `make -C os legal-info`, the SBOM, and packs the source and licence archives for a release. |
| `../../.github/workflows/legal.yml` | Runs the above on every `v*` tag (and as a dry run on OS pull requests) and attaches the files to the draft release. |

Run it locally against an existing build:

```
os/legal/package-legal.sh 0.1.0 /tmp/eitri-legal      # needs a configured, extracted Buildroot output
python3 -I os/legal/make-sbom.py --legal-info ../build/br-out/legal-info \
        --release-lock release.lock --version 0.1.0 --out /tmp/sbom   # SBOM only
```

`make -C os legal-info` sets `BR2_DL_DIR`; if you call Buildroot directly, export
`BR2_DL_DIR=<build>/br-dl` yourself or it looks for downloads in its own `dl/` and fails.

## How a release satisfies the GPL and LGPL

Eitri distributes binaries (disk image, RAUC update bundle, in-place installer payload) that contain
GPL and LGPL programs. For each release we publish the **corresponding source alongside the binaries**
(GPLv2 section 3(a), GPLv3 section 6(a)/(d), LGPLv2.1 section 6):

1. **Source tarball, same place as the binaries.** `eitri-<version>-sources.tar` is attached to the
   same GitHub release as `eitri-plus4-<version>.img.zst` and `.raucb`. It is Buildroot's
   `legal-info/sources/`: the exact upstream archive of every package, plus Buildroot's own patches.
   It is never deleted while the binaries are downloadable.
2. **Build recipe.** `eitri-<version>-licenses.tar.xz` carries `buildroot.config` and the licence
   texts; `eitri-<version>-repo-source.tar.gz` is `git archive` of the Eitri tag: the defconfig, DTS,
   kernel and U-Boot config fragments, the local packages (camera, configurator, mcuflash,
   klipper-layer), and the scripts that control the build. Together with the Buildroot version in
   `release.lock` these let anyone rebuild the image (`make -C os image`).
3. **Written offer, as a fallback.** If a release is ever distributed in a form that cannot point to a
   download (a pre-flashed microSD, printers sold with the image), the box and the image carry a
   written offer valid for at least three years for the source on a physical medium at cost
   (GPLv2 3(b)). The offer text is `/usr/share/doc/eitri/SOURCE-OFFER` in the image (see the
   finding "image does not carry notices" below: this file is not installed yet).
4. **Klipper, Kalico, Moonraker.** They are not in the root filesystem; the installer clones them,
   unmodified and pinned, from upstream into `/data`. Where Eitri itself ships them (the data bundle
   `data-seed.tar.zst`), the pinned commit is the source (`release.lock`: `firmware.klipper`,
   `firmware.kalico`, `moonraker`) and the tarball contains the unmodified clone. Eitri's own
   `klipper-layer/extras` are GPL-3.0-or-later and are in the repo-source archive.
5. **Web UIs.** Fluidd and Mainsail are shipped as minified release zips. The preferred form for
   modification is the upstream tag, which `release.lock` pins and the licence files point to; the
   sources tarball includes the zip, and the tag URL is in `NOTICE`.

Every modification we make to a GPL package is either a file in this repository or a Buildroot
patch under `os/board/*/patches/`; both are in the archives above. Today there are no patches
(only hash files), and the kernel and U-Boot are unmodified upstream plus config fragments and a DTS.

Binary-only redistribution of permissively licensed packages (BSD, MIT, Apache, ISC, zlib) requires
reproducing their copyright notices in documentation shipped with the binary. The licence texts in
`legal-info/licenses/` are that documentation; they should be installed in the image.

## What is in the image (Buildroot 2026.02.3, run of 2026-10-21, defconfig `eitri_plus4_defconfig`)

108 target packages (plus 95 host build tools that are not distributed). Full per-package list:
`sbom-summary.txt` in the release, or `legal-info/manifest.csv`.

Counts by class (heuristic, from the licence string; dual-licensed packages count as copyleft
even though the permissive option can be chosen):

* strong copyleft (GPL): 44 packages
* weak copyleft (LGPL, MPL): 15 packages
* permissive (MIT, BSD, ISC, Apache-2.0, Zlib, PSF, curl, public domain): 48 packages
* proprietary / redistributable binary: 1 (`linux-firmware`)

### GPL/LGPL packages that need source distribution

Strong copyleft (GPL): gcc-final (runtime libs, GPL-3.0 with GCC exception), glibc (LGPL-2.1+ library,
GPL programs), linux-headers, linux 6.18.57 (GPL-2.0 with syscall note), U-Boot v2026.10 (GPL-2.0+),
busybox, bash, readline, util-linux, e2fsprogs, dosfstools, kmod, xz (GPL parts), dbus (GPL option),
systemd (LGPL-2.1+, GPL-2.0+ udev), polkit, iproute2, iputils, i2c-tools, usbutils, ethtool, kbd,
less, nano, git, htop, chrony, dnsmasq, rsync, squashfs-tools, nftables, nftables-python, libnftnl,
libatomic_ops (gpl extension), libv4l (utilities), NetworkManager (GPL-2.0+ app), ca-certificates
(script), libcap and zstd (dual, permissive option available), and Eitri's own GPL components:
eitri-ustreamer (GPL-3.0+), eitri-katapult (GPL-3.0+), eitri-fluidd (GPL-3.0+), eitri-mainsail
(GPL-3.0+), eitri-klipper-layer (extras GPL-3.0-or-later).

Weak copyleft (LGPL/MPL, source plus relinking rights; all are shared libraries so the user can
replace them): avahi, libglib2, util-linux-libs, libxcrypt, libdaemon, libmnl, libusb, libnl,
json-glib, libgpiod, libndp, nss-mdns, rauc (LGPL-2.1), libnss and libnspr (MPL-2.0).

Permissive (notices only): expat, bzip2, openssl 3.6.2 (Apache-2.0), zlib, curl, c-ares, libffi,
pcre2, duktape, libevent, python 3.14 and its wheels (aiohttp, yarl, multidict, jinja2, ...), ncurses,
sqlite, openssh, sudo, tmux, jq, libjpeg-turbo, libbsd, libmd, popt, nginx, iw, wpa_supplicant,
wireless-regdb, tzdata, go2rtc (MIT), uv (Apache-2.0 or MIT), TF-A BL31 (BSD-3-Clause), mcuflash,
configurator, camera (Apache-2.0).

### Proprietary and non-GPL blobs

| blob | where | terms | status |
|---|---|---|---|
| `linux-firmware` 20251011, only `rtlwifi/*` (rtl8188e/eu/fu, rtl8192c/cu/de/e/eu/se, rtl8710bu) | `/lib/firmware/rtlwifi/` in the image | `LICENCE.rtlwifi_firmware.txt` (Realtek). Binary redistribution allowed **unmodified**, with the copyright notice and disclaimer; no reverse engineering; patent licence only in combination with an OSI-approved OS. No source obligation. | Redistributable. Licence text must ship with the binary. Buildroot labels the package "Proprietary" and the whole 557 MiB tarball ends up in `legal-info/sources`; `package-legal.sh` drops it from the source tarball (set `KEEP_FIRMWARE_SRC=1` to keep it) but keeps `WHENCE` and the Realtek licence in the licence archive. |
| `wireless-regdb` `regulatory.db` | `/lib/firmware/` | ISC | permissive. |
| Rockchip `rkbin` DDR init (`rk3328_ddr_333MHz_v1.21.bin`) | **not** in the default image; only when built with `make -C os ddrbin-config` | rkbin `LICENSE`: "non-exclusive license to use, copy, distribute the Software"; no decompiling or reverse engineering; notices must be kept; provided "as is". | Redistribution is allowed by that text, but it is not an OSI licence: a ddrbin image must say so, keep the notice, and be labelled non-open. The default image uses U-Boot's open TPL. `make-sbom.py --fail-on-unknown` accepts `rockchip-rkbin` only because it is in `PROPRIETARY_REVIEWED`. |
| Rockchip BL31 | not used; TF-A BL31 is built from source (BSD-3-Clause) | | none |

No Qidi binaries, `.tft`/`.HMI` assets or Cython modules are in the image (clean-room rule).

### Size of the source archive (this build)

* Buildroot `legal-info/sources`: 1.29 GB (1,292,764,856 bytes), 108 packages; already compressed
  archives, so zstd/xz do not shrink it (1.29 GB measured).
* Of that, `linux-firmware` is 557 MiB (all of linux-firmware, though one family is installed).
  Without it: about 709 MB. Next largest: linux 148 MB, gcc 91 MB, nss 75 MB, openssl 53 MB, U-Boot 45 MB.
* `licenses/` is 3.5 MB; the host tools' sources add 163 MB (`host-sources`, not distributed, not included).
* One GitHub release asset may be at most 2 GiB: the trimmed source tar fits with room.

## SBOM

`make-sbom.py` emits `eitri-<version>.cdx.json` (CycloneDX 1.5), `eitri-<version>.spdx.json` (SPDX 2.3)
and `sbom-summary.txt`. Each component has name, version, purl (`pkg:generic`), download location,
Buildroot's licence text verbatim, an SPDX expression only when every part maps to a known identifier
(otherwise `NOASSERTION`; 12 of 111 are `NOASSERTION` because Buildroot gives prose such as
"MIT with advertising clause"), and, with `--hash-sources`, the SHA-256 of the source archive.
Klipper, Kalico and Moonraker are added as `runtime-fetched` components from `release.lock`.
Not yet in the SBOM: the Python wheels of the `/data` venvs (Klipper, Moonraker, Klarity) because the
data seed does not exist yet; add its `uv pip freeze` / `uv.lock` when it does.

## Open findings (need changes outside this directory)

1. **Fixed (hardening run).** `make legal-info` used to fail for `eitri-camera`, `eitri-configurator` and
   `eitri-mcuflash` because `*_LICENSE_FILES` was an absolute path outside the build directory. Each
   package now copies the repository `LICENSE` into its build directory in a `POST_RSYNC_HOOKS` step and
   names it `LICENSE`. `make -C os legal-info` now completes: 107 target packages in the manifest, the
   SBOM tool reports 110 components (with Klipper, Kalico and Moonraker). Buildroot still prints
   "no hash file for LICENSE" for these local packages; it is a warning.
2. **The image carries no licence texts or notices.** Add a post-build step copying
   `legal-info/licenses`, `manifest.csv`, the root `NOTICE`/`os/NOTICE` and a `SOURCE-OFFER` text to
   `/usr/share/doc/eitri/`, or enable a rootfs overlay with the same. (Buildroot's legal-info is a
   separate output, not part of the rootfs.) About 3.5 MB.
3. Fixed: the `os-image.yml` publish step now creates the draft only if `legal.yml` has not, and uploads
   with `--clobber`.
4. `uboot` is listed as version `custom` in the manifest; set `BR2_TARGET_UBOOT_CUSTOM_VERSION` naming
   or document `v2026.10` here and in `NOTICE`.
5. Local package source tarballs name the build machine's path as the "source site"; the SBOM maps
   these to `NOASSERTION`. The repo-source archive is the real source.
