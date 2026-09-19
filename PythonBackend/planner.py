"""
planner.py -- the CEO layer.

    goal -> plan -> tasks -> agents

This module itself never touches a tool -- it only decides who should do
the work and in what order, and hands each specialist a concrete,
self-contained instruction. But the PLANNER *role* (the big model) doesn't
stop being in charge once that task graph is handed off: executor.py runs
every task's actual tool-selection loop under role="PLANNER" too, so the
same model that designed the plan is also the one deciding, live, which
tool to call and with what arguments for each task. This file only covers
the "which tasks, in what order" half of that -- see executor.py for the
"which tool, right now" half.
"""

import json
import sys

import llm_client

from agents import AGENTS, agent_capabilities_block
from memory import date_block, facts_as_string, IDENTITY, SITUATIONAL_AWARENESS

MAX_TASKS = 8


def log(message: str) -> None:
    print(f"[planner] {message}", file=sys.stderr, flush=True)


def _planner_system_prompt() -> str:
    # Built as plain strings (not f-string braces) so the literal JSON in
    # the examples below never has to fight with f-string brace-escaping.
    schema_example = (
        '{"goal": "<restated goal, one line>", "tasks": '
        '[{"id": 1, "agent": "<name from the list above>", '
        '"description": "<concrete, self-contained instruction for that agent>", '
        '"depends_on": []}]}'
    )
    clarify_example = '{"clarify": "<one direct question>"}'

    return f"""{IDENTITY}

{date_block()}

You are the PLANNING layer only. You never search the web, never write
files, never touch the calendar, never call any tool. Your only output is a
task graph describing who should do the work.

{SITUATIONAL_AWARENESS}
Use it to make a task description concrete instead of vague -- e.g. if the
Context section shows Slack frontmost, "close this app" becomes a task
description naming Slack specifically, not "the app the user means."

Agents available:
{agent_capabilities_block()}
- answer: no tools at all -- use this ONLY when nothing else is needed: pure
  conversation, opinions, or anything answerable from context alone.
- daily_review: produces an end-of-day recap of today's activity, searches,
  notes, and reminders/calendar, with concrete recommendations for
  tomorrow -- use this ONLY when the user explicitly asks for a summary,
  recap, or review of their day. Not for a question about one specific
  app, search, or reminder.

Respond with exactly ONE raw JSON object and nothing else -- no markdown
fences, no commentary, no explanation before or after.

Schema:
{schema_example}

Rules:
- Use the fewest tasks that fully solve the goal. A simple one-tool request is ONE task.
- Only use agent names from the list above -- never invent one.
- "depends_on" lists task ids that must finish first. The dependent task will
  automatically be given those results, so don't make it repeat a lookup an
  earlier task already did.
- If the goal is genuinely ambiguous and no reasonable default exists, respond
  with {clarify_example} instead of "tasks".
- Never output more than {MAX_TASKS} tasks."""


class Task:
    def __init__(self, id, agent, description, depends_on=None):
        self.id = int(id)
        self.agent = agent
        self.description = description
        self.depends_on = [int(d) for d in (depends_on or [])]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "description": self.description,
            "depends_on": self.depends_on,
        }


class Plan:
    def __init__(self, goal, tasks, clarify=None):
        self.goal = goal
        self.tasks = tasks
        self.clarify = clarify

    @property
    def needs_clarification(self) -> bool:
        return self.clarify is not None


