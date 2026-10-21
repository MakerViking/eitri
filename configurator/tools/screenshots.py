#!/usr/bin/env python3
"""Capture every wizard step at desktop and phone widths, dark and light, using
headless Chromium driven over the DevTools protocol against a --mock server.

    python3 tools/screenshots.py            # everything -> configurator/screenshots/
    python3 tools/screenshots.py --only desktop

The mock server runs a real walk-through (detect, apply, flash, calibrate), so
the images show genuine UI states: scanning, mid-flash progress, results.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "screenshots"
CHROMIUM = shutil.which("chromium") or "/usr/bin/chromium"

VIEWPORTS = {
    "desktop": dict(width=1440, height=980, mobile=False, dpr=1, max_h=2600),
    "phone": dict(width=390, height=844, mobile=True, dpr=2, max_h=4200),
}


class Cdp:
    def __init__(self, ws):
        self.ws, self.n, self.pending = ws, 0, {}

    @classmethod
    async def connect(cls, port: int, session):
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.25)
        else:
            raise RuntimeError("chromium did not come up")
        ws = await session.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=0)
        self = cls(ws)
        self.reader = asyncio.create_task(self._read())
        return self

    async def _read(self):
        async for msg in self.ws:
            d = json.loads(msg.data)
            if "id" in d and d["id"] in self.pending:
                self.pending.pop(d["id"]).set_result(d)

    async def call(self, method, **params):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send_str(json.dumps({"id": self.n, "method": method, "params": params}))
        d = await fut
        if "error" in d:
            raise RuntimeError(f"{method}: {d['error']}")
        return d.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"js error: {r['exceptionDetails'].get('exception', {}).get('description')} in {expr[:90]}")
        return r["result"].get("value")


class Shooter:
    def __init__(self, cdp: Cdp, vp: dict, folder: Path):
        self.cdp, self.vp, self.folder = cdp, vp, folder
        folder.mkdir(parents=True, exist_ok=True)

    async def viewport(self, height):
        v = self.vp
        await self.cdp.call("Emulation.setDeviceMetricsOverride", width=v["width"], height=height, deviceScaleFactor=v["dpr"], mobile=v["mobile"])

    async def wait(self, expr, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if await self.cdp.js(f"Boolean({expr})"):
                return
            await asyncio.sleep(0.15)
        raise TimeoutError(expr)

    async def click(self, text, scope="document"):
        ok = await self.cdp.js(f"""(() => {{
          const els = [...{scope}.querySelectorAll('button, a, label, summary')];
          const el = els.find(e => e.textContent.trim().toLowerCase().includes({json.dumps(text.lower())}) && !e.disabled);
          if (!el) return false; el.scrollIntoView({{block: 'center'}}); el.click(); return true; }})()""")
        if not ok:
            raise RuntimeError(f"nothing to click: {text!r}")

    async def shot(self, name, settle=1.3, full=True):
        await asyncio.sleep(settle)
        base = self.vp["height"]
        if full:
            # Grow the viewport to the content so the whole step is captured and
            # the sticky action bar lands at the bottom, as it does when scrolled.
            await self.viewport(base)
            await self.cdp.js("window.scrollTo(0, 0)")
            h = await self.cdp.js("Math.ceil(document.documentElement.scrollHeight)")
            await self.viewport(min(max(base, h), self.vp["max_h"]))
            await asyncio.sleep(0.35)
        r = await self.cdp.call("Page.captureScreenshot", format="png")
        import base64
        (self.folder / f"{name}.png").write_bytes(base64.b64decode(r["data"]))
        if full:
            await self.viewport(base)
        print("  ", self.folder.name, name)

    async def next(self):
        await self.click("", scope="document.querySelector('.actionbar')") if False else None
        await self.cdp.js("document.querySelector('.actionbar .btn-primary').click()")


def start_server(port: int, data_dir: Path, *extra: str) -> subprocess.Popen:
    shutil.rmtree(data_dir, ignore_errors=True)
    p = subprocess.Popen([sys.executable, "-m", "eitri_configurator", "--mock", "--port", str(port), "--data-dir", str(data_dir), *extra], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read()
            return p
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("mock server did not start")


def pairing_code(data_dir: Path) -> str:
    """What the printer's screen would show."""
    return json.loads((data_dir / "eitri" / "configurator" / "pairing.json").read_text())["code"]


