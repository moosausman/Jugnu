"""
workflows.py -- runs that aren't a single foreground turn.

RunManager owns:
  * background workflows -- long work the user doesn't want to wait on
    ("research X in the background"), each on its own thread, so the
    conversation stays free; several can run at once
  * scheduled work -- the scheduler hands due jobs here
  * resumption -- at startup, runs the backend stopped in the middle of are
    marked interrupted; background ones resume automatically (they were
    already meant to run unattended), foreground ones are offered to the
    user to resume

Every background run is a full run: its own RunContext (cancellable,
pausable, with approvals routed to the UI), checkpoints to tasks_store after
every task, the same planner/executor/verifier as a foreground turn, and a
message in the conversation when it finishes.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional

import audit
import run_context
import tasks_store
from redact import redact

MAX_BACKGROUND = 3


def log(message: str) -> None:
    print(f"[workflows] {message}", file=sys.stderr, flush=True)


class RunManager:
    def __init__(self, emit: Callable[[dict], None], get_conversation: Callable[[], list]):
        self.emit = emit
        self.get_conversation = get_conversation
        self._slots = threading.Semaphore(MAX_BACKGROUND)
        self._threads: dict[str, threading.Thread] = {}

    # -- starting ---------------------------------------------------------------
    def start_background(self, goal: str, *, plan=None, origin: str = "background", recent_context: str = "",
                         session: Optional[str] = None, schedule_id: Optional[str] = None,
                         shared: Optional[dict] = None, run_id: Optional[str] = None) -> str:
        ctx = run_context.RunContext(goal=goal, origin=origin, user_initiated=True, emit=self.emit)
        if run_id:
            ctx.run_id = run_id
        else:
            tasks_store.create(ctx.run_id, goal, origin=origin, background=True, context=recent_context,
                               schedule_id=schedule_id, status="queued")
        t = threading.Thread(target=self._run, args=(ctx, goal, plan, recent_context, session, shared or {}),
                             name=f"bg-{ctx.run_id}", daemon=True)
        self._threads[ctx.run_id] = t
        t.start()
        return ctx.run_id

    def resume(self, run_id: str) -> bool:
        run = tasks_store.get(run_id)
        if not run or not run.get("plan"):
            return False
        plan, shared = tasks_store.prepare_resume(run)
        self.start_background(run["goal"], plan=plan, origin=run.get("origin") or "background",
                              recent_context=run.get("context") or "", session=run.get("session"),
                              shared=shared, run_id=run_id)
        return True

    # -- the run itself -------------------------------------------------------------
    def _run(self, ctx, goal, plan, recent_context, session, shared) -> None:
        # Imported here: session imports this module's instance at startup.
        import session as sess
        from planner import make_plan
        from tools import macbridge

        self._slots.acquire()
        run_context.register(ctx)
        token = run_context.activate(ctx)
        started = time.time()
        self.emit({"type": "run_started", "run_id": ctx.run_id, "goal": goal[:300], "origin": ctx.origin,
                   "background": True})
        audit.log("turn_start", run_id=ctx.run_id, summary=f"[{ctx.origin}] {goal}")
        try:
            if plan is None:
                context = recent_context or sess.build_context(list(self.get_conversation()), goal)
                recent_context = context
                plan = make_plan(goal, recent_context=context)
                audit.log("plan", run_id=ctx.run_id, summary=plan.objective, detail=plan.to_dict())
            macbridge.reset_session()
            target = session or plan.session
            if target:
                if not macbridge.session_available(target):
                    raise RuntimeError(sess._session_unavailable_message(target))
                macbridge.set_active_session(target)
            if plan.needs_clarification:
                tasks_store.wait_for_user(ctx.run_id, plan.clarify)
                self._report(ctx, goal, f"Before I can do \"{goal[:80]}\", I need to know: {plan.clarify}",
                             state="waiting_user")
                return
            if plan.request_type in ("scheduled",):
                plan.request_type = "agentic"   # it's running now; don't reschedule it
            self.emit({"type": "plan", "run_id": ctx.run_id, "goal": plan.goal, "objective": plan.objective,
                       "assumptions": plan.assumptions, "session": macbridge.active_session(),
                       "tasks": [t.to_dict() for t in plan.tasks], "background": True})
            answer = sess._execute(ctx, plan, recent_context, shared, None, None, None, None, run_id=ctx.run_id)
            if plan.pending_question:
                self._report(ctx, goal, f"Paused \"{plan.objective[:80]}\" -- I need to know: {answer}",
                             state="waiting_user")
            else:
                minutes = (time.time() - started) / 60
                header = f"Finished in the background ({minutes:.0f} min): {plan.objective}" if minutes >= 1 \
                    else f"Finished in the background: {plan.objective}"
                self._report(ctx, goal, f"{header}\n\n{answer}", state="done")
        except run_context.RunCancelled:
            tasks_store.finish(ctx.run_id, "cancelled")
            self._report(ctx, goal, f"Stopped the background task: {goal[:120]}", state="cancelled")
        except Exception as e:
            message = redact(str(e))
            log(f"background run {ctx.run_id} failed: {message}")
            tasks_store.finish(ctx.run_id, "failed", error=message)
            self._report(ctx, goal, f"A background task failed: {goal[:120]}\n{message}", state="failed")
        finally:
            run_context.deactivate(token)
            run_context.unregister(ctx)
            self._threads.pop(ctx.run_id, None)
            self._slots.release()

    def _report(self, ctx, goal: str, message: str, state: str) -> None:
        import session as sess
        sess.append_proactive_message(self.get_conversation(), message, origin=ctx.origin)
        self.emit({"type": "background_done", "run_id": ctx.run_id, "goal": goal[:300], "content": message,
                   "state": state, "origin": ctx.origin})
        self.emit({"type": "run_finished", "run_id": ctx.run_id, "state": state})
        audit.log("turn_end", run_id=ctx.run_id, status=state, summary=message[:1000])

    # -- startup --------------------------------------------------------------------
    def on_startup(self) -> None:
        """Resume background work interrupted by the last shutdown; offer the
        rest to the user."""
        for run in tasks_store.mark_interrupted():
            if not run:
                continue
            if run.get("background") and run.get("plan"):
                log(f"resuming interrupted background run {run['run_id']}")
                self.resume(run["run_id"])
            else:
                self.emit({"type": "run_interrupted", "run_id": run["run_id"], "goal": run["goal"][:300]})

    # -- scheduler callbacks ------------------------------------------------------------
    def run_scheduled(self, item: dict) -> str:
        return self.start_background(item["instruction"], origin="scheduled", session=item.get("session"),
                                     schedule_id=item["id"])

    def deliver_timer(self, item: dict) -> None:
        import session as sess
        from tools.macbridge import call
        text = item.get("title") or item["instruction"]
        try:
            call("/notifications/send", {"title": "Jugnu", "body": text, "delaySeconds": 0.1}, session="primary")
        except Exception as e:
            log(f"timer notification failed: {e}")
        message = f"⏰ {item['instruction']}"
        sess.append_proactive_message(self.get_conversation(), message, origin="timer")
        self.emit({"type": "nudge", "id": item["id"], "content": message, "origin": "timer"})
