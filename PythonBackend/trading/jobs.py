"""
jobs.py -- many simulations at once.

A sweep over 400 parameter combinations takes a while, and staring at it is
a waste of a terminal. Every heavy command becomes a job: it runs on a
worker, the prompt comes straight back, and the result is there when you
ask for it. Start five, go and look at prices, come back.

Each job keeps its own output lines, so `job 3` re-prints exactly what it
produced -- the terminal isn't a place where results scroll away forever.
"""
from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

_counter = itertools.count(1)


@dataclass
class Job:
    id: int
    label: str
    where: str = "local"                 # local | jugnu
    status: str = "queued"               # queued | running | done | failed | cancelled
    started: float = field(default_factory=time.time)
    finished: float | None = None
    lines: list[str] = field(default_factory=list)
    error: str = ""
    payload: dict = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        return (self.finished or time.time()) - self.started

    @property
    def done(self) -> bool:
        return self.status in ("done", "failed", "cancelled")


class Runner:
    """Jobs, their output, and who to tell when one finishes."""

    def __init__(self, workers: int = 4, on_change=None):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self.jobs: dict[int, Job] = {}
        self.lock = threading.Lock()
        self.on_change = on_change          # called with the job on every state change

    def submit(self, label: str, work, *, where: str = "local", payload: dict | None = None) -> Job:
        """`work(job)` may call job.write(...) as it goes."""
        job = Job(id=next(_counter), label=label, where=where, payload=payload or {})
        job.write = lambda *lines: self._write(job, *lines)     # type: ignore[attr-defined]
        with self.lock:
            self.jobs[job.id] = job
        self.pool.submit(self._run, job, work)
        return job

    def _run(self, job: Job, work) -> None:
        job.status = "running"
        self._changed(job)
        try:
            work(job)
            job.status = "done" if job.status == "running" else job.status
        except Exception as e:                       # noqa: BLE001 - jobs never crash the app
            job.status = "failed"
            job.error = f"{type(e).__name__}: {e}"
        finally:
            job.finished = time.time()
            self._changed(job)

    def _write(self, job: Job, *lines) -> None:
        for line in lines:
            job.lines.extend(str(line).split("\n"))
        self._changed(job)

    def _changed(self, job: Job) -> None:
        if self.on_change:
            try:
                self.on_change(job)
            except Exception:
                pass

    def get(self, job_id: int) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def all(self) -> list[Job]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.id)

    def running(self) -> list[Job]:
        return [j for j in self.all() if not j.done]

    def clear_finished(self) -> int:
        with self.lock:
            done = [i for i, j in self.jobs.items() if j.done]
            for i in done:
                del self.jobs[i]
        return len(done)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False)
