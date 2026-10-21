"""Stand-in for /usr/libexec/eitri/eitri-access (mock mode and tests).

    eitri_access.py STATE_FILE set-key|set-password|disable|status

Same contract as the real helper: the key or password arrives on stdin (one line), `status` prints JSON.
State is a JSON file in the scratch data directory.  The password is kept as a salted SHA-256 only so a
test can check that it arrived, and never printed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def invalid(message: str) -> int:
    print(json.dumps({"ok": False, "error": message, "kind": "invalid"}))
    print(f"eitri-access: {message}", file=sys.stderr)
    return 3


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("set-key", "set-password", "disable", "status"):
        print("usage: eitri-access set-key|set-password [--ssh-login]|disable|status", file=sys.stderr)
        return 2
    path, verb, extra = Path(argv[0]), argv[1], argv[2:]
    if extra and not (verb == "set-password" and extra == ["--ssh-login"]):
        return invalid("unexpected arguments")
    state = load(path)
    keys: list[str] = state.get("keys", [])
    if verb == "status":
        # Same shape as the real helper (os/.../eitri-access status).
        print(json.dumps({"ok": True, "schema": 1, "keys": [{"type": "ssh-ed25519", "fingerprint": k, "comment": ""} for k in keys],
                          "password": {"set": bool(state.get("password")), "ssh_login": bool(state.get("ssh_login"))},
                          "generation": 1, "applied": 1, "pending": False}))
        return 0
    if verb == "disable":
        state = {"keys": [], "password": None, "ssh_login": False}
    else:
        line = sys.stdin.readline()
        if not line.endswith("\n") or "\n" in line[:-1] or sys.stdin.read(1):
            return invalid("input must be exactly one line")
        value = line[:-1]
        if verb == "set-key":
            parts = value.split(" ")
            if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
                return invalid("not an OpenSSH public key")
            blob = base64.b64decode(parts[1])
            fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
            if fp not in keys:
                keys.append(fp)
            state["keys"] = keys
        else:
            if len(value) < 8:
                return invalid("password must be at least 8 characters")
            state["password"] = hashlib.sha256(b"mock-salt" + value.encode()).hexdigest()
            state["ssh_login"] = extra == ["--ssh-login"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
