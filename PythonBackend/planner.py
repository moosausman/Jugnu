"""
planner.py -- goal interpretation and planning.

    request -> objective -> assumptions -> task graph (agents, dependencies,
    risk, completion criteria, verification, model tier)

This module never touches a tool. It produces a validated schemas.PlanSpec
and turns it into the Plan/Task objects the executor runs. Three entry
points:

    make_plan(goal, context)        a fresh plan for a user request
    replan(plan, done, failed, why) revised remaining work after a failure
                                    or a disproven assumption
    request_json(...)               the shared "ask for JSON, validate,
                                    correct" loop (also used by proactive.py,
                                    verifier.py and learning.py)

The prompt is assembled from focused blocks (_ROLE, _INTERPRETATION,
_ROUTING, _CLARIFY_POLICY, ...) rather than one monolith, so each concern
can be read and changed on its own.
"""

from __future__ import annotations

import json
import sys

from pydantic import ValidationError

import llm_client
from agents import AGENTS, agent_capabilities_block
from memory import (
    date_block, facts_as_string, IDENTITY, SITUATIONAL_AWARENESS,
    DEFAULT_SESSION, agent_session_live,
)
from schemas import MAX_TASKS, PlanSpec, ReplanSpec, TaskSpec, validation_message

PSEUDO_AGENTS = {
    "answer": "no tools -- pure conversation, opinions, explanations, or anything answerable from "
              "context and memory alone. Also for teaching/explaining a concept in the reply itself.",
    "daily_review": "an end-of-day recap of today's activity, searches, notes and calendar with "
                    "recommendations -- ONLY when the user explicitly asks for a recap of their day.",
}


def log(message: str) -> None:
    print(f"[planner] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Plan objects the executor runs
# ---------------------------------------------------------------------------

class Task:
    def __init__(self, id, agent, description, depends_on=None, *, title="", risk="low",
                 completion_criteria="", verification="", model_tier="", optional=False,
                 max_attempts=2):
        self.id = int(id)
        self.agent = agent
        self.description = description
        self.depends_on = [int(d) for d in (depends_on or [])]
        self.title = title or (description[:60] + ("…" if len(description) > 60 else ""))
        self.risk = risk
        self.completion_criteria = completion_criteria
        self.verification = verification
        agent_obj = AGENTS.get(agent)
        self.model_tier = model_tier or (agent_obj.default_tier if agent_obj else "standard")
        self.optional = optional
        self.max_attempts = max(1, min(int(max_attempts or 1), 3))
        # Runtime state, owned by the executor.
        self.status = "pending"
        self.attempts = 0
        self.result = ""
        self.note = ""
        self.recovery = ""   # the verifier's suggested recovery after a failed attempt

    @classmethod
    def from_spec(cls, spec: TaskSpec) -> "Task":
        return cls(spec.id, spec.agent, spec.description, spec.depends_on, title=spec.title,
                   risk=spec.risk, completion_criteria=spec.completion_criteria,
                   verification=spec.verification, model_tier=spec.model_tier or "",
                   optional=spec.optional, max_attempts=spec.max_attempts)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "agent": self.agent, "title": self.title, "description": self.description,
            "depends_on": self.depends_on, "risk": self.risk,
            "completion_criteria": self.completion_criteria, "verification": self.verification,
            "model_tier": self.model_tier, "optional": self.optional, "max_attempts": self.max_attempts,
            "status": self.status, "attempts": self.attempts, "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        t = cls(d["id"], d["agent"], d["description"], d.get("depends_on"), title=d.get("title", ""),
                risk=d.get("risk", "low"), completion_criteria=d.get("completion_criteria", ""),
                verification=d.get("verification", ""), model_tier=d.get("model_tier", ""),
                optional=d.get("optional", False), max_attempts=d.get("max_attempts", 2))
        t.status = d.get("status", "pending")
        t.attempts = d.get("attempts", 0)
        t.result = d.get("result", "")
        t.note = d.get("note", "")
        return t


