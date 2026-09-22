"""
proactive.py -- decides whether Jugnu should do something unprompted, and
if so, what kind of something.

bridge.py's background watcher notices *that* something changed (the user
switched to a different frontmost app -- see awareness.py) on a timer, with
no user message in sight. This module decides what, if anything, that's
worth -- three possible outcomes:

  - nothing (the default, by far the most common)
  - SAY something -- a message, exactly like before this module grew a
    third option
  - ACT -- actually run one agent's task, unprompted, then report what
    happened

The model only ever picks WHAT would help and describes it as a concrete,
self-contained task for one agent -- the same shape planner.py already
uses for real task graphs. It does NOT get to decide whether that task
runs immediately or waits for a human yes. That's a hard, deterministic
Python-side gate (see agents.py's Agent.autonomous_safe): only agents whose
entire tool surface is read-only or an established "just do it" action
(research, contacts, lookup, memory) ever run without confirmation.
Anything else -- calendar, files, mail, system, notion, browser,
temp_email, automation -- gets PROPOSED as a message instead, and nothing
executes until the user actually says go ahead in a normal turn. This
split is a whitelist, not a blacklist, on purpose: if it's ever wrong, it
fails toward "asked when it didn't need to," never the other way.

Autonomous ACTION is its own opt-in, separate from JUGNU_PROACTIVE (which
by itself still only ever produces messages, exactly like before this
file changed) -- see PROACTIVE_ACTIONS_ENABLED / JUGNU_PROACTIVE_ACTIONS
below and in bridge.py's module docstring. An existing JUGNU_PROACTIVE=1
setup gains zero new capability just from this file being updated.

Deliberately conservative throughout: silence is the default outcome for
"is this worth anything", and acting is the least-reached-for of the three
outcomes even when actions are enabled. This is only ever called after
significant_change() already fired and bridge.py's cooldown has elapsed
(see JUGNU_PROACTIVE_COOLDOWN_SECONDS) -- and it should still return None
most of those times. An app switch is a reason to *check*, never a reason
to *speak or act* on its own.
"""

import os
import sys

from agents import AGENTS, agent_capabilities_block
from executor import run_task
from memory import recent_transcript, facts_as_string, persona_reply, date_block, IDENTITY
from planner import Task, request_json
import awareness as aware

MAX_TRANSCRIPT_TURNS = 10
MAX_JSON_RETRIES = 1  # one retry on malformed JSON, same as planner.make_plan

PROACTIVE_ACTIONS_ENABLED = os.environ.get("JUGNU_PROACTIVE_ACTIONS", "0") == "1"


def log(message: str) -> None:
    print(f"[proactive] {message}", file=sys.stderr, flush=True)


def _system_prompt() -> str:
    base = f"""{IDENTITY}

{date_block()}

The user just switched to a different app. Nobody asked you anything --
you're privately deciding, once, whether this specific moment calls for
initiative on your part."""

    if not PROACTIVE_ACTIONS_ENABLED:
        # Unchanged from before this file could act at all -- an
        # explicit choice, not an oversight: JUGNU_PROACTIVE=1 alone
        # should never grant tool-execution power on its own.
        return base + """

Respond with exactly one raw JSON object, nothing else:

  {"action": "none"}
  {"action": "say", "message": "<one or two sentences, no preamble>"}

Say something ONLY if you can tie it to something concrete: a reminder or
request from the conversation below that this app switch now makes
relevant ("let me know when I open Xcode"), an unfinished thread that this
moment directly continues, or a known fact that plainly applies right
now. A plausible-sounding guess is not a concrete reason. Never comment on
the switch itself ("I see you opened X") -- lead with the substance.

Silence is correct almost every time an app switch happens with no such
tie-in -- which is most of the time. Respond with {"action": "none"}
unless something concrete applies.

Respond with the JSON object. Nothing else -- no explanation, no markdown fences."""

    return base + f"""

Respond with exactly one raw JSON object, nothing else:

  {{"action": "none"}}
  {{"action": "say", "message": "<one or two sentences, no preamble>"}}
  {{"action": "act", "agent": "<name from the list below>", "task": "<concrete, self-contained instruction>", "why": "<one clause>"}}

Agents available for "act":
{agent_capabilities_block()}

Say something ("say") ONLY if you can tie it to something concrete: a
reminder or request from the conversation below that this app switch now
makes relevant, an unfinished thread this moment directly continues, or a
known fact that plainly applies right now. A plausible-sounding guess is
not a concrete reason. Never comment on the switch itself.

Act ("act") ONLY for something genuinely worth doing right now without
being asked, described as one single, self-contained task for one agent --
not a vague intention. Whether it actually runs immediately or gets
proposed to the user first depends on which agent it is, not on you --
just name the single best agent and task; don't try to guess or hedge
based on how sensitive it might be.

Silence is correct almost every time an app switch happens with no such
tie-in -- which is most of the time. Respond with {{"action": "none"}}
unless something concrete applies.

Respond with the JSON object. Nothing else -- no explanation, no markdown fences."""


