#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare the versions and digests pinned in the OS tree with release.lock.

release.lock is the single list of what an Eitri release is built from. The OS
tree has to repeat some of those values (Buildroot package versions, the
defconfig, the hash files), and a copy that drifts is a build that no longer
matches what the lock claims. This script fails on any difference.

Usage: check-pins.py [--buildroot DIR]

With --buildroot (default: ../build/buildroot next to the repository when it
exists) the versions that Buildroot itself provides (Python, nginx, RAUC) are
checked as well.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

OS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = OS_DIR.parent
BOARD = OS_DIR / "board" / "eitri-plus4"


def mk_var(path: Path, name: str) -> str:
    """Value of a simple 'NAME = value' assignment in a package makefile."""
    match = re.search(rf"^{re.escape(name)}\s*=\s*(\S+)\s*$", path.read_text(), re.M)
    if not match:
        raise KeyError(f"{name} not found in {path}")
    return match.group(1)


def hash_for(path: Path, filename: str) -> str:
    """sha256 recorded for FILENAME in a Buildroot .hash file."""
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "sha256" and parts[2] == filename:
            return parts[1]
    raise KeyError(f"no sha256 for {filename} in {path}")


def defconfig_value(name: str) -> str:
    text = (OS_DIR / "configs" / "eitri_plus4_defconfig").read_text()
    match = re.search(rf'^{re.escape(name)}="?([^"\n]*)"?$', text, re.M)
    if not match:
        raise KeyError(f"{name} not set in the defconfig")
    return match.group(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_br = REPO_ROOT.parent / "build" / "buildroot"
    ap.add_argument("--buildroot", type=Path, default=default_br if default_br.is_dir() else None)
    args = ap.parse_args()

    with (REPO_ROOT / "release.lock").open("rb") as handle:
        lock = tomllib.load(handle)

    problems: list[str] = []
    checked = 0

    def expect(what: str, have: str, want: str) -> None:
        nonlocal checked
        checked += 1
        if have != want:
            problems.append(f"{what}: tree has {have!r}, release.lock has {want!r}")

    def attempt(what: str, func) -> None:
        try:
            func()
        except (KeyError, OSError) as exc:
            problems.append(f"{what}: {exc}")

    pkg = OS_DIR / "package"

    # --- Boot chain and kernel -------------------------------------------------
    attempt("kernel version", lambda: expect(
        "kernel version", defconfig_value("BR2_LINUX_KERNEL_CUSTOM_VERSION_VALUE"), lock["os"]["linux"]["version"]))
    attempt("kernel digest", lambda: expect(
        "kernel tarball sha256",
        hash_for(BOARD / "patches/linux/linux.hash", f"linux-{lock['os']['linux']['version']}.tar.xz"),
        lock["os"]["linux"]["sha256"]))
    attempt("u-boot version", lambda: expect(
        "u-boot tarball", defconfig_value("BR2_TARGET_UBOOT_CUSTOM_TARBALL_LOCATION").rsplit("/", 1)[-1],
        f"{lock['os']['uboot']['version']}.tar.gz"))
    attempt("u-boot defconfig", lambda: expect(
        "u-boot board defconfig", defconfig_value("BR2_TARGET_UBOOT_BOARD_DEFCONFIG") + "_defconfig",
        lock["os"]["uboot"]["defconfig"]))
    attempt("tf-a", lambda: expect(
        "TF-A commit", defconfig_value("BR2_TARGET_ARM_TRUSTED_FIRMWARE_CUSTOM_VERSION_VALUE"),
        lock["os"]["tfa"]["commit"]))
    attempt("tf-a platform", lambda: expect(
        "TF-A platform", defconfig_value("BR2_TARGET_ARM_TRUSTED_FIRMWARE_PLATFORM"), lock["os"]["tfa"]["platform"]))

    # --- Packages written for Eitri ---------------------------------------------
    def tag(version: str) -> str:
        return version.removeprefix("v")

    attempt("go2rtc version", lambda: expect(
        "go2rtc version", mk_var(pkg / "go2rtc/go2rtc.mk", "GO2RTC_VERSION"), tag(lock["camera"]["go2rtc"]["version"])))
    attempt("go2rtc digest", lambda: expect(
        "go2rtc sha256", hash_for(pkg / "go2rtc/go2rtc.hash", "go2rtc_linux_arm64"),
        lock["camera"]["go2rtc"]["sha256"]))
    attempt("uv version", lambda: expect("uv version", mk_var(pkg / "uv/uv.mk", "UV_VERSION"), lock["uv"]["version"]))
    attempt("uv digest", lambda: expect(
        "uv sha256", hash_for(pkg / "uv/uv.hash", "uv-aarch64-unknown-linux-gnu.tar.gz"), lock["uv"]["sha256"]))
    attempt("ustreamer version", lambda: expect(
        "ustreamer version", mk_var(pkg / "eitri-ustreamer/eitri-ustreamer.mk", "EITRI_USTREAMER_VERSION"),
        tag(lock["camera"]["ustreamer"]["version"])))
    attempt("katapult commit", lambda: expect(
        "katapult commit", mk_var(pkg / "eitri-katapult/eitri-katapult.mk", "EITRI_KATAPULT_VERSION"),
        lock["katapult"]["commit"]))
    for ui in ("fluidd", "mainsail"):
        mk = pkg / f"eitri-{ui}" / f"eitri-{ui}.mk"
        var = f"EITRI_{ui.upper()}_VERSION"
        attempt(f"{ui} version", lambda mk=mk, var=var, ui=ui: expect(
            f"{ui} version", mk_var(mk, var), tag(lock["ui"][ui]["version"])))
        attempt(f"{ui} digest", lambda mk=mk, ui=ui: expect(
            f"{ui} sha256", hash_for(mk.with_suffix(".hash"), f"{ui}.zip"), lock["ui"][ui]["sha256"]))

    # --- Versions that Buildroot supplies ---------------------------------------
    if args.buildroot and (args.buildroot / "Makefile").is_file():
        br = args.buildroot
        attempt("buildroot version", lambda: expect(
            "Buildroot version", (br / "Makefile").read_text().split("export BR2_VERSION := ", 1)[1].split()[0],
            lock["os"]["buildroot"]["version"]))
        attempt("python version", lambda: expect(
            "Python version",
            mk_var(br / "package/python3/python3.mk", "PYTHON3_VERSION_MAJOR")
            + "." + mk_var(br / "package/python3/python3.mk", "PYTHON3_VERSION").rsplit(".", 1)[-1],
            lock["python"]["version"]))
        attempt("nginx version", lambda: expect(
            "nginx version", mk_var(br / "package/nginx/nginx.mk", "NGINX_VERSION"), lock["os"]["nginx"]["version"]))
        attempt("rauc version", lambda: expect(
            "RAUC version", mk_var(br / "package/rauc/rauc.mk", "RAUC_VERSION"), lock["os"]["rauc"]["version"]))
    else:
        print("check-pins: no Buildroot checkout found, skipping Python, nginx and RAUC", file=sys.stderr)

    for problem in problems:
        print(f"MISMATCH {problem}")
    if problems:
        return 1
    print(f"pins OK: {checked} values agree with release.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
