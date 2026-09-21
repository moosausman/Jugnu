"""
skill_library.py -- reusable workflows Jugnu has learned (procedural memory
at the level of whole goals).

skills.py remembers low-level *tool routes* per agent ("playing a song is
resolve -> play -> verify"). This is the level above: a named multi-step
workflow built from a plan that worked -- "Prepare for a test", "Organize
my downloads", "Weekly review" -- with:

    name, purpose, trigger phrases     how it's recognised
    inputs, preconditions              what it needs before it can run
    steps (agent, instruction, deps)   the plan template, with parallelism
    tools, risk                        what it touches and how dangerous
    expected_output, verification      what "done" looks like
    version + history                  every edit keeps the previous version
    successes / failures               how reliable it has been

Lifecycle: reflection proposes a skill as "suggested" -- it does nothing
until the user accepts it ("active"). Active skills are offered to the
planner when a request matches, as a starting point it adapts (never a
script it must follow). The user can edit, disable or delete any skill;
skills that keep failing are flagged for review rather than silently used.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing

import audit
from memory import APP_SUPPORT_DIR
from redact import contains_secret, redact

DB_FILE = APP_SUPPORT_DIR / "skill_library.db"
STATUSES = ("suggested", "active", "disabled")
RISK_ORDER = {"read": 0, "low": 1, "medium": 2, "high": 3}
FLAG_FAILURE_RATE = 0.5
MIN_USES_BEFORE_FLAG = 3

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#-]*")
_STOP = frozenset("a an the and or of to in on at for with my me i you your it this that is be do".split())
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                purpose         TEXT NOT NULL,
                trigger_phrases TEXT,
                inputs          TEXT,
                preconditions   TEXT,
                steps           TEXT NOT NULL,
                tools           TEXT,
                risk            TEXT NOT NULL DEFAULT 'low',
                expected_output TEXT,
                verification    TEXT,
                status          TEXT NOT NULL DEFAULT 'suggested',
                version         INTEGER NOT NULL DEFAULT 1,
                history         TEXT,
                successes       INTEGER NOT NULL DEFAULT 0,
                failures        INTEGER NOT NULL DEFAULT 0,
                source_run_id   TEXT,
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL,
                last_used_at    REAL
            )
        """)


_init()

_JSON_FIELDS = ("trigger_phrases", "inputs", "preconditions", "steps", "tools", "history")


def _row(r) -> dict:
    d = dict(r)
    for f in _JSON_FIELDS:
        try:
            d[f] = json.loads(d.get(f) or "[]")
        except json.JSONDecodeError:
            d[f] = []
    uses = d["successes"] + d["failures"]
    d["success_rate"] = round(d["successes"] / uses, 3) if uses else None
    d["needs_review"] = uses >= MIN_USES_BEFORE_FLAG and d["success_rate"] is not None \
        and (1 - d["success_rate"]) >= FLAG_FAILURE_RATE
    return d


def _tokens(text: str) -> set:
    return {t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 1}


def steps_from_plan(plan) -> list[dict]:
    """A plan's tasks, as a reusable step template."""
    steps = []
    for t in plan.tasks:
        if t.status not in ("done",) and not getattr(t, "optional", False):
            continue
        steps.append({"id": t.id, "agent": t.agent, "title": t.title, "instruction": redact(t.description)[:1500],
                      "depends_on": [d for d in t.depends_on], "risk": t.risk,
                      "completion_criteria": t.completion_criteria})
    ids = {s["id"] for s in steps}
    for s in steps:
        s["depends_on"] = [d for d in s["depends_on"] if d in ids]
    return steps


def _max_risk(steps: list) -> str:
    return max((s.get("risk", "low") for s in steps), key=lambda r: RISK_ORDER.get(r, 1), default="low")


# ---------------------------------------------------------------------------
# Create / update
# ---------------------------------------------------------------------------

