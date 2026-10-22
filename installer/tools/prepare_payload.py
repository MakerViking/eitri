#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a (signature-verified) install.json into the download list and the files stage 2 reads.

Runs on the stock image (Python 3.7, stdlib only).  The shell installer does the
transport; this tool does all JSON handling so the shell never parses JSON.

    prepare_payload.py plan     --install-json install.json --flavor klipper --out-dir DIR
        writes DIR/downloads.tsv  NAME<TAB>URL<TAB>SHA256<TAB>SIZE   (URLs resolved against --base-url)
               DIR/install.env    key=value summary for install.sh

    prepare_payload.py finalize --install-json install.json --flavor klipper --dir PAYLOAD
        writes PAYLOAD/payload.env + PAYLOAD/payload.sha256 after verifying every file, including the
        data seed (archive digest, its manifest, version, flavor, pinned firmware commit, size)

    prepare_payload.py check-seed --install-json install.json --flavor klipper --dir PAYLOAD
        only the data-seed checks of `finalize`; prints what it found as JSON
"""
import argparse
import hashlib
import json
import os
import re
import sys
from urllib.parse import urljoin

# eitri_update.seed holds the one definition of what a data seed manifest must say (shared with eitri-update).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(_HERE, "..", "eitri-update"), os.path.join(_HERE, "..", "mcuflash")]
from eitri_mcuflash.errors import FlashError  # noqa: E402
from eitri_update import seed as seedlib  # noqa: E402

SHA = re.compile(r"^[0-9a-f]{64}$")
FLAVORS = ("klipper", "kalico")
# Canonical on-disk names, whatever the release calls its files.
FILE_NAMES = {
    "rootfs": "rootfs.img.zst",
    "stage2_initramfs": "stage2-initramfs.cpio.gz",
    "mcu_firmware": "mcu-firmware.tar.zst",
}
# The data seed (seed/SPEC.md) keeps its own file names: eitri-seed-install insists on data-seed-<version>-<flavor>.tar.zst.
MIB = 1024 * 1024
REQUIRED = ("rootfs", "stage2_initramfs", "data_seed", "mcu_firmware")
# mcu_policy: "flash" (default) - the release carries MCU firmware for each flavor; "keep-stock" - it may carry none, both
# boards keep the firmware they shipped with and the installer must not touch them (install.sh --mcu skip).
MCU_POLICIES = ("flash", "keep-stock")


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
    if key == "data_seed":
        seed_manifest = entry.get("manifest")
        if not isinstance(seed_manifest, dict) or not seed_manifest.get("url"):
            raise PayloadError("data_seed has no manifest entry for flavor %s (the seed's own manifest is verified before anything is changed)" % flavor)
        if not SHA.match(str(seed_manifest.get("sha256", ""))) or not isinstance(seed_manifest.get("size"), int) or seed_manifest["size"] <= 0:
            raise PayloadError("data_seed manifest: sha256 and size are required")
    return entry


def mcu_policy(doc):
    policy = doc.get("mcu_policy", "flash")
    if policy not in MCU_POLICIES:
        raise PayloadError("install.json: mcu_policy must be one of %s" % ", ".join(MCU_POLICIES))
    return policy


def has_mcu_firmware(doc, flavor):
    entry = doc.get("files", {}).get("mcu_firmware")
    if isinstance(entry, dict) and isinstance(entry.get(flavor), dict):
        return True
    if mcu_policy(doc) == "keep-stock":
        return False
    raise PayloadError("install.json has no mcu_firmware for flavor %s (a release without MCU firmware must say "
                       "\"mcu_policy\": \"keep-stock\")" % flavor)


def required(doc, flavor):
    """The payload keys this release must carry for the flavor (mcu_firmware only when it has any)."""
    return tuple(k for k in REQUIRED if k != "mcu_firmware" or has_mcu_firmware(doc, flavor))


def seed_files(doc, flavor):
    """[(canonical file name, entry)] for the data seed archive and its manifest."""
    version = str(doc.get("version", ""))
    if not seedlib.safe_version(version):
        raise PayloadError("install.json: version %r cannot name a data seed file" % version)
    entry = _entry(doc, "data_seed", flavor)
    return [(seedlib.archive_name(version, flavor), entry), (seedlib.manifest_name(version, flavor), entry["manifest"])]


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
    for key in required(doc, flavor):
        _entry(doc, key, flavor)
    seed_files(doc, flavor)


def downloads(doc, flavor):
    """[(canonical file name, entry)] of everything stage 1 has to fetch, in download order (the small signed seed
    manifest comes before the big archive so a mismatch is found early)."""
    out = []
    for key in required(doc, flavor):
        if key == "data_seed":
            archive, manifest = seed_files(doc, flavor)
            out.extend([manifest, archive])
        else:
            out.append((FILE_NAMES[key], _entry(doc, key, flavor)))
    return out


def zstd_tool(doc):
    """Optional files.zstd: a static aarch64 zstd for the stock system, which has none (Debian 10 on the printer); stage 1
    needs it to look into the data seed before anything is changed.  Same rules as every other file: signed digest."""
    entry = doc.get("files", {}).get("zstd")
    if entry is None:
        return None
    if not isinstance(entry, dict) or not SHA.match(str(entry.get("sha256", ""))) or not isinstance(entry.get("size"), int) \
            or entry["size"] <= 0 or not entry.get("url"):
        raise PayloadError("files.zstd needs url, sha256 and size")
    return entry


def plan(doc, flavor, base_url, out_dir):
    validate(doc, flavor)
    tool = zstd_tool(doc)
    os.makedirs(out_dir, exist_ok=True)
    total = 0
    lines = []
    for name, e in downloads(doc, flavor):
        url = urljoin(base_url, e["url"]) if base_url else e["url"]
        lines.append("\t".join((name, url, e["sha256"], str(e["size"]))))
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
        "mcu_policy": mcu_policy(doc),
        "has_mcu_firmware": 1 if has_mcu_firmware(doc, flavor) else 0,
        "zstd_url": (urljoin(base_url, tool["url"]) if base_url else tool["url"]) if tool else "",
        "zstd_sha256": tool["sha256"] if tool else "",
        "zstd_size": tool["size"] if tool else "",
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


def check_staged_seed(doc, flavor, directory):
    """The staged data seed is the one this (signed) install.json announced, for the flavor chosen, from this release,
    carrying the firmware commit the release pins, built for the interpreter ABI install.json names (`target.python_abi`,
    optional: the stock system cannot know the image's), and small enough for /data.
    Returns what stage 2 and the summary need.  Nothing is trusted from the seed's own manifest that the signed
    install.json does not confirm."""
    archive, manifest = seed_files(doc, flavor)
    arc_name, arc_entry = archive
    man_name, man_entry = manifest
    arc_path, man_path = os.path.join(directory, arc_name), os.path.join(directory, man_name)
    try:
        seedlib.check_archive_file(arc_path, arc_entry["sha256"], arc_entry["size"])
        seedlib.check_archive_file(man_path, man_entry["sha256"], man_entry["size"])
        info = seedlib.check_manifest(
            seedlib.load_manifest(man_path), flavor=flavor, version=str(doc["version"]),
            archive={"name": arc_name, "sha256": arc_entry["sha256"], "size": arc_entry["size"]},
            release_flavor=doc["flavors"].get(flavor), expect_abi=doc.get("target", {}).get("python_abi") or None)
        seedlib.check_archive_contents(arc_path, flavor, info)
    except FlashError as exc:
        raise PayloadError(exc.message) from exc
    need = int(info["uncompressed_size"] * 1.1) + 64 * MIB
    have = doc["layout"]["data_min_mib"] * MIB
    if need > have:
        raise PayloadError("the %s data seed unpacks to %d MiB and needs about %d MiB of room, but this release's /data is planned at "
                           "%d MiB" % (flavor, info["uncompressed_size"] // MIB, need // MIB, have // MIB))
    return {"version": info["version"], "archive_name": arc_name, "archive_sha256": arc_entry["sha256"], "manifest_name": man_name,
            "uncompressed_size": info["uncompressed_size"], "commit": info["commit"], "describe": info["describe"]}


def finalize(doc, flavor, directory):
    validate(doc, flavor)
    sums = []
    for name, e in downloads(doc, flavor):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            raise PayloadError("%s is missing" % name)
        if os.path.getsize(path) != e["size"] or sha256_file(path) != e["sha256"]:
            raise PayloadError("%s does not match install.json" % name)
        sums.append("%s  %s\n" % (e["sha256"], name))
    seed = check_staged_seed(doc, flavor, directory)
    rootfs = _entry(doc, "rootfs", flavor)
    env = {
        "version": doc.get("version", ""),
        "flavor": flavor,
        "rootfs_file": FILE_NAMES["rootfs"],
        "rootfs_raw_size": rootfs["raw_size"],
        "rootfs_raw_sha256": rootfs["raw_sha256"],
        "data_seed_file": seed["archive_name"],
        "data_seed_sha256": seed["archive_sha256"],
        "data_seed_manifest_file": seed["manifest_name"],
        "data_seed_version": seed["version"],
        "data_seed_unpacked_bytes": seed["uncompressed_size"],
        "mcu_firmware_file": FILE_NAMES["mcu_firmware"] if has_mcu_firmware(doc, flavor) else "",
        "mcu_policy": mcu_policy(doc),
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
    c = sub.add_parser("check-seed")
    c.add_argument("--install-json", required=True)
    c.add_argument("--flavor", required=True)
    c.add_argument("--dir", required=True)
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
        elif args.cmd == "check-seed":
            validate(doc, args.flavor)
            print(json.dumps(check_staged_seed(doc, args.flavor, args.dir), sort_keys=True))
        else:
            finalize(doc, args.flavor, args.dir)
    except PayloadError as exc:
        sys.stderr.write("prepare_payload: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
