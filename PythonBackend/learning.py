"""
learning.py -- how Jugnu gets better with use, without touching its code.

Two layers:

1. Continuous, per tool call (install_hooks):
   * every call updates tool reliability stats (memory_store.tool_stats)
   * a genuine failure is remembered as an error memory, keyed by a
     normalised signature of tool + error type + message
   * when the same tool later succeeds in the same run, the difference
     between the failing and the succeeding arguments is recorded as the
     fix -- the next time that failure shows up, tool_runtime puts the
     known fix right next to the error the model sees

2. Per task, after meaningful work (reflect(), see reflection section):
   a structured reflection -- what was the goal, did it work, what failed,
   what should be remembered, what should change next time -- turned into
   validated memories and skill proposals. Added in the reflection phase.

What learning may change: memories, tool preferences, planning strategies
and reusable skills -- all inspectable and editable by the user. What it
may never change: Jugnu's source code or its safety rules. A reflection
that concludes the code itself should change can only file a proposal
(audit kind "code_change_proposal"), which nothing ever applies
automatically.
"""

from __future__ import annotations

import json
import threading

import memory_store
import tool_runtime
from tool_runtime import ErrorType

# Failures that say something about the tool or the approach, and so are
# worth remembering. Denials and policy blocks are the user's choices, not
# mistakes; unknown tools and bad arguments are model slips that don't
# generalise beyond the moment.
_LEARNABLE = {
    ErrorType.NOT_FOUND, ErrorType.CONFLICT, ErrorType.OS_PERMISSION, ErrorType.TIMEOUT,
    ErrorType.UNAVAILABLE, ErrorType.RATE_LIMITED, ErrorType.TOOL_FAILURE, ErrorType.INVALID_ARGUMENTS,
}

_pending_failures: dict[tuple[str, str], dict] = {}   # (run_id, tool) -> last failure
_lock = threading.Lock()


def _arg_diff(before: dict, after: dict) -> str:
    changed = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            new = after.get(key, "<removed>")
            text = json.dumps(new, default=str) if not isinstance(new, str) else new
            changed.append(f"{key}={text[:80]}")
    return ", ".join(changed[:6]) or "same arguments on retry"


def _on_outcome(outcome: "tool_runtime.ToolOutcome", ctx) -> None:
    context = "agent_session" if _active_session() == "agent" else ""
    memory_store.record_tool_use(outcome.tool, outcome.ok, outcome.duration, outcome.error_type, context)

    key = (ctx.run_id, outcome.tool)
    if not outcome.ok:
        if outcome.error_type in _LEARNABLE and outcome.decision != "denied":
            memory_store.record_error(outcome.tool, outcome.error_type, outcome.error or "", outcome.args,
                                      run_id=ctx.run_id)
            with _lock:
                _pending_failures[key] = {"error_type": outcome.error_type, "error": outcome.error or "",
                                          "args": dict(outcome.args)}
                if len(_pending_failures) > 500:
                    _pending_failures.clear()
        return

    with _lock:
        failure = _pending_failures.pop(key, None)
    if failure:
        fix = f"retry with {_arg_diff(failure['args'], outcome.args)}"
        memory_store.record_fix(outcome.tool, failure["error_type"], failure["error"], fix, run_id=ctx.run_id)


def _active_session() -> str:
    try:
        from tools.macbridge import active_session
        return active_session()
    except Exception:
        return "primary"


_installed = False


def install_hooks() -> None:
    """Idempotent. Called by bridge.py / main.py at startup."""
    global _installed
    if _installed:
        return
    tool_runtime.add_hook(_on_outcome)
    _installed = True


# ---------------------------------------------------------------------------
# 2. Reflection -- after meaningful work
# ---------------------------------------------------------------------------

import queue as _queue  # noqa: E402
import re as _re  # noqa: E402
import sys as _sys  # noqa: E402

import audit  # noqa: E402