def extract_json_object(text: str) -> dict:
    """Pulls the first {...} JSON object out of a model's raw response,
    tolerating a markdown code fence around it. Shared with proactive.py,
    which asks for the same "one JSON object, nothing else" shape for its
    own decision-making -- not planner-specific despite living here."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"model did not return JSON: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def request_json(messages: list, role: str, retries: int = 1, validate=None):
    """Ask the model for exactly one JSON object, retrying on either a
    malformed response or a domain-validation failure by feeding the
    error back and asking for a correction -- the "ask, parse, correct"
    loop planner.make_plan and proactive.consider both need, previously
    duplicated between the two with a gap in both copies: a bare
    llm_client.chat() failure (Ollama not responding, a dropped API
    request) sat *outside* the try/except in each, so a transient hiccup
    aborted the whole call instead of being retried like a bad JSON
    response was. Here that class of failure is retried too (with no
    correction message, since the model never got a chance to answer).

    `validate`, if given, receives the parsed dict and can raise to
    reject it (e.g. an unknown agent name) -- that also triggers a
    retry-with-correction, the same as malformed JSON. Its return value
    is what request_json returns on success; with no validate, the raw
    parsed dict is returned.

    `messages` is mutated in place with each failed attempt + correction,
    the same contract agent_core.run_tool_loop uses, so a caller that
    wants the full transcript afterward still has it. Raises the last
    error once retries are exhausted -- callers decide their own
    fallback rather than this function guessing one for them.
    """
    last_err = None
    for _attempt in range(retries + 1):
        try:
            response = llm_client.chat(messages=messages, think=False, role=role)
        except Exception as e:
            last_err = e
            continue  # transient call failure -- nothing to correct, just retry

        raw = response["message"].content or ""
        try:
            data = extract_json_object(raw)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return validate(data) if validate else data
        except Exception as e:
            last_err = e
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": f"That wasn't valid: {e}. Respond with ONLY the corrected JSON object.",
            })
    raise last_err


def _validate(data: dict, goal_fallback: str) -> Plan:
    if data.get("clarify"):
        return Plan(goal=data.get("goal", goal_fallback), tasks=[], clarify=str(data["clarify"]))

    raw_tasks = data.get("tasks") or []
    if not raw_tasks:
        raise ValueError("planner returned no tasks and no clarify question")

    valid_agents = set(AGENTS.keys()) | {"answer", "daily_review"}
    tasks = []
    seen_ids = set()

    for t in raw_tasks[:MAX_TASKS]:
        agent = t.get("agent")
        if agent not in valid_agents:
            raise ValueError(f"planner used unknown agent: {agent!r}")
        tid = int(t["id"])
        if tid in seen_ids:
            raise ValueError(f"duplicate task id: {tid}")
        seen_ids.add(tid)
        tasks.append(Task(
            id=tid,
            agent=agent,
            description=(t.get("description") or "").strip(),
            depends_on=t.get("depends_on", []),
        ))

    for t in tasks:
        for dep in t.depends_on:
            if dep not in seen_ids:
                raise ValueError(f"task {t.id} depends on unknown task {dep}")

    return Plan(goal=data.get("goal", goal_fallback), tasks=tasks)


def make_plan(goal: str, recent_context: str = "", retries: int = 1) -> Plan:
    """Asks the planner model for a task graph. Retries on a malformed
    response, a validation failure (unknown agent, bad task id, ...), or
    the model call itself failing (see request_json), feeding the error
    back for a correction each time. If it still can't get a valid plan,
    falls back to a single 'answer' task rather than failing the whole
    turn."""
    context_parts = []
    facts = facts_as_string()
    if facts:
        context_parts.append(facts)
    if recent_context:
        # Not just prior conversation any more -- session.build_context()
        # also folds in situational awareness and longitudinal memory, so
        # a "Recent conversation:" label would undersell (and could cause
        # the model to discount) everything past the transcript itself.
        context_parts.append(f"Context:\n{recent_context}")
    context_parts.append(f"User's goal: {goal}")

    messages = [
        {"role": "system", "content": _planner_system_prompt()},
        {"role": "user", "content": "\n\n".join(context_parts)},
    ]

    try:
        return request_json(
            messages, role="PLANNER", retries=retries,
            validate=lambda data: _validate(data, goal_fallback=goal),
        )
    except Exception as e:
        log(f"couldn't get a valid plan after retries ({e}) -- falling back to a single answer task")
        return Plan(goal=goal, tasks=[Task(id=1, agent="answer", description=goal)])
