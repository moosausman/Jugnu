"""
session.py -- one shared "turn" for every frontend.

bridge.py (the Swift app) and main.py (the CLI) both call run_turn(); each
only owns presentation, through callbacks.

A turn, end to end:

    route        small talk -> direct reply (no planning)
    resume?      if a run is waiting on the user and this message answers
                 it, that run continues with the answer
    context      recent turns + what's on screen + relevant memories,
                 lessons and habits
    plan         planner.make_plan -> validated objective + task graph
    dispatch     scheduled  -> saved to the scheduler, confirmed
                 background -> handed to the run manager, user keeps chatting
                 otherwise  -> executed now: parallel tasks, verification,
                               retries, replanning, checkpoints
    persist      answer saved; the run's final state saved; reflection
                 queued (learning.py) so the next similar task goes better

append_proactive_message() records unprompted messages (nudges, background
results) the same way.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import audit
import memory_store
import router
import run_context
import semantic_memory as smem
import tasks_store
import awareness as aware
from executor import execute_plan
from memory import persona_reply, recent_transcript, save_conversation
from planner import Plan, make_plan
from tools import macbridge

# Foreground turns, background runs and proactive messages all append to
# the same conversation list; this keeps those writes (and the save that
# follows) from interleaving.
_conv_lock = threading.RLock()

# Set by bridge.py at startup (workflows.RunManager); None in the CLI, where
# background requests simply run in the foreground.
run_manager = None


def _safe(fn) -> str:
    """Memory must never be the reason a turn fails."""
    try:
        return fn() or ""
    except Exception:
        return ""


def build_context(conversation: list, user_text: str) -> str:
    """Recent turns, situational awareness (both desktops), and longitudinal
    memory relevant to this message -- handed to the planner and every task."""
    context = recent_transcript(conversation)
    extra_context = "\n\n".join(filter(None, [
        aware.as_prompt_block(),
        aware.agent_desktop_block(),
        _safe(lambda: memory_store.context_block(user_text)),
        smem.relevant_memories_as_string(user_text, k=3),
        smem.get_habit_summary(),
    ]))
    return f"{context}\n\n{extra_context}" if extra_context else context


def _apply_session(plan: Plan) -> Optional[str]:
    """Targets the plan's macOS session for this run. Returns a refusal
    message if that session isn't running."""
    macbridge.reset_session()
    if plan.session and not plan.needs_clarification:
        if not macbridge.session_available(plan.session):
            return _session_unavailable_message(plan.session)
        macbridge.set_active_session(plan.session)
    return None


def _fast_path(user_text: str, session_choice: Optional[str] = None) -> Optional[str]:
    try:
        import fastpath
        return fastpath.try_handle(user_text, session=session_choice)
    except run_context.RunCancelled:
        raise
    except Exception as e:  # a pattern miss or a bug here must never cost the turn
        import sys
        print(f"[session] fast path skipped: {e}", file=sys.stderr, flush=True)
        return None


def _direct_plan(user_text: str, agent: str) -> Plan:
    """A one-task plan without asking the planner. Read-risk so the verifier
    judges it by what the tools returned rather than demanding a change, and
    one attempt: a simple request that fails should say why, not try again."""
    from planner import Task
    task = Task(1, agent, user_text, title=user_text[:60], risk="read", max_attempts=1)
    return Plan(user_text, [task], objective=user_text, request_type="quick_action", complexity="simple")


def _checkpointer(run_id: str):
    def _save(plan, shared):
        tasks_store.checkpoint(run_id, plan, shared, status="running", session=macbridge.active_session())
    return _save


def _schedule_from_plan(plan: Plan) -> str:
    import scheduler
    spec = plan.schedule
    item = scheduler.add(spec.instruction, at=spec.at, in_minutes=spec.in_minutes,
                         every_minutes=spec.every_minutes, until=spec.until, kind=spec.kind,
                         title=spec.instruction[:120] if spec.kind == "notify" else plan.objective[:120],
                         session=plan.session)
    when = item["next_run_text"]
    repeat = f", then every {_every(item['repeat_minutes'])}" if item.get("repeat_minutes") else ""
    if spec.kind == "notify":
        return f"Timer set for {when}{repeat}: \"{spec.instruction}\"."
    return f"Scheduled: {item['title']} -- first run {when}{repeat}. You can see or cancel it under Scheduled."


