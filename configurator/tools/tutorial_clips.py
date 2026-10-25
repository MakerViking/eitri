#!/usr/bin/env python3
"""Record the setup-wizard clips for docs/GETTING-STARTED.md from a --mock server in headless Chromium.

    python3 tools/tutorial_clips.py     # -> ../docs/tutorial/06-pair, 07-wizard-detect, 07-wizard-options, 08-zoffset, 09-done (.mp4 + .gif)

The mock plays a printer installed without tools: both boards answer, the toolhead cannot be flashed over
serial (--mock-toolhead-unresolved, the real flasher's behaviour today), so the boards keep their firmware.
Every screen is the real UI; only the printer behind it is simulated. Needs Pillow and ffmpeg.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from PIL import Image, ImageFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools" / "tutorial"))
from screenshots import ROOT, Cdp, click_text, pairing_code, press_next, start_chrome, start_server, wait_for  # noqa: E402
from termclip import BAR, CAPTION_H, caption_bar, encode  # noqa: E402

OUT = ROOT.parent / "docs" / "tutorial"
VIEW_W, VIEW_H = 1280, 720
W = 960
FPS = 10
BOLD = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
# 6 fps and 800 px keep the GIFs small; the MP4 keeps the full frame rate and width
GIF = "fps=6,scale=800:-1:flags=lanczos,palettegen=max_colors=64"


class Recorder:
    """Grabs the page as fast as Chromium allows and remembers when each frame was taken; the caption is
    whatever was set at that moment."""

    def __init__(self, cdp: Cdp):
        self.cdp, self.frames, self.caption, self.running = cdp, [], "", False
        self.cuts: list[tuple[str, int, int]] = []
        self._from = 0

    async def _loop(self):
        while self.running:
            try:
                # headless Chromium now and then never answers a capture during a page transition
                r = await asyncio.wait_for(self.cdp.call("Page.captureScreenshot", format="jpeg", quality=92), 2.0)
            except asyncio.TimeoutError:
                continue
            self.frames.append((time.monotonic(), r["data"], self.caption))
            await asyncio.sleep(0.04)

    def start(self):
        self.running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        await self.task

    def cut(self, name: str):
        """Everything since the previous cut becomes the clip `name`."""
        self.cuts.append((name, self._from, len(self.frames)))
        self._from = len(self.frames)

    def say(self, text: str):
        print(f"  {len(self.frames):4d} frames | {text}", flush=True)
        self.caption = text


def compose(shot_b64: str, caption: str, font) -> Image.Image:
    shot = Image.open(io.BytesIO(base64.b64decode(shot_b64))).convert("RGB")
    h = round(shot.height * W / shot.width)
    img = Image.new("RGB", (W, h + CAPTION_H), BAR)
    img.paste(shot.resize((W, h), Image.LANCZOS), (0, 0))
    if caption:
        caption_bar(img, caption, font)
    return img


def encode_clip(frames: list, out: Path, tail: float = 1.5) -> None:
    """Resample the captured frames (taken at irregular times) to FPS; a frame shown again is the same image object,
    which encode() writes once."""
    font = ImageFont.truetype(BOLD, 16)
    t0 = frames[0][0]
    images, i, last, cur = [], 0, None, None
    for n in range(int((frames[-1][0] + tail - t0) * FPS) + 1):
        while i + 1 < len(frames) and frames[i + 1][0] <= t0 + n / FPS:
            i += 1
        if i != last:
            cur, last = compose(frames[i][1], frames[i][2], font), i
        images.append(cur)
    encode(images, out, fps=FPS, crf=26, gif=GIF)
    print(f"{out}.mp4 / .gif: {len(images) / FPS:.1f} s")


async def scroll(cdp: Cdp, y: int | str, settle: float = 1.0):
    await cdp.js(f"window.scrollTo({{top: {y}, behavior: 'smooth'}})")
    await asyncio.sleep(settle)


async def type_code(cdp: Cdp, code: str):
    for i in range(1, len(code) + 1):
        await cdp.js(f"(() => {{ const el = document.querySelector('.code-input'); el.value = {json.dumps(code[:i])}; el.dispatchEvent(new Event('input', {{bubbles: true}})); }})()")
        await asyncio.sleep(0.28)


async def step(cdp: Cdp, rec: Recorder, caption: str, ready: str, hold: float = 2.5, timeout: float = 40):
    """Press the wizard's primary button, caption the step at once (so it changes with the page), wait for `ready`."""
    await press_next(cdp)
    rec.say(caption)
    await wait_for(cdp, ready, timeout)
    await asyncio.sleep(hold)


