"""
run_context.py -- per-run state shared by every layer of one piece of work.

A "run" is one user request (or one scheduled/background workflow) from
planning to final answer. The RunContext travels with it through the
executor, every task's tool loop, and every tool call, carrying:

  * identity (run_id, current task) for the audit log and the UI
  * cancellation and pause -- checked between tool calls, while waiting
    for an approval, and between tasks
  * whether the user started this (proactive/unprompted work is held to a
    stricter policy -- see policy.decide)
  * taint -- set once the run has read untrusted content that looked like
    injected instructions, which tightens the policy for the rest of it
  * approvals granted "for this task", so the same kind of action isn't
    re-asked twenty times inside one job the user already approved
  * an emit() hook for progress events

It's found through a ContextVar, so deep code (the tool runtime) doesn't
need it threaded through every signature. Worker threads started by the
executor copy the context explicitly (contextvars don't cross threads on
their own).
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional


class RunCancelled(Exception):
    """Raised at the next checkpoint after a run is cancelled."""


@dataclass
class RunContext:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    request_id: str = ""               # the UI request this run answers, if any
    goal: str = ""
    user_initiated: bool = True
    origin: str = "user"               # user | scheduled | proactive | background
    emit: Optional[Callable[[dict], None]] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    resume_event: threading.Event = field(default_factory=lambda: _set_event())
    tainted: bool = False
    task_approvals: set = field(default_factory=set)     # (tool, capability) keys
    started_at: float = field(default_factory=time.time)
    notes: dict = field(default_factory=dict)             # working memory for this run
    # Model usage by this run -- what it cost. Filled in by llm_client.
    # "calls"/tokens are paid API calls; "local_calls" ran free on Ollama.
    usage: dict = field(default_factory=lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                                 "local_calls": 0})
    _usage_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0, local: bool = False) -> None:
        with self._usage_lock:
            if local:
                self.usage["local_calls"] += 1
                return
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += int(prompt_tokens or 0)
            self.usage["completion_tokens"] += int(completion_tokens or 0)

    # The task a piece of code is working on. Per thread (a ContextVar), not
    # per run: with parallel tasks, several run at once inside one run.
    @property
    def current_task(self):
        return _task_id.get()

    @current_task.setter
    def current_task(self, value) -> None:
        _task_id.set(value)

    # -- cancellation / pause -------------------------------------------------
    def cancel(self) -> None:
        self.cancel_event.set()
        self.resume_event.set()  # wake a paused run so it can notice

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def pause(self) -> None:
        self.resume_event.clear()

    def resume(self) -> None:
        self.resume_event.set()

    @property
    def paused(self) -> bool:
        return not self.resume_event.is_set()

    def checkpoint(self) -> None:
        """Blocks while paused; raises RunCancelled if cancelled."""
        if self.cancelled:
            raise RunCancelled()
        while not self.resume_event.wait(0.5):
            if self.cancelled:
                raise RunCancelled()
        if self.cancelled:
            raise RunCancelled()

    # -- events -----------------------------------------------------------------
    def send(self, payload: dict) -> None:
        if self.emit is None:
            return
        payload = dict(payload)
        payload.setdefault("run_id", self.run_id)
        if self.request_id:
            payload.setdefault("id", self.request_id)
        try:
            self.emit(payload)
        except Exception:
            pass

    def progress(self, message: str, **extra) -> None:
        self.send({"type": "progress", "message": message, **extra})


class TaskStopped(Exception):
    """Raised inside one task (not the whole run) when that task hit its
    time limit or was superseded -- the executor treats it as a failure of
    that task only."""


_task_id: contextvars.ContextVar = contextvars.ContextVar("jugnu_task_id", default=None)
_task_stop: contextvars.ContextVar = contextvars.ContextVar("jugnu_task_stop", default=None)


def set_task_stop(event: "threading.Event | None") -> None:
    _task_stop.set(event)


def check_task_stop() -> None:
    event = _task_stop.get()
    if event is not None and event.is_set():
        raise TaskStopped()


def _set_event() -> threading.Event:
    e = threading.Event()
    e.set()
    return e


_current: contextvars.ContextVar[Optional[RunContext]] = contextvars.ContextVar("jugnu_run", default=None)


def current() -> RunContext:
    """The active run. Code running outside any run (a background thread
    nobody set a context on) gets an UNATTENDED context: nothing the user
    started, so policy asks before anything beyond reading. Failing toward
    "asked when it didn't need to" is the safe direction."""
    ctx = _current.get()
    if ctx is None:
        ctx = RunContext(user_initiated=False, origin="background")
        _current.set(ctx)
    return ctx


def get() -> Optional[RunContext]:
    return _current.get()


def activate(ctx: RunContext):
    """Makes `ctx` current; returns the token for deactivate()."""
    return _current.set(ctx)


def deactivate(token) -> None:
    try:
        _current.reset(token)
    except ValueError:
        pass


# Registry of live runs, so the bridge can cancel/pause them by id.
_live: dict[str, RunContext] = {}
_live_lock = threading.Lock()


def register(ctx: RunContext) -> None:
    with _live_lock:
        _live[ctx.run_id] = ctx


def unregister(ctx: RunContext) -> None:
    with _live_lock:
        _live.pop(ctx.run_id, None)


def find(run_id: str) -> Optional[RunContext]:
    with _live_lock:
        return _live.get(run_id)


def live_runs() -> list[RunContext]:
    with _live_lock:
        return list(_live.values())