def _every(minutes: float) -> str:
    if minutes % 1440 == 0:
        days = int(minutes // 1440)
        return "day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        hours = int(minutes // 60)
        return "hour" if hours == 1 else f"{hours} hours"
    return f"{int(minutes)} minutes"


def run_turn(
    conversation: list,
    user_text: str,
    on_plan: Optional[Callable[[Plan], None]] = None,
    on_task_start=None,
    on_status=None,
    on_tool_call=None,
    on_tool_result=None,
    session_override: Optional[str] = None,
) -> str:
    """Runs one full turn and returns the user-facing answer. `conversation`
    is mutated in place (user and assistant messages appended) and saved.

    session_override -- "primary" or "agent" when the user picked the account
    for this request in the app; it beats the default and anything inferred.

    Callbacks, all optional:
      on_plan(plan)                        -- once, when a real plan exists
      on_task_start(task)                  -- each task attempt
      on_status(task, label)               -- "thinking" or a tool name
      on_tool_call(task, name, args)       -- right before a tool runs
      on_tool_result(task, name, args, res) -- right after
    """
    ctx = run_context.current()
    with _conv_lock:
        conversation.append({"role": "user", "content": user_text})

    route = router.quick_route(user_text)
    if route == "control":
        answer = "Nothing's running right now." if not _other_runs(ctx) else \
            "Use the Stop button (⌘.) on the task you want to stop -- it's in the activity panel."
        _finish(conversation, answer)
        return answer
    if route == "conversation":
        transcript = recent_transcript(conversation, limit=6)
        answer = persona_reply(f"Conversation so far:\n{transcript}\n\nReply naturally and briefly to the "
                               f"user's latest message.", role="AGENT") or "👍"
        _finish(conversation, answer)
        smem.record_event("user_message", user_text)
        return answer

    # "No, that's wrong / I meant ..." -- learn from the correction (in the
    # background) as well as acting on it.
    try:
        import learning
        if learning.looks_like_correction(user_text) and len(conversation) > 2:
            learning.record_correction_later(recent_transcript(conversation[:-1], limit=4), user_text)
    except Exception:
        pass

    # Zero model calls: one obvious tool call ("play X", "pause", "open
    # Safari") runs straight through, unless a paused run is waiting on an
    # answer (the message might be that answer).
    if not tasks_store.pending_question():
        quick = _fast_path(user_text, session_override)
        if quick is not None:
            audit.log("plan", run_id=ctx.run_id, summary=f"(fast path) {user_text}", detail={"fast_path": True})
            _finish(conversation, quick)
            smem.record_event("user_message", user_text)
            return quick

    # Context first, THEN log the message -- logging first let recall() find
    # this very message and feed it back as "relevant memory".
    full_context = build_context(conversation[:-1], user_text)
    smem.record_event("user_message", user_text)

    waiting = tasks_store.pending_question()
    if waiting and router.is_answer_to(waiting.get("question") or "", user_text):
        return _continue_waiting_run(conversation, waiting, user_text, full_context,
                                     on_plan, on_task_start, on_status, on_tool_call, on_tool_result)

    tasks_store.create(ctx.run_id, user_text, origin=ctx.origin, request_id=ctx.request_id, context=full_context)
    # A request plainly about one thing goes straight to that agent; only
    # multi-step, scheduled or ambiguous requests pay for the planner.
    direct = router.direct_agent(user_text)
    plan = _direct_plan(user_text, direct) if direct else make_plan(user_text, recent_context=full_context)
    # Explicit words beat model inference: "on the Jugnu account" / "on my
    # account" and "in the background" are honoured even when the planner
    # misses them (it has).
    # The account the user picked in the app wins; then explicit words
    # ("on the Jugnu account") the planner might have missed.
    hinted_session = router.session_hint(user_text)
    if session_override in ("primary", "agent"):
        plan.session = session_override
    elif hinted_session and not plan.session:
        plan.session = hinted_session
    if router.wants_background(user_text) and plan.request_type in ("agentic", "quick_action") and plan.tasks:
        plan.request_type = "background"
    audit.log("plan", run_id=ctx.run_id, summary=plan.objective, detail=plan.to_dict())
    tasks_store.checkpoint(ctx.run_id, plan, {}, status="planning")

    refusal = _apply_session(plan)
    if refusal:
        tasks_store.finish(ctx.run_id, "failed", refusal)
        _finish(conversation, refusal)
        return refusal

    if plan.needs_clarification:
        tasks_store.wait_for_user(ctx.run_id, plan.clarify)
        _finish(conversation, plan.clarify)
        return plan.clarify

    if plan.request_type == "scheduled" and plan.schedule is not None:
        try:
            answer = _schedule_from_plan(plan)
            tasks_store.finish(ctx.run_id, "done", answer)
        except Exception as e:
            answer = f"I couldn't schedule that: {e}"
            tasks_store.finish(ctx.run_id, "failed", answer)
        _finish(conversation, answer)
        return answer

    if plan.request_type == "background" and run_manager is not None:
        tasks_store.finish(ctx.run_id, "handed_off", "")
        run_id = run_manager.start_background(user_text, plan=plan, recent_context=full_context,
                                              session=macbridge.active_session())
        answer = (f"On it in the background: {plan.objective}. I'll message you when it's done -- "
                  "you can keep going meanwhile.")
        audit.log("background_started", run_id=run_id, summary=plan.objective)
        _finish(conversation, answer)
        return answer

    if on_plan:
        on_plan(plan)

    answer = _execute(ctx, plan, full_context, {}, on_task_start, on_status, on_tool_call, on_tool_result)
    _finish(conversation, answer)
    return answer


def _execute(ctx, plan: Plan, context: str, shared: dict, on_task_start, on_status, on_tool_call,
             on_tool_result, run_id: Optional[str] = None) -> str:
    run_id = run_id or ctx.run_id

    def _on_tool_call(task, name, args):
        smem.record_event("tool_call", f"{name}({args})", metadata={"task_id": task.id, "tool": name})
        if name == "search_web" and isinstance(args, dict) and args.get("query"):
            smem.record_search(args["query"])
        if on_tool_call:
            on_tool_call(task, name, args)

    tasks_store.set_status(run_id, "running")
    try:
        answer = execute_plan(plan, recent_context=context, on_task_start=on_task_start, on_status=on_status,
                              on_tool_call=_on_tool_call, on_tool_result=on_tool_result,
                              on_checkpoint=_checkpointer(run_id), shared_memory=shared)
    except run_context.RunCancelled:
        tasks_store.finish(run_id, "cancelled")
        raise
    except Exception as e:
        tasks_store.finish(run_id, "failed", error=str(e))
        raise

    if plan.pending_question:
        tasks_store.wait_for_user(run_id, plan.pending_question)
    else:
        final = "done" if all(t.status == "done" or t.optional for t in plan.tasks) else "partial"
        tasks_store.finish(run_id, final, answer)
        _queue_reflection(run_id, plan, answer)
    return answer


def _continue_waiting_run(conversation, waiting: dict, user_text: str, context: str,
                          on_plan, on_task_start, on_status, on_tool_call, on_tool_result) -> str:
    """The user answered a question a paused run asked: continue that run
    (completed work is kept) with the answer in hand."""
    ctx = run_context.current()
    run_id = waiting["run_id"]
    note = f"The user answered your question \"{waiting.get('question', '')}\": {user_text}"
    if waiting.get("plan") and waiting["plan"].get("tasks"):
        plan, shared = tasks_store.prepare_resume(waiting)
        plan.assumptions.append(note)
        plan.clarify = None
        plan.pending_question = None
    else:  # a plan-level clarification: plan afresh with the answer
        shared = {}
        plan = make_plan(f"{waiting['goal']}\n\n({note})", recent_context=context)
        if plan.needs_clarification:
            tasks_store.wait_for_user(run_id, plan.clarify)
            _finish(conversation, plan.clarify)
            return plan.clarify
    refusal = _apply_session(plan)
    if refusal:
        _finish(conversation, refusal)
        return refusal
    audit.log("run_resumed", run_id=run_id, summary=note)
    ctx.send({"type": "run_resumed", "resumed_run_id": run_id})
    if on_plan:
        on_plan(plan)
    answer = _execute(ctx, plan, f"{context}\n\n{note}", shared, on_task_start, on_status, on_tool_call,
                      on_tool_result, run_id=run_id)
    _finish(conversation, answer)
    return answer


def _other_runs(ctx) -> bool:
    return any(r.run_id != ctx.run_id for r in run_context.live_runs())


def _queue_reflection(run_id: str, plan: Plan, answer: str) -> None:
    """Post-task learning, off the critical path (learning.reflect_later)."""
    try:
        import learning
        reflect = getattr(learning, "reflect_later", None)
        if reflect:
            reflect(run_id, plan, answer)
    except Exception:
        pass


def record_interrupted_turn(conversation: list, answer: str) -> None:
    """Persists a turn that was cancelled mid-way, so the transcript shows
    the user message was seen and stopped rather than silently vanishing."""
    if conversation and conversation[-1].get("role") == "user":
        _finish(conversation, answer)


def _finish(conversation: list, answer: str) -> None:
    with _conv_lock:
        conversation.append({"role": "assistant", "content": answer})
        save_conversation(conversation)
    smem.record_event("assistant_message", answer)


def _session_unavailable_message(name: str) -> str:
    # Refuse rather than quietly doing the work on the other desktop -- the
    # user would believe it happened somewhere it didn't.
    if name == "agent":
        return ("The Jugnu account isn't running, so I haven't started. Log into it "
                "once (menu bar user icon > Jugnu), then switch back -- it keeps "
                "running in the background.")
    return "MacBridge isn't running in your account, so I haven't started."


def append_proactive_message(conversation: list, message: str, origin: str = "proactive") -> None:
    """Records an unprompted message (a nudge, a finished background task, a
    timer) the same way a normal turn's answer is recorded."""
    with _conv_lock:
        conversation.append({"role": "assistant", "content": message})
        save_conversation(conversation)
    smem.record_event("assistant_message", message, metadata={"proactive": True, "origin": origin})
