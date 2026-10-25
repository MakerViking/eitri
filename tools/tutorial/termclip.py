#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render a terminal "tape" (typed commands + output) into an MP4 and a GIF for docs/GETTING-STARTED.md.

    tools/tutorial/termclip.py docs/tutorial/tapes/04-dry-run.json docs/tutorial/04-dry-run

A tape is JSON: {"title": ..., "events": [...]}, each event one of
    {"prompt": "mks@mkspi:~$ ", "type": "sudo bash install.sh", "cps": 18}   a command typed at a prompt
    {"out": ["line", ...], "lps": 25}                                      output appearing line by line
    {"hidden": "password: "}                                               a prompt whose answer is not echoed
    {"pause": 1.5}                                                         hold the current frame
    {"caption": "text"}                                                    the caption bar from here on
Output lines are shown verbatim; the tapes say where each one comes from. Needs Pillow and ffmpeg.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 960, 540
FPS = 12
PAD = 18
FONT = "/usr/share/fonts/TTF/DejaVuSansMono.ttf"
BOLD = "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf"
SIZE = 14
CAPTION_H = 46
BG, FG, DIM = (13, 17, 23), (220, 225, 230), (139, 148, 158)
GREEN, YELLOW, RED, CYAN, BLUE = (63, 185, 80), (210, 153, 34), (248, 81, 73), (86, 182, 194), (88, 166, 255)
BAR = (22, 27, 34)


def color_of(line: str):
    s = line.strip()
    if s.startswith(("ok ", "ok\t")) or " signature OK" in s or (s.startswith("[eitri]") and "verified" in s):
        return GREEN
    if s.startswith("warn") or "WARNING" in s:
        return YELLOW
    if s.startswith(("FAIL", "POINT OF NO RETURN")) or "ERROR" in s:
        return RED
    if s.startswith("==>"):
        return CYAN
    return FG


class Term:
    def __init__(self, title: str):
        self.font = ImageFont.truetype(FONT, SIZE)
        self.bold = ImageFont.truetype(BOLD, SIZE)
        self.lh = SIZE + 6
        self.cols = int((W - 2 * PAD) / self.font.getlength("M"))
        self.rows = (H - 2 * PAD - CAPTION_H - 28) // self.lh
        self.lines: list[tuple[str, tuple]] = []
        self.cur = ""
        self.cur_color = FG
        self.caption = ""
        self.title = title
        self.frames: list[Image.Image] = []

    def _wrap(self, text: str) -> list[str]:
        # break at the last space that fits, like a terminal showing a pre-wrapped message; a word longer than a
        # whole row (a URL, a hash) is cut where it must be
        out = []
        while len(text) > self.cols:
            cut = text.rfind(" ", 0, self.cols + 1)
            if cut <= len(text) - len(text.lstrip()) + 2:
                cut = self.cols
            out.append(text[:cut].rstrip())
            text = "  " + text[cut:].lstrip()
        out.append(text)
        return out

    def newline(self):
        for part in self._wrap(self.cur):
            self.lines.append((part, self.cur_color))
        self.cur, self.cur_color = "", FG

    def frame(self, cursor: bool = True, n: int = 1):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        # window chrome
        d.rectangle([0, 0, W, 28], fill=BAR)
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([12 + i * 20, 9, 24 + i * 20, 21], fill=c)
        d.text((W // 2, 14), self.title, font=self.font, fill=DIM, anchor="mm")
        shown = (self.lines[-self.rows:] + [(p, self.cur_color) for p in self._wrap(self.cur)])[-self.rows:]
        y = 28 + PAD // 2
        for i, (text, col) in enumerate(shown):
            d.text((PAD, y), text, font=self.font, fill=col)
            if cursor and i == len(shown) - 1:
                x = PAD + self.font.getlength(text)
                d.rectangle([x + 1, y + 2, x + 9, y + SIZE + 2], fill=FG)
            y += self.lh
        if self.caption:
            caption_bar(img, self.caption, self.bold)
        for _ in range(n):
            self.frames.append(img)

    def hold(self, seconds: float):
        self.frame(n=max(1, int(seconds * FPS)))


def render(tape: dict) -> list[Image.Image]:
    t = Term(tape.get("title", "Terminal"))
    t.frame(n=FPS // 2)
    for ev in tape["events"]:
        if "caption" in ev:
            t.caption = ev["caption"]
        elif "pause" in ev:
            t.hold(ev["pause"])
        elif "prompt" in ev:
            t.cur, t.cur_color = ev["prompt"], FG
            t.hold(0.5)
            cps = ev.get("cps", 16)
            step = max(1, round(cps / FPS))
            text = ev.get("type", "")
            for i in range(0, len(text), step):
                t.cur = ev["prompt"] + text[: i + step]
                t.frame()
            t.hold(0.4)
            t.newline()
        elif "hidden" in ev:
            t.cur = ev["hidden"]
            t.hold(1.2)
            t.newline()
        elif "out" in ev:
            lps = ev.get("lps", 20)
            per, batch = max(1, round(FPS / lps)), max(1, round(lps / FPS))   # frames per line, lines per frame
            lines = ev["out"]
            for i in range(0, len(lines), batch):
                for line in lines[i:i + batch]:
                    t.cur, t.cur_color = line, color_of(line)
                    t.newline()
                t.frame(cursor=False, n=per)
    t.hold(2.5)
    return t.frames


def caption_bar(img: Image.Image, text: str, font) -> None:
    """The caption band at the bottom of a frame, shared by every tutorial clip."""
    d = ImageDraw.Draw(img)
    d.rectangle([0, img.height - CAPTION_H, img.width, img.height], fill=BAR)
    d.text((img.width // 2, img.height - CAPTION_H // 2), text, font=font, fill=BLUE, anchor="mm")


def encode(frames: list[Image.Image], out: Path, *, fps: int = FPS, crf: int = 23, gif: str = "palettegen=max_colors=32") -> None:
    """Write `out`.mp4 and `out`.gif in one ffmpeg pass. A frame that is the same image object as the one before (a
    hold) is hard-linked instead of encoded again. `gif` is the filter chain ending in palettegen, before paletteuse."""
    tmp = Path(tempfile.mkdtemp(prefix="tutorial-clip-"))
    try:
        prev = None
        for i, f in enumerate(frames):
            path = tmp / f"f{i:05d}.png"
            if f is prev:
                os.link(tmp / f"f{i - 1:05d}.png", path)
            else:
                f.save(path)
            prev = f
        graph = f"[0:v]split[m][g];[g]split[a][b];[a]{gif}:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle[gif]"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(fps), "-i", str(tmp / "f%05d.png"),
                        "-filter_complex", graph,
                        "-map", "[m]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf), "-tune", "animation",
                        "-movflags", "+faststart", str(out.with_suffix(".mp4")),
                        "-map", "[gif]", str(out.with_suffix(".gif"))], check=True)
    finally:
        shutil.rmtree(tmp)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    tape = json.loads(Path(sys.argv[1]).read_text())
    frames = render(tape)
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    encode(frames, out)
    print(f"{out}.mp4 / .gif: {len(frames) / FPS:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
