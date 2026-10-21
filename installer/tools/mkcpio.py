#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write a gzip-compressed "newc" cpio archive (Linux initramfs format) with only the stdlib.

The stock printer image has Python 3.7 but no guarantee of a cpio binary, and
the installer needs to append its own files (scripts, config, kernel modules of
the *running* stock kernel) to the CI-built RAM-installer initramfs.  Linux
unpacks concatenated archives in order, so the result is simply `cat`-ed after
the base archive.

    mkcpio.py OUT.cpio.gz --dir etc/eitri --file usr/lib/eitri/stage2.sh=./stage2.sh:0755 \
                          --link lib/modules=/lib/modules --root-owner
"""
import argparse
import gzip
import os
import stat
import sys

NEWC_MAGIC = b"070701"


def _pad4(n: int) -> bytes:
    return b"\0" * (-n % 4)


class Writer:
    def __init__(self, fh):
        self.fh = fh
        self.ino = 0

    def _entry(self, name: str, mode: int, data: bytes = b"", mtime: int = 0, nlink: int = 1) -> None:
        self.ino += 1
        nm = name.encode("utf-8") + b"\0"
        hdr = NEWC_MAGIC + b"".join(
            b"%08X" % v
            for v in (self.ino, mode, 0, 0, nlink, mtime, len(data), 0, 0, 0, 0, len(nm), 0)
        )
        self.fh.write(hdr + nm + _pad4(len(hdr) + len(nm)))
        self.fh.write(data + _pad4(len(data)))

    def dir(self, name: str, mode: int = 0o755) -> None:
        self._entry(name, stat.S_IFDIR | mode, nlink=2)

    def file(self, name: str, data: bytes, mode: int = 0o644) -> None:
        self._entry(name, stat.S_IFREG | mode, data)

    def symlink(self, name: str, target: str) -> None:
        self._entry(name, stat.S_IFLNK | 0o777, target.encode("utf-8"))

    def close(self) -> None:
        self._entry("TRAILER!!!", 0, nlink=1)


def parent_dirs(path: str):
    parts = path.strip("/").split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output")
    ap.add_argument("--dir", action="append", default=[], metavar="DEST")
    ap.add_argument("--file", action="append", default=[], metavar="DEST=SRC[:MODE]")
    ap.add_argument("--tree", action="append", default=[], metavar="DEST=SRCDIR", help="add a directory tree")
    ap.add_argument("--link", action="append", default=[], metavar="DEST=TARGET")
    args = ap.parse_args(argv)

    made = set()
    with gzip.GzipFile(args.output, "wb", mtime=0) as gz:
        w = Writer(gz)

        def ensure(path: str) -> None:
            """Create *path* and its parents as directories (once)."""
            for d in parent_dirs(path + "/x"):
                if d not in made:
                    made.add(d)
                    w.dir(d)

        def ensure_parent(path: str) -> None:
            if "/" in path:
                ensure(path.rsplit("/", 1)[0])

        for d in args.dir:
            ensure(d.strip("/"))
        for spec in args.file:
            dest, _, rest = spec.partition("=")
            src, _, mode = rest.rpartition(":") if rest.count(":") and rest.rsplit(":", 1)[1].isdigit() else (rest, "", "")
            ensure_parent(dest.strip("/"))
            with open(src, "rb") as fh:
                data = fh.read()
            w.file(dest.strip("/"), data, int(mode, 8) if mode else (os.stat(src).st_mode & 0o777))
        for spec in args.tree:
            dest, _, srcdir = spec.partition("=")
            for root, dirs, files in os.walk(srcdir):
                dirs.sort()
                rel = os.path.relpath(root, srcdir)
                target = dest.strip("/") if rel == "." else dest.strip("/") + "/" + rel
                ensure(target)
                for f in sorted(files):
                    p = os.path.join(root, f)
                    if os.path.islink(p):
                        w.symlink(target + "/" + f, os.readlink(p))
                    elif os.path.isfile(p):
                        w.file(target + "/" + f, open(p, "rb").read(), os.stat(p).st_mode & 0o777)
        for spec in args.link:
            dest, _, target = spec.partition("=")
            ensure_parent(dest.strip("/"))
            w.symlink(dest.strip("/"), target)
        w.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
