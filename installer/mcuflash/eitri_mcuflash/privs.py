# SPDX-License-Identifier: Apache-2.0
"""Where root ends: commands that touch the service user's files on /data run AS the service user.

eitri-update and eitri-mcuflash run as root (systemd units, or the eitri account through sudo).  The Klipper/Kalico
clone, the klipper-layer, printer_data and the MCU build trees on /data belong to the service user ("eitri"), which
also runs the unauthenticated configurator, Moonraker, Klipper and Klarity.  Anything root executes out of those trees
(overlay-install.sh, spi_flash.py, a firmware `make`) and every git command in them (core.fsmonitor, hooks and filter
drivers are programs named by the repository's own config) would hand root to whoever can write there.  So:

  * as_service_user(argv) wraps argv so that it runs with the service user's uid, gid and groups and a fresh
    environment; when we are not root it returns argv unchanged (tests, checkouts);
  * as_owner_of(tree, argv) does the same for code that comes out of one tree (spi_flash.py and make from the clone):
    it runs as the tree's owner, which on Eitri is the service user and on the stock system whoever owns the clone;
  * git_argv() adds -c overrides that switch off fsmonitor and hooks and empty the safe.directory list, and, when we
    are root, runs git as the owner of the tree (the service user on Eitri).  The overrides alone are not enough (a
    repository's config can also name filter drivers that `git status` / `describe --dirty` run): the identity switch
    is the boundary, the overrides are a second line.

The switch is a few lines of Python run by this interpreter (python3 -I, nothing imported from anywhere writable):
initgroups, setgid, setuid, a check that root cannot be regained, then exec with a clean environment.  The image's
setpriv is busybox's, which cannot change uids, and util-linux's runuser/setpriv are not built.
"""
from __future__ import annotations

import os
import pwd
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import FlashError

SERVICE_USER = "eitri"
# -c overrides for every git command run over a tree that is not root's: no fsmonitor program, no hooks, and only the
# trees this process names itself count as safe (an empty value clears the list).
GIT_HARDENING = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-c", "safe.directory=")
MARK = "--eitri-run-as"
# argv: MARK UID GID USER(or "") [NAME=VALUE ...] -- PROG ARGS...
RUN_AS = r"""
import os, pwd, sys
a = sys.argv[1:]
assert a[0] == "--eitri-run-as"
uid, gid, user = int(a[1]), int(a[2]), a[3]
sep = a.index("--")
home = "/"
try:
    home = pwd.getpwuid(uid).pw_dir or "/"
except KeyError:
    pass
env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": home, "LANG": "C.UTF-8"}
if user:
    env["USER"] = env["LOGNAME"] = user
for kv in a[4:sep]:
    k, _, v = kv.partition("=")
    env[k] = v
cmd = a[sep + 1:]
if os.geteuid() == 0:
    if uid == 0:
        sys.exit("eitri run-as: refusing to switch to uid 0")
    if user:
        os.initgroups(user, gid)
    else:
        os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    try:
        os.setuid(0)
    except OSError:
        pass
    else:
        sys.exit("eitri run-as: root could be regained")
if os.getuid() != uid or os.geteuid() != uid or os.getgid() != gid or os.getegid() != gid:
    sys.exit("eitri run-as: could not switch to %d:%d" % (uid, gid))
try:
    os.execvpe(cmd[0], cmd, env)
except OSError as exc:
    sys.exit("eitri run-as: cannot execute %s: %s" % (cmd[0], exc.strerror))
"""

# Tests replace these.
geteuid = os.geteuid
getpwnam = pwd.getpwnam
getpwuid = pwd.getpwuid
python = sys.executable or "/usr/bin/python3"


def service_ids(user: str = SERVICE_USER) -> Tuple[int, int]:
    try:
        pw = getpwnam(user)
    except KeyError:
        raise FlashError("the service user %r does not exist; refusing to run its files as root" % user, code="no_service_user")
    if pw.pw_uid == 0:
        raise FlashError("the service user %r is root; refusing to run its files as root" % user, code="no_service_user")
    return pw.pw_uid, pw.pw_gid


def is_root() -> bool:
    return geteuid() == 0


def run_as(uid: int, gid: int, user: str, argv: Sequence[str], keep_env: Optional[Dict[str, str]] = None) -> List[str]:
    """argv run as uid:gid (user's supplementary groups, or none when user is ""), with a fresh environment plus keep_env."""
    return ([python, "-I", "-c", RUN_AS, MARK, str(uid), str(gid), user]
            + ["%s=%s" % kv for kv in sorted((keep_env or {}).items())] + ["--"] + list(argv))


def parse_run_as(argv: Sequence[str]) -> Optional[Dict[str, object]]:
    """The parts of a run_as() command line ({"uid", "gid", "user", "env", "argv"}), or None if argv is not one."""
    argv = list(argv)
    if len(argv) < 8 or argv[1:4] != ["-I", "-c", RUN_AS] or argv[4] != MARK:
        return None
    sep = argv.index("--", 8)
    return {"uid": int(argv[5]), "gid": int(argv[6]), "user": argv[7],
            "env": dict(kv.partition("=")[::2] for kv in argv[8:sep]), "argv": argv[sep + 1:]}


def as_service_user(argv: Sequence[str], *, keep_env: Optional[Dict[str, str]] = None, user: str = SERVICE_USER) -> List[str]:
    """argv run with the service user's identity when we are root; unchanged otherwise.
    keep_env: variables the command needs (the environment is replaced)."""
    if not is_root():
        return list(argv)
    uid, gid = service_ids(user)
    return run_as(uid, gid, user, argv, keep_env)


def _owner(tree: str, user: str) -> Optional[Tuple[int, int, str]]:
    """(uid, gid, name) to run *tree*'s code as: its owner, the service user when it does not exist yet, None for a
    root-owned tree (nothing to switch to)."""
    try:
        st = os.stat(tree)
    except OSError:
        uid, gid = service_ids(user)
        return uid, gid, user
    if st.st_uid == 0:
        return None
    try:
        name = getpwuid(st.st_uid).pw_name
    except KeyError:
        name = ""
    return st.st_uid, st.st_gid, name


def as_owner_of(tree: str, argv: Sequence[str], *, keep_env: Optional[Dict[str, str]] = None, user: str = SERVICE_USER) -> List[str]:
    """argv, which runs code out of *tree*, as the owner of *tree* when we are root (on Eitri the service user; on the stock
    system whoever owns the clone).  Unchanged when we are not root or the tree is root's."""
    if not is_root():
        return list(argv)
    owner = _owner(tree, user)
    if owner is None:
        return list(argv)
    return run_as(owner[0], owner[1], owner[2], argv, keep_env)


def git_argv(repo: str, *args: str, user: str = SERVICE_USER) -> List[str]:
    """`git -C repo args...`, hardened; when we are root, run as the owner of *repo* (root only for a root-owned tree;
    the service user when *repo* does not exist yet)."""
    return as_owner_of(repo, ["git", *GIT_HARDENING, "-C", repo, *args], user=user)


def hand_to_owner_of(tree: str, *paths: str, user: str = SERVICE_USER) -> None:
    """Give files root just created for a command run with as_owner_of(tree) to that owner (no-op when not root)."""
    if not is_root():
        return
    owner = _owner(tree, user)
    if owner is None:
        return
    for path in paths:
        os.chown(path, owner[0], owner[1], follow_symlinks=False)
