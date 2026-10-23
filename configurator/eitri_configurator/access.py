"""Optional remote access to the printer: an SSH public key and/or a password for the `eitri` user.

Off by default.  The configurator itself never touches `authorized_keys`, `shadow` or `sshd_config`:
it validates what the person typed and hands it to the image's privileged helper through one narrow
sudoers rule,

    sudo -n /usr/libexec/eitri/eitri-access set-key | set-password [--ssh-login] | disable | status
    sudo -n /usr/libexec/eitri/eitri-access admin-set-key | admin-close-window

"eitri" (the user the services run as) gets a shell but never root. Root is the separate "admin" account,
whose keys can be added here only during first setup: completing setup closes that window for good, and
after that only a USB stick at the printer adds administrator keys (see the helper's documentation).

(`set-key` reads one OpenSSH public key on stdin, `set-password` reads the password on stdin, `--ssh-login`
additionally lets sshd accept it, `status` prints JSON, exit 3 means the input was rejected, exit 4 means
saved but not applied yet).  The sudoers rules allow exactly those command lines.  The helper validates
again; this module is the first of two strict gates and is also what keeps secrets out of argv, logs and
responses:

* a key or password only ever travels over the helper's stdin, never argv or an environment variable;
* nothing here logs, stores or returns a password; a public key is returned only as its fingerprint;
* helper output that contains the secret is never shown.

In `--mock` mode the helper is `mock/eitri_access.py`, which keeps its state in the scratch data
directory, so the real subprocess path is exercised.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import struct
from dataclasses import dataclass
from typing import Any

from .options import ValidationError
from .settings import Settings

log = logging.getLogger("eitri.configurator.access")

HELPER = "/usr/libexec/eitri/eitri-access"
SSH_USER = "eitri"
ADMIN_USER = "admin"

MAX_KEY_CHARS = 4096
PASSWORD_MIN = 10
PASSWORD_MAX = 128
TIMEOUT = 25.0

_ECDSA = {"nistp256": 65, "nistp384": 97, "nistp521": 133}
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_COMMENT_UNSAFE = re.compile(r"[^A-Za-z0-9 ._@+=:,/()\-]")
_GUESSABLE = ("password", "eitri", "qidi", "qwerty", "letmein", "12345678", "abcdefgh", "snapmaker")


class AccessError(Exception):
    """The helper could not do it (missing, refused, failed).  `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message, self.status = message, status


@dataclass(frozen=True)
class PublicKey:
    line: str            # normalised single line handed to the helper: "<type> <base64> [comment]"
    type: str
    fingerprint: str     # SHA256:... like `ssh-keygen -l`
    bits: int | None
    comment: str


# --- public keys -----------------------------------------------------------------------------

def _strings(blob: bytes) -> list[bytes] | None:
    """Split an SSH wire blob into its length-prefixed strings; None when it is malformed."""
    out, i = [], 0
    while i < len(blob):
        if i + 4 > len(blob):
            return None
        (n,) = struct.unpack(">I", blob[i:i + 4])
        i += 4
        if n > len(blob) - i:
            return None
        out.append(blob[i:i + n])
        i += n
    return out


def _bad(msg: str) -> ValidationError:
    return ValidationError({"key": msg})


def parse_public_key(raw: object) -> PublicKey:
    """Validate one OpenSSH public key line.  Raises ValidationError({"key": ...}); the message never echoes the input."""
    if not isinstance(raw, str):
        raise _bad("paste the public key as text")
    text = raw.strip()
    if not text:
        raise _bad("paste your public key (the line from the .pub file)")
    if len(text) > MAX_KEY_CHARS:
        raise _bad("that is too long to be one public key")
    if "PRIVATE KEY" in text or text.startswith("-----BEGIN"):
        raise _bad("that looks like a PRIVATE key. Never paste it anywhere: paste the matching .pub file instead")
    if any(ord(c) < 32 and c not in "\t" or ord(c) == 127 for c in text):
        raise _bad("paste exactly one key on one line (no line breaks or control characters)")
    parts = text.split(None, 2)
    ktype = parts[0]
    if len(parts) < 2:
        raise _bad("a public key is a type, a long base64 string and an optional comment, for example: ssh-ed25519 AAAA... you@laptop")
    if ktype not in ("ssh-ed25519", "ssh-rsa", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
                     "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"):
        raise _bad("not a supported public key type. Use ssh-ed25519 (recommended), ECDSA or RSA of 2048 bits or more; "
                   "key options such as command=\"...\" are not accepted")
    b64 = parts[1]
    try:
        blob = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise _bad("the key body is not valid base64: copy the whole line from the .pub file") from None
    if base64.b64encode(blob).decode() != b64:
        raise _bad("the key body is not canonical base64: copy the whole line from the .pub file")
    fields = _strings(blob)
    if not fields or fields[0].decode("ascii", "replace") != ktype:
        raise _bad("the key body does not match its type: copy the whole line from the .pub file")
    rest, bits = fields[1:], None
    if ktype == "ssh-ed25519":
        ok = len(rest) == 1 and len(rest[0]) == 32
    elif ktype == "sk-ssh-ed25519@openssh.com":
        ok = len(rest) == 2 and len(rest[0]) == 32 and rest[1].startswith(b"ssh:")
    elif ktype == "ssh-rsa":
        ok = len(rest) == 2 and bool(rest[1]) and rest[1][0] < 0x80
        if ok:
            bits = int.from_bytes(rest[1], "big").bit_length()
            if bits < 2048:
                raise _bad("that RSA key is too small (under 2048 bits). Make a new one: ssh-keygen -t ed25519")
            if bits > 16384:
                raise _bad("that RSA key is unreasonably large")
    else:                                    # ECDSA, plain or FIDO (the FIDO form adds the application string)
        fido = ktype.startswith("sk-")
        curve = "nistp256" if fido else ktype.rsplit("-", 1)[-1]
        ok = (len(rest) == (3 if fido else 2) and rest[0] == curve.encode() and curve in _ECDSA
              and len(rest[1]) == _ECDSA[curve] and rest[1][:1] == b"\x04" and (not fido or rest[2].startswith(b"ssh:")))
    if not ok:
        raise _bad("that is not a well-formed public key: copy the whole line from the .pub file")
    comment = _COMMENT_UNSAFE.sub("_", parts[2]).strip()[:80] if len(parts) > 2 else ""
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return PublicKey(" ".join(p for p in (ktype, b64, comment) if p), ktype, fp, bits, comment)


# --- passwords ------------------------------------------------------------------------------

def validate_password(password: object, confirm: object) -> str:
    """Return the password if acceptable.  Raises ValidationError; messages never echo it."""
    if not isinstance(password, str) or not password:
        raise ValidationError({"password": "choose a password"})
    if confirm != password:
        raise ValidationError({"confirm": "the two passwords do not match"})
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValidationError({"password": "no control characters or line breaks"})
    if password != password.strip():
        raise ValidationError({"password": "no spaces at the start or end"})
    if len(password) < PASSWORD_MIN:
        raise ValidationError({"password": f"use at least {PASSWORD_MIN} characters; a few random words make a good one"})
    if len(password.encode()) > PASSWORD_MAX:
        raise ValidationError({"password": f"at most {PASSWORD_MAX} bytes"})
    low = password.lower()
    if len(set(password)) < 4 or any(g in low for g in _GUESSABLE):
        raise ValidationError({"password": "too easy to guess: avoid repeats, 'password', the printer's name and keyboard runs"})
    return password


# --- helper ---------------------------------------------------------------------------------

def normalize_status(doc: Any) -> dict[str, Any]:
    """The UI's view of `eitri-access status`: counts and booleans only, whatever else the helper reports.

    The helper prints {"keys": [{"type", "fingerprint", "comment"}], "password": {"set", "ssh_login"}, "pending", ...};
    plain numbers and booleans are accepted too.
    """
    d = doc if isinstance(doc, dict) else {}
    raw_keys = d.get("keys", 0)
    fingerprints: list[str] = []
    if isinstance(raw_keys, list):
        count = len(raw_keys)
        for k in raw_keys:
            fp = k.get("fingerprint") if isinstance(k, dict) else k
            if isinstance(fp, str) and _FINGERPRINT.fullmatch(fp):
                fingerprints.append(fp)
    else:
        count = raw_keys if isinstance(raw_keys, int) and not isinstance(raw_keys, bool) and raw_keys >= 0 else 0
    adm = d.get("admin") if isinstance(d.get("admin"), dict) else {}
    adm_keys = adm.get("keys", [])
    admin = {"window_open": adm.get("window_open") is True,
             "keys": len(adm_keys) if isinstance(adm_keys, list) else 0,
             "fingerprints": [k["fingerprint"] for k in adm_keys if isinstance(adm_keys, list) and isinstance(k, dict)
                              and isinstance(k.get("fingerprint"), str) and _FINGERPRINT.fullmatch(k["fingerprint"])][:8],
             "user": ADMIN_USER}
    pw = d.get("password", False)
    password = bool(pw.get("set")) if isinstance(pw, dict) else pw is True
    ssh_login = bool(pw.get("ssh_login")) if isinstance(pw, dict) else False
    return {"available": True, "keys": count, "fingerprints": fingerprints[:8], "password": password,
            "password_auth": password and ssh_login, "pending": d.get("pending") is True,
            "enabled": count > 0 or password, "user": SSH_USER, "admin": admin}


UNAVAILABLE = {"available": False, "keys": 0, "fingerprints": [], "password": False, "password_auth": False, "pending": False, "enabled": False, "user": SSH_USER,
               "admin": {"window_open": False, "keys": 0, "fingerprints": [], "user": ADMIN_USER}}


class RemoteAccess:
    def __init__(self, settings: Settings):
        self.s = settings
        self._lock = asyncio.Lock()

    @property
    def cmd(self) -> list[str]:
        return list(self.s.access_cmd)

    async def _run(self, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C", **self.s.access_env}
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.cmd, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        except (FileNotFoundError, PermissionError) as exc:
            raise AccessError("This image has no remote-access helper, so remote access cannot be changed here.", 503) from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise AccessError("The remote-access helper did not answer in time.", 504) from None
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    @staticmethod
    def _explain(rc: int, err: str, secret: str | None) -> AccessError:
        first = next((ln.strip() for ln in err.splitlines() if ln.strip()), "")
        low = first.lower()
        if "no new privileges" in low:
            return AccessError("The configurator's sandbox blocks sudo (NoNewPrivileges), so it cannot change remote access.", 503)
        if "password is required" in low or "not allowed" in low or "may not run sudo" in low or "not in the sudoers" in low:
            return AccessError("The system does not let the configurator change remote access (the sudo rule for eitri-access is missing).", 503)
        if rc == 127 or "not found" in low:
            return AccessError("This image has no remote-access helper, so remote access cannot be changed here.", 503)
        safe = "".join(c for c in first if c.isprintable())[:200]
        if secret and secret in safe:
            safe = ""
        return AccessError(f"The printer refused the change{': ' + safe if safe else ''}.")

    async def status(self) -> dict[str, Any]:
        """Never raises: a missing or broken helper just means "not available"."""
        try:
            rc, out, err = await self._run(["status"])
        except AccessError as exc:
            return {**UNAVAILABLE, "reason": exc.message}
        if rc != 0:
            return {**UNAVAILABLE, "reason": self._explain(rc, err, None).message}
        try:
            return normalize_status(json.loads(out))
        except ValueError:
            return {**UNAVAILABLE, "reason": "The remote-access helper gave an answer the configurator does not understand."}

    @staticmethod
    def _rejection(out: str, secret: str | None) -> str:
        """The helper's own message for rejected input (exit 3); it never echoes its input, and we check anyway."""
        try:
            msg = json.loads(out).get("error", "")
        except (ValueError, AttributeError):
            msg = ""
        msg = "".join(c for c in msg if isinstance(msg, str) and c.isprintable())[:200] if isinstance(msg, str) else ""
        if secret and secret in msg:
            msg = ""
        return msg or "the printer did not accept that"

    async def _change(self, args: list[str], stdin: str | None, secret: str | None, field: str) -> dict[str, Any]:
        async with self._lock:
            rc, out, err = await self._run(args, stdin)
            if rc == 3:
                raise ValidationError({field: self._rejection(out, secret)})
            if rc not in (0, 4):                          # 4: saved, applied by systemd a moment later (status says "pending")
                log.warning("eitri-access %s failed (exit %d)", args[0], rc)       # never the output: it could echo input
                raise self._explain(rc, err, secret)
        st = await self.status()
        if rc == 4:
            st["pending"] = True
        return st

    async def set_key(self, key: PublicKey) -> dict[str, Any]:
        st = await self._change(["set-key"], key.line + "\n", None, "key")
        log.warning("remote access: SSH key %s added for %s", key.fingerprint, SSH_USER)
        return st

    async def set_admin_key(self, key: PublicKey) -> dict[str, Any]:
        st = await self._change(["admin-set-key"], key.line + "\n", None, "key")
        log.warning("remote access: administrator SSH key %s added for %s", key.fingerprint, ADMIN_USER)
        return st

    async def close_admin_window(self) -> bool:
        """End first setup's window for administrator keys. Best effort: a failure is logged, never raised,
        because it must not stop setup from finishing (the window then stays open until the next attempt)."""
        try:
            rc, _out, err = await self._run(["admin-close-window"])
        except AccessError as exc:
            log.warning("could not close the administrator-key window: %s", exc.message)
            return False
        if rc != 0:
            log.warning("could not close the administrator-key window (exit %d): %s", rc, self._explain(rc, err, None).message)
            return False
        return True

    async def set_password(self, password: str, ssh_login: bool = False) -> dict[str, Any]:
        st = await self._change(["set-password", *(["--ssh-login"] if ssh_login else [])], password + "\n", password, "password")
        log.warning("remote access: password set for %s (SSH password login %s)", SSH_USER, "allowed" if ssh_login else "not allowed")
        return st

    async def disable(self) -> dict[str, Any]:
        st = await self._change(["disable"], None, None, "confirm")
        log.warning("remote access: removed again (keys and password)")
        return st


def default_cmd() -> list[str]:
    return ["sudo", "-n", HELPER]


def mock_cmd(state_file: str) -> list[str]:
    import sys
    from pathlib import Path
    return [sys.executable, "-I", str(Path(__file__).resolve().parent / "mock" / "eitri_access.py"), state_file]


__all__ = ["AccessError", "PublicKey", "RemoteAccess", "parse_public_key", "validate_password", "normalize_status", "default_cmd", "mock_cmd"]
