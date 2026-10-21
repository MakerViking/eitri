# SPDX-License-Identifier: Apache-2.0
"""minisign (https://jedisct1.github.io/minisign/) public keys and signatures.

Supports both signature algorithms minisign emits: "Ed" (signs the file) and
"ED" (signs the BLAKE2b-512 hash of the file, the default since 0.6), and checks
the trusted-comment ("global") signature too, so the comment cannot be altered
independently.

    python3 -m eitri_update.minisign verify FILE [--sig FILE.minisig] --pubkey KEY.pub [--pubkey ...]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import glob
import hashlib
import os
import sys
from dataclasses import dataclass
from typing import Iterable, List, Optional

from . import ed25519


class SignatureError(Exception):
    pass


@dataclass(frozen=True)
class PublicKey:
    key_id: bytes
    key: bytes
    comment: str = ""

    @property
    def key_id_hex(self) -> str:
        return self.key_id[::-1].hex().upper()


@dataclass(frozen=True)
class Signature:
    algorithm: bytes
    key_id: bytes
    signature: bytes
    trusted_comment: str
    global_signature: bytes


def _b64(line: str, what: str, length: int) -> bytes:
    try:
        raw = base64.b64decode(line.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("%s is not valid base64" % what) from exc
    if len(raw) != length:
        raise SignatureError("%s has the wrong length (%d, expected %d)" % (what, len(raw), length))
    return raw


def _after_label(line: str, label: str) -> str:
    rest = line[len(label) :]
    return rest[1:] if rest.startswith(" ") else rest


def parse_public_key(text: str) -> PublicKey:
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    comment = ""
    if lines and lines[0].startswith("untrusted comment:"):
        comment = lines.pop(0)[len("untrusted comment:") :].strip()
    if len(lines) != 1:
        raise SignatureError("a minisign public key file has one key line")
    raw = _b64(lines[0], "public key", 42)
    if raw[:2] != b"Ed":
        raise SignatureError("unsupported public key algorithm %r" % raw[:2])
    return PublicKey(key_id=raw[2:10], key=raw[10:], comment=comment)


def parse_signature(text: str) -> Signature:
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    if len(lines) != 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment:"):
        raise SignatureError("not a minisign signature file")
    raw = _b64(lines[1], "signature", 74)
    if raw[:2] not in (b"Ed", b"ED"):
        raise SignatureError("unsupported signature algorithm %r" % raw[:2])
    return Signature(
        algorithm=raw[:2],
        key_id=raw[2:10],
        signature=raw[10:],
        trusted_comment=_after_label(lines[2], "trusted comment:"),
        global_signature=_b64(lines[3], "global signature", 64),
    )


def verify_bytes(data: bytes, sig: Signature, keys: Iterable[PublicKey]) -> PublicKey:
    """Verify *data*; returns the key that signed it or raises SignatureError."""
    matching = [k for k in keys if k.key_id == sig.key_id]
    if not matching:
        raise SignatureError("signed by unknown key %s" % sig.key_id[::-1].hex().upper())
    message = hashlib.blake2b(data, digest_size=64).digest() if sig.algorithm == b"ED" else data
    for key in matching:
        if ed25519.verify(key.key, message, sig.signature):
            if not ed25519.verify(key.key, sig.signature + sig.trusted_comment.encode("utf-8"), sig.global_signature):
                raise SignatureError("trusted comment signature is invalid")
            return key
    raise SignatureError("signature does not match the file")


REVOKED_FILE = "revoked-keys.txt"


def load_revoked(paths: Iterable[str]) -> List[str]:
    """Key ids (16 hex digits, as minisign prints them) listed in a `revoked-keys.txt` next to the keys.  The list
    ships inside the signed OS image, so retiring a compromised key does not depend on the key being retired."""
    out: List[str] = []
    for path in paths:
        f = os.path.join(path if os.path.isdir(path) else os.path.dirname(path), REVOKED_FILE)
        if not os.path.isfile(f):
            continue
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                token = line.split("#", 1)[0].strip().upper()
                if token:
                    out.append(token)
    return out


def load_keys(paths: Iterable[str]) -> List[PublicKey]:
    paths = list(paths)
    keys: List[PublicKey] = []
    for path in paths:
        files = sorted(glob.glob(os.path.join(path, "*.pub"))) if os.path.isdir(path) else [path]
        for f in files:
            with open(f, "r", encoding="utf-8") as fh:
                keys.append(parse_public_key(fh.read()))
    revoked = set(load_revoked(paths))
    keys = [k for k in keys if k.key_id_hex not in revoked]
    if not keys:
        raise SignatureError("no trusted public keys found")
    return keys


def verify_file(path: str, sig_path: Optional[str], key_paths: Iterable[str]) -> Signature:
    sig_path = sig_path or path + ".minisig"
    keys = load_keys(key_paths)
    try:
        with open(sig_path, "r", encoding="utf-8") as fh:
            sig = parse_signature(fh.read())
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise SignatureError("cannot read %s: %s" % (exc.filename, exc.strerror)) from exc
    verify_bytes(data, sig, keys)
    return sig


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m eitri_update.minisign")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("file")
    v.add_argument("--sig")
    v.add_argument("--pubkey", action="append", required=True, help="public key file or directory of *.pub")
    args = ap.parse_args(argv)
    try:
        sig = verify_file(args.file, args.sig, args.pubkey)
    except SignatureError as exc:
        sys.stderr.write("signature verification FAILED: %s\n" % exc)
        return 1
    sys.stdout.write("ok: %s\n" % sig.trusted_comment)
    return 0


if __name__ == "__main__":
    sys.exit(main())
