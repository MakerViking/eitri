#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare two check-VM manifests of the stock root and /boot (vm-check.sh): what changed between them.

    compare-manifests.py BEFORE_DIR AFTER_DIR [--spot GLOB ...]

Prints identical / changed / added / removed counts for the stock root and /boot, every changed/added/removed path
(grouped by top directory, at most 40 each), and a spot check of the files that must survive the conversion
unchanged: /home/*/printer_data/config/* and /boot/extlinux/* (extlinux.conf may be renamed extlinux.conf.stock)."""
import fnmatch
import os
import sys


def load(path):
    out = {}
    if not os.path.isfile(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.rstrip("\n")
        if len(line) > 66 and line[64:66] == "  ":
            name = line[66:]
            out[name[2:] if name.startswith("./") else name] = line[:64]
    return out


def loadlinks(path):
    return set(l.rstrip("\n") for l in open(path, encoding="utf-8", errors="replace")) if os.path.isfile(path) else set()


def report(name, a, b):
    same = sorted(k for k in a if k in b and a[k] == b[k])
    changed = sorted(k for k in a if k in b and a[k] != b[k])
    added = sorted(k for k in b if k not in a)
    removed = sorted(k for k in a if k not in b)
    print("%s: %d files before, %d after: %d identical, %d changed, %d added, %d removed"
          % (name, len(a), len(b), len(same), len(changed), len(added), len(removed)))
    for title, lst in (("changed", changed), ("added", added), ("removed", removed)):
        if not lst:
            continue
        groups = {}
        for k in lst:
            top = "/".join(k.split("/")[:3])
            groups.setdefault(top, []).append(k)
        print("  %s (by directory):" % title)
        for top, ks in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:40]:
            print("    %5d  %s%s" % (len(ks), top, ("  e.g. " + ks[0]) if len(ks) > 1 else ""))
    return same, changed, added, removed


def main(argv):
    before, after = argv[1], argv[2]
    sa, sb = load(os.path.join(before, "stock.sha256")), load(os.path.join(after, "stock.sha256"))
    ba, bb = load(os.path.join(before, "boot.sha256")), load(os.path.join(after, "boot.sha256"))
    report("stock root", sa, sb)
    la, lb = loadlinks(os.path.join(before, "stock.links")), loadlinks(os.path.join(after, "stock.links"))
    print("stock root symlinks: %d before, %d after, %d only before, %d only after" % (len(la), len(lb), len(la - lb), len(lb - la)))
    for l in sorted(lb - la)[:10]:
        print("    + %s" % l)
    for l in sorted(la - lb)[:10]:
        print("    - %s" % l)
    report("/boot", ba, bb)
    print("spot check (must be unchanged):")
    bad = 0
    spots = [k for k in sa if fnmatch.fnmatch(k, "home/*/printer_data/config/*")]
    for k in sorted(spots):
        ok = sb.get(k) == sa[k]
        bad += not ok
        print("  %-4s %s  %s" % ("ok" if ok else "DIFF", sa[k][:16], k))
    for k in sorted(x for x in ba if x.startswith("extlinux/")):
        alt = bb.get(k) or bb.get(k + ".stock")
        ok = alt == ba[k]
        bad += not ok
        print("  %-4s %s  /boot/%s%s" % ("ok" if ok else "DIFF", ba[k][:16], k, "" if bb.get(k) else " (now %s.stock)" % k))
    print("spot check: %d of %d files unchanged" % (len(spots) + len([x for x in ba if x.startswith('extlinux/')]) - bad,
                                                    len(spots) + len([x for x in ba if x.startswith('extlinux/')])))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
