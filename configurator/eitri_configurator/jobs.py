"""Long-running operations (detect, flash, calibrate...) and their event streams.

A `Job` buffers every event it emits, so a browser that reloads, or a phone
that picks the wizard up mid-flash, can replay history and then follow live
via Server-Sent Events.  Only one *exclusive* job runs at a time: two flashes
or a flash during a calibration move must never overlap.
"""
from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

Event = dict[str, Any]


class Job:
    def __init__(self, kind: str, exclusive: bool):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.exclusive = exclusive
        self.status = "running"          # running | done | failed
        self.started = time.time()
        self.finished: float | None = None
        self.result: Any = None
        self.events: list[Event] = []
        self._seq = itertools.count(1)
        self._subs: set[asyncio.Queue[Event | None]] = set()
        self.task: asyncio.Task | None = None

    def emit(self, type_: str, **data: Any) -> Event:
        ev: Event = {"seq": next(self._seq), "t": round(time.time(), 3), "type": type_, **data}
        self.events.append(ev)
        for q in self._subs:
            q.put_nowait(ev)
        return ev

    def snapshot(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "started": self.started, "finished": self.finished,
            "result": self.result, "last_seq": self.events[-1]["seq"] if self.events else 0,
        }

    def _close(self) -> None:
        for q in self._subs:
            q.put_nowait(None)

    async def stream(self, since: int = 0, heartbeat: float | None = None):
        """Replay buffered events after `since`, then follow live until the job ends.
        With `heartbeat`, yields None whenever that many seconds pass quietly."""
        q: asyncio.Queue[Event | None] = asyncio.Queue()
        backlog = [e for e in self.events if e["seq"] > since]
        self._subs.add(q)
        try:
            for ev in backlog:
                yield ev
            last = backlog[-1]["seq"] if backlog else since
            if self.status != "running":
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), heartbeat)
                except asyncio.TimeoutError:
                    yield None
                    continue
                if ev is None:
                    return
                if ev["seq"] > last:       # drop dupes emitted between snapshot and subscribe
                    last = ev["seq"]
                    yield ev
        finally:
            self._subs.discard(q)


class JobBusy(Exception):
    def __init__(self, job: Job):
        super().__init__(f"{job.kind} is already running")
        self.job = job


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.last_by_kind: dict[str, Job] = {}

    def active(self) -> Job | None:
        for j in self.jobs.values():
            if j.exclusive and j.status == "running":
                return j
        return None

    def start(
        self,
        kind: str,
        runner: Callable[[Job], Awaitable[Any]],
        *,
        exclusive: bool = True,
    ) -> Job:
        busy = self.active()
        if exclusive and busy:
            raise JobBusy(busy)
        job = Job(kind, exclusive)
        self.jobs[job.id] = job
        self.last_by_kind[kind] = job
        job.task = asyncio.create_task(self._run(job, runner))
        # Keep memory bounded on a 976 MB box: retain the last few finished jobs.
        finished = [j for j in self.jobs.values() if j.status != "running"]
        for old in sorted(finished, key=lambda j: j.started)[:-12]:
            self.jobs.pop(old.id, None)
        return job

    async def _run(self, job: Job, runner: Callable[[Job], Awaitable[Any]]) -> None:
        try:
            job.result = await runner(job)
            ok = not (isinstance(job.result, dict) and job.result.get("ok") is False)
            job.status = "done" if ok else "failed"
        except asyncio.CancelledError:
            job.status = "failed"
            job.result = {"ok": False, "error": "cancelled"}
            raise
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            job.status = "failed"
            job.result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            job.emit("log", level="error", message=job.result["error"])
        finally:
            job.finished = time.time()
            job.emit("job.end", ok=job.status == "done", result=job.result)
            job._close()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def shutdown(self) -> None:
        for j in self.jobs.values():
            if j.task and not j.task.done():
                j.task.cancel()
        await asyncio.gather(*(j.task for j in self.jobs.values() if j.task), return_exceptions=True)
