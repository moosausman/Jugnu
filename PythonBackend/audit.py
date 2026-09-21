"""
audit.py -- durable record of what Jugnu planned, did, and was allowed to do.

One SQLite table (audit.db in Jugnu's data folder). Every row is redacted
before it is written, so the log itself can never become the place a
secret leaks. Written synchronously -- a row per tool call is cheap, and
an audit trail that can lose its last entries on a crash defeats its
purpose.

kinds:
    turn_start / turn_end      a user request and its final answer
    plan                       the validated plan for a run
    tool_call / tool_result    every tool invocation and its outcome
    approval_request / approval_decision
    policy_block               a call refused by permissions
    error / recovery           failures and what was done about them
    verification               verifier verdicts
    reflection                 post-task learning summaries
    memory_change              memories created/updated/deleted
    injection_warning          untrusted content that looked like instructions
    code_change_proposal       proposed edits to Jugnu itself (never auto-applied)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing

from memory import APP_SUPPORT_DIR
from redact import redact, redact_obj

AUDIT_DB = APP_SUPPORT_DIR / "audit.db"
KEEP_DAYS = 120

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(AUDIT_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       REAL NOT NULL,
                kind     TEXT NOT NULL,
                run_id   TEXT,
                task_id  TEXT,
                tool     TEXT,
                risk     TEXT,
                status   TEXT,
                summary  TEXT,
                detail   TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")


_init()


def log(kind: str, *, run_id: str | None = None, task_id=None, tool: str | None = None,
        risk: str | None = None, status: str | None = None, summary: str = "",
        detail=None) -> int | None:
    """Appends one audit row. Never raises -- auditing must not break a turn."""
    try:
        detail_text = None
        if detail is not None:
            detail_text = json.dumps(redact_obj(detail), default=str)[:20000]
        with _lock, closing(_connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO events (ts, kind, run_id, task_id, tool, risk, status, summary, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), kind, run_id, None if task_id is None else str(task_id), tool, risk,
                 status, redact(summary or "")[:2000], detail_text),
            )
            return cur.lastrowid
    except Exception:
        return None


def recent(limit: int = 200, run_id: str | None = None, kinds: list | None = None) -> list[dict]:
    query = "SELECT * FROM events"
    clauses, params = [], []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if kinds:
        clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        if item.get("detail"):
            try:
                item["detail"] = json.loads(item["detail"])
            except json.JSONDecodeError:
                pass
        out.append(item)
    return out


def prune(keep_days: int = KEEP_DAYS) -> int:
    cutoff = time.time() - keep_days * 86400
    with _lock, closing(_connect()) as conn, conn:
        cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return cur.rowcount


def clear() -> None:
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM events")