async def record(session, chrome_port: int, server_port: int, data_dir: Path):
    cdp = await Cdp.connect(chrome_port, session)
    await cdp.call("Page.enable")
    await cdp.call("Emulation.setDeviceMetricsOverride", width=VIEW_W, height=VIEW_H, deviceScaleFactor=1, mobile=False)
    await cdp.call("Page.addScriptToEvaluateOnNewDocument", source="try { localStorage.setItem('eitri-theme', 'dark') } catch (e) {}")
    base = f"http://127.0.0.1:{server_port}"
    current = lambda name: f"document.querySelector('[data-step={name}].current')"   # noqa: E731

    # 06: the locked door, and the code the printer's screen shows
    await cdp.call("Page.navigate", url=f"{base}/#/welcome")
    await wait_for(cdp, "document.querySelector('.code-input')")
    await asyncio.sleep(1.2)
    rec = Recorder(cdp)
    rec.say("Open http://eitri.local:8099 and type the code from the printer's screen")
    rec.start()
    await asyncio.sleep(2.0)
    await type_code(cdp, pairing_code(data_dir))
    await asyncio.sleep(0.8)
    await cdp.js("document.querySelector('.gate-form').requestSubmit()")
    await wait_for(cdp, "document.querySelector('.step-head h1')")
    rec.say("Paired: this browser may now set up the printer")
    await asyncio.sleep(3.0)
    rec.cut("06-pair")

    # 07 to 09: the wizard, one continuous run cut into a clip per guide step
    rec.say("Welcome: press Begin setup")
    await asyncio.sleep(2.0)
    await step(cdp, rec, "Network: the connection it already has is fine", f"{current('network')} && document.querySelector('.wifi-item')")
    await step(cdp, rec, "Detect: the wizard asks each control board what it is",
               f"{current('detect')} && !document.querySelector('.actionbar .btn-primary').disabled && document.querySelector('.alert')",
               hold=0.6, timeout=60)
    rec.say("Both boards found. They keep the firmware they run now")
    await scroll(cdp, "document.querySelector('.alert').getBoundingClientRect().top + scrollY - 120", 3.2)
    rec.cut("07-wizard-detect")

    await step(cdp, rec, "Firmware: Klipper is the recommended choice", "document.querySelector('.flavor-card')")
    await step(cdp, rec, "Options: camera, Fluidd or Mainsail, access, crash reports", "document.querySelector('.opt')", hold=1.5)
    await scroll(cdp, "document.body.scrollHeight / 3", 1.6)
    await scroll(cdp, "document.body.scrollHeight", 1.8)
    await scroll(cdp, 0, 0.6)
    await step(cdp, rec, "Review: the files it will write. Next applies them", "document.querySelector('.file')")
    await step(cdp, rec, "Applying: it writes the configuration and starts Klipper", current("flash"), hold=0, timeout=60)
    rec.say("Flash: nothing to do. Your boards keep their firmware")
    await asyncio.sleep(3.2)
    rec.cut("07-wizard-options")

    await step(cdp, rec, "Calibrate: open Z-offset, paper under the nozzle, Start",
               f"{current('calibrate')} && document.querySelector('.tool')", hold=1.5)
    await click_text(cdp, "Z-offset", "document.querySelector('.tools')", smooth=True)
    await asyncio.sleep(1.2)
    await click_text(cdp, "Start", "document.querySelector('.tool-detail')", smooth=True)
    await wait_for(cdp, "document.querySelector('.zval')")
    rec.say("Lower the nozzle in small steps until the paper drags slightly")
    await scroll(cdp, "document.querySelector('.tool-detail').getBoundingClientRect().bottom + scrollY - innerHeight + 110", 1.2)
    for i in (1, 1, 2, 3):                                  # -0.5, -0.5, -0.1, -0.05
        await cdp.js(f"document.querySelectorAll('.nudge-btn')[{i}].click()")
        await asyncio.sleep(0.9)
    await asyncio.sleep(0.6)
    rec.say("Accept this height, then Save and restart")
    await cdp.js("[...document.querySelectorAll('.tool-detail .btn')].find(b => b.textContent.includes('Accept')).click()")
    await wait_for(cdp, "[...document.querySelectorAll('.tool-detail .btn')].some(b => b.textContent.includes('Save and restart'))")
    await asyncio.sleep(1.4)
    await click_text(cdp, "Save and restart", "document.querySelector('.tool-detail')", smooth=True)
    await wait_for(cdp, "document.querySelector('.tool.done')")
    await scroll(cdp, 0, 0.8)
    rec.say("Z-offset done. Input shaper and bed mesh are optional")
    await asyncio.sleep(2.4)
    rec.cut("08-zoffset")

    await step(cdp, rec, "Remote access: optional, skip it if you do not need it", f"{current('remote')} && document.querySelector('.keybox')")
    await step(cdp, rec, "Done: open Fluidd or Mainsail and print", f"{current('done')} && document.querySelector('.klarity-card')", hold=1.5)
    await scroll(cdp, "document.body.scrollHeight / 2", 2.0)
    await asyncio.sleep(1.0)
    rec.cut("09-done")
    await rec.stop()
    for name, a, b in rec.cuts:
        encode_clip(rec.frames[a:b], OUT / name)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="eitri-clips-"))
    chrome_port, server_port = 9334, 8198
    server = start_server(server_port, work / "data", "--mock-speed", "2", "--mock-toolhead-unresolved")
    chrome = start_chrome(chrome_port, work / "chrome")
    try:
        async with aiohttp.ClientSession() as session:
            await record(session, chrome_port, server_port, work / "data")
    finally:
        chrome.terminate(); server.terminate()
        chrome.wait(); server.wait()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
