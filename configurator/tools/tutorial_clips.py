#!/usr/bin/env python3
"""Record the setup-wizard clips for docs/GETTING-STARTED.md from a --mock server in headless Chromium.

    python3 tools/tutorial_clips.py     # -> ../docs/tutorial/06-pair, 07-wizard-detect, 07-wizard-options, 08-zoffset, 09-done (.mp4 + .gif)

The mock plays a printer installed without tools: both boards answer, the toolhead cannot be flashed over
serial (EITRI_MOCK_TOOLHEAD_UNRESOLVED, the real flasher's behaviour today), so the boards keep their firmware.
Every screen is the real UI; only the printer behind it is simulated. Needs Pillow and ffmpeg.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screenshots import CHROMIUM, ROOT, Cdp, pairing_code, start_server  # noqa: E402

OUT = ROOT.parent / "docs" / "tutorial"
VIEW_W, VIEW_H = 1280, 720
W = 960
FPS = 10
CAPTION_H = 46
BAR, BLUE = (22, 27, 34), (88, 166, 255)
BOLD = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"


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
        ImageDraw.Draw(img).text((W // 2, h + CAPTION_H // 2), caption, font=font, fill=BLUE, anchor="mm")
    return img


def encode(frames: list, out: Path, tail: float = 1.5) -> None:
    font = ImageFont.truetype(BOLD, 16)
    t0, t_end = frames[0][0], frames[-1][0] + tail
    tmp = Path(tempfile.mkdtemp(prefix="wizclip-"))
    try:
        i, n, last = 0, 0, None
        while t0 + n / FPS <= t_end:
            t = t0 + n / FPS
            while i + 1 < len(frames) and frames[i + 1][0] <= t:
                i += 1
            key = (i, frames[i][2])
            if key != last:
                cur = compose(frames[i][1], frames[i][2], font)
                last = key
            cur.save(tmp / f"f{n:05d}.png")
            n += 1
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS), "-i", str(tmp / "f%05d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-tune", "animation", "-movflags", "+faststart",
                        str(out.with_suffix(".mp4"))], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS), "-i", str(tmp / "f%05d.png"),
                        "-vf", "fps=6,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle",
                        str(out.with_suffix(".gif"))], check=True)
        print(f"{out}.mp4 / .gif: {n / FPS:.1f} s")
    finally:
        shutil.rmtree(tmp)


async def wait(cdp: Cdp, expr: str, timeout: float = 40):
    end = time.time() + timeout
    while time.time() < end:
        if await cdp.js(f"Boolean({expr})"):
            return
        await asyncio.sleep(0.15)
    raise TimeoutError(expr)


async def click_text(cdp: Cdp, text: str, scope: str = "document"):
    ok = await cdp.js(f"""(() => {{
      const el = [...{scope}.querySelectorAll('button, a, label, summary')].find(e => e.textContent.trim().toLowerCase().includes({json.dumps(text.lower())}) && !e.disabled);
      if (!el) return false; el.scrollIntoView({{block: 'center', behavior: 'smooth'}}); el.click(); return true; }})()""")
    if not ok:
        raise RuntimeError(f"nothing to click: {text!r}")


async def next_step(cdp: Cdp):
    await cdp.js("document.querySelector('.actionbar .btn-primary').click()")


async def scroll(cdp: Cdp, y: int | str, settle: float = 1.0):
    await cdp.js(f"window.scrollTo({{top: {y}, behavior: 'smooth'}})")
    await asyncio.sleep(settle)


async def type_code(cdp: Cdp, code: str):
    for i in range(1, len(code) + 1):
        await cdp.js(f"(() => {{ const el = document.querySelector('.code-input'); el.value = {json.dumps(code[:i])}; el.dispatchEvent(new Event('input', {{bubbles: true}})); }})()")
        await asyncio.sleep(0.28)


async def record(session, chrome_port: int, server_port: int, data_dir: Path):
    cdp = await Cdp.connect(chrome_port, session)
    await cdp.call("Page.enable")
    await cdp.call("Emulation.setDeviceMetricsOverride", width=VIEW_W, height=VIEW_H, deviceScaleFactor=1, mobile=False)
    await cdp.call("Page.addScriptToEvaluateOnNewDocument", source="try { localStorage.setItem('eitri-theme', 'dark') } catch (e) {}")
    base = f"http://127.0.0.1:{server_port}"

    # 06: the locked door, and the code the printer's screen shows
    await cdp.call("Page.navigate", url=f"{base}/#/welcome")
    await wait(cdp, "document.querySelector('.code-input')")
    await asyncio.sleep(1.2)
    rec = Recorder(cdp)
    rec.say("Open http://eitri.local:8099 and type the code from the printer's screen")
    rec.start()
    await asyncio.sleep(2.0)
    await type_code(cdp, pairing_code(data_dir))
    await asyncio.sleep(0.8)
    await cdp.js("document.querySelector('.gate-form').requestSubmit()")
    await wait(cdp, "document.querySelector('.step-head h1')")
    rec.say("Paired: this browser may now set up the printer")
    await asyncio.sleep(3.0)
    await rec.stop()
    encode(rec.frames, OUT / "06-pair")

    # 07 to 09: the wizard, one continuous run cut into a clip per guide step. Each caption is set when the
    # step is entered, so it changes with the page, not after it.
    rec = Recorder(cdp)
    rec.say("Welcome: press Begin setup")
    rec.start()
    await asyncio.sleep(2.0)
    await next_step(cdp)
    rec.say("Network: the connection it already has is fine")
    await wait(cdp, "document.querySelector('[data-step=network].current') && document.querySelector('.wifi-item')")
    await asyncio.sleep(2.5)
    await next_step(cdp)
    rec.say("Detect: the wizard asks each control board what it is")
    await wait(cdp, "document.querySelector('[data-step=detect].current')")
    await wait(cdp, "!document.querySelector('.actionbar .btn-primary').disabled && document.querySelector('.alert')", timeout=60)
    await asyncio.sleep(0.6)
    rec.say("Both boards found. They keep the firmware they run now")
    await scroll(cdp, "document.querySelector('.alert').getBoundingClientRect().top + scrollY - 120", 3.2)
    rec.cut("07-wizard-detect")

    await next_step(cdp)
    rec.say("Firmware: Klipper is the recommended choice")
    await wait(cdp, "document.querySelector('.flavor-card')")
    await asyncio.sleep(2.5)
    await next_step(cdp)
    rec.say("Options: camera, Fluidd or Mainsail, access, crash reports")
    await wait(cdp, "document.querySelector('.opt')")
    await asyncio.sleep(1.5)
    await scroll(cdp, "document.body.scrollHeight / 3", 1.6)
    await scroll(cdp, "document.body.scrollHeight", 1.8)
    await scroll(cdp, 0, 0.6)
    await next_step(cdp)
    rec.say("Review: the files it will write. Next applies them")
    await wait(cdp, "document.querySelector('.file')")
    await asyncio.sleep(2.5)
    await next_step(cdp)
    rec.say("Applying: it writes the configuration and starts Klipper")
    await wait(cdp, "document.querySelector('[data-step=flash].current')", timeout=60)
    rec.say("Flash: nothing to do. Your boards keep their firmware")
    await asyncio.sleep(3.2)
    rec.cut("07-wizard-options")

    await next_step(cdp)
    rec.say("Calibrate: open Z-offset, paper under the nozzle, Start")
    await wait(cdp, "document.querySelector('[data-step=calibrate].current') && document.querySelector('.tool')")
    await asyncio.sleep(1.5)
    await click_text(cdp, "Z-offset", "document.querySelector('.tools')")
    await asyncio.sleep(1.2)
    await click_text(cdp, "Start", "document.querySelector('.tool-detail')")
    await wait(cdp, "document.querySelector('.zval')", timeout=30)
    rec.say("Lower the nozzle in small steps until the paper drags slightly")
    await scroll(cdp, "document.querySelector('.tool-detail').getBoundingClientRect().bottom + scrollY - innerHeight + 110", 1.2)
    for i in (1, 1, 2, 3):                                  # -0.5, -0.5, -0.1, -0.05
        await cdp.js(f"document.querySelectorAll('.nudge-btn')[{i}].click()")
        await asyncio.sleep(0.9)
    await asyncio.sleep(0.6)
    rec.say("Accept this height, then Save and restart")
    await cdp.js("[...document.querySelectorAll('.tool-detail .btn')].find(b => b.textContent.includes('Accept')).click()")
    await wait(cdp, "[...document.querySelectorAll('.tool-detail .btn')].some(b => b.textContent.includes('Save and restart'))")
    await asyncio.sleep(1.4)
    await click_text(cdp, "Save and restart", "document.querySelector('.tool-detail')")
    await wait(cdp, "document.querySelector('.tool.done')", timeout=30)
    await scroll(cdp, 0, 0.8)
    rec.say("Z-offset done. Input shaper and bed mesh are optional")
    await asyncio.sleep(2.4)
    rec.cut("08-zoffset")

    await next_step(cdp)
    rec.say("Remote access: optional, skip it if you do not need it")
    await wait(cdp, "document.querySelector('[data-step=remote].current') && document.querySelector('.keybox')")
    await asyncio.sleep(2.5)
    await next_step(cdp)
    rec.say("Done: open Fluidd or Mainsail and print")
    await wait(cdp, "document.querySelector('[data-step=done].current') && document.querySelector('.klarity-card')")
    await asyncio.sleep(1.5)
    await scroll(cdp, "document.body.scrollHeight / 2", 2.0)
    await asyncio.sleep(1.0)
    rec.cut("09-done")
    await rec.stop()
    for name, a, b in rec.cuts:
        encode(rec.frames[a:b], OUT / name)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="eitri-clips-"))
    chrome_port, server_port = 9334, 8198
    os.environ["EITRI_MOCK_TOOLHEAD_UNRESOLVED"] = "1"         # the mcuflash mock reads it from the server's environment
    server = start_server(server_port, work / "data", "--mock-speed", "2")
    chrome = subprocess.Popen([CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb",
                               f"--remote-debugging-port={chrome_port}", f"--user-data-dir={work / 'chrome'}", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as session:
            await record(session, chrome_port, server_port, work / "data")
    finally:
        chrome.terminate(); server.terminate()
        chrome.wait(); server.wait()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
