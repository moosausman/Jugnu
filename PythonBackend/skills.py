"""
skills.py -- procedural memory. Jugnu remembering HOW to do things.

THE PROBLEM
-----------
memory.py remembers what the user said. semantic_memory.py remembers what
happened. Neither remembers how a job was actually done, so every task was
re-derived from nothing, every time.

That re-derivation is the lag. A multi-step task -- open WhatsApp, find the
search bar, search, find the row, open it, find the composer, draft, refresh,
send -- costs eight or nine model round trips before anything is achieved,
each one carrying a large accessibility snapshot back into context. Against
executor.py's MAX_TASK_ITERATIONS of 15 that regularly ran out partway, which
is exactly the "reached my tool limit" failure that keeps showing up. The
second, third and tenth attempts cost precisely as much as the first,
because nothing was learned.

THE IDEA
--------
When a task succeeds, write down the sequence of tools that achieved it,
keyed by what was being attempted. When a similar task comes round again,
hand that sequence to the agent up front as a starting point.

The agent still decides. A recalled skill is a hint, never a script:
arguments differ, apps change, and the model is better placed than this
module to notice when the old route no longer fits. What it removes is the
*search* -- knowing that playing a song means resolve-then-play-then-verify
is most of the work, and it only has to be discovered once.

WHY NOT JUST REPLAY THE CALLS
-----------------------------
Because a lot of arguments are single-use. Accessibility element IDs expire
after 120 seconds; replaying one is worse than having nothing, since it
looks valid and fails confusingly. `_generalize` strips those to
placeholders, so what's stored is the SHAPE of the solution -- which tools,
in which order, with which stable arguments -- rather than a recording that
can only ever work once.

COST
----
Recall is one embedding plus a cosine over a small table, on an indexed
query, and it is the only thing on the critical path. Recording happens on
semantic_memory's existing background writer, so a finished task never waits
for it. Everything degrades to silence on failure: a broken skill store must
never be the reason a task fails.
"""

from __future__ import annotations

import json
import re
import sys
import time
from contextlib import closing

import numpy as np

import semantic_memory as smem

# Reuse semantic_memory's database, connection helper, embedding layer and
# background writer rather than standing up a second set of all of them.
_connect = smem._connect


def log(message: str) -> None:
    print(f"[skills] {message}", file=sys.stderr, flush=True)


# A skill has to clear this similarity bar before it's offered. Set too low,
# every task gets handed an irrelevant procedure and the hint becomes noise
# that actively misleads; the cost of missing a match is just today's normal
# behaviour, so this errs high deliberately.
MIN_SIMILARITY = 0.62

# Procedures longer than this are almost certainly the model flailing rather
# than a clean route worth repeating.
MAX_STEPS = 12

# Below this success rate a skill is doing more harm than good.
MIN_SUCCESS_RATE = 0.4
MIN_ATTEMPTS_BEFORE_JUDGING = 3


def _init_db() -> None:
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal       TEXT NOT NULL,
                    agent      TEXT NOT NULL,
                    steps      TEXT NOT NULL,   -- JSON [{tool, args}]
                    embedding  TEXT,            -- JSON float list, nullable
                    successes  INTEGER NOT NULL DEFAULT 1,
                    failures   INTEGER NOT NULL DEFAULT 0,
                    created    REAL NOT NULL,
                    last_used  REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_agent ON skills(agent)")
    except Exception as e:
        log(f"could not initialise skill store: {e}")


_init_db()


# --- storing ---------------------------------------------------------------

