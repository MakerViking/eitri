# SPDX-License-Identifier: Apache-2.0
"""Downloads with hash verification, resume and size limits (stdlib only)."""
from __future__ import annotations

import hashlib
import os
import shutil
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from eitri_mcuflash.errors import FlashError

CHUNK = 1 << 20
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
USER_AGENT = "eitri-update/0.1"

Opener = Callable[[urllib.request.Request, float], "object"]


def _open(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - scheme is validated by the caller


def _check_scheme(url: str, allow_insecure: bool) -> None:
    if url.startswith("https://") or url.startswith("file://"):
        return
    if allow_insecure and url.startswith("http://"):
        return
    raise FlashError("refusing to fetch %s: only https:// (or file://) URLs are allowed" % url, code="insecure_url")


def fetch_small(url: str, *, limit: int = MAX_MANIFEST_BYTES, opener: Opener = _open, allow_insecure: bool = False, timeout: float = 30.0) -> bytes:
    _check_scheme(url, allow_insecure)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    try:
        with opener(req, timeout) as resp:  # type: ignore[attr-defined]
            data = resp.read(limit + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise FlashError("cannot download %s: %s" % (url, getattr(exc, "reason", exc)), code="network") from exc
    if len(data) > limit:
        raise FlashError("%s is larger than %d bytes" % (url, limit), code="network")
    return data


def download(
    url: str,
    dest: str,
    *,
    sha256: str,
    size: int,
    progress: Optional[Callable[[int, int], None]] = None,
    opener: Opener = _open,
    allow_insecure: bool = False,
    retries: int = 4,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Download *url* to *dest* (atomically), verifying size and SHA-256.  Resumes ``dest + '.part'``."""
    _check_scheme(url, allow_insecure)
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest) and _hash_file(dest) == sha256 and os.path.getsize(dest) == size:
        return dest
    last = "unknown error"
    for attempt in range(retries):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > size:
            os.unlink(part)
            have = 0
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = "bytes=%d-" % have
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout) as resp:  # type: ignore[attr-defined]
                status = getattr(resp, "status", 200)
                if have and status != 206:
                    have = 0                      # server ignored the range: start over
                mode = "ab" if have else "wb"
                with open(part, mode) as fh:
                    got = have
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > size:
                            raise FlashError("%s is larger than the %d bytes announced in the manifest" % (url, size), code="size_mismatch")
                        fh.write(chunk)
                        if progress:
                            progress(got, size)
                    fh.flush()
                    os.fsync(fh.fileno())
        except FlashError:
            if os.path.exists(part):
                os.unlink(part)
            raise
        except (urllib.error.URLError, OSError, ConnectionError) as exc:
            last = str(getattr(exc, "reason", exc))
            sleep(min(2 ** attempt, 15))
            continue
        if os.path.getsize(part) != size:
            last = "connection ended after %d of %d bytes" % (os.path.getsize(part), size)
            sleep(1)
            continue
        if _hash_file(part) != sha256:
            os.unlink(part)
            raise FlashError("downloaded file does not match the signed checksum (%s)" % url, code="checksum",
                             recovery="Try again; if it keeps failing the mirror is serving a corrupt or tampered file.")
        os.replace(part, dest)
        return dest
    raise FlashError("download of %s failed: %s" % (url, last), code="network")


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def free_bytes(path: str) -> int:
    probe = path
    while not os.path.exists(probe):
        probe = os.path.dirname(probe) or "/"
    return shutil.disk_usage(probe).free
