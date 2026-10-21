#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a (signature-verified) install.json into the download list and the files stage 2 reads.

Runs on the stock image (Python 3.7, stdlib only).  The shell installer does the
transport; this tool does all JSON handling so the shell never parses JSON.

    prepare_payload.py plan     --install-json install.json --flavor klipper --out-dir DIR
        writes DIR/downloads.tsv  NAME<TAB>URL<TAB>SHA256<TAB>SIZE   (URLs resolved against --base-url)
               DIR/install.env    key=value summary for install.sh

    prepare_payload.py finalize --install-json install.json --flavor klipper --dir PAYLOAD
        writes PAYLOAD/payload.env + PAYLOAD/payload.sha256 after verifying every file
"""
import argparse
import hashlib
import json
import os
import re
import sys
from urllib.parse import urljoin

SHA = re.compile(r"^[0-9a-f]{64}$")
FLAVORS = ("klipper", "kalico")
# Canonical on-disk names, whatever the release calls its files.
FILE_NAMES = {
    "rootfs": "rootfs.img.zst",
    "stage2_initramfs": "stage2-initramfs.cpio.gz",
    "data_seed": "data-seed.tar.zst",
    "mcu_firmware": "mcu-firmware.tar.zst",
}
REQUIRED = ("rootfs", "stage2_initramfs", "data_seed", "mcu_firmware")


class PayloadError(Exception):
    pass


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PayloadError("cannot read %s: %s" % (path, exc)) from exc
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise PayloadError("unsupported install.json schema")
    return doc


def _entry(doc, key, flavor):
    files = doc.get("files", {})
    entry = files.get(key)
    if key in ("data_seed", "mcu_firmware") and isinstance(entry, dict):
        entry = entry.get(flavor)
    if not isinstance(entry, dict):
        raise PayloadError("install.json has no %s%s" % (key, (" for flavor " + flavor) if key in ("data_seed", "mcu_firmware") else ""))
    if not SHA.match(str(entry.get("sha256", ""))):
        raise PayloadError("%s: sha256 must be 64 lowercase hex digits" % key)
    if not isinstance(entry.get("size"), int) or entry["size"] <= 0:
        raise PayloadError("%s: size missing" % key)
    if not entry.get("url"):
        raise PayloadError("%s: url missing" % key)
    return entry


def validate(doc, flavor):
    if flavor not in FLAVORS:
        raise PayloadError("flavor must be klipper or kalico")
    if doc.get("target", {}).get("compatible") != "firefly,roc-rk3328-cc":
        raise PayloadError("this release is not for the Firefly ROC-RK3328-CC (X-Plus 4 mainboard)")
    if flavor not in doc.get("flavors", {}):
        raise PayloadError("this release does not offer %s" % flavor)
    # ttyS0 and ttyS2 are the MCU UARTs: a kernel console there would talk to (and reset) the boards.
    cmdline = str(doc.get("kernel_cmdline", ""))
    if re.search(r"(^|\s)console=tty(S|FIQ)", cmdline) or "\n" in cmdline:
        raise PayloadError("kernel_cmdline must not put a console on a serial port (ttyS0/ttyS2 are the MCU UARTs)")
    layout = doc.get("layout", {})
    for key in ("root_mib", "data_min_mib"):
        if not isinstance(layout.get(key), int) or layout[key] <= 0:
            raise PayloadError("layout.%s missing" % key)
    rootfs = _entry(doc, "rootfs", flavor)
    if not isinstance(rootfs.get("raw_size"), int) or not SHA.match(str(rootfs.get("raw_sha256", ""))):
        raise PayloadError("rootfs needs raw_size and raw_sha256")
    if rootfs["raw_size"] > layout["root_mib"] * 1024 * 1024:
        raise PayloadError("rootfs image does not fit its %d MiB slot" % layout["root_mib"])
    for key in REQUIRED:
        _entry(doc, key, flavor)


def plan(doc, flavor, base_url, out_dir):
    validate(doc, flavor)
    os.makedirs(out_dir, exist_ok=True)
    total = 0
    lines = []
    for key in REQUIRED:
        e = _entry(doc, key, flavor)
        url = urljoin(base_url, e["url"]) if base_url else e["url"]
        lines.append("\t".join((FILE_NAMES[key], url, e["sha256"], str(e["size"]))))
        total += e["size"]
    with open(os.path.join(out_dir, "downloads.tsv"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    stock = doc.get("stock", {})
    env = {
        "version": doc.get("version", ""),
        "channel": doc.get("channel", ""),
        "flavor_describe": doc["flavors"][flavor].get("describe", ""),
        "flavor_commit": doc["flavors"][flavor].get("commit", ""),
        "root_mib": doc["layout"]["root_mib"],
        "data_min_mib": doc["layout"]["data_min_mib"],
        "payload_bytes": total,
        "kernel_cmdline": doc.get("kernel_cmdline", "console=tty1 loglevel=4 panic=10 net.ifnames=0"),
        "stock_soc_known": ",".join(stock.get("soc", [])),
    }
    with open(os.path.join(out_dir, "install.env"), "w") as fh:
        for k, v in env.items():
            fh.write("%s=%s\n" % (k, v))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def finalize(doc, flavor, directory):
    validate(doc, flavor)
    sums = []
    for key in REQUIRED:
        e = _entry(doc, key, flavor)
        path = os.path.join(directory, FILE_NAMES[key])
        if not os.path.isfile(path):
            raise PayloadError("%s is missing" % FILE_NAMES[key])
        if os.path.getsize(path) != e["size"] or sha256_file(path) != e["sha256"]:
            raise PayloadError("%s does not match install.json" % FILE_NAMES[key])
        sums.append("%s  %s\n" % (e["sha256"], FILE_NAMES[key]))
    rootfs = _entry(doc, "rootfs", flavor)
    env = {
        "version": doc.get("version", ""),
        "flavor": flavor,
        "rootfs_file": FILE_NAMES["rootfs"],
        "rootfs_raw_size": rootfs["raw_size"],
        "rootfs_raw_sha256": rootfs["raw_sha256"],
        "data_seed_file": FILE_NAMES["data_seed"],
        "mcu_firmware_file": FILE_NAMES["mcu_firmware"],
        "root_mib": doc["layout"]["root_mib"],
        "data_min_mib": doc["layout"]["data_min_mib"],
    }
    # checksums first: stage 2 verifies them before it reads anything else
    with open(os.path.join(directory, "payload.sha256"), "w") as fh:
        fh.writelines(sums)
    with open(os.path.join(directory, "payload.env"), "w") as fh:
        for k, v in env.items():
            fh.write("%s=%s\n" % (k, v))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--install-json", required=True)
    p.add_argument("--flavor", required=True)
    p.add_argument("--base-url", default="")
    p.add_argument("--out-dir", required=True)
    f = sub.add_parser("finalize")
    f.add_argument("--install-json", required=True)
    f.add_argument("--flavor", required=True)
    f.add_argument("--dir", required=True)
    args = ap.parse_args(argv)
    try:
        doc = load(args.install_json)
        if args.cmd == "plan":
            plan(doc, args.flavor, args.base_url, args.out_dir)
        else:
            finalize(doc, args.flavor, args.dir)
    except PayloadError as exc:
        sys.stderr.write("prepare_payload: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