def _gather_context(conversation: list, curr: "aware.Snapshot", prev) -> str:
    transcript = recent_transcript(conversation, limit=MAX_TRANSCRIPT_TURNS)
    facts = facts_as_string()
    change = aware.describe_change(prev, curr)

    parts = [aware.as_prompt_block(curr)]
    if change:
        parts.append(change)
    if transcript:
        parts.append(f"Recent conversation:\n{transcript}")
    if facts:
        parts.append(facts)
    return "\n\n".join(filter(None, parts))


def _phrase(user_content: str) -> "str | None":
    """One small persona-voiced call to turn an already-decided outcome
    (an action just taken, or one being proposed) into something worth
    reading -- see memory.persona_reply, the shared helper this, executor.
    _synthesize, and daily_review's final pass all now go through. Never
    shows the user a raw agent result: those are written tersely, for
    another layer to phrase (see AGENT_DISCIPLINE in memory.py), not to
    read directly."""
    return persona_reply(user_content) or None


def _do_and_report(agent_name: str, task_description: str, why: str) -> "str | None":
    """Actually runs the task -- reusing run_task() exactly the way a real
    planned task would -- then reports what happened. Only ever reached
    for agents.py's autonomous_safe agents; see _act_on_decision."""
    task = Task(id=1, agent=agent_name, description=task_description)
    # Start from the default session, not wherever the last turn left it.
    from tools.macbridge import reset_session
    import run_context
    reset_session()
    # Nobody asked for this: policy treats it as unattended, so anything
    # beyond reading waits for an explicit yes (see policy.decide).
    ctx = run_context.RunContext(goal=task_description, user_initiated=False, origin="proactive")
    token = run_context.activate(ctx)
    try:
        result = run_task(task, shared_memory={})
    except Exception as e:
        log(f"autonomous action failed ({agent_name}): {e}")
        return None  # a failed autonomous action stays silent -- never
        # surface a raw error as an unprompted message
    finally:
        run_context.deactivate(token)

    return _phrase(
        f"You just did this on your own, unprompted, because: "
        f"{why or 'it seemed worth doing right now'}\n\n"
        f"Task: {task_description}\nResult: {result}\n\n"
        "Tell the user what you did, in one or two sentences, naturally -- "
        "not a status report, just how you'd mention it in passing."
    )


def _propose(agent_name: str, task_description: str, why: str) -> "str | None":
    """Offers the action instead of taking it -- for anything outside
    agents.py's autonomous_safe whitelist. Nothing runs until the user
    actually says yes in a normal turn; that reply is handled entirely by
    the existing planner/executor pipeline, no special-casing needed."""
    return _phrase(
        f"You'd like to do this, but it's the kind of thing you check "
        f"first: {task_description}\nWhy now: {why or 'it seemed worth doing right now'}\n\n"
        "Offer it to the user in one sentence and ask if you should go "
        "ahead -- concrete, not generic. You're only asking; don't do it yet."
    )


