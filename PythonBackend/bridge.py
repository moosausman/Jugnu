"""
Jugnu backend bridge -- JSON-over-stdio protocol for the native Swift frontend.

Run with:  python3 bridge.py
(run from the project root, so `session`, `agents`, `planner`, `executor`,
`tools`, `memory`, and `llm_client` are importable -- or set the JUGNU_ROOT
env var to point at that root if this file lives elsewhere)

Every user message goes through the planner first: it decides which
specialist agent(s) should handle the goal and in what order. That same
PLANNER-role model then also drives each task's actual tool selection
during execution -- the AGENT-role model only renders already-decided
results into words (see planner.py / executor.py / agent_core.py /
llm_client.py for the full model-role split).

Protocol -- one JSON object per line, UTF-8:

  Swift -> Python
    {"type": "send",  "id": "<uuid>", "text": "..."}
    {"type": "clear"}
    {"type": "reset_facts"}
    {"type": "reset_memory"}
    {"type": "ping"}
    {"type": "daily_review", "id": "<uuid>"}
    {"type": "quit"}

  Python -> Swift
    {"type": "ready",     "planner_model": "...", "agent_model": "...", "tool_count": N, "history": [...], "proactive": bool, "activity_tracking": bool, "daily_review": bool}
    {"type": "plan",      "id": "<uuid>", "goal": "...", "tasks": [{"id":1,"agent":"...","description":"...","depends_on":[]}, ...]}
    {"type": "status",    "id": "<uuid>", "label": "thinking" | "<tool name>" | "<agent>: <task>", "task_id": N, "agent": "..."}
    {"type": "tool",      "id": "<uuid>", "name": "...", "content": "<json args>", "task_id": N}
    {"type": "assistant", "id": "<uuid>", "content": "..."}
    {"type": "nudge",     "id": "<uuid>", "content": "..."}
    {"type": "digest",    "id": "<uuid>", "content": "..."}
    {"type": "error",     "id": "<uuid>", "message": "..."}
    {"type": "cleared"}
    {"type": "facts_cleared"}
    {"type": "memory_cleared"}

  "reset_facts" wipes memory.py's saved facts (name, preferences, standing
  instructions) and refreshes the live conversation's system prompt to
  match. "reset_memory" wipes semantic_memory.py's longitudinal store
  (activity log, search history, habit patterns) -- neither one touches
  the conversation itself; that's still only "clear". "quit" is answered
  by exiting the process, not by an emitted event -- it's what makes
  BackendBridge.stop()'s graceful-shutdown wait on the Swift side actually
  graceful instead of always falling through to a hard SIGTERM.


  "plan" is only sent when the planner produced a real task graph -- if it
  asked a clarifying question instead, that question just arrives as a
  normal "assistant" message, with no plan/status/tool events before it.

  "nudge" is Jugnu's smaller form of initiative: an assistant message that
  wasn't a reply to any "send" -- nobody asked. It only ever appears if
  proactive mode is on (see below) and, even then, rarely: most polls
  produce nothing. It's already appended to the persisted conversation by
  the time it's emitted, same as a normal "assistant" message; the Swift
  side just needs to decide how to surface it (folded in like any other
  assistant turn, or called out distinctly -- e.g. a softer visual cue
  since the user didn't just ask a question). Since proactive.py grew the
  ability to act, not just speak (see JUGNU_PROACTIVE_ACTIONS below), a
  "nudge" can now be one of three things: a plain comment, a report of
  something Jugnu already did on its own (a whitelisted, read-only-or-
  reversible action -- see agents.py's autonomous_safe), or a proposal
  asking whether to do something more consequential -- nothing runs for
  that last kind until the user actually says yes in a normal reply.

  "digest" is Jugnu's end-of-day recap -- much longer and richer than a
  "nudge", and arrives two ways: unprompted, once a day, if daily-review
  mode is on (see below); or on demand, as a direct reply to a
  {"type": "daily_review"} request (also reachable through plain language,
  e.g. "give me a recap of today" -- see planner.py / executor.py's
  "daily_review" pseudo-agent). Either way it's already persisted to the
  conversation by the time it's emitted, same as "nudge".

PROACTIVE MODE (initiative) -- off by default:
  JUGNU_PROACTIVE=1                        turns it on
  JUGNU_PROACTIVE_POLL_SECONDS      (20)    how often to glance at what's
                                             frontmost (awareness.py)
  JUGNU_PROACTIVE_COOLDOWN_SECONDS (600)    minimum gap between nudges,
                                             even if the model would say
                                             yes every single poll
  JUGNU_PROACTIVE_ACTIONS=1                a SEPARATE opt-in, on top of
                                             JUGNU_PROACTIVE, for letting
                                             Jugnu actually run a tool-
                                             backed task on its own
                                             initiative, not just comment.
                                             Even then, only agents whose
                                             entire tool surface is
                                             read-only or an established
                                             "just do it" action ever run
                                             without confirmation -- see
                                             agents.py's autonomous_safe
                                             and proactive.py's module
                                             docstring for the full split.
                                             JUGNU_PROACTIVE=1 alone never
                                             grants this -- it must be set
                                             explicitly.
  See proactive.py for the actual "is this worth it, and what" judgment,
  and awareness.py for what "something changed" means. The watcher never
  runs while a real user turn is in flight (see turn_lock below), and a
  "clear" request resets its notion of "current conversation" the same
  instant it resets the persisted one.

ACTIVITY TRACKING (what daily_review.py's recap is built from) -- OFF by
default, like every other ambient-capture feature here:
  JUGNU_TRACK_ACTIVITY=1                   turns it on
  (This block used to claim "on by default, JUGNU_TRACK_ACTIVITY=0 turns
  it off" -- the exact opposite of what the code below does, which reads
  os.environ.get("JUGNU_TRACK_ACTIVITY", "0"). SettingsStore.swift had
  already noticed the contradiction and hardcoded its own `false` default
  rather than trust either. The code was right; this text was wrong.
  Leaving it off by default is also the correct call on its own merits:
  continuous logging of every app and window title you touch should never
  switch itself on without you choosing it.)
  See activity_log.py -- logs frontmost app + window title + duration all
  day, no screenshots, nothing sent to a model until you actually ask for
  a recap.

DAILY REVIEW (the other half of initiative -- a scheduled recap, not a
per-change nudge) -- off by default:
  JUGNU_DAILY_REVIEW=1                     turns on the daily trigger
  JUGNU_DAILY_REVIEW_TIME          (21:00) local time it fires, HH:MM
  See daily_review.py for what goes into it. The on-demand path
  ({"type": "daily_review"} or asking in plain language) works regardless
  of this setting -- this only controls the unprompted once-a-day version.

stdout carries ONLY protocol JSON -- never print() anything else to it.
Diagnostics go to stderr via log(), which Xcode's console (or a redirected
log file) will show without corrupting the protocol stream.
"""