def propose(candidate, plan, run_id: str | None = None, tools: list | None = None) -> dict | None:
    """Stores a skill suggested by reflection (status 'suggested'). If a
    skill with the same name exists, the proposal becomes a new version of
    it only if it's still suggested; active skills are never overwritten by
    reflection -- the user owns them."""
    steps = steps_from_plan(plan)
    if len(steps) < 2:
        return None  # a one-step "workflow" is just a tool call
    blob = json.dumps(candidate.model_dump(), default=str) + json.dumps(steps)
    if contains_secret(blob):
        return None
    existing = find_by_name(candidate.name)
    if existing and existing["status"] != "suggested":
        return None
    now = time.time()
    fields = {
        "name": candidate.name.strip()[:80], "purpose": candidate.purpose.strip()[:400],
        "trigger_phrases": json.dumps(candidate.trigger_phrases[:8]), "inputs": json.dumps(candidate.inputs[:10]),
        "preconditions": json.dumps(candidate.preconditions[:10]), "steps": json.dumps(steps),
        "tools": json.dumps(sorted(set(tools or []))[:40]), "risk": _max_risk(steps),
        "expected_output": candidate.expected_output[:400], "verification": candidate.verification[:400],
    }
    with _lock, closing(_connect()) as conn, conn:
        if existing:
            _snapshot(conn, existing)
            conn.execute(
                f"UPDATE skills SET {', '.join(f'{k} = ?' for k in fields)}, version = version + 1, updated_at = ?, "
                "source_run_id = ? WHERE id = ?", (*fields.values(), now, run_id, existing["id"]))
            skill_id = existing["id"]
        else:
            skill_id = uuid.uuid4().hex[:10]
            conn.execute(
                f"INSERT INTO skills (id, {', '.join(fields)}, status, version, history, created_at, updated_at, "
                f"source_run_id) VALUES (?, {', '.join('?' * len(fields))}, 'suggested', 1, '[]', ?, ?, ?)",
                (skill_id, *fields.values(), now, now, run_id))
    audit.log("skill_change", run_id=run_id, status="suggested", summary=fields["name"], detail={"id": skill_id})
    return get(skill_id)


def _snapshot(conn, current: dict) -> None:
    """Pushes the current version onto the skill's history."""
    history = current.get("history") or []
    snap = {k: current[k] for k in ("version", "name", "purpose", "trigger_phrases", "inputs", "preconditions",
                                    "steps", "expected_output", "verification", "risk", "updated_at")}
    history = (history + [snap])[-20:]
    conn.execute("UPDATE skills SET history = ? WHERE id = ?", (json.dumps(history), current["id"]))


def update(skill_id: str, **changes) -> dict:
    """A user edit: any of name, purpose, trigger_phrases, inputs,
    preconditions, steps, expected_output, verification, status. Every edit
    creates a new version; the previous one is kept in history."""
    current = get(skill_id)
    if not current:
        raise KeyError(skill_id)
    allowed = {"name", "purpose", "trigger_phrases", "inputs", "preconditions", "steps", "expected_output",
               "verification", "status"}
    fields = {}
    for k, v in changes.items():
        if k not in allowed or v is None:
            continue
        if k == "status" and v not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if k == "steps":
            if not isinstance(v, list) or not v:
                raise ValueError("steps must be a non-empty list")
            fields["risk"] = _max_risk(v)
        fields[k] = json.dumps(v) if k in _JSON_FIELDS else str(v)
    if not fields:
        return current
    if contains_secret(json.dumps(fields)):
        raise ValueError("refusing to store something that looks like a secret")
    content_change = any(k != "status" for k in fields)
    with _lock, closing(_connect()) as conn, conn:
        if content_change:
            _snapshot(conn, current)
        conn.execute(
            f"UPDATE skills SET {', '.join(f'{k} = ?' for k in fields)}, updated_at = ?"
            + (", version = version + 1" if content_change else "") + " WHERE id = ?",
            (*fields.values(), time.time(), skill_id))
    audit.log("skill_change", status="edited", summary=current["name"], detail={"id": skill_id, "fields": list(fields)})
    return get(skill_id)


