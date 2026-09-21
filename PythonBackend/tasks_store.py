"""
tasks_store.py -- durable runs: checkpoints, resumption, history.

Every run (a user request, a background workflow, a scheduled job) is a row
here with its full plan -- every task's status, attempts, result and note --
plus the shared working memory (results passed between tasks). The executor
checkpoints after every task, so if the app or backend stops mid-run:

  * the run is found at next startup with status "running"/"queued" and
    marked "interrupted"
  * resume() rebuilds the Plan, turns unfinished tasks back into pending
    ones (completed tasks and their results are kept -- nothing is redone),
    and runs the remainder

A run can also be "waiting_user": it stopped to ask a question. The user's
next reply continues it (session.py) instead of starting from scratch.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing

from memory import APP_SUPPORT_DIR
from redact import redact

DB_FILE = APP_SUPPORT_DIR / "runs.db"
KEEP_DAYS = 60
ACTIVE_STATES = ("queued", "planning", "running", "paused")
RESUMABLE_STATES = ("interrupted", "waiting_user")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id      TEXT PRIMARY KEY,
                request_id  TEXT,
                origin      TEXT NOT NULL,
                goal        TEXT NOT NULL,
                objective   TEXT,
                status      TEXT NOT NULL,
                background  INTEGER NOT NULL DEFAULT 0,
                session     TEXT,
                plan        TEXT,
                shared      TEXT,
                context     TEXT,
                answer      TEXT,
                question    TEXT,
                error       TEXT,
                schedule_id TEXT,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                finished_at REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at)")


_init()


def _j(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row(r: sqlite3.Row, full: bool = True) -> dict:
    d = dict(r)
    d["background"] = bool(d["background"])
    d["plan"] = _j(d.get("plan"), None) if full else None
    d["shared"] = _j(d.get("shared"), {}) if full else None
    if not full:
        d.pop("context", None)
    else:
        d["shared"] = {int(k) if str(k).isdigit() else k: v for k, v in (d["shared"] or {}).items()}
    return d


def create(run_id: str, goal: str, *, origin: str = "user", request_id: str = "", background: bool = False,
           context: str = "", schedule_id: str | None = None, status: str = "planning") -> None:
    now = time.time()
    with _lock, closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, request_id, origin, goal, status, background, context, "
            "schedule_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, request_id, origin, redact(goal)[:4000], status, int(background),
             redact(context or "")[:20000], schedule_id, now, now))


def checkpoint(run_id: str, plan, shared: dict | None = None, status: str | None = None,
               session: str | None = None) -> None:
    """Persists the plan (with every task's state) and shared results."""
    plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else plan
    # Task results are needed to resume; they're kept (redacted).
    if hasattr(plan, "tasks"):
        for t, td in zip(plan.tasks, plan_dict["tasks"]):
            td["result"] = redact(getattr(t, "result", "") or "")[:8000]
    fields = {"plan": json.dumps(plan_dict, default=str), "updated_at": time.time(),
              "objective": plan_dict.get("objective", "")}
    if shared is not None:
        fields["shared"] = json.dumps({str(k): redact(str(v))[:8000] for k, v in shared.items()})
    if status:
        fields["status"] = status
    if session:
        fields["session"] = session
    _update(run_id, **fields)


def set_status(run_id: str, status: str, **extra) -> None:
    _update(run_id, status=status, updated_at=time.time(), **extra)


def finish(run_id: str, status: str, answer: str = "", error: str = "") -> None:
    now = time.time()
    _update(run_id, status=status, answer=redact(answer or "")[:20000], error=redact(error or "")[:2000],
            updated_at=now, finished_at=now, question=None)


def wait_for_user(run_id: str, question: str) -> None:
    _update(run_id, status="waiting_user", question=redact(question)[:2000], updated_at=time.time())


def _update(run_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock, closing(_connect()) as conn, conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE run_id = ?", (*fields.values(), run_id))


def get(run_id: str) -> dict | None:
    with closing(_connect()) as conn:
        r = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _row(r) if r else None


def list_runs(limit: int = 50, statuses: tuple | None = None, full: bool = False) -> list[dict]:
    sql = "SELECT * FROM runs"
    params: list = []
    if statuses:
        sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(int(limit))
    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = _row(r, full=full)
        if not full:
            plan = _j(r["plan"], None)
            tasks = (plan or {}).get("tasks", [])
            d["task_count"] = len(tasks)
            d["tasks_done"] = sum(1 for t in tasks if t.get("status") == "done")
            d["tasks"] = [{k: t.get(k) for k in ("id", "title", "agent", "status", "note")} for t in tasks]
        out.append(d)
    return out


def pending_question(max_age_seconds: float = 3 * 3600) -> dict | None:
    """The most recent foreground run waiting on the user's answer."""
    cutoff = time.time() - max_age_seconds
    with closing(_connect()) as conn:
        r = conn.execute("SELECT * FROM runs WHERE status = 'waiting_user' AND updated_at >= ? "
                         "ORDER BY updated_at DESC LIMIT 1", (cutoff,)).fetchone()
    return _row(r) if r else None


def mark_interrupted() -> list[dict]:
    """At startup: anything that was mid-flight when the backend stopped."""
    with _lock, closing(_connect()) as conn, conn:
        rows = conn.execute(f"SELECT run_id FROM runs WHERE status IN ({','.join('?' * len(ACTIVE_STATES))})",
                            ACTIVE_STATES).fetchall()
        ids = [r["run_id"] for r in rows]
        if ids:
            conn.execute(f"UPDATE runs SET status = 'interrupted', updated_at = ? "
                         f"WHERE run_id IN ({','.join('?' * len(ids))})", (time.time(), *ids))
    return [get(i) for i in ids]


def prepare_resume(run: dict):
    """(Plan, shared) ready to continue: unfinished tasks become pending
    again, finished ones keep their results."""
    from planner import Plan
    plan = Plan.from_dict(run["plan"])
    for t in plan.tasks:
        if t.status in ("running", "verifying", "queued", "blocked", "cancelled"):
            t.status = "pending"
            t.attempts = max(0, t.attempts - 1)
    shared = dict(run.get("shared") or {})
    for t in plan.tasks:
        if t.status == "done" and t.id not in shared:
            shared[t.id] = t.result
    return plan, shared


def prune(keep_days: int = KEEP_DAYS) -> int:
    cutoff = time.time() - keep_days * 86400
    with _lock, closing(_connect()) as conn, conn:
        return conn.execute("DELETE FROM runs WHERE updated_at < ? AND status NOT IN ('waiting_user')",
                            (cutoff,)).rowcount


def delete(run_id: str) -> None:
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
