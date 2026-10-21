# SPDX-License-Identifier: Apache-2.0
"""'Follow upstream' mode: toolchain add-on + Moonraker update_manager switch.

Default (Eitri channel): updates arrive as signed release bundles; Moonraker's
update manager is told not to touch Klipper/Moonraker.  Upstream mode installs a
gcc + arm-none-eabi-gcc add-on under /data/eitri/toolchain and points Moonraker's
update_manager at the real upstream repositories, so `git pull` (via Moonraker or
by hand), any branch, and on-device MCU builds all work.  The Klipper/Kalico
tree itself is never modified by either mode.
"""
from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import tarfile
from typing import Any, Dict, Iterable, List, Optional

from eitri_mcuflash import privs
from eitri_mcuflash.errors import FlashError
from eitri_mcuflash.events import EventSink
from eitri_mcuflash.fsutil import atomic_write, fsync_dir
from eitri_mcuflash.proc import Runner, run

from . import fetch
from .manifest import Addon, Manifest
from .state import State

DEFAULT_ORIGINS = {
    "klipper": {"origin": "https://github.com/Klipper3d/klipper.git", "branch": "master"},
    "kalico": {"origin": "https://github.com/KalicoCrew/kalico.git", "branch": "main"},
    "moonraker": {"origin": "https://github.com/Arksine/moonraker.git", "branch": "master"},
}

# Lives next to moonraker.conf, which has `[include eitri-update-manager.conf]`.  Not inside
# printer_data/config/eitri: that is a symlink to the (read-only) klipper-layer config set.
ETC_LINK = "eitri-update-manager.conf"


class UpstreamPaths:
    def __init__(self, data_root: str = "/data", printer_data: str = "/data/printer_data") -> None:
        self.eitri = os.path.join(data_root, "eitri")
        self.toolchain = os.path.join(self.eitri, "toolchain")
        self.templates = os.path.join(self.eitri, "moonraker")
        self.config_dir = os.path.join(printer_data, "config")
        self.link = os.path.join(self.config_dir, ETC_LINK)
        self.updates = os.path.join(self.eitri, "updates")
        self.klipper = os.path.join(data_root, "firmware", "klipper")
        self.moonraker = os.path.join(data_root, "firmware", "moonraker")
        self.seed_locks = os.path.join(self.eitri, "seed", "locks")       # the data seed's hash-pinned requirement sets
        self.venv_klippy = os.path.join(data_root, "venv", "klippy")
        self.venv_moonraker = os.path.join(data_root, "venv", "moonraker")


def render_eitri_update_manager() -> str:
    return (
        "# Managed by eitri-update (Eitri channel). Do not edit; `eitri-update upstream enable` replaces this link.\n"
        "[update_manager]\n"
        "enable_auto_refresh: False\n"
        "enable_system_updates: False\n"
    )


def render_upstream_update_manager(flavor: str, paths: UpstreamPaths, upstream: Optional[Dict[str, Dict[str, str]]] = None) -> str:
    up = dict(DEFAULT_ORIGINS)
    for key, value in (upstream or {}).items():
        if key in up and isinstance(value, dict):
            up[key] = {**up[key], **{k: str(v) for k, v in value.items() if k in ("origin", "branch")}}
    k, m = up[flavor], up["moonraker"]
    # The upstream repositories' own requirement files pin versions the seed deliberately replaced (Jinja2==2.11.3, an old
    # python-can ...) and would downgrade the venvs on a 3.14 image.  The data seed's lock files are given instead when they
    # exist: Moonraker compares the file's content before and after an update, so it never installs anything unless a forced
    # recovery asks for the pinned set.  Without the seed's locks (a bench image) no pip step is configured at all.
    reqs = {}
    for name, key in (("klippy", "klipper"), ("moonraker", "moonraker")):
        lock = os.path.join(paths.seed_locks, name + ".txt")
        reqs[key] = "requirements: %s\n" % lock if os.path.isfile(lock) else ""
    return (
        "# Managed by eitri-update (upstream mode). `eitri-update upstream disable` returns to the Eitri channel.\n"
        "[update_manager]\n"
        "enable_auto_refresh: True\n"
        "refresh_interval: 168\n"
        "enable_system_updates: False\n"
        "\n"
        "[update_manager klipper]\n"
        "type: git_repo\n"
        "channel: dev\n"
        "path: %s\n"
        "origin: %s\n"
        "primary_branch: %s\n"
        "virtualenv: %s\n"
        "%smanaged_services: klipper\n"
        "info_tags:\n"
        "  desc=%s (upstream mode)\n"
        "\n"
        "[update_manager moonraker]\n"
        "type: git_repo\n"
        "channel: dev\n"
        "path: %s\n"
        "origin: %s\n"
        "primary_branch: %s\n"
        "virtualenv: %s\n"
        "%smanaged_services: moonraker\n"
    ) % (paths.klipper, k["origin"], k["branch"], paths.venv_klippy, reqs["klipper"], flavor.capitalize(),
         paths.moonraker, m["origin"], m["branch"], paths.venv_moonraker, reqs["moonraker"])