import datetime
import json
import os
import queue
import sys
import threading
import time
import uuid

ROOT = os.environ.get("JUGNU_ROOT", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import llm_client
from tools.tools import SCHEMAS
from tools import macbridge
from memory import (
    load_conversation, clear_conversation, clear_facts,
    persona_system_prompt, facts_as_string, APP_SUPPORT_DIR,
)
import activity_log
import awareness as aware
import daily_review
import proactive
import semantic_memory as smem
import session
import skills
import approvals
import audit
import permissions
import run_context
import learning
import memory_store
import scheduler
import skill_library
import tasks_store
import workflows

_run_manager = None
from redact import redact

PROACTIVE_ENABLED = os.environ.get("JUGNU_PROACTIVE", "0") == "1"
PROACTIVE_POLL_SECONDS = float(os.environ.get("JUGNU_PROACTIVE_POLL_SECONDS", "20"))
PROACTIVE_COOLDOWN_SECONDS = float(os.environ.get("JUGNU_PROACTIVE_COOLDOWN_SECONDS", "600"))

ACTIVITY_TRACKING_ENABLED = os.environ.get("JUGNU_TRACK_ACTIVITY", "0") == "1"

DAILY_REVIEW_ENABLED = os.environ.get("JUGNU_DAILY_REVIEW", "0") == "1"
DAILY_REVIEW_TIME = os.environ.get("JUGNU_DAILY_REVIEW_TIME", "21:00")
_DAILY_REVIEW_MARKER = APP_SUPPORT_DIR / "daily_review_last_run"


def log(message: str) -> None:
    print(f"[bridge] {message}", file=sys.stderr, flush=True)


# The proactive watcher and daily-review scheduler emit from their own
# threads while the main loop may be emitting a turn's events. print() is
# not atomic across threads -- the payload and its newline are separate
# writes -- so two events could interleave into one unparseable line that
# the Swift side silently drops.
_emit_lock = threading.Lock()


def emit(payload: dict) -> None:
    line = json.dumps(payload)
    with _emit_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def visible_history(conversation: list) -> list:
    return [
        {"role": m["role"], "content": m.get("content", "")}
        for m in conversation
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]


def handle_send(conversation: list, request_id: str, text: str) -> None:
    ctx = run_context.RunContext(request_id=request_id, goal=text, emit=emit, origin="user")
    run_context.register(ctx)
    token = run_context.activate(ctx)
    emit({"type": "run_started", "id": request_id, "run_id": ctx.run_id, "goal": text[:300]})
    audit.log("turn_start", run_id=ctx.run_id, summary=text)
    try:
        def on_plan(plan):
            emit({
                "type": "plan",
                "id": request_id,
                "run_id": ctx.run_id,
                "goal": plan.goal,
                "objective": plan.objective,
                "assumptions": plan.assumptions,
                "request_type": plan.request_type,
                "complexity": plan.complexity,
                "session": macbridge.active_session(),
                "tasks": [t.to_dict() for t in plan.tasks],
            })

        def on_task_start(task):
            ctx.current_task = task.id
            emit({
                "type": "status", "id": request_id,
                "label": f"{task.agent}: {task.description}",
                "task_id": task.id, "agent": task.agent,
            })

        def on_status(task, label):
            emit({
                "type": "status", "id": request_id, "label": label,
                "task_id": task.id, "agent": task.agent,
            })

        def on_tool_call(task, name, args):
            emit({
                "type": "tool", "id": request_id, "name": name,
                "content": redact(json.dumps(args)), "task_id": task.id,
            })

        def on_tool_result(task, name, args, result):
            # stderr only -- not part of the stdout wire protocol. Redacted,
            # since stderr ends up in Xcode's console and log files.
            log(redact(f"tool_result {name}({json.dumps(args)}) -> {str(result)[:300]}"))

        answer = session.run_turn(
            conversation,
            text,
            on_plan=on_plan,
            on_task_start=on_task_start,
            on_status=on_status,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
        )
        # What the turn cost: model calls and tokens (see llm_client._record_usage).
        emit({"type": "assistant", "id": request_id, "run_id": ctx.run_id, "content": answer, "usage": ctx.usage})
        audit.log("turn_end", run_id=ctx.run_id, status="ok", summary=answer[:1000],
                  detail={"usage": ctx.usage, "seconds": round(time.time() - ctx.started_at, 1)})

    except run_context.RunCancelled:
        answer = "Stopped. Nothing further will run for that request."
        session.record_interrupted_turn(conversation, answer)
        emit({"type": "assistant", "id": request_id, "run_id": ctx.run_id, "content": answer, "cancelled": True,
              "usage": ctx.usage})
        audit.log("turn_end", run_id=ctx.run_id, status="cancelled", detail={"usage": ctx.usage})
    except Exception as e:
        message = redact(str(e))
        log(f"error during turn: {message}")
        emit({"type": "error", "id": request_id, "run_id": ctx.run_id, "message": message})
        audit.log("error", run_id=ctx.run_id, status="turn_failed", summary=message)
    finally:
        run_context.deactivate(token)
        run_context.unregister(ctx)
        approvals.cancel_for_run(ctx.run_id)


def handle_daily_review(conv_ref: "_ConversationRef", request_id: str) -> None:
    ctx = run_context.RunContext(request_id=request_id, goal="daily review", emit=emit, origin="user")
    run_context.register(ctx)
    token = run_context.activate(ctx)
    try:
        report = daily_review.build_report()
        if report:
            session.append_proactive_message(conv_ref.value, report)
        emit({
            "type": "digest", "id": request_id,
            "content": report or "Not enough happened today yet to put together a recap.",
        })
    except run_context.RunCancelled:
        emit({"type": "digest", "id": request_id, "content": "Stopped."})
    except Exception as e:
        log(f"error building daily review: {redact(str(e))}")
        emit({"type": "error", "id": request_id, "message": redact(str(e))})
    finally:
        run_context.deactivate(token)
        run_context.unregister(ctx)


def _ready_payload(conversation: list) -> dict:
    return {
        "type": "ready",
        "planner_model": llm_client.resolved_model_name("PLANNER"),
        "agent_model": llm_client.resolved_model_name("AGENT"),
        "tool_count": len(SCHEMAS),
        "history": visible_history(conversation),
        "proactive": PROACTIVE_ENABLED,
        "proactive_actions": proactive.PROACTIVE_ACTIONS_ENABLED,
        "activity_tracking": ACTIVITY_TRACKING_ENABLED,
        "daily_review": DAILY_REVIEW_ENABLED,
    }


class _ConversationRef:
    """Tiny mutable box around the conversation list. The watcher thread is
    created once at startup, but a "clear" request reassigns `conversation`
    to a brand-new list -- a closure capturing that local variable directly
    would keep nudging into the stale, pre-clear list forever. Everything
    below reads/writes `.value` instead of a bare name, so both threads
    always see whichever conversation is actually current."""

    def __init__(self, value: list):
        self.value = value


class ProactiveWatcher:
    """Background loop for Jugnu's one form of initiative: noticing, on its
    own timer, that the user switched to a different app, and -- rarely,
    only when proactive.consider() finds something concrete to say --
    speaking up before being asked. This class only owns the polling,
    change-detection, cooldown, and never-talk-over-a-real-turn bookkeeping;
    see awareness.py for "what changed" and proactive.py for "is it worth
    it". Off by default -- see JUGNU_PROACTIVE in the module docstring.
    """

    def __init__(self, conv_ref: _ConversationRef, turn_lock: threading.Lock, emit_fn):
        self._conv_ref = conv_ref
        self._turn_lock = turn_lock
        self._emit = emit_fn
        self._prev_snapshot = None
        self._last_nudge_at = 0.0

    def start(self) -> None:
        threading.Thread(target=self._loop, name="proactive-watcher", daemon=True).start()

    def _loop(self) -> None:
        while True:
            time.sleep(PROACTIVE_POLL_SECONDS)
            try:
                self._tick()
            except Exception as e:
                log(f"proactive watcher error: {e}")

    def _tick(self) -> None:
        snapshot = aware.capture()
        prev, self._prev_snapshot = self._prev_snapshot, snapshot

        if not aware.significant_change(prev, snapshot):
            return
        if time.time() - self._last_nudge_at < PROACTIVE_COOLDOWN_SECONDS:
            return

        # Never run alongside a real, user-initiated turn -- if one's in
        # flight, skip this tick entirely; the next poll will try again.
        if not self._turn_lock.acquire(blocking=False):
            return
        try:
            self._maybe_nudge(prev, snapshot)
        finally:
            self._turn_lock.release()

    def _maybe_nudge(self, prev, snapshot) -> None:
        conversation = self._conv_ref.value
        message = proactive.consider(conversation, snapshot, prev)
        if not message:
            return

        self._last_nudge_at = time.time()
        session.append_proactive_message(conversation, message)
        self._emit({"type": "nudge", "id": str(uuid.uuid4()), "content": message})


def _load_last_review_date():
    try:
        return datetime.date.fromisoformat(_DAILY_REVIEW_MARKER.read_text().strip())
    except Exception:
        return None


def _save_last_review_date(d) -> None:
    try:
        _DAILY_REVIEW_MARKER.write_text(d.isoformat())
    except Exception:
        pass  # a missed persist just risks one duplicate recap on restart -- not worth blocking over


class DailyReviewScheduler:
    """Fires once a day at JUGNU_DAILY_REVIEW_TIME (local time, default
    21:00) and emits Jugnu's end-of-day recap unprompted, as a "digest"
    message -- much bigger than proactive.py's in-the-moment nudges, and
    on a fixed daily clock rather than triggered by a change. Off by
    default -- see JUGNU_DAILY_REVIEW in the module docstring. Last-run
    date is persisted to disk so a restart late in the day doesn't fire a
    duplicate recap.
    """

    def __init__(self, conv_ref: _ConversationRef, turn_lock: threading.Lock, emit_fn, at: str):
        self._conv_ref = conv_ref
        self._turn_lock = turn_lock
        self._emit = emit_fn
        self._hour, self._minute = self._parse_time(at)
        self._last_run_date = _load_last_review_date()

    @staticmethod
    def _parse_time(at: str) -> tuple:
        try:
            h, m = at.split(":")
            return int(h), int(m)
        except Exception:
            log(f"invalid JUGNU_DAILY_REVIEW_TIME {at!r}, defaulting to 21:00")
            return 21, 0

    def start(self) -> None:
        threading.Thread(target=self._loop, name="daily-review-scheduler", daemon=True).start()

    def _loop(self) -> None:
        while True:
            time.sleep(60)  # minute-granularity is plenty for a once-a-day trigger
            try:
                self._tick()
            except Exception as e:
                log(f"daily review scheduler error: {e}")

    def _tick(self) -> None:
        now = datetime.datetime.now()
        if self._last_run_date == now.date():
            return
        if (now.hour, now.minute) < (self._hour, self._minute):
            return

        if not self._turn_lock.acquire(blocking=False):
            return  # a real turn is running -- try again next minute rather than skip the day
        try:
            self._run(now.date())
        finally:
            self._turn_lock.release()

    def _run(self, today) -> None:
        report = daily_review.build_report()
        # Mark today as done regardless of whether the report came back
        # empty -- an empty/failed attempt shouldn't retry every minute
        # for the rest of the day.
        self._last_run_date = today
        _save_last_review_date(today)
        if not report:
            return

        conversation = self._conv_ref.value
        session.append_proactive_message(conversation, report)
        self._emit({"type": "digest", "id": str(uuid.uuid4()), "content": report})


def _find_run(request: dict):
    """A live run by run_id, or by the UI request id that started it."""
    run_id = request.get("run_id")
    if run_id:
        return run_context.find(run_id)
    req_id = request.get("id")
    for ctx in run_context.live_runs():
        if req_id and ctx.request_id == req_id:
            return ctx
    runs = run_context.live_runs()
    return runs[0] if len(runs) == 1 and not run_id and not req_id else None


class TurnWorker:
    """Runs user turns (and on-demand reviews) one at a time on its own
    thread, so the stdin loop stays free to receive approvals, cancel,
    pause and settings changes while work is in progress. Before this,
    the loop ran each turn inline -- nothing could reach the backend
    until the turn ended, which made approvals impossible."""

    def __init__(self, conv_ref: "_ConversationRef", turn_lock: threading.Lock):
        self._conv_ref = conv_ref
        self._turn_lock = turn_lock
        self._queue: "queue.Queue" = queue.Queue()
        self.busy = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="turn-worker", daemon=True).start()

    def submit(self, job) -> None:
        self._queue.put(job)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            self.busy.set()
            try:
                with self._turn_lock:
                    job()
            except Exception as e:
                log(f"turn worker error: {redact(str(e))}")
            finally:
                self.busy.clear()