def revert(skill_id: str, version: int) -> dict:
    current = get(skill_id)
    if not current:
        raise KeyError(skill_id)
    snap = next((h for h in current["history"] if h.get("version") == version), None)
    if not snap:
        raise ValueError(f"no version {version} in history")
    return update(skill_id, **{k: snap[k] for k in ("name", "purpose", "trigger_phrases", "inputs",
                                                    "preconditions", "steps", "expected_output", "verification")})


def delete(skill_id: str) -> bool:
    with _lock, closing(_connect()) as conn, conn:
        n = conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,)).rowcount
    if n:
        audit.log("skill_change", status="deleted", detail={"id": skill_id})
    return bool(n)


def record_use(skill_id: str, succeeded: bool) -> None:
    col = "successes" if succeeded else "failures"
    with _lock, closing(_connect()) as conn, conn:
        conn.execute(f"UPDATE skills SET {col} = {col} + 1, last_used_at = ? WHERE id = ?", (time.time(), skill_id))


# ---------------------------------------------------------------------------
# Read / match
# ---------------------------------------------------------------------------

def get(skill_id: str) -> dict | None:
    with closing(_connect()) as conn:
        r = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    return _row(r) if r else None


def find_by_name(name: str) -> dict | None:
    with closing(_connect()) as conn:
        r = conn.execute("SELECT * FROM skills WHERE lower(name) = lower(?)", (name.strip(),)).fetchone()
    return _row(r) if r else None


def list_all(status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM skills"
    params = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY (status = 'active') DESC, updated_at DESC"
    with closing(_connect()) as conn:
        return [_row(r) for r in conn.execute(sql, params).fetchall()]


def match(request: str, limit: int = 2, min_score: float = 0.34) -> list[dict]:
    """Active skills relevant to a request, best first."""
    q = _tokens(request)
    if not q:
        return []
    skills = [s for s in list_all("active") if not s["needs_review"]]
    if not skills:
        return []
    docs = [(s, _tokens(" ".join([s["name"], s["purpose"], " ".join(s["trigger_phrases"])]))) for s in skills]
    df: dict = {}
    for _, toks in docs:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    scored = []
    for s, toks in docs:
        overlap = q & toks
        if not overlap:
            continue
        idf = sum(math.log(1 + n / df[t]) for t in overlap)
        denom = sum(math.log(1 + n / df.get(t, 1)) for t in q) or 1
        score = idf / denom
        phrase_hit = any(p.lower() in request.lower() for p in s["trigger_phrases"] if len(p) > 3)
        if phrase_hit:
            score = max(score, 0.9)
        if score >= min_score:
            scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [dict(s, match_score=round(sc, 3)) for sc, s in scored[:limit]]


def planner_block(request: str) -> tuple[str, list[str]]:
    """Prompt text describing matching skills, plus their ids (so the run
    can credit or debit them afterwards)."""
    matches = match(request)
    if not matches:
        return "", []
    lines = ["━━━ LEARNED SKILLS THAT MATCH ━━━",
             "Workflows that worked before for requests like this. Use one as the starting point for your plan "
             "-- adapt it to this request (different inputs, missing steps, changed circumstances); don't copy "
             "blindly."]
    for s in matches:
        rate = f"{int(s['success_rate'] * 100)}% success over {s['successes'] + s['failures']} uses" \
            if s["success_rate"] is not None else "not used yet"
        lines.append(f"\nSkill \"{s['name']}\" (v{s['version']}, {rate}): {s['purpose']}")
        if s["inputs"]:
            lines.append("  needs: " + "; ".join(s["inputs"]))
        if s["preconditions"]:
            lines.append("  preconditions: " + "; ".join(s["preconditions"]))
        for st in s["steps"]:
            deps = f" (after {', '.join(map(str, st['depends_on']))})" if st.get("depends_on") else ""
            lines.append(f"  {st['id']}. [{st['agent']}] {st['instruction'][:300]}{deps}")
        if s["verification"]:
            lines.append(f"  done when: {s['verification']}")
    return "\n".join(lines), [s["id"] for s in matches]