def _atomic_symlink(target: str, link: str) -> None:
    os.makedirs(os.path.dirname(link), exist_ok=True)
    tmp = link + ".new"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)
    fsync_dir(os.path.dirname(link))


def check_archive_members(members: Iterable[Any]) -> None:
    """Refuse a toolchain archive that could write outside its staging directory.  Names are checked, and so are
    links: a symlink to an absolute path (or one that climbs out of the top directory) followed by a regular
    member beneath it would otherwise be written through the link.  Relative links inside the tree are fine (real
    toolchains have many); devices and fifos are not."""
    for m in members:
        name = m.name
        parts = name.split("/")
        if name.startswith("/") or ".." in parts:
            raise FlashError("toolchain archive contains an unsafe path: %s" % name, code="bad_archive")
        if m.isdev() or m.isfifo():
            raise FlashError("toolchain archive contains a device or fifo: %s" % name, code="bad_archive")
        if m.issym():
            target = m.linkname
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            top = parts[0]
            if target.startswith("/") or not (joined == top or joined.startswith(top + "/")):
                raise FlashError("toolchain archive contains a link that leaves the toolchain: %s -> %s" % (name, target), code="bad_archive")
        elif m.islnk():
            target = m.linkname
            tparts = target.split("/")
            if target.startswith("/") or ".." in tparts or tparts[0] != parts[0]:
                raise FlashError("toolchain archive contains a hard link that leaves the toolchain: %s -> %s" % (name, target), code="bad_archive")