def _validate_decision(data: dict) -> dict:
    """Shape-check only -- deliberately performs NO side effects.

    This is what gets handed to request_json's `validate` hook, and that
    hook is called inside a retry loop that catches any exception and asks
    the model again. _act_on_decision used to be passed here directly,
    which meant the retry loop was wrapped around code that actually runs
    an agent task: if anything downstream of the action raised (a network
    blip in _phrase's persona_reply call was the realistic one), the
    exception was caught as "invalid JSON", the model was re-prompted, it
    answered "act" again, and the task RAN A SECOND TIME. Validation and
    execution are separated now -- see consider(), which acts once, after
    the retry loop has already finished.
    """
    action = data.get("action", "none")
    if action not in ("none", "say", "act"):
        raise ValueError(f"unknown action {action!r} -- expected none, say, or act")

    if action == "say" and not (data.get("message") or "").strip():
        raise ValueError('action "say" requires a non-empty "message"')

    # Only worth correcting the model on act-specific fields when acting is
    # actually possible. With actions disabled the prompt never offers
    # "act" at all, so a stray one is just noise -- _act_on_decision drops
    # it silently rather than burning a retry on a correction that can't
    # change the outcome.
    if action == "act" and PROACTIVE_ACTIONS_ENABLED:
        if data.get("agent") not in AGENTS:
            raise ValueError(
                f"unknown agent {data.get('agent')!r} -- use one from the list provided"
            )
        if not (data.get("task") or "").strip():
            raise ValueError('action "act" requires a non-empty "task"')

    return data


def _act_on_decision(decision: dict) -> "str | None":
    """Carries out an already-validated decision. Called exactly once, from
    consider(), outside request_json's retry loop -- see _validate_decision."""
    action = decision.get("action", "none")

    if action == "say":
        message = (decision.get("message") or "").strip()
        return message or None

    if action == "act" and PROACTIVE_ACTIONS_ENABLED:
        agent_name = decision.get("agent")
        task_description = (decision.get("task") or "").strip()
        why = (decision.get("why") or "").strip()

        if agent_name not in AGENTS or not task_description:
            # Malformed or hallucinated agent name -- default to silence
            # rather than guess what was meant.
            return None

        if AGENTS[agent_name].autonomous_safe:
            return _do_and_report(agent_name, task_description, why)
        return _propose(agent_name, task_description, why)

    return None  # "none", or "act" while actions are disabled


def consider(conversation: list, curr: "aware.Snapshot", prev: "aware.Snapshot | None" = None) -> "str | None":
    """Returns a message worth surfacing right now, unprompted -- whether
    that's a plain comment, a report of something just done, or a proposal
    -- or None if silence is the right call (the default; see module
    docstring)."""
    context = _gather_context(conversation, curr, prev)
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": context},
    ]

    try:
        decision = request_json(
            messages, role="EASY", retries=MAX_JSON_RETRIES, validate=_validate_decision
        )
    except Exception as e:
        log(f"couldn't get a valid decision after retries ({e}) -- defaulting to silence")
        return None  # never guess -- silence is always the safe default here

    # Outside the retry loop on purpose: whatever this does happens once,
    # and a failure here can no longer be mistaken for a bad response and
    # replayed. See _validate_decision.
    #
    # Still guarded, though: moving the action out of `validate` also moved
    # it out from under request_json's except clause, and consider()'s
    # contract with its one caller (bridge.py's ProactiveWatcher) is that
    # it returns a message or None and never raises. Silence is always the
    # safe outcome for unprompted behaviour -- an error here must not
    # become an unprompted error message, and must not take the watcher
    # thread's tick down with it.
    try:
        return _act_on_decision(decision)
    except Exception as e:
        log(f"couldn't carry out the decision ({e}) -- staying silent")
        return None