# Arguments that are only valid for seconds, or that are specific to one run.
# Storing their values would produce a procedure that looks reusable and
# isn't -- an expired elementId is worse than no hint at all, because the
# agent tries it and gets a confusing failure instead of simply looking.
_VOLATILE_ARG_NAMES = {
    "element_id", "elementid", "header_element_id", "editor_element_id",
    "send_element_id", "search_element_id", "job_id", "jobid",
    "message_id", "id", "pid",
}
# Deliberately NOT volatile: "uri". A spotify:track: or https: URI is a
# durable identifier, and for a task like "play Blinding Lights" it is the
# single most valuable thing learned -- redacting it throws away the answer
# and forces the lookup to happen again every time, which is the cost this
# module exists to remove. Genuinely single-use identifiers are caught by
# name above or by shape below.

# Values that look machine-generated and single-use regardless of key name.
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", re.ASCII)
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-fA-F-]{20,}:\d+$", re.ASCII)


def _generalize(args: dict) -> dict:
    """Replace single-use values with placeholders, keep the stable shape."""
    out: dict = {}
    for key, value in (args or {}).items():
        lowered = key.lower()
        if lowered in _VOLATILE_ARG_NAMES:
            out[key] = f"<{key}: look this up fresh>"
            continue
        if isinstance(value, str) and (_UUID_RE.match(value) or _SNAPSHOT_ID_RE.match(value)):
            out[key] = f"<{key}: look this up fresh>"
            continue
        if isinstance(value, str) and len(value) > 120:
            out[key] = value[:120] + "…"
            continue
        out[key] = value
    return out


def record(goal: str, agent: str, steps: list[dict], succeeded: bool = True) -> None:
    """Remember (or forget) how a task went. Returns immediately; the write
    happens on semantic_memory's background thread."""
    goal = (goal or "").strip()
    if not goal or not agent:
        return
    # A task solved with no tools teaches nothing about procedure.
    if succeeded and not steps:
        return
    smem._ensure_worker()
    smem._write_queue.put(lambda: _record_sync(goal, agent, steps, succeeded))