def _handle_management(kind: str, request: dict, conv_ref, worker) -> bool:
    """Requests that don't need the turn lock. Returns True if handled."""
    if kind == "approval_response":
        ok = approvals.respond(str(request.get("approval_id", "")), str(request.get("decision", "deny")),
                               str(request.get("note", "") or ""))
        if not ok:
            emit({"type": "approval_resolved", "approval_id": request.get("approval_id"),
                  "decision": "expired"})
        return True
    if kind in ("cancel", "pause", "resume"):
        ctx = _find_run(request)
        if ctx is None:
            emit({"type": "notice", "message": f"No running task to {kind}."})
            return True
        if kind == "cancel":
            ctx.cancel()
            approvals.cancel_for_run(ctx.run_id)
        elif kind == "pause":
            ctx.pause()
        else:
            ctx.resume()
        emit({"type": "run_state", "run_id": ctx.run_id, "id": ctx.request_id,
              "state": {"cancel": "cancelling", "pause": "paused", "resume": "running"}[kind]})
        audit.log(f"run_{kind}", run_id=ctx.run_id)
        return True
    if kind == "get_permissions":
        emit({"type": "permissions", "permissions": permissions.describe()})
        return True
    if kind == "set_permissions":
        try:
            permissions.update(request.get("patch") or {})
            audit.log("permissions_change", summary="permissions updated", detail=request.get("patch"))
            emit({"type": "permissions", "permissions": permissions.describe(), "saved": True})
        except Exception as e:
            emit({"type": "notice", "message": f"Couldn't save permissions: {redact(str(e))}"})
        return True
    if kind == "get_approvals":
        emit({"type": "approvals", "approvals": approvals.pending()})
        return True
    if kind == "get_audit":
        try:
            rows = audit.recent(limit=int(request.get("limit") or 200), run_id=request.get("run_id"))
        except Exception as e:
            rows = []
            log(f"audit read failed: {e}")
        emit({"type": "audit", "events": rows})
        return True
    if kind == "ping":
        emit(_ready_payload(conv_ref.value))
        return True
    if kind.startswith("memory_") or kind == "tool_stats":
        _handle_memory_request(kind, request)
        return True
    if kind.startswith("run_") or kind == "runs_list":
        _handle_run_request(kind, request)
        return True
    if kind.startswith("schedule"):
        _handle_schedule_request(kind, request)
        return True
    if kind.startswith("skill") or kind == "proposals_list":
        _handle_skill_request(kind, request)
        return True
    if kind == "os_permissions":
        threading.Thread(target=_emit_os_permissions, name="os-permissions", daemon=True).start()
        return True
    return False