_REFLECTION_PROMPT = """You are Jugnu's REFLECTION step. A task just finished.
Look at what the user wanted, what was planned, what happened, and extract
what should change how Jugnu works next time. Be concrete and conservative:
remember only what is durable and useful -- not one-off details, not
anything the user didn't reveal or confirm, and NEVER secrets.

Respond with exactly one JSON object:
{"objective": "what the user was trying to accomplish",
 "succeeded": true|false,
 "outcome_summary": "one or two sentences",
 "what_worked": ["..."], "what_failed": ["..."],
 "user_corrections": ["anything the user corrected"],
 "lessons": ["generalizable lesson for similar tasks"],
 "memories": [{"content": "self-contained sentence", "kind": "user|semantic|strategy|episodic",
               "key": "optional.canonical.subject", "confidence": 0.0-1.0,
               "source": "user_stated|observed|tool_result|reflection|inferred",
               "scope": "global or project:/path or app:Name or task:type", "importance": 0.0-1.0}],
 "skill": null or {"name": "Short verb phrase", "purpose": "...", "trigger_phrases": ["..."],
                   "inputs": ["what it needs"], "preconditions": ["..."],
                   "expected_output": "...", "verification": "how to tell it worked"},
 "planning_advice": "how to plan this kind of request better next time, or empty",
 "code_change_suggestion": null or "only if Jugnu's own code/tools seem to need a change"}

Rules:
- source "user_stated" ONLY for things the user literally said in this task.
  Use "observed" for facts seen on the machine, "inferred" for guesses.
- Propose a "skill" only if this was a multi-step workflow that succeeded and
  would plausibly be requested again.
- Lessons and advice must be specific ("CSC236 materials are under
  ~/Documents/UofT, not ~/Desktop"), never generic ("be careful")."""

_MIN_TASKS_FOR_REFLECTION = 2
_reflect_queue: "_queue.Queue" = _queue.Queue(maxsize=50)
_reflect_worker_started = False


def _log(message: str) -> None:
    print(f"[learning] {message}", file=_sys.stderr, flush=True)


def _worth_reflecting(plan, answer: str) -> bool:
    """Reflection is a model call after the run. Worth it for multi-step
    work (that's where reusable skills and strategies come from); a simple
    request's failures are already recorded as error memories by the
    per-call hooks, for free."""
    tasks = getattr(plan, "tasks", [])
    if getattr(plan, "complexity", "simple") == "simple" or len(tasks) < 2:
        return False
    if len(tasks) >= _MIN_TASKS_FOR_REFLECTION:
        return True
    if any(t.status != "done" or t.attempts > 1 for t in tasks):
        return True  # something went wrong -- worth learning from
    return getattr(plan, "complexity", "simple") == "complex"


def reflect_later(run_id: str, plan, answer: str) -> None:
    """Queues a reflection on a finished run; never blocks the turn."""
    global _reflect_worker_started
    if not _worth_reflecting(plan, answer):
        _credit_skills(plan)
        return
    if not _reflect_worker_started:
        threading.Thread(target=_reflect_loop, name="reflection", daemon=True).start()
        _reflect_worker_started = True
    try:
        _reflect_queue.put_nowait((run_id, plan, answer))
    except _queue.Full:
        _log("reflection queue full; skipping")


def _reflect_loop() -> None:
    while True:
        run_id, plan, answer = _reflect_queue.get()
        try:
            reflect(run_id, plan, answer)
        except Exception as e:
            _log(f"reflection failed for {run_id}: {e}")


def _credit_skills(plan) -> None:
    import skill_library
    ok = all(t.status == "done" or getattr(t, "optional", False) for t in getattr(plan, "tasks", []))
    for skill_id in getattr(plan, "skill_ids", []) or []:
        skill_library.record_use(skill_id, ok)


