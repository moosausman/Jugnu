"""
session.py -- one shared "turn" for every frontend.

bridge.py (JSON-over-stdio for the Swift app) and main.py (the CLI REPL)
used to each hand-roll the exact same five steps -- append the user
message, build context (recent transcript + situational awareness +
longitudinal memory), plan, execute, persist the answer -- with only
their I/O (emit JSON vs. print()) actually differing. That's real
duplication: any change to how context is built, or to what gets logged
to semantic memory, had to be made twice and kept in sync by hand.

run_turn() is that shared spine. Each frontend now only owns presentation:
it passes in callbacks for the moments it wants to show something (a plan,
a status update, a tool call) and gets back the final answer string.

append_proactive_message() is the other, smaller thing frontends share:
recording an unprompted message (see bridge.py's proactive watcher /
proactive.py) the same way a normal turn's answer gets recorded, without
going through run_turn() itself -- there's no user message or plan for
one of those.
"""

from typing import Callable, Optional

from memory import recent_transcript, save_conversation
from planner import make_plan, Plan
from executor import execute_plan
import awareness as aware
import semantic_memory as smem


def build_context(conversation: list, user_text: str) -> str:
    """Recent turns, situational awareness (what's frontmost and open on
    the user's screen right now), and longitudinal memory (things possibly
    relevant to this message, and recurring habits) -- handed to the
    planner and, via depends_on, to every task agent after it."""
    context = recent_transcript(conversation)
    extra_context = "\n\n".join(filter(None, [
        aware.as_prompt_block(),
        smem.relevant_memories_as_string(user_text),
        smem.get_habit_summary(),
    ]))
    return f"{context}\n\n{extra_context}" if extra_context else context


def run_turn(
    conversation: list,
    user_text: str,
    on_plan: Optional[Callable[[Plan], None]] = None,
    on_task_start=None,
    on_status=None,
    on_tool_call=None,
    on_tool_result=None,
) -> str:
    """Runs one full turn -- plan, execute, persist -- and returns the
    user-facing answer. `conversation` is mutated in place (user and
    assistant messages appended) and saved to disk before returning.

    Callbacks, all optional:
      on_plan(plan)                        -- once, only if a real plan (not
                                               a clarifying question) came back
      on_task_start(task)                  -- once per task, before it runs
      on_status(task, label)               -- "thinking" or a tool name
      on_tool_call(task, name, args)       -- right before a tool runs
      on_tool_result(task, name, args, res) -- right after
    """
    conversation.append({"role": "user", "content": user_text})
    smem.record_event("user_message", user_text)

    full_context = build_context(conversation[:-1], user_text)
    plan = make_plan(user_text, recent_context=full_context)

    if on_plan and not plan.needs_clarification:
        on_plan(plan)

    def _on_tool_call(task, name, args):
        # Recorded here, once, so every frontend gets habit-tracking for
        # free instead of remembering to call smem.record_event /
        # record_search itself in its own on_tool_call.
        smem.record_event("tool_call", f"{name}({args})", metadata={"task_id": task.id, "tool": name})
        if name == "search_web" and isinstance(args, dict) and args.get("query"):
            smem.record_search(args["query"])
        if on_tool_call:
            on_tool_call(task, name, args)

    answer = execute_plan(
        plan,
        recent_context=full_context,
        on_task_start=on_task_start,
        on_status=on_status,
        on_tool_call=_on_tool_call,
        on_tool_result=on_tool_result,
    )

    conversation.append({"role": "assistant", "content": answer})
    smem.record_event("assistant_message", answer)
    save_conversation(conversation)
    return answer


def append_proactive_message(conversation: list, message: str) -> None:
    """Records an unprompted message the same way a normal turn's answer
    gets recorded -- appended, persisted, logged to the timeline -- for
    bridge.py's proactive watcher (see proactive.py), which has no user
    message or plan to hang a normal run_turn() call off of."""
    conversation.append({"role": "assistant", "content": message})
    smem.record_event("assistant_message", message, metadata={"proactive": True})
    save_conversation(conversation)