def _emit_os_permissions() -> None:
    """macOS grants for each live session's MacBridge (your account, and the
    Jugnu account when it's logged in). Never triggers a prompt."""
    from tools.extended_tools import permissions_check
    result = {}
    for name in ("primary", "agent"):
        if not macbridge.session_available(name):
            result[name] = {"ok": False, "error": "not running"}
            continue
        token = macbridge._active.set(name)
        try:
            result[name] = permissions_check()
        except Exception as e:
            result[name] = {"ok": False, "error": redact(str(e))}
        finally:
            macbridge._active.reset(token)
    emit({"type": "os_permissions", "sessions": result})


def _emit_skills() -> None:
    emit({"type": "skills", "skills": skill_library.list_all(), "routes": skills.list_skills(limit=100)})


def _handle_skill_request(kind: str, request: dict) -> None:
    skill_id = str(request.get("skill_id", ""))
    try:
        if kind == "skills_list":
            pass
        elif kind == "skill_accept":
            skill_library.update(skill_id, status="active")
        elif kind == "skill_disable":
            skill_library.update(skill_id, status="disabled")
        elif kind == "skill_update":
            skill_library.update(skill_id, **(request.get("fields") or {}))
        elif kind == "skill_revert":
            skill_library.revert(skill_id, int(request.get("version") or 0))
        elif kind == "skill_delete":
            skill_library.delete(skill_id)
        elif kind == "proposals_list":
            emit({"type": "proposals", "proposals": audit.recent(limit=100, kinds=["code_change_proposal"])})
            return
    except (KeyError, ValueError) as e:
        emit({"type": "notice", "message": f"Skill change failed: {redact(str(e))}"})
    _emit_skills()


