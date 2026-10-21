"""Camera preview: fetch a still from the camera streamer and push it to the panel.

Klarity never opens the camera itself (UVC is single-opener); it only does an HTTP GET of
ustreamer's ``/snapshot``.  Pushing the JPEG to the panel uses the Nextion ``twfile``
transparent file write.  That path is implemented but **unverified on the real panel**
(see TODO markers), so it is disabled unless ``[camera] push_enabled: true``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import urllib.request
from urllib.error import HTTPError, URLError

from .protocol import Cmd, ProtocolError
from .screen import Screen, ScreenError
from .screenmap import CameraSpec, ScreenMap

log = logging.getLogger(__name__)

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class CameraError(RuntimeError):
    """Snapshot unavailable or not a usable JPEG."""


def validate_jpeg(data: bytes) -> bytes:
    if not data.startswith(JPEG_SOI) or JPEG_EOI not in data[-64:]:
        raise CameraError("snapshot is not a complete JPEG")
    return data


def _fetch_blocking(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "klarity"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (http(s) from config)
            data = response.read(MAX_SNAPSHOT_BYTES + 1)
    except HTTPError as exc:
        exc.close()  # releases the response body
        raise CameraError(f"snapshot fetch failed: HTTP {exc.code}") from exc
    except (URLError, OSError, ValueError) as exc:
        raise CameraError(f"snapshot fetch failed: {exc}") from exc
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise CameraError("snapshot larger than limit")
    return validate_jpeg(data)


class SnapshotFetcher:
    def __init__(self, url: str, timeout: float = 3.0) -> None:
        if not url.startswith(("http://", "https://")):
            raise CameraError(f"snapshot_url must be http(s): {url!r}")
        self.url = url
        self.timeout = timeout

    async def fetch(self) -> bytes:
        return await asyncio.to_thread(_fetch_blocking, self.url, self.timeout)


def resize_jpeg(data: bytes, width: int, height: int, quality: int = 70) -> bytes:
    """Scale-to-fit and re-encode as a baseline JPEG the panel's decoder can handle.

    ustreamer delivers the full 1920x1080 frame; the panel widget is a few hundred pixels
    and the UART is slow, so the frame must shrink first.  Needs Pillow (``klarity[camera]``).
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise CameraError(
            "Pillow is required to resize camera frames (pip install klarity[camera])"
        ) from exc
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            img.thumbnail((width, height), Image.Resampling.BILINEAR)
            out = io.BytesIO()
            img.save(out, "JPEG", quality=quality, progressive=False, optimize=False)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise CameraError(f"cannot decode snapshot: {exc}") from exc
    return out.getvalue()


class CameraPreview:
    """While the panel shows the camera page, refresh its picture every ``interval`` seconds."""

    def __init__(
        self,
        screen: Screen,
        smap: ScreenMap,
        fetcher: SnapshotFetcher,
        *,
        push_enabled: bool,
    ) -> None:
        self.screen = screen
        self.smap = smap
        self.fetcher = fetcher
        self.push_enabled = push_enabled
        self.frames_pushed = 0

    @property
    def spec(self) -> CameraSpec | None:
        return self.smap.camera

    def active(self) -> bool:
        spec = self.spec
        if spec is None or not self.screen.connected.is_set():
            return False
        page = self.smap.pages[spec.page_key]
        return (
            page.id is not None
            and page.id == self.screen.current_page
            and self.smap.is_page_active(page)
        )

    async def run(self) -> None:
        failures = 0
        while True:
            spec = self.spec
            if not self.active() or spec is None:
                await asyncio.sleep(0.5)
                continue
            try:
                await self.push_frame(spec)
                failures = 0
                await asyncio.sleep(spec.interval)
            except (CameraError, ScreenError, ProtocolError) as exc:
                failures += 1
                log.warning("camera preview: %s", exc)
                await asyncio.sleep(min(spec.interval * 2**failures, 30))

    async def push_frame(self, spec: CameraSpec) -> None:
        jpeg = await self.fetcher.fetch()
        jpeg = await asyncio.to_thread(resize_jpeg, jpeg, spec.width, spec.height)
        if not self.push_enabled:
            log.debug("camera frame ready (%d bytes); push disabled", len(jpeg))
            return
        # TODO(hardware): verify against the stock .tft that (a) ``twfile`` is accepted,
        # (b) the target path/storage (``ram/`` vs SD) exists and (c) the picture
        # component's ``path`` attribute is how a runtime file is displayed.
        await self.screen.send_file(spec.picture, jpeg)
        await self.screen.send(Cmd.set_attr(spec.obj, "path", spec.picture))
        self.frames_pushed += 1