def _record_sync(goal: str, agent: str, steps: list[dict], succeeded: bool) -> None:
    try:
        generalized = [
            {"tool": s.get("tool"), "args": _generalize(s.get("args") or {})}
            for s in steps[:MAX_STEPS]
            if s.get("tool")
        ]
        vector = smem._embed(goal)

        with closing(_connect()) as conn, conn:
            # Merge into an existing skill for the same agent and a near-identical
            # goal, rather than accumulating one row per phrasing of the same job.
            existing = _best_match_row(conn, goal, agent, vector, threshold=0.90)
            if existing:
                if succeeded:
                    conn.execute(
                        "UPDATE skills SET successes = successes + 1, last_used = ?, "
                        "steps = ? WHERE id = ?",
                        (time.time(), json.dumps(generalized), existing["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE skills SET failures = failures + 1, last_used = ? WHERE id = ?",
                        (time.time(), existing["id"]),
                    )
                return

            if not succeeded:
                return  # nothing to learn from a first attempt that failed

            now = time.time()
            conn.execute(
                "INSERT INTO skills (goal, agent, steps, embedding, created, last_used) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (goal, agent, json.dumps(generalized),
                 json.dumps(vector) if vector else None, now, now),
            )
    except Exception as e:
        log(f"failed to record skill: {e}")


# --- recalling -------------------------------------------------------------

def _best_match_row(conn, goal: str, agent: str, vector, threshold: float):
    """Closest stored skill for this agent, or None. Embedding similarity
    when available, keyword overlap when it isn't -- the same graceful
    degradation semantic_memory.recall() uses, so this keeps working with no
    embedding model installed."""
    rows = conn.execute(
        "SELECT id, goal, steps, embedding, successes, failures FROM skills "
        "WHERE agent = ? ORDER BY last_used DESC LIMIT 200",
        (agent,),
    ).fetchall()
    if not rows:
        return None

    scored = []
    if vector:
        expected = len(vector)
        usable, vectors = [], []
        for row in rows:
            if not row["embedding"]:
                continue
            try:
                v = json.loads(row["embedding"])
            except (json.JSONDecodeError, TypeError):
                continue
            if len(v) != expected:
                continue          # embedding model changed; old rows unusable
            usable.append(row)
            vectors.append(v)
        if usable:
            matrix = np.asarray(vectors, dtype=np.float32)
            for score, row in zip(smem._cosine_against(vector, matrix), usable):
                scored.append((score, row))

    if not scored:
        words = set(smem._WORD_RE.findall(goal.lower()))
        for row in rows:
            scored.append((smem._keyword_score(words, row["goal"]), row))

    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best_row = scored[0]
    return best_row if best_score >= threshold else None


def recall(goal: str, agent: str) -> dict | None:
    """The most relevant known-good procedure for this task, or None.

    Skips skills that have been failing: one that no longer works is worse
    than none, since it sends the agent confidently down a dead route.
    """
    goal = (goal or "").strip()
    if not goal or not agent:
        return None
    try:
        vector = smem._embed(goal)
        with closing(_connect()) as conn:
            row = _best_match_row(conn, goal, agent, vector, threshold=MIN_SIMILARITY)
            if not row:
                return None

            attempts = row["successes"] + row["failures"]
            if attempts >= MIN_ATTEMPTS_BEFORE_JUDGING:
                if row["successes"] / attempts < MIN_SUCCESS_RATE:
                    return None

            return {
                "id": row["id"],
                "goal": row["goal"],
                "steps": json.loads(row["steps"]),
                "successes": row["successes"],
                "failures": row["failures"],
            }
    except Exception as e:
        log(f"recall failed: {e}")
        return None


def as_prompt_block(skill: dict) -> str:
    """Format a recalled skill for the agent's context.

    Worded as a starting point rather than an instruction on purpose. The
    agent has to stay free to deviate: apps change, arguments differ, and
    the placeholders below are deliberately not real values.
    """
    lines = [
        "━━━ YOU HAVE DONE THIS BEFORE ━━━",
        f"A similar task ({skill['goal']!r}) was completed successfully "
        f"{skill['successes']}x using this sequence:",
        "",
    ]
    for i, step in enumerate(skill["steps"], 1):
        args = step.get("args") or {}
        rendered = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
        lines.append(f"  {i}. {step['tool']}({rendered})")
    lines += [
        "",
        "Start from this instead of working it out again -- that rediscovery is "
        "what makes these tasks slow and what exhausts the tool budget.",
        "",
        "It is a route, not a script. Anything shown as <...: look this up fresh> "
        "was only valid for the run it came from (element IDs expire in 120 "
        "seconds) and must be fetched again now. If the app has changed or the "
        "steps clearly don't fit this request, ignore this and work it out -- "
        "you'll be recorded doing the better version.",
    ]
    return "\n".join(lines)


def feedback(skill_id: int, worked: bool) -> None:
    """Record whether a recalled skill actually helped."""
    smem._ensure_worker()

    def _job() -> None:
        try:
            column = "successes" if worked else "failures"
            with closing(_connect()) as conn, conn:
                conn.execute(
                    f"UPDATE skills SET {column} = {column} + 1, last_used = ? WHERE id = ?",
                    (time.time(), skill_id),
                )
        except Exception as e:
            log(f"feedback failed: {e}")

    smem._write_queue.put(_job)


# --- inspection / housekeeping --------------------------------------------

def list_skills(limit: int = 50) -> list[dict]:
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT id, goal, agent, steps, successes, failures, last_used "
                "FROM skills ORDER BY successes DESC, last_used DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": r["id"], "goal": r["goal"], "agent": r["agent"],
             "steps": len(json.loads(r["steps"])),
             "successes": r["successes"], "failures": r["failures"]}
            for r in rows
        ]
    except Exception as e:
        log(f"list failed: {e}")
        return []


def clear_all() -> None:
    """Forget every learned procedure. Paired with bridge.py's reset_memory."""
    smem._flush()
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("DELETE FROM skills")
    except Exception as e:
        log(f"clear failed: {e}")