class Plan:
    def __init__(self, goal, tasks, clarify=None, session=None, *, objective="", request_type="agentic",
                 complexity="standard", assumptions=None, success_criteria=None, schedule=None):
        self.goal = goal
        self.tasks = tasks
        self.clarify = clarify
        # "agent" / "primary", or None for the default. See session.run_turn.
        self.session = session
        self.objective = objective or goal
        self.request_type = request_type
        self.complexity = complexity
        self.assumptions = list(assumptions or [])
        self.success_criteria = list(success_criteria or [])
        self.schedule = schedule           # schemas.ScheduleSpec or None
        self.replans = 0
        self.pending_question: str | None = None   # set when execution stopped to ask the user
        self.skill_ids: list[str] = []             # learned skills offered to the planner for this plan

    @property
    def needs_clarification(self) -> bool:
        return self.clarify is not None

    def to_dict(self) -> dict:
        return {
            "goal": self.goal, "objective": self.objective, "request_type": self.request_type,
            "complexity": self.complexity, "assumptions": self.assumptions,
            "success_criteria": self.success_criteria, "session": self.session,
            "clarify": self.clarify, "replans": self.replans, "skill_ids": self.skill_ids,
            "schedule": self.schedule.model_dump() if self.schedule is not None else None,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        from schemas import ScheduleSpec
        plan = cls(d.get("goal", ""), [Task.from_dict(t) for t in d.get("tasks", [])], clarify=d.get("clarify"),
                   session=d.get("session"), objective=d.get("objective", ""),
                   request_type=d.get("request_type", "agentic"), complexity=d.get("complexity", "standard"),
                   assumptions=d.get("assumptions"), success_criteria=d.get("success_criteria"),
                   schedule=ScheduleSpec(**d["schedule"]) if d.get("schedule") else None)
        plan.replans = d.get("replans", 0)
        plan.skill_ids = list(d.get("skill_ids") or [])
        return plan


# ---------------------------------------------------------------------------
# JSON request/validate/correct loop
# ---------------------------------------------------------------------------

def extract_json_object(text: str) -> dict:
    """Pulls the first {...} JSON object out of a model's raw response,
    tolerating a markdown code fence around it."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"model did not return JSON: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def request_json(messages: list, role: str, retries: int = 1, validate=None):
    """Ask the model for exactly one JSON object, retrying on a malformed
    response, a failed validation (the exact errors are fed back), or a
    failed model call. `validate(data)` may raise to reject; its return
    value is returned. Raises the last error once retries are exhausted."""
    last_err = None
    for _attempt in range(retries + 1):
        try:
            response = llm_client.chat(messages=messages, think=False, role=role)
        except Exception as e:
            last_err = e
            continue
        raw = response["message"].content or ""
        try:
            data = extract_json_object(raw)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return validate(data) if validate else data
        except ValidationError as e:
            last_err = ValueError(validation_message(e))
        except Exception as e:
            last_err = e
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user",
                         "content": f"That wasn't valid: {last_err}. Respond with ONLY the corrected JSON object."})
    raise last_err


# ---------------------------------------------------------------------------
# Prompt blocks
# ---------------------------------------------------------------------------

_ROLE = """You are Jugnu's PLANNER. You never call tools yourself. You read the
user's request and produce a plan other agents will execute. Planning well is
the difference between doing what was literally typed and getting done what
the user actually needs."""

_INTERPRETATION = """━━━ INTERPRET THE GOAL ━━━
- Work out the real objective behind the words, and state it in "objective".
  "Help me prepare for my CSC236 test" means: find the course material,
  work out what the test covers, fit a study plan around their calendar,
  produce notes and practice problems, and set up reminders -- not "reply
  with study tips".
- Treat the request as a goal to be completed, not a single command. Include
  every step needed to actually finish it, including the verification that
  it worked.
- Use context: recent conversation, what's on screen, memories and learned
  lessons below. A remembered fact beats asking; a remembered failure is a
  path to avoid.
- Simple things stay simple: a one-tool request is ONE task. Conversation is
  ONE "answer" task. Don't pad plans."""

_CLARIFY_POLICY = """━━━ WHEN TO ASK ━━━
Return {"clarify": "..."} ONLY when missing information would materially
change the result AND no tool could find it (e.g. "email my professor" with no
way to know which professor). For low-risk ambiguity, make the most
reasonable assumption, write it in "assumptions", and proceed. Never ask
about things you can look up (files, calendar, memory, the web)."""

_ROUTING = """━━━ CHOOSING AGENTS ━━━
- "Remember that ..." / "note that I ..." / stable preferences -> memory.
- Anything about the user's files, including moving to Trash -> files.
- Documents, notes, study guides, practice problems, reports, drafts -> writer
  (give it the source material via depends_on).
- Code, builds, tests, git, debugging -> developer.
- Current facts, news, prices, anything on the open web -> research;
  weather/places/Wikipedia -> lookup.
- Apps, volume, music, clipboard, notifications, AppleScript -> system.
- Clicking/typing in app UIs, terminal work, screenshots -> automation.
- A single step that genuinely needs tools from several of these -> operator.
- Each task description must be concrete and self-contained: name the
  exact files, folders, dates, apps and outputs. The agent sees ONLY its
  own task plus results of its dependencies."""

_STRUCTURE = """━━━ STRUCTURE ━━━
- depends_on: ids that must finish first. Tasks with no dependency between
  them run IN PARALLEL -- use that for independent lookups.
- risk: your estimate -- read (only looks), low (creates something new),
  medium (changes existing things), high (sends, publishes, deletes
  permanently, installs, spends money, changes security). High-risk steps
  are always confirmed with the user by the system; plan them anyway when
  the user asked for them, and prefer reversible alternatives (drafts before
  sending, Trash before deleting).
- completion_criteria: how to tell the task is actually done.
- verification: how to check it (e.g. "file exists at ~/Documents/X.md and
  contains 3 sections", "event appears in calendar_list", "tests pass").
- model_tier: "fast" for trivial lookups, "deep" for hard reasoning, writing,
  analysis or code, otherwise "standard".
- optional: true if the objective can still be met without it.
- request_type: "conversation", "quick_action" (one direct action),
  "agentic" (multi-step work), "scheduled" (to run at a later time or on
  repeat -- fill "schedule"), or "background" (long work the user doesn't
  want to wait on).
- complexity: simple | standard | complex.
- At most """ + str(MAX_TASKS) + """ tasks."""

_SCHEMA = """━━━ OUTPUT ━━━
Exactly ONE raw JSON object, no markdown fences, no commentary:
{"objective": "...", "request_type": "agentic", "complexity": "standard",
 "assumptions": ["..."], "success_criteria": ["..."],
 "tasks": [{"id": 1, "agent": "...", "title": "short label",
            "description": "concrete self-contained instruction",
            "depends_on": [], "risk": "read", "completion_criteria": "...",
            "verification": "...", "model_tier": "standard", "optional": false}]}
For a scheduled request add:
 "schedule": {"kind": "task" | "notify", "at": "2026-09-21T09:00" | null, "in_minutes": 30 | null,
              "every_minutes": 1440 | null, "instruction": "what to do each time"}
kind "notify" is a timer/alarm -- "remind me in 20 minutes to stretch" -- and
its instruction is the message to show; kind "task" does real work when due.
and tasks may be empty (the instruction is planned fresh each time it runs).
Or, only when truly necessary: {"clarify": "one direct question"}"""

_EXAMPLE = """━━━ EXAMPLE (shape only) ━━━
"Help me prepare for my CSC236 test" ->
objective: "Prepare the user for the CSC236 test: materials found, coverage
identified, study plan scheduled, notes + practice problems written, reminders set."
tasks: 1 files: find CSC236 materials (notes, slides, past tests, syllabus) in
~/Documents, ~/Desktop, ~/Downloads (read) | 2 calendar: list events for the next
14 days, find the test date and free study blocks (read) | 3 writer (deps 1): read
the materials and write a topic coverage list | 4 writer (deps 2,3): study plan
day-by-day in ~/Documents/CSC236/Study Plan.md | 5 writer (deps 3): notes + practice
problems with worked solutions in ~/Documents/CSC236/Practice.md | 6 calendar
(deps 4): create reminders for each study session (low) -- tasks 1 and 2 run in
parallel."""


def _session_block() -> str:
    if not agent_session_live():
        return ""
    default = "agent" if DEFAULT_SESSION == "agent" else "primary"
    return f"""━━━ WHICH DESKTOP ━━━
This Mac has two logins. "agent" is the Jugnu account -- a separate macOS user
Jugnu fully controls, which the user switches to when they want to check
progress. "primary" is the user's own account, with their files, windows and
logins. Add "session" to the plan:
- "agent" when they ask for it ("on the Jugnu account", "in your account",
  "run it over there", "in the background so I can check later"), or for
  self-contained work that doesn't need their data.
- "primary" for THEIR things ("my files", "my Desktop", "the window I have open").
- Otherwise the default: "{default}".
The two accounts share only /Users/Shared."""


def _planner_system_prompt() -> str:
    agents_block = agent_capabilities_block() + "\n" + "\n".join(
        f"- {name}: {desc}" for name, desc in PSEUDO_AGENTS.items())
    blocks = [
        IDENTITY, date_block(), _ROLE, _INTERPRETATION, SITUATIONAL_AWARENESS,
        "━━━ AGENTS ━━━\n" + agents_block, _ROUTING, _STRUCTURE, _CLARIFY_POLICY,
        _session_block(), _EXAMPLE, _SCHEMA,
    ]
    return "\n\n".join(b for b in blocks if b)


# ---------------------------------------------------------------------------
# Validation -> Plan
# ---------------------------------------------------------------------------

def _valid_agents() -> set:
    return set(AGENTS) | set(PSEUDO_AGENTS)


def _validate(data: dict, goal_fallback: str) -> Plan:
    # Backwards compatibility with the old {"goal": ..., "tasks": ...} shape.
    if "objective" not in data and data.get("goal"):
        data["objective"] = data["goal"]
    data.setdefault("objective", goal_fallback[:600] or "Respond to the user")
    if data.get("clarify"):
        return Plan(goal=goal_fallback, tasks=[], clarify=str(data["clarify"])[:600],
                    objective=str(data.get("objective", goal_fallback))[:600])
    spec = PlanSpec(**data)
    unknown = [t.agent for t in spec.tasks if t.agent not in _valid_agents()]
    if unknown:
        raise ValueError(f"unknown agent(s) {unknown}; use only: {sorted(_valid_agents())}")
    tasks = [Task.from_spec(t) for t in spec.tasks]
    return Plan(goal=goal_fallback, tasks=tasks, session=spec.session, objective=spec.objective,
                request_type=spec.request_type, complexity=spec.complexity,
                assumptions=spec.assumptions, success_criteria=spec.success_criteria,
                schedule=spec.schedule)


def make_plan(goal: str, recent_context: str = "", retries: int = 1) -> Plan:
    """A validated plan for `goal`. Falls back to a single "answer" task if
    no valid plan can be produced, so a turn never fails at planning."""
    context_parts = []
    facts = facts_as_string()
    if facts:
        context_parts.append(facts)
    if recent_context:
        context_parts.append(f"Context:\n{recent_context}")
    skill_ids: list[str] = []
    try:
        import skill_library
        block, skill_ids = skill_library.planner_block(goal)
        if block:
            context_parts.append(block)
    except Exception as e:
        log(f"skill lookup failed: {e}")
    context_parts.append(f"User's request: {goal}")

    messages = [
        {"role": "system", "content": _planner_system_prompt()},
        {"role": "user", "content": "\n\n".join(context_parts)},
    ]
    try:
        plan = request_json(messages, role="PLANNER", retries=retries,
                            validate=lambda data: _validate(data, goal_fallback=goal))
        plan.skill_ids = skill_ids
        return plan
    except Exception as e:
        log(f"couldn't get a valid plan after retries ({e}) -- falling back to a single answer task")
        return Plan(goal=goal, tasks=[Task(id=1, agent="answer", description=goal)],
                    objective=goal, request_type="conversation", complexity="simple")


# ---------------------------------------------------------------------------
# Replanning
# ---------------------------------------------------------------------------

_REPLAN_ROLE = """You are Jugnu's PLANNER, revising a plan that hit a problem.
Some tasks are done, one failed or proved an assumption wrong. Produce ONLY the
remaining work needed to still reach the objective, as new tasks (ids must not
reuse existing ones; depends_on may reference completed task ids, whose results
the new tasks will receive). Fix the cause, don't repeat what failed: use a
different agent, tool, location or approach. Update "assumptions" if one was
wrong. If the objective genuinely can't be reached, set "abandon": true and
explain in "reason" -- a clear partial result beats a doomed retry."""


def replan(plan: Plan, why: str, recent_context: str = "") -> ReplanSpec | None:
    done = [t for t in plan.tasks if t.status == "done"]
    others = [t for t in plan.tasks if t.status in ("failed", "skipped", "blocked")]
    lines = [f"Objective: {plan.objective}"]
    if plan.assumptions:
        lines.append("Assumptions so far: " + "; ".join(plan.assumptions))
    lines.append("Completed tasks:")
    for t in done:
        lines.append(f"- #{t.id} [{t.agent}] {t.description}\n  Result: {t.result[:1200]}")
    lines.append("Tasks that did not complete:")
    for t in others:
        lines.append(f"- #{t.id} [{t.agent}] {t.description} -- {t.status}: {t.note or t.result[:600]}")
    lines.append(f"What went wrong: {why}")
    if recent_context:
        lines.append(f"Context:\n{recent_context[:3000]}")
    max_id = max([t.id for t in plan.tasks] or [0])
    schema = ('{"reason": "...", "assumptions": ["..."], "abandon": false, "tasks": [{"id": '
              f'{max_id + 1}, "agent": "...", "title": "...", "description": "...", "depends_on": [], '
              '"risk": "read", "completion_criteria": "...", "verification": "...", "model_tier": "standard"}]}')
    messages = [
        {"role": "system", "content": "\n\n".join([
            IDENTITY, date_block(), _REPLAN_ROLE,
            "━━━ AGENTS ━━━\n" + agent_capabilities_block() + "\n- answer: " + PSEUDO_AGENTS["answer"],
            _ROUTING, "Respond with exactly one JSON object:\n" + schema])},
        {"role": "user", "content": "\n".join(lines)},
    ]
    existing = {t.id for t in plan.tasks}
    completed = {t.id for t in done}

    def _validate_replan(data: dict) -> ReplanSpec:
        # Models often restart numbering at 1 or reuse the failed task's id.
        # That's a bookkeeping slip, not a planning error -- renumber the new
        # tasks past every existing id (keeping dependencies consistent)
        # rather than rejecting an otherwise good replan.
        raw_tasks = data.get("tasks") or []
        clash = any(isinstance(t, dict) and int(t.get("id", 0) or 0) in existing for t in raw_tasks)
        if clash:
            mapping, next_id = {}, max_id + 1
            for t in raw_tasks:
                if isinstance(t, dict):
                    mapping[int(t.get("id", 0) or 0)] = next_id
                    next_id += 1
            for t in raw_tasks:
                if isinstance(t, dict):
                    t["id"] = mapping[int(t.get("id", 0) or 0)]
                    t["depends_on"] = [mapping.get(int(d), int(d)) if int(d) not in completed else int(d)
                                       for d in (t.get("depends_on") or [])]
        spec = ReplanSpec(**data)
        if spec.abandon:
            return spec
        if not spec.tasks:
            raise ValueError("give at least one task, or set abandon: true")
        new_ids = {t.id for t in spec.tasks}
        if new_ids & existing:
            raise ValueError(f"task ids {sorted(new_ids & existing)} are already used; start at {max_id + 1}")
        for t in spec.tasks:
            if t.agent not in _valid_agents():
                raise ValueError(f"unknown agent {t.agent!r}")
            for d in t.depends_on:
                if d not in new_ids and d not in completed:
                    raise ValueError(f"task {t.id} depends on {d}, which is neither new nor completed")
        # Cycle check among the new tasks via PlanSpec's validator.
        PlanSpec(objective=plan.objective, tasks=[
            t.model_copy(update={"depends_on": [d for d in t.depends_on if d in new_ids]}) for t in spec.tasks])
        return spec

    try:
        return request_json(messages, role="PLANNER", retries=1, validate=_validate_replan)
    except Exception as e:
        log(f"replan failed: {e}")
        import audit
        audit.log("error", status="replan_invalid", summary=str(e)[:500])
        return None