def _handle_run_request(kind: str, request: dict) -> None:
    run_id = str(request.get("run_id", ""))
    if kind == "runs_list":
        emit({"type": "runs", "runs": tasks_store.list_runs(limit=int(request.get("limit") or 60))})
    elif kind == "run_get":
        emit({"type": "run_detail", "run": tasks_store.get(run_id)})
    elif kind == "run_resume":
        ok = _run_manager is not None and _run_manager.resume(run_id)
        if not ok:
            emit({"type": "notice", "message": "That task can't be resumed (no saved plan)."})
    elif kind == "run_delete":
        tasks_store.delete(run_id)
        emit({"type": "runs", "runs": tasks_store.list_runs(limit=60)})


def _handle_schedule_request(kind: str, request: dict) -> None:
    try:
        if kind == "schedule_cancel":
            scheduler.cancel(str(request.get("schedule_id", "")))
        elif kind == "schedule_toggle":
            scheduler.set_enabled(str(request.get("schedule_id", "")), bool(request.get("enabled")))
        elif kind == "schedule_add":
            scheduler.add(str(request.get("instruction", "")), at=request.get("at") or None,
                          in_minutes=request.get("in_minutes"), every_minutes=request.get("every_minutes"),
                          kind=request.get("kind") or "task")
    except ValueError as e:
        emit({"type": "notice", "message": f"Schedule change failed: {e}"})
    emit({"type": "schedules", "schedules": scheduler.list_schedules()})