def _list_archive(archive: str) -> List[Any]:
    """Members of a .tar.zst / .tar.xz, read without extracting anything."""
    try:
        if archive.endswith(".zst"):
            proc = subprocess.Popen(["zstd", "-dc", archive], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                    members = list(tf)
            finally:
                proc.stdout.close()
                err = proc.stderr.read().decode("utf-8", "replace")
                proc.stderr.close()
                if proc.wait() != 0:
                    raise FlashError("cannot read the toolchain archive: %s" % err.strip(), code="bad_archive")
            return members
        with tarfile.open(archive, mode="r:xz") as tf:
            return list(tf)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise FlashError("cannot read the toolchain archive: %s" % exc, code="bad_archive") from exc


def check_tree_contained(root: str) -> None:
    """After extraction: every path must resolve inside *root* (belt and braces for check_archive_members)."""
    real_root = os.path.realpath(root)
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            if os.path.islink(path):
                resolved = os.path.realpath(path)
                if resolved != real_root and not resolved.startswith(real_root + os.sep):
                    raise FlashError("the unpacked toolchain has a link that leaves it: %s -> %s" % (path, os.readlink(path)), code="bad_archive")


class UpstreamMode:
    def __init__(self, state: State, events: EventSink, paths: UpstreamPaths, runner: Runner = run, opener=fetch._open) -> None:
        self.state, self.events, self.paths, self.runner, self.opener = state, events, paths, runner, opener

    # -- inspection ------------------------------------------------------------
    def toolchain_installed(self) -> bool:
        """Complete = the compiler is there AND the VERSION marker, which is written last: a torn install has no marker."""
        return (os.path.isfile(os.path.join(self.paths.toolchain, "bin", "arm-none-eabi-gcc"))
                and self._toolchain_version() is not None)

    def status(self) -> Dict[str, Any]:
        link = self.paths.link
        target = os.readlink(link) if os.path.islink(link) else None
        return {
            "channel": self.state.channel,
            "flavor": self.state.flavor,
            "toolchain": self.toolchain_installed(),
            "toolchain_version": self._toolchain_version(),
            "update_manager": os.path.basename(target) if target else None,
        }

    def _toolchain_version(self) -> Optional[str]:
        try:
            with open(os.path.join(self.paths.toolchain, "VERSION")) as fh:
                return fh.read().strip()
        except OSError:
            return None

    # -- enable ------------------------------------------------------------------
    def enable(self, manifest: Manifest, *, dry_run: bool = False, restart: bool = True, progress=None) -> None:
        flavor = self.state.flavor
        addon = manifest.addons.get("toolchain")
        have = self.toolchain_installed()
        if have and addon is not None and self._toolchain_version() != addon.version:
            have = False                              # a different (or unmarked) version: install the one the channel names
        if addon is None and not have:
            raise FlashError("this channel does not publish a toolchain add-on", code="no_addon")
        self.events.step("upstream", "start", msg="enabling upstream mode (%s)" % flavor)
        self._check_tree(flavor)
        if dry_run:
            self.events.info("[dry-run] would %sinstall the toolchain add-on, switch update_manager to upstream and restart moonraker"
                             % ("re" if have else ""))
            self.events.step("upstream", "skipped", msg="dry run")
            return
        if not have:
            self._install_toolchain(addon, progress)  # type: ignore[arg-type]
        atomic_write(os.path.join(self.paths.templates, "update_manager.upstream.conf"),
                     render_upstream_update_manager(flavor, self.paths, manifest.upstream), mode=0o644)
        atomic_write(os.path.join(self.paths.templates, "update_manager.eitri.conf"), render_eitri_update_manager(), mode=0o644)
        _atomic_symlink(os.path.join(self.paths.templates, "update_manager.upstream.conf"), self.paths.link)
        self.state.set_text("channel", "upstream")
        self.state.add_history("upstream-enabled", flavor=flavor)
        if restart:
            self._restart_moonraker()
        self.events.step("upstream", "ok", msg="upstream mode is on; `git pull` and on-device MCU builds are available")

    def _check_tree(self, flavor: str) -> None:
        git_dir = os.path.join(self.paths.klipper, ".git")
        if not os.path.isdir(git_dir):
            raise FlashError("%s is not a git clone, so there is nothing to follow upstream" % self.paths.klipper, code="no_clone")
        # git status reads the clone's own config (core.fsmonitor, hooks): hardened, and as the service user when root.
        res = self.runner(privs.git_argv(self.paths.klipper, "status", "--porcelain"))
        if res.returncode == 0 and res.output.strip():
            self.events.warn("the Klipper tree has local modifications; Moonraker will refuse to update it until they are cleaned up")

    def _install_toolchain(self, addon: Addon, progress=None) -> None:
        needed = addon.installed_size + addon.size + 256 * 1024 * 1024
        free = fetch.free_bytes(self.paths.eitri)
        if free < needed:
            raise FlashError("not enough space on /data for the toolchain: need %d MiB, have %d MiB" % (needed >> 20, free >> 20),
                             code="no_space", recovery="Free space (old gcodes, timelapses) or stay on the Eitri channel.")
        os.makedirs(self.paths.updates, exist_ok=True)
        archive = os.path.join(self.paths.updates, "toolchain-%s.tar" % addon.version + (".zst" if addon.url.endswith(".zst") else ".xz"))
        self.events.step("toolchain-download", "start", msg="downloading the toolchain add-on (%d MiB)" % (addon.size >> 20))
        fetch.download(addon.url, archive, sha256=addon.sha256, size=addon.size, opener=self.opener,
                       progress=(lambda got, total: self.events.progress("toolchain-download", 100.0 * got / total)))
        self.events.step("toolchain-download", "ok")
        self.events.step("toolchain-install", "start", msg="unpacking")
        staging = self.paths.toolchain + ".new"
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        flag = ["--zstd"] if archive.endswith(".zst") else ["-J"]
        try:
            check_archive_members(_list_archive(archive))
        except FlashError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        res = self.runner(["tar"] + flag + ["-xf", archive, "-C", staging, "--no-same-owner", "--strip-components=1"])
        if res.returncode != 0:
            shutil.rmtree(staging, ignore_errors=True)
            raise FlashError("unpacking the toolchain failed: %s" % res.output.strip(), code="bad_archive")
        try:
            check_tree_contained(staging)
        except FlashError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if not os.path.isfile(os.path.join(staging, "bin", "arm-none-eabi-gcc")):
            shutil.rmtree(staging, ignore_errors=True)
            raise FlashError("the toolchain archive does not contain bin/arm-none-eabi-gcc", code="bad_archive")
        # Durable before it becomes visible: on ext4 a power cut after the rename could otherwise leave the new
        # directory entry pointing at zero-length files.  VERSION is the completeness marker and is written last.
        os.sync()
        atomic_write(os.path.join(staging, "VERSION"), addon.version + "\n", mode=0o644)
        fsync_dir(staging)
        old = self.paths.toolchain + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(self.paths.toolchain):
            os.rename(self.paths.toolchain, old)
        os.rename(staging, self.paths.toolchain)
        fsync_dir(os.path.dirname(self.paths.toolchain))
        shutil.rmtree(old, ignore_errors=True)
        os.unlink(archive)
        self.events.step("toolchain-install", "ok", msg="toolchain %s installed" % addon.version)

    # -- disable ---------------------------------------------------------------------
    def disable(self, *, remove_toolchain: bool = True, dry_run: bool = False, restart: bool = True) -> None:
        self.events.step("upstream", "start", msg="returning to the Eitri channel")
        if dry_run:
            self.events.info("[dry-run] would switch update_manager back%s" % (" and remove the toolchain" if remove_toolchain else ""))
            self.events.step("upstream", "skipped", msg="dry run")
            return
        atomic_write(os.path.join(self.paths.templates, "update_manager.eitri.conf"), render_eitri_update_manager(), mode=0o644)
        _atomic_symlink(os.path.join(self.paths.templates, "update_manager.eitri.conf"), self.paths.link)
        self.state.set_text("channel", "eitri")
        # The tree may now sit on any upstream commit; the next Eitri release resets it to its pinned one.
        self.state.update(needs_pin_reset=True)
        self.state.add_history("upstream-disabled")
        if remove_toolchain and os.path.isdir(self.paths.toolchain):
            shutil.rmtree(self.paths.toolchain)
        if restart:
            self._restart_moonraker()
        self.events.step("upstream", "ok", msg="back on the Eitri channel; the next `eitri-update upgrade` re-pins Klipper and the MCU firmware")

    def _restart_moonraker(self) -> None:
        res = self.runner(["systemctl", "restart", "moonraker"])
        if res.returncode != 0:
            self.events.warn("could not restart moonraker (%s); restart it to apply the change" % res.output.strip())