def _run_evidence(run_id: str, plan, answer: str) -> str:
    lines = [f"User's request: {plan.goal}", f"Planned objective: {plan.objective}"]
    if plan.assumptions:
        lines.append("Assumptions: " + "; ".join(plan.assumptions))
    lines.append("Tasks:")
    for t in plan.tasks:
        lines.append(f"- [{t.agent}] {t.title}: {t.status} after {t.attempts} attempt(s)"
                     + (f" -- note: {t.note[:200]}" if t.note else "")
                     + (f"\n  result: {(t.result or '')[:600]}" if t.result else ""))
    try:
        events = audit.recent(limit=120, run_id=run_id, kinds=["tool_result", "approval_decision", "recovery"])
    except Exception:
        events = []
    tool_lines = []
    for e in reversed(events):
        if e["kind"] == "tool_result":
            tool_lines.append(f"- {e['tool']}: {e['status']}" + ("" if e["status"] == "ok" else f" ({(e['summary'] or '')[:120]})"))
        elif e["kind"] == "approval_decision":
            tool_lines.append(f"- approval for {e['tool']}: {e['status']}")
        else:
            tool_lines.append(f"- recovery: {e['status']} {(e['summary'] or '')[:120]}")
    if tool_lines:
        lines.append("Tool activity:\n" + "\n".join(tool_lines[-40:]))
    lines.append(f"Final answer given to the user:\n{answer[:2000]}")
    return "\n".join(lines)


def _user_said(content: str, user_texts: list[str]) -> bool:
    """Whether a would-be "user_stated" memory is actually grounded in what
    the user wrote in this run (most of its meaningful words appear)."""
    words = {w for w in _re.findall(r"[a-z0-9]+", content.lower()) if len(w) > 3}
    if not words:
        return False
    said = set(_re.findall(r"[a-z0-9]+", " ".join(user_texts).lower()))
    return len(words & said) / len(words) >= 0.6


def reflect(run_id: str, plan, answer: str, user_texts: list | None = None):
    """Runs one reflection and applies it. Returns the validated Reflection
    (or None if the model couldn't produce a valid one)."""
    from planner import request_json
    from schemas import Reflection
    user_texts = user_texts if user_texts is not None else [plan.goal]
    messages = [{"role": "system", "content": _REFLECTION_PROMPT},
                {"role": "user", "content": _run_evidence(run_id, plan, answer)}]
    try:
        reflection = request_json(messages, role="EASY", retries=1, validate=lambda d: Reflection(**d))
    except Exception as e:
        _log(f"no valid reflection: {e}")
        _credit_skills(plan)
        return None
    # Whether it worked is a fact about the run, not an opinion about the
    # final answer: a reflection once concluded "succeeded" from a reply
    # that wrongly claimed a file had been created.
    actually_ok = all(t.status == "done" or getattr(t, "optional", False) for t in plan.tasks)
    if reflection.succeeded and not actually_ok:
        not_done = [t.title for t in plan.tasks if t.status != "done" and not getattr(t, "optional", False)]
        reflection = reflection.model_copy(update={
            "succeeded": False,
            "outcome_summary": f"Did not complete: {', '.join(not_done)}. " + reflection.outcome_summary,
            "skill": None,
        })
    apply_reflection(run_id, plan, reflection, user_texts)
    return reflection


def apply_reflection(run_id: str, plan, reflection, user_texts: list[str]) -> dict:
    """Turns a validated reflection into memories, lessons, a suggested
    skill and (at most) a code-change proposal. Returns what was stored."""
    import skill_library
    stored = {"memories": 0, "lessons": 0, "skill": None, "proposal": False}

    for cand in reflection.memories:
        source = cand.source
        if source == "user_stated" and not _user_said(cand.content, user_texts):
            source = "inferred"  # the model can't vouch for what the user said
        try:
            memory_store.remember(cand.content, kind=cand.kind, key=cand.key, confidence=cand.confidence,
                                  source=source, scope=cand.scope, importance=cand.importance, run_id=run_id)
            stored["memories"] += 1
        except memory_store.MemoryRejected:
            continue

    task_scope = f"task:{_task_type(plan)}"
    for lesson in reflection.lessons[:5]:
        try:
            memory_store.remember(lesson, kind="strategy", confidence=0.65, source="reflection",
                                  scope=task_scope, importance=0.6, run_id=run_id)
            stored["lessons"] += 1
        except memory_store.MemoryRejected:
            pass
    if reflection.planning_advice.strip():
        try:
            memory_store.remember(reflection.planning_advice.strip(), kind="strategy",
                                  key=f"planning.{_task_type(plan)}", confidence=0.6, source="reflection",
                                  scope=task_scope, importance=0.6, run_id=run_id)
        except memory_store.MemoryRejected:
            pass

    if reflection.outcome_summary:
        try:
            status = "succeeded" if reflection.succeeded else "did not fully succeed"
            memory_store.remember(f"{reflection.objective or plan.objective} -- {status}: {reflection.outcome_summary}",
                                  kind="episodic", confidence=0.8, source="reflection", importance=0.5,
                                  run_id=run_id)
        except memory_store.MemoryRejected:
            pass

    if reflection.skill and reflection.succeeded:
        tools = [e["tool"] for e in audit.recent(limit=200, run_id=run_id, kinds=["tool_result"]) if e.get("tool")]
        skill = skill_library.propose(reflection.skill, plan, run_id=run_id, tools=tools)
        if skill:
            stored["skill"] = skill["id"]
            _notify({"type": "skill_suggested", "skill": {"id": skill["id"], "name": skill["name"],
                                                           "purpose": skill["purpose"]}})

    if reflection.code_change_suggestion:
        # Never applied. Recorded for the user to review, with the evidence.
        audit.log("code_change_proposal", run_id=run_id, status="proposed",
                  summary=reflection.code_change_suggestion[:1500],
                  detail={"objective": plan.objective, "what_failed": reflection.what_failed})
        stored["proposal"] = True

    _credit_skills(plan)
    audit.log("reflection", run_id=run_id, status="succeeded" if reflection.succeeded else "failed",
              summary=reflection.outcome_summary[:500],
              detail={"stored": stored, "lessons": reflection.lessons, "what_failed": reflection.what_failed})
    return stored