def _emit_memories(request: dict) -> None:
    kind = request.get("kind") or None
    emit({
        "type": "memories",
        "kind": kind,
        "query": request.get("query", ""),
        "items": memory_store.list_memories(kind=kind, query=request.get("query") or "",
                                            include_inactive=bool(request.get("include_inactive")),
                                            limit=int(request.get("limit") or 300)),
        "counts": memory_store.counts(),
    })


def _handle_memory_request(kind: str, request: dict) -> None:
    """View/edit/delete what Jugnu remembers. Every change is audited by
    memory_store itself."""
    try:
        if kind == "memory_list":
            _emit_memories(request)
        elif kind == "memory_get":
            emit({"type": "memory_detail", "memory": memory_store.get(str(request.get("memory_id", "")))})
        elif kind == "memory_add":
            memory_store.remember(str(request.get("content", "")), kind=request.get("kind") or "user",
                                  key=request.get("key") or None, confidence=float(request.get("confidence") or 1.0),
                                  source="user_stated", scope=request.get("scope") or "global",
                                  importance=float(request.get("importance") or 0.7))
            _emit_memories({"kind": request.get("list_kind")})
        elif kind == "memory_update":
            fields = request.get("fields") or {}
            memory_store.update(str(request.get("memory_id", "")),
                                content=fields.get("content"), confidence=fields.get("confidence"),
                                scope=fields.get("scope"), importance=fields.get("importance"),
                                key=fields.get("key"), status=fields.get("status"))
            _emit_memories({"kind": request.get("list_kind")})
        elif kind == "memory_delete":
            memory_store.delete(str(request.get("memory_id", "")))
            _emit_memories({"kind": request.get("list_kind")})
        elif kind == "memory_clear":
            memory_store.clear(request.get("kind") or None)
            _emit_memories({})
        elif kind == "tool_stats":
            emit({"type": "tool_stats", "tools": memory_store.tool_stats()})
    except (KeyError, ValueError) as e:
        emit({"type": "notice", "message": f"Memory change failed: {redact(str(e))}"})


