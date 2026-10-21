"""Test harness: a real aiohttp server on an ephemeral port, in mock mode, driven
over plain HTTP.  No pytest plugins - the loop lives in a helper thread."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from aiohttp import web

from eitri_configurator.app import CTX, create_app
from eitri_configurator.mock.seed import seed
from eitri_configurator.settings import Settings


class Server:
    def __init__(self, tmp: Path, **kw):
        seed(tmp)
        self.settings = Settings.for_mock(tmp, mock_speed=kw.pop("mock_speed", 0), **kw)
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(10), "server did not start"

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.app = create_app(self.settings)
        self.runner = web.AppRunner(self.app, access_log=None)
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = self.runner.addresses[0][1]
        self.ready.set()
        self.loop.run_forever()

    def close(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop)
        fut.result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ctx(self):
        return self.app[CTX]

    def req(self, method: str, path: str, data=None, headers=None, base: str | None = None):
        body = json.dumps(data).encode() if data is not None else None
        h = {"Content-Type": "application/json", **(headers or {})}
        r = urllib.request.Request((base or self.base) + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers.get("Content-Type", "").startswith("application/json") else raw), resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw), e.headers
            except ValueError:
                return e.code, raw, e.headers

    def get(self, path, **kw):
        return self.req("GET", path, **kw)[:2]

    def post(self, path, data=None, **kw):
        return self.req("POST", path, data if data is not None else {}, **kw)[:2]

    def put(self, path, data, **kw):
        return self.req("PUT", path, data, **kw)[:2]

    def job(self, started, timeout: float = 20):
        status, body = started
        assert status == 202, body
        jid = body["job"]["id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, j = self.get(f"/api/jobs/{jid}")
            if j["status"] != "running":
                return j
            time.sleep(0.05)
        raise AssertionError("job did not finish")

    def sse(self, jid: str, since: int = 0) -> list[dict]:
        events = []
        with urllib.request.urlopen(f"{self.base}/api/jobs/{jid}/events?since={since}", timeout=20) as resp:
            for line in resp:
                if line.startswith(b"data: "):
                    events.append(json.loads(line[6:]))
        return events


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path)
    yield s
    s.close()


@pytest.fixture
def make_server(tmp_path):
    made = []

    def make(**kw):
        s = Server(tmp_path, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


@pytest.fixture
def detected(srv):
    j = srv.job(srv.post("/api/detect"))
    assert j["status"] == "done"
    return srv