def _task_type(plan) -> str:
    agents = sorted({t.agent for t in getattr(plan, "tasks", [])})
    return "+".join(agents)[:60] or "general"


# ---------------------------------------------------------------------------
# 3. Corrections -- the user telling Jugnu it got something wrong
# ---------------------------------------------------------------------------

_CORRECTION_RE = _re.compile(
    r"^\s*(?:no[,.!]|nope|wrong|that'?s (?:not|wrong|incorrect)|not (?:what|that)|i (?:meant|said)|"
    r"actually[,]? |you (?:got|made) (?:it|that|a mistake)|incorrect|that isn'?t|it'?s not|don'?t do that)", _re.I)

_CORRECTION_PROMPT = """The user just corrected Jugnu. Extract what should be
remembered so the same mistake doesn't happen again.
Respond with exactly one JSON object:
{"corrected_facts": [{"content": "self-contained fact in the user's words", "key": "optional.subject"}],
 "lesson": "what Jugnu should do differently next time (specific), or empty"}"""


def looks_like_correction(text: str) -> bool:
    return bool(_CORRECTION_RE.match(text or ""))


_notify_fn = None


def set_notifier(fn) -> None:
    global _notify_fn
    _notify_fn = fn


def _notify(payload: dict) -> None:
    if _notify_fn:
        try:
            _notify_fn(payload)
        except Exception:
            pass


def record_correction_later(previous_exchange: str, correction: str) -> None:
    threading.Thread(target=_record_correction, args=(previous_exchange, correction),
                     name="correction", daemon=True).start()


def _record_correction(previous_exchange: str, correction: str) -> None:
    from planner import request_json
    try:
        data = request_json([
            {"role": "system", "content": _CORRECTION_PROMPT},
            {"role": "user", "content": f"What happened:\n{previous_exchange[-3000:]}\n\nThe user's correction: {correction}"},
        ], role="EASY", retries=1)
    except Exception as e:
        _log(f"couldn't extract correction: {e}")
        return
    for fact in (data.get("corrected_facts") or [])[:5]:
        content = str((fact or {}).get("content", "")).strip()
        if not content:
            continue
        source = "user_stated" if _user_said(content, [correction]) else "inferred"
        try:
            memory_store.remember(content, kind="user", key=(fact or {}).get("key") or None, confidence=0.95,
                                  source=source, importance=0.8)
        except memory_store.MemoryRejected:
            continue
    lesson = str(data.get("lesson") or "").strip()
    if lesson:
        try:
            memory_store.remember(lesson, kind="strategy", confidence=0.8, source="reflection", importance=0.75)
        except memory_store.MemoryRejected:
            pass
    audit.log("reflection", status="correction", summary=correction[:300], detail=data)
