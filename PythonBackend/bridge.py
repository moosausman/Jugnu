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
import sys
import threading
import time
import uuid

ROOT = os.environ.get("JUGNU_ROOT", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import llm_client
from tools.tools import SCHEMAS
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

PROACTIVE_ENABLED = os.environ.get("JUGNU_PROACTIVE", "0") == "1"
PROACTIVE_POLL_SECONDS = float(os.environ.get("JUGNU_PROACTIVE_POLL_SECONDS", "20"))
PROACTIVE_COOLDOWN_SECONDS = float(os.environ.get("JUGNU_PROACTIVE_COOLDOWN_SECONDS", "600"))

ACTIVITY_TRACKING_ENABLED = os.environ.get("JUGNU_TRACK_ACTIVITY", "0") == "1"

DAILY_REVIEW_ENABLED = os.environ.get("JUGNU_DAILY_REVIEW", "0") == "1"
DAILY_REVIEW_TIME = os.environ.get("JUGNU_DAILY_REVIEW_TIME", "21:00")
_DAILY_REVIEW_MARKER = APP_SUPPORT_DIR / "daily_review_last_run"


def log(message: str) -> None:
    print(f"[bridge] {message}", file=sys.stderr, flush=True)


def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def visible_history(conversation: list) -> list:
    return [
        {"role": m["role"], "content": m.get("content", "")}
        for m in conversation
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]


def handle_send(conversation: list, request_id: str, text: str) -> None:
    try:
        def on_plan(plan):
            emit({
                "type": "plan",
                "id": request_id,
                "goal": plan.goal,
                "tasks": [t.to_dict() for t in plan.tasks],
            })

        def on_task_start(task):
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
                "content": json.dumps(args), "task_id": task.id,
            })

        def on_tool_result(task, name, args, result):
            # stderr only -- not part of the documented stdout wire protocol,
            # so this can't break the Swift frontend's parsing. Exists so
            # failures (e.g. a shell_stage permission/AppleScript error) are
            # visible when running `python3 bridge.py` directly instead of
            # silently vanishing -- previously this callback wasn't wired up
            # at all, so tool results never surfaced anywhere.
            preview = str(result)[:300]
            log(f"tool_result {name}({json.dumps(args)}) -> {preview}")

        answer = session.run_turn(
            conversation,
            text,
            on_plan=on_plan,
            on_task_start=on_task_start,
            on_status=on_status,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
        )
        emit({"type": "assistant", "id": request_id, "content": answer})

    except Exception as e:
        log(f"error during turn: {e}")
        emit({"type": "error", "id": request_id, "message": str(e)})


def handle_daily_review(conv_ref: "_ConversationRef", request_id: str) -> None:
    try:
        report = daily_review.build_report()
        if report:
            session.append_proactive_message(conv_ref.value, report)
        emit({
            "type": "digest", "id": request_id,
            "content": report or "Not enough happened today yet to put together a recap.",
        })
    except Exception as e:
        log(f"error building daily review: {e}")
        emit({"type": "error", "id": request_id, "message": str(e)})


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


def main() -> None:
    conv_ref = _ConversationRef(load_conversation())
    emit(_ready_payload(conv_ref.value))

    turn_lock = threading.Lock()

    # Drop expired timeline rows and reclaim their pages. Queued onto
    # semantic_memory's background writer, so it never delays "ready" or
    # the first turn. Without this, memory grew without bound -- the prune
    # function existed but its own docstring admitted nothing ever called it.
    smem.maintain()

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
            log(f"could not parse request: {line!r}")
            continue

        kind = request.get("type")

        if kind == "send":
            request_id = request.get("id", "")
            emit({"type": "status", "id": request_id, "label": "thinking"})
            with turn_lock:
                handle_send(conv_ref.value, request_id, request.get("text", ""))

        elif kind == "clear":
            with turn_lock:
                conv_ref.value = clear_conversation()
            emit({"type": "cleared"})

        elif kind == "reset_facts":
            # Deliberately its own request type, not folded into "clear":
            # facts (name, preferences, standing instructions) and the
            # conversation are different things to forget, and someone
            # clearing one shouldn't have to also lose the other.
            with turn_lock:
                clear_facts()
                # Facts are baked into the conversation's system prompt
                # (see persona_system_prompt) -- without refreshing it
                # here, the live in-memory conversation would keep
                # talking as if the old facts still applied until the
                # next full restart, even though the file on disk is
                # already empty.
                conv_ref.value[0]["content"] = persona_system_prompt(facts_as_string())
            emit({"type": "facts_cleared"})

        elif kind == "reset_memory":
            # Same reasoning as reset_facts, for the other longitudinal
            # store: activity log, search history, habit patterns --
            # everything semantic_memory.py has been quietly building up.
            # Doesn't touch the conversation or facts at all.
            with turn_lock:
                smem.clear_all()
                # Learned procedures are derived from the same history, so
                # "forget what I've been doing" has to include how it
                # learned to do it -- otherwise the timeline is wiped while
                # skills quietly keep replaying routes built from it.
                skills.clear_all()
            emit({"type": "memory_cleared"})

        elif kind == "ping":
            emit(_ready_payload(conv_ref.value))

        elif kind == "daily_review":
            request_id = request.get("id", "")
            emit({"type": "status", "id": request_id, "label": "thinking"})
            with turn_lock:
                handle_daily_review(conv_ref, request_id)

        elif kind == "quit":
            # Answering this is what makes BackendBridge.stop()'s graceful
            # shutdown actually graceful -- without a handler here this
            # fell through to "unknown request type" and did nothing, so
            # every stop() unconditionally burned its full 1-second
            # timeout waiting for an exit that was never coming, before
            # falling back to SIGTERM every single time. All of this
            # module's background threads (ProactiveWatcher,
            # DailyReviewScheduler, ActivityLogger) are daemon=True, so
            # breaking out of this loop and letting main() return is
            # enough for the process to actually exit -- no explicit
            # sys.exit() needed.
            log("received quit -- shutting down")
            break

        else:
            log(f"unknown request type: {kind!r}")


if __name__ == "__main__":
    main()
