#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Wrap a U-Boot command script into a legacy uImage ("boot.scr"), like mkimage -T script.

The stock printer image has no mkimage, and the installer must not depend on a
package that may not be installed, so this writes the 64-byte legacy header
itself.  Format reference: U-Boot include/image.h (image_header_t), script
payload = two big-endian u32 (data length, 0 terminator) followed by the text.

    mkscr.py boot.cmd boot.scr
    mkscr.py --check boot.scr          # verify header/data CRCs, print the script
"""
import argparse
import struct
import sys
import zlib

IH_MAGIC = 0x27051956
IH_OS_LINUX = 5
IH_ARCH_ARM64 = 22
IH_TYPE_SCRIPT = 6
IH_COMP_NONE = 0
HEADER = struct.Struct(">IIIIIIIBBBB32s")  # magic hcrc time size load ep dcrc os arch type comp name


def build(script: bytes, name: str = "Eitri boot script", timestamp: int = 0) -> bytes:
    if not script.endswith(b"\n"):
        script += b"\n"
    payload = struct.pack(">II", len(script), 0) + script
    dcrc = zlib.crc32(payload) & 0xFFFFFFFF
    raw_name = name.encode("ascii", "replace")[:31]
    head = HEADER.pack(IH_MAGIC, 0, timestamp, len(payload), 0, 0, dcrc, IH_OS_LINUX, IH_ARCH_ARM64,
                       IH_TYPE_SCRIPT, IH_COMP_NONE, raw_name)
    hcrc = zlib.crc32(head) & 0xFFFFFFFF
    return head[:4] + struct.pack(">I", hcrc) + head[8:] + payload


def check(image: bytes) -> bytes:
    """Validate a boot.scr and return the script text; raises ValueError on any inconsistency."""
    if len(image) < HEADER.size:
        raise ValueError("too short for a uImage header")
    magic, hcrc, _t, size, _l, _e, dcrc, _os, _arch, typ, comp, _name = HEADER.unpack(image[: HEADER.size])
    if magic != IH_MAGIC:
        raise ValueError("bad magic 0x%08x" % magic)
    zeroed = image[:4] + b"\0\0\0\0" + image[8 : HEADER.size]
    if zlib.crc32(zeroed) & 0xFFFFFFFF != hcrc:
        raise ValueError("header CRC mismatch")
    if typ != IH_TYPE_SCRIPT or comp != IH_COMP_NONE:
        raise ValueError("not an uncompressed script image")
    payload = image[HEADER.size : HEADER.size + size]
    if len(payload) != size or zlib.crc32(payload) & 0xFFFFFFFF != dcrc:
        raise ValueError("data CRC mismatch")
    length, term = struct.unpack(">II", payload[:8])
    if term != 0 or length != len(payload) - 8:
        raise ValueError("script length field is inconsistent")
    return payload[8:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="verify an existing boot.scr and print the script")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?")
    args = ap.parse_args(argv)
    if args.check:
        try:
            sys.stdout.write(check(open(args.input, "rb").read()).decode("utf-8", "replace"))
        except ValueError as exc:
            sys.stderr.write("mkscr: %s: %s\n" % (args.input, exc))
            return 1
        return 0
    if not args.output:
        ap.error("output file required")
    image = build(open(args.input, "rb").read())
    check(image)
    with open(args.output, "wb") as fh:
        fh.write(image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
