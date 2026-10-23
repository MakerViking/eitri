"""First-run pairing, sessions and the lock after setup.

The configurator can flash the MCUs, rewrite the printer configuration, join Wi-Fi and
restart services, so nothing that changes state is reachable without proof that the caller
is standing at the printer:

* **Pairing code.**  An 8-digit one-time code is generated on first start, kept in
  ``auth.json`` (0600) and published for the touchscreen daemon in ``pairing.json`` and the
  journal.  Presenting it to ``POST /api/pair`` yields a session; the code is then rotated, so
  every extra browser needs a fresh look at the screen.  Wrong guesses back off and, after a
  few, rotate the code, which makes guessing 10^8 combinations pointless.
* **Sessions.**  Random 256-bit tokens, stored only as SHA-256 digests.  The browser holds one
  in an ``HttpOnly; SameSite=Strict`` cookie and must echo a derived CSRF token in a header;
  scripts send ``Authorization: Bearer <token>`` (not ambient, so no CSRF).
* **Lock.**  Completing setup revokes every other session, gives the finishing browser a few
  minutes of grace, and closes pairing.  Reading stays open; writing again needs the
  maintenance flag (``eitri-configurator unlock`` on the printer, over SSH or from the screen),
  and then the on-screen code as before.

GET routes stay open (the wizard needs them before it can pair).  The trust model is the same
LAN-plus-physical-presence model as Moonraker's, but writes need the person at the printer.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

from .settings import Settings
from .state import StateStore, atomic_write

log = logging.getLogger("eitri.configurator.auth")

COOKIE = "eitri_session"
CSRF_HEADER = "X-Eitri-CSRF"
SESSION_TTL = 12 * 3600          # seconds a pairing lasts
GRACE_TTL = 10 * 60              # the finishing browser keeps write access this long after setup
MAX_SESSIONS = 8
FAILS_BEFORE_DELAY = 5           # consecutive wrong codes before pairing backs off
FAILS_BEFORE_ROTATE = 10         # ... and before the code is replaced
TOTAL_FAILS_BEFORE_ROTATE = 50   # wrong codes from all clients together before the code is replaced
MAX_TRACKED_CLIENTS = 256
BASE_DELAY = 30
MAX_DELAY = 900


class PairError(Exception):
    def __init__(self, status: int, message: str, retry_after: int | None = None, **extra: Any):
        super().__init__(message)
        self.status, self.message, self.retry_after, self.extra = status, message, retry_after, extra


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(token: str) -> str:
    """Per-session CSRF token, derivable from the session token but not from the cookie alone by a page
    that cannot read it (the cookie is HttpOnly; the token is handed to the same-origin page)."""
    return hmac.new(token.encode(), b"eitri-configurator-csrf", hashlib.sha256).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(10 ** 8):08d}"


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_code(raw: object) -> str:
    return re.sub(r"\D", "", raw) if isinstance(raw, str) else ""


class Auth:
    def __init__(self, settings: Settings, state: StateStore, clock=time.time):
        self.s, self.state, self.clock = settings, state, clock
        d = settings.configurator_dir
        self.path = d / "auth.json"
        self.pairing_file = d / "pairing.json"
        self.unlock_flag = d / "unlock"
        # Wrong codes are counted per client address, so one device guessing locks out only itself, never
        # the owner's browser. A total across all clients still rotates the code, so many addresses do not
        # make guessing any less pointless.
        self._clients: dict[str, tuple[int, float]] = {}        # address -> (wrong codes in a row, blocked until)
        self._total_fails = 0
        self._published: tuple | None = None
        self._data: dict[str, Any] = {"code": "", "sessions": {}}
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self._data["code"] = str(loaded.get("code") or "")
                sessions = loaded.get("sessions")
                if isinstance(sessions, dict):
                    self._data["sessions"] = {k: v for k, v in sessions.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass
        if not re.fullmatch(r"\d{8}", self._data["code"]):
            self._data["code"] = new_code()
        self._prune()
        self._save()
        self.publish()

    # --- state ------------------------------------------------------------
    def maintenance(self) -> bool:
        return self.unlock_flag.exists()

    @property
    def locked(self) -> bool:
        """Setup is finished and nobody has opened maintenance on the printer: pairing is closed."""
        return bool(self.state.data.get("completed")) and not self.maintenance()

    @property
    def code(self) -> str:
        return self._data["code"]

    def _save(self) -> None:
        atomic_write(self.path, json.dumps(self._data, indent=2), mode=0o600)

    def _prune(self) -> None:
        now = self.clock()
        sessions = self._data["sessions"]
        for k in [k for k, v in sessions.items() if not isinstance(v.get("expires"), (int, float)) or v["expires"] <= now]:
            del sessions[k]
        # Keep only the newest MAX_SESSIONS.
        for k in sorted(sessions, key=lambda k: sessions[k].get("created", 0))[:-MAX_SESSIONS]:
            del sessions[k]

    # --- what the touchscreen / console shows --------------------------------
    def publish(self) -> None:
        """Write pairing.json for Klarity and log the code, when what is shown changes."""
        locked = self.locked
        shown = ("locked", None) if locked else ("pairing", self.code)
        if shown == self._published:
            return
        self._published = shown
        doc = {"state": shown[0], "code": display_code(shown[1]) if shown[1] else None, "updated": int(self.clock())}
        try:
            atomic_write(self.pairing_file, json.dumps(doc), mode=0o640)
        except OSError:
            log.exception("could not write %s", self.pairing_file)
        if locked:
            log.warning("Configurator is locked: setup is complete. Run `eitri-configurator unlock` on the printer to change settings.")
        else:
            log.warning("PAIRING CODE %s  (enter it in the browser; shown on the printer screen when Klarity is running)", display_code(self.code))

    def sync(self) -> None:
        """Cheap per-request check: the lock can change under us (CLI unlock, state reset)."""
        self.publish()

    def _rotate(self) -> None:
        self._data["code"] = new_code()
        self._save()
        self.publish()

    # --- pairing -------------------------------------------------------------
    def _remember(self, client: str, fails: int, until: float) -> None:
        self._clients.pop(client, None)
        self._clients[client] = (fails, until)
        while len(self._clients) > MAX_TRACKED_CLIENTS:      # oldest first: a flood of addresses cannot grow memory
            del self._clients[next(iter(self._clients))]

    def pair(self, raw_code: object, client: str = "unknown") -> tuple[str, float]:
        now = self.clock()
        if self.locked:
            raise PairError(423, "Setup is complete and the configurator is locked. Unlock it on the printer "
                                 "(`eitri-configurator unlock`), then pair again.", locked=True)
        fails, until = self._clients.get(client, (0, 0.0))
        if now < until:
            wait = int(until - now) + 1
            raise PairError(429, f"Too many wrong codes. Try again in {wait} seconds.", retry_after=wait)
        code = normalize_code(raw_code)
        if not hmac.compare_digest(code.encode(), self.code.encode()):
            fails += 1
            self._total_fails += 1
            if fails >= FAILS_BEFORE_ROTATE or self._total_fails >= TOTAL_FAILS_BEFORE_ROTATE:
                self._remember(client, 0, now + BASE_DELAY)
                self._total_fails = 0
                self._rotate()
                raise PairError(401, "Wrong code. For safety a new code was generated: read it from the printer again.", rotated=True)
            if fails >= FAILS_BEFORE_DELAY:
                until = now + min(BASE_DELAY * 2 ** (fails - FAILS_BEFORE_DELAY), MAX_DELAY)
            self._remember(client, fails, until)
            log.warning("wrong pairing code from %s (%d in a row)", client, fails)
            raise PairError(401, "That code is not right. Check the code on the printer screen.")
        self._clients.pop(client, None)
        token = secrets.token_urlsafe(32)
        expires = now + SESSION_TTL
        self._data["sessions"][_digest(token)] = {"created": now, "expires": expires}
        self._prune()
        self._rotate()          # one-time: a second browser needs the next code (also saves the session)
        log.info("browser paired")
        return token, expires

    # --- sessions ------------------------------------------------------------
    def session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        sess = self._data["sessions"].get(_digest(token))
        if not sess or sess["expires"] <= self.clock():
            return None
        return sess

    def csrf_ok(self, token: str, supplied: str | None) -> bool:
        return bool(supplied) and hmac.compare_digest(csrf_for(token).encode(), supplied.encode())

    def revoke(self, token: str) -> None:
        if self._data["sessions"].pop(_digest(token), None) is not None:
            self._save()

    def complete(self, token: str | None) -> None:
        """Setup is done: drop every other session, leave the caller a short grace, close pairing."""
        keep = _digest(token) if token else None
        now = self.clock()
        sessions = {}
        for k, v in self._data["sessions"].items():
            if k == keep:
                sessions[k] = {**v, "expires": min(v["expires"], now + GRACE_TTL)}
        self._data["sessions"] = sessions
        self.unlock_flag.unlink(missing_ok=True)
        self._data["code"] = new_code()
        self._save()
        self.publish()

    async def watch(self, interval: float = 5.0) -> None:
        """Keep pairing.json true when `eitri-configurator unlock|lock` runs in another process."""
        import asyncio
        while True:
            await asyncio.sleep(interval)
            self._prune()
            self.publish()

    def status(self, token: str | None, client: str = "unknown") -> dict[str, Any]:
        sess = self.session(token)
        blocked = self._clients.get(client, (0, 0.0))[1] - self.clock()
        out: dict[str, Any] = {
            "required": True, "paired": sess is not None, "locked": self.locked,
            "pairing_open": not self.locked,
            "retry_after": int(blocked) + 1 if blocked > 0 else 0,
        }
        if sess is not None and token:
            out.update(csrf=csrf_for(token), expires=sess["expires"])
        return out


# --- command-line helpers (run on the printer, not through the API) ---------------------

def _auth_paths(data_dir: Path) -> tuple[Path, Path]:
    d = data_dir / "eitri" / "configurator"
    return d / "unlock", d / "pairing.json"


def unlock(data_dir: Path) -> Path:
    """Open maintenance: pairing works again until setup is completed or `lock` is run."""
    flag, _ = _auth_paths(data_dir)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch(mode=0o640)
    return flag


UNIT = "eitri-configurator.service"


def unlock_and_start(data_dir: Path, systemctl: tuple[str, ...] = ("systemctl", "--no-ask-password"), timeout: float = 20.0,
                     sleep=time.sleep, clock=time.time) -> dict[str, Any]:
    """`eitri-configurator unlock` on the printer: open maintenance, make sure the service is running (a configurator
    that retired after setup is started again) and wait until it shows the pairing code.

    Returns {"flag": Path, "started": bool, "error": str|None, "pairing": dict}: `pairing` is the published pairing.json,
    with a "code" only once the service has switched from "locked" to "pairing".
    """
    t0 = clock()
    before = show_pairing(data_dir)
    flag = unlock(data_dir)
    out: dict[str, Any] = {"flag": flag, "started": False, "error": None, "pairing": before}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*systemctl, *args], capture_output=True, text=True, timeout=60)

    try:
        was_active = run("is-active", "--quiet", UNIT).returncode == 0
        if not was_active:
            started = run("start", UNIT)
            if started.returncode != 0:
                out["error"] = (started.stderr.strip() or started.stdout.strip() or f"systemctl start exited {started.returncode}")[:300]
                return out
            out["started"] = True
    except (OSError, subprocess.SubprocessError) as exc:
        out["error"] = f"cannot control {UNIT}: {exc}"
        return out
    deadline = clock() + timeout
    while True:
        info = show_pairing(data_dir)
        fresh = was_active or (isinstance(info.get("updated"), (int, float)) and info["updated"] >= int(t0) - 1)
        if info.get("state") == "pairing" and info.get("code") and fresh:
            out["pairing"] = info
            return out
        if clock() >= deadline:
            return out
        sleep(0.25)


def lock(data_dir: Path) -> None:
    flag, _ = _auth_paths(data_dir)
    flag.unlink(missing_ok=True)


def show_pairing(data_dir: Path) -> dict[str, Any]:
    _, pf = _auth_paths(data_dir)
    try:
        return json.loads(pf.read_text())
    except (OSError, ValueError):
        return {"state": "unknown", "code": None}