def main() -> None:
    conv_ref = _ConversationRef(load_conversation())
    approvals.set_emitter(emit)
    emit(_ready_payload(conv_ref.value))

    turn_lock = threading.Lock()
    worker = TurnWorker(conv_ref, turn_lock)
    worker.start()

    # Drop expired timeline rows and reclaim their pages. Queued onto
    # semantic_memory's background writer, so it never delays "ready" or
    # the first turn.
    smem.maintain()
    threading.Thread(target=audit.prune, name="audit-prune", daemon=True).start()
    threading.Thread(target=memory_store.maintain, name="memory-maintain", daemon=True).start()
    learning.install_hooks()
    learning.set_notifier(emit)

    global _run_manager
    _run_manager = workflows.RunManager(emit, lambda: conv_ref.value)
    session.run_manager = _run_manager
    scheduler.SchedulerLoop(on_task=_run_manager.run_scheduled, on_notify=_run_manager.deliver_timer).start()
    threading.Thread(target=_run_manager.on_startup, name="resume-interrupted", daemon=True).start()
    threading.Thread(target=tasks_store.prune, name="runs-prune", daemon=True).start()

    if ACTIVITY_TRACKING_ENABLED:
        activity_log.ActivityLogger().start()
        log("activity tracking on")

    if PROACTIVE_ENABLED:
        ProactiveWatcher(conv_ref, turn_lock, emit).start()
        log(
            f"proactive mode on (poll={PROACTIVE_POLL_SECONDS:.0f}s, "
            f"cooldown={PROACTIVE_COOLDOWN_SECONDS:.0f}s)"
        )

    if DAILY_REVIEW_ENABLED:
        DailyReviewScheduler(conv_ref, turn_lock, emit, at=DAILY_REVIEW_TIME).start()
        log(f"daily review scheduled for {DAILY_REVIEW_TIME} local time")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log(f"could not parse request: {redact(line)[:200]!r}")
            continue

        kind = request.get("type")

        try:
            if _handle_management(kind, request, conv_ref, worker):
                continue
        except Exception as e:
            log(f"error handling {kind}: {redact(str(e))}")
            emit({"type": "notice", "message": redact(str(e))})
            continue

        if kind == "send":
            request_id = request.get("id", "")
            text = request.get("text", "")
            emit({"type": "status", "id": request_id, "label": "thinking"})
            worker.submit(lambda rid=request_id, t=text: handle_send(conv_ref.value, rid, t))

        elif kind == "daily_review":
            request_id = request.get("id", "")
            emit({"type": "status", "id": request_id, "label": "thinking"})
            worker.submit(lambda rid=request_id: handle_daily_review(conv_ref, rid))

        elif kind in ("clear", "reset_facts", "reset_memory"):
            # These rewrite state a running turn is using, so they wait for
            # it rather than racing it -- but on a thread, so the stdin loop
            # (and with it approvals/cancel) never blocks behind them.
            def _reset(kind=kind):
                with turn_lock:
                    if kind == "clear":
                        conv_ref.value = clear_conversation()
                        emit({"type": "cleared"})
                    elif kind == "reset_facts":
                        clear_facts()
                        conv_ref.value[0]["content"] = persona_system_prompt(facts_as_string())
                        emit({"type": "facts_cleared"})
                    else:
                        smem.clear_all()
                        skills.clear_all()
                        # Learned experience too -- everything except the
                        # user's own stated facts (that's "reset facts").
                        for k in ("episodic", "semantic", "error", "strategy"):
                            memory_store.clear(k)
                        emit({"type": "memory_cleared"})
            threading.Thread(target=_reset, name=f"{kind}", daemon=True).start()

        elif kind == "quit":
            # Every background thread is daemon=True, so returning from
            # main() is enough for the process to exit.
            log("received quit -- shutting down")
            for ctx in run_context.live_runs():
                ctx.cancel()
            break

        else:
            log(f"unknown request type: {kind!r}")


if __name__ == "__main__":
    main()