def api_pair(server_port: int, data_dir: Path) -> tuple[str, str]:
    """Pair over the API like a script would; returns (bearer token, session cookie value is the same token)."""
    req = urllib.request.Request(f"http://127.0.0.1:{server_port}/api/pair", method="POST", data=json.dumps({"code": pairing_code(data_dir)}).encode(),
                                 headers={"Content-Type": "application/json"})
    body = json.load(urllib.request.urlopen(req))
    return body["token"], body["csrf"]


async def adopt_session(cdp: Cdp, server_port: int, token: str) -> None:
    """Give the browser the cookie a paired user would have."""
    await cdp.call("Network.enable")
    await cdp.call("Network.setCookie", name="eitri_session", value=token, url=f"http://127.0.0.1:{server_port}/", httpOnly=True, sameSite="Strict")


async def walk(session, chrome_port, server_port, data_dir, vp_name, theme, folder):
    vp = VIEWPORTS[vp_name]
    cdp = await Cdp.connect(chrome_port, session)
    s = Shooter(cdp, vp, folder)
    await cdp.call("Page.enable")
    await s.viewport(vp["height"])
    await cdp.call("Page.addScriptToEvaluateOnNewDocument", source=f"try {{ localStorage.setItem('eitri-theme', '{theme}') }} catch (e) {{}}")
    base = f"http://127.0.0.1:{server_port}"
    extras = folder / "extras" if theme == "dark" else folder
    ex = Shooter(cdp, vp, extras)
    # The door first: nothing works until the browser is paired with the code the printer shows.
    await cdp.call("Page.navigate", url=f"{base}/#/welcome")
    await s.wait("document.querySelector('.code-input')")
    await s.shot("00-pair", settle=1.4)
    if theme == "dark":
        await cdp.js("(() => { const i = document.querySelector('.code-input'); i.value = '00000000'; i.dispatchEvent(new Event('input', {bubbles: true})); document.querySelector('.gate-form').requestSubmit(); })()")
        await s.wait("document.querySelector('.gate-form .field-error').textContent.length > 0")
        await ex.shot("00b-pair-wrong-code", settle=0.5)
    code = pairing_code(data_dir)
    await cdp.js(f"(() => {{ const i = document.querySelector('.code-input'); i.value = {json.dumps(code)}; i.dispatchEvent(new Event('input', {{bubbles: true}})); document.querySelector('.gate-form').requestSubmit(); }})()")
    await s.wait("document.querySelector('.step-head h1')")

    await s.shot("01-welcome", settle=1.6)
    await s.next()

    await s.wait("document.querySelector('[data-step=network].current') && document.querySelector('.wifi-item')")
    await s.shot("02-network")
    if theme == "dark":
        await ex.click("Workshop")
        await ex.shot("02b-network-join")
    await s.next()

    await s.wait("document.querySelector('[data-step=detect].current')")
    if theme == "dark":
        await asyncio.sleep(1.2)
        await ex.shot("03a-detect-scanning", settle=0.2, full=False)
    await s.wait("document.querySelector('.alert.ok') && !document.querySelector('.actionbar .btn-primary').disabled")
    await s.shot("03-detect")
    await s.next()

    await s.wait("document.querySelector('.flavor-card')")
    await s.shot("04-flavor")
    if theme == "dark":
        await ex.click("Kalico")
        await ex.shot("04b-flavor-kalico", settle=0.7)
        await ex.click("Klipper")
    await s.next()

    await s.wait("document.querySelector('.opt')")
    await s.shot("05-options")
    if theme == "dark":
        await cdp.js("document.querySelector('[data-id=chamber_heater] input').click()")
        await ex.shot("05b-options-chamber", settle=0.6)
        await cdp.js("document.querySelector('[data-id=chamber_heater] input').click()")
        await asyncio.sleep(0.5)
    await s.next()

    await s.wait("document.querySelector('.file')")
    await s.shot("06-review")
    if theme == "dark":
        await ex.shot("06b-review-writing", settle=0.0, full=False) if False else None
    await s.next()

    await s.wait("document.querySelector('[data-step=flash].current') && document.querySelector('.ack')", timeout=40)
    await s.shot("07-flash")
    await s.click("Flash Klipper firmware")
    await s.next()
    await s.wait("[...document.querySelectorAll('.fboard.live .pct')].some(e => parseInt(e.textContent) >= 52)", timeout=60)
    await s.shot("07b-flash-progress", settle=0.1)
    await s.wait("document.querySelectorAll('.fboard.ok').length === 2", timeout=90)
    await s.shot("07c-flash-done", settle=0.8)
    await s.next()

    await s.wait("document.querySelector('[data-step=calibrate].current') && document.querySelector('.tool')")
    await s.shot("08-calibrate")
    await s.click("Z-offset", scope="document.querySelector('.tools')")
    await s.click("Start", scope="document.querySelector('.tool-detail')")
    await s.wait("document.querySelector('.zval')", timeout=30)
    await ex.click("−0.5") if False else None
    await cdp.js("document.querySelectorAll('.nudge-btn')[1].click()")
    await asyncio.sleep(0.6)
    await s.shot("08b-calibrate-zoffset")
    await cdp.js("[...document.querySelectorAll('.tool-detail .btn')].find(b => b.textContent.includes('Accept')).click()")
    await s.wait("[...document.querySelectorAll('.tool-detail .btn')].some(b => b.textContent.includes('Save and restart'))")
    await s.click("Save and restart", scope="document.querySelector('.tool-detail')")
    await s.wait("document.querySelector('.tool.done')", timeout=30)
    await s.click("Input shaper", scope="document.querySelector('.tools')")
    await s.click("Measure resonances", scope="document.querySelector('.tool-detail')")
    await s.wait("document.querySelector('.axis')", timeout=60)
    await s.shot("08c-calibrate-shaper")
    await s.click("Bed mesh", scope="document.querySelector('.tools')")
    await s.click("Probe the bed", scope="document.querySelector('.tool-detail')")
    await s.wait("document.querySelector('.meshmap')", timeout=60)
    await s.shot("08d-calibrate-mesh", settle=2.2)
    await s.next()

    await s.wait("document.querySelector('[data-step=done].current') && document.querySelector('.klarity-card')")
    await s.shot("09-done", settle=2.6)
    if theme == "dark":
        # Finishing locked the configurator: a browser that is not paired now meets the locked door.
        await cdp.call("Network.enable")
        await cdp.call("Network.clearBrowserCookies")
        await cdp.call("Page.navigate", url=f"{base}/#/welcome")
        await cdp.call("Page.reload")
        await ex.wait("document.querySelector('.gate-steps')")
        await ex.shot("09b-locked", settle=1.2)


async def fail_pass(session, chrome_port, server_port, data_dir, vp_name, folder):
    """A flash that fails halfway, to show the error and recovery state."""
    import urllib.request as ur
    token, _csrf = api_pair(server_port, data_dir)
    def call(method, path, data=None):
        req = ur.Request(f"http://127.0.0.1:{server_port}/api/{path}", method=method, data=json.dumps(data).encode() if data is not None else None,
                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        return json.load(ur.urlopen(req))
    job = call("POST", "detect")["job"]["id"]
    while call("GET", f"jobs/{job}")["status"] == "running":
        await asyncio.sleep(0.3)
    call("PUT", "state", {"step": "flash"})
    cdp = await Cdp.connect(chrome_port, session)
    vp = VIEWPORTS[vp_name]
    s = Shooter(cdp, vp, folder / "extras")
    await cdp.call("Page.enable")
    await adopt_session(cdp, server_port, token)
    await s.viewport(vp["height"])
    await cdp.call("Page.navigate", url=f"http://127.0.0.1:{server_port}/#/flash")
    await s.wait("document.querySelector('.ack')")
    await s.click("Flash Klipper firmware")
    await s.next()
    await s.wait("document.querySelector('.fboard.fail')", timeout=60)
    await s.shot("07d-flash-failed", settle=1.0)


async def assign_pass(session, chrome_port, server_port, data_dir, vp_name, folder):
    """Probing found two boards but cannot say which is which: the manual assignment card."""
    cdp = await Cdp.connect(chrome_port, session)
    vp = VIEWPORTS[vp_name]
    s = Shooter(cdp, vp, folder / "extras")
    await cdp.call("Page.enable")
    token, _csrf = api_pair(server_port, data_dir)
    await adopt_session(cdp, server_port, token)
    await s.viewport(vp["height"])
    import urllib.request as ur
    req = ur.Request(f"http://127.0.0.1:{server_port}/api/state", method="PUT", data=b'{"step":"detect"}',
                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    ur.urlopen(req).read()
    await cdp.call("Page.navigate", url=f"http://127.0.0.1:{server_port}/#/detect")
    await s.wait("document.querySelector('.assign')", timeout=40)
    await s.shot("03c-detect-assign", settle=1.2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(VIEWPORTS))
    ap.add_argument("--themes", default="dark,light")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="eitri-shots-", dir=ROOT.parent.parent / "build" / "configurator")) if (ROOT.parent.parent / "build" / "configurator").exists() else Path(tempfile.mkdtemp())
    chrome_port, server_port = 9333, 8197
    async with aiohttp.ClientSession() as session:
        for vp_name in [a.only] if a.only else VIEWPORTS:
            for theme in a.themes.split(","):
                folder = OUT / (vp_name if theme == "dark" else f"{vp_name}-light")
                print(f"== {vp_name} / {theme}")
                data_dir = work / "data"
                server = start_server(server_port, data_dir, "--mock-speed", "2")
                chrome = subprocess.Popen([CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb",
                                           f"--remote-debugging-port={chrome_port}", f"--user-data-dir={work / (vp_name + theme)}", "about:blank"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    await walk(session, chrome_port, server_port, data_dir, vp_name, theme, folder)
                finally:
                    chrome.terminate(); server.terminate()
                    chrome.wait(); server.wait()
        for vp_name in [a.only] if a.only else VIEWPORTS:
            data_dir = work / "data"
            server = start_server(server_port, data_dir, "--mock-speed", "3", "--mock-fail", "toolhead:flash")
            chrome = subprocess.Popen([CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb",
                                       f"--remote-debugging-port={chrome_port}", f"--user-data-dir={work / (vp_name + 'fail')}", "about:blank"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                print(f"== {vp_name} / flash failure")
                await fail_pass(session, chrome_port, server_port, data_dir, vp_name, OUT / vp_name)
            finally:
                chrome.terminate(); server.terminate()
                chrome.wait(); server.wait()
        for vp_name in [a.only] if a.only else VIEWPORTS:
            data_dir = work / "data"
            server = start_server(server_port, data_dir, "--mock-speed", "3", "--mock-unassigned")
            chrome = subprocess.Popen([CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb",
                                       f"--remote-debugging-port={chrome_port}", f"--user-data-dir={work / (vp_name + 'assign')}", "about:blank"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                print(f"== {vp_name} / manual assignment")
                await assign_pass(session, chrome_port, server_port, data_dir, vp_name, OUT / vp_name)
            finally:
                chrome.terminate(); server.terminate()
                chrome.wait(); server.wait()
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
