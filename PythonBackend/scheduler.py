"""
scheduler.py -- things the user asked Jugnu to do later, or on repeat.

Two kinds of schedule:
    task     an instruction run as a full background workflow when due
             ("every weekday at 8am, prepare me for the day")
    notify   a timer/alarm: a notification (and a chat message) when due
             ("remind me in 25 minutes to stretch")

Stored in runs.db, so schedules survive restarts. The loop (SchedulerLoop)
checks every few seconds. A schedule that came due while Jugnu wasn't
running is caught up once if it was missed by less than CATCH_UP_HOURS,
otherwise skipped to its next occurrence -- a stale "remind me in 5
minutes" from yesterday shouldn't fire today.

Scheduled workflows count as user-initiated (the user set them up), so they
run under the normal autonomy mode -- and, like everything else, anything
consequential still waits for an explicit approval.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import closing

import audit
from tasks_store import DB_FILE

CATCH_UP_HOURS = 12
MIN_INTERVAL_MINUTES = 1

_lock = threading.Lock()


def log(message: str) -> None:
    print(f"[scheduler] {message}", file=sys.stderr, flush=True)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id            TEXT PRIMARY KEY,
                kind          TEXT NOT NULL DEFAULT 'task',
                title         TEXT NOT NULL,
                instruction   TEXT NOT NULL,
                next_run      REAL,
                every_seconds REAL,
                until         REAL,
                enabled       INTEGER NOT NULL DEFAULT 1,
                session       TEXT,
                created_at    REAL NOT NULL,
                last_run      REAL,
                last_status   TEXT,
                last_run_id   TEXT,
                run_count     INTEGER NOT NULL DEFAULT 0
            )
        """)


_init()


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def parse_when(at: str | None = None, in_minutes: float | None = None, now: float | None = None) -> float:
    """Epoch seconds for a schedule's first run. `at` accepts ISO-8601
    ("2026-09-21T09:00", with or without seconds/offset) or a bare time of
    day ("09:00", "9:30pm") meaning its next occurrence."""
    now = now or time.time()
    if in_minutes is not None and in_minutes >= 0:
        return now + float(in_minutes) * 60
    if not at:
        raise ValueError("a schedule needs 'at' or 'in_minutes'")
    text = at.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.timestamp()          # naive = local time
        return parsed.timestamp()
    except ValueError:
        pass
    for fmt in ("%H:%M", "%I:%M%p", "%I:%M %p", "%I%p", "%I %p"):
        try:
            t = dt.datetime.strptime(text.upper().replace(".", ""), fmt).time()
        except ValueError:
            continue
        today = dt.datetime.fromtimestamp(now)
        candidate = today.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if candidate.timestamp() <= now:
            candidate += dt.timedelta(days=1)
        return candidate.timestamp()
    raise ValueError(f"couldn't understand the time {at!r}; use ISO format like 2026-09-21T09:00 or HH:MM")


def _fmt(ts: float | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts).strftime("%a %d %b %Y, %H:%M")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add(instruction: str, *, at: str | None = None, in_minutes: float | None = None,
        every_minutes: float | None = None, until: str | None = None, kind: str = "task",
        title: str = "", session: str | None = None) -> dict:
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction is required")
    if kind not in ("task", "notify"):
        raise ValueError("kind must be 'task' or 'notify'")
    if at is None and in_minutes is None:
        in_minutes = 0 if kind == "task" else None
    next_run = parse_when(at, in_minutes)
    every = None
    if every_minutes:
        if float(every_minutes) < MIN_INTERVAL_MINUTES:
            raise ValueError(f"repeat interval must be at least {MIN_INTERVAL_MINUTES} minute")
        every = float(every_minutes) * 60
    until_ts = parse_when(until) if until else None
    item = {
        "id": uuid.uuid4().hex[:10], "kind": kind, "title": (title or instruction)[:120],
        "instruction": instruction[:2000], "next_run": next_run, "every_seconds": every,
        "until": until_ts, "enabled": 1, "session": session, "created_at": time.time(),
    }
    with _lock, closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO schedules (id, kind, title, instruction, next_run, every_seconds, until, enabled, "
            "session, created_at) VALUES (:id, :kind, :title, :instruction, :next_run, :every_seconds, :until, "
            ":enabled, :session, :created_at)", item)
    audit.log("schedule_created", summary=f"{kind}: {item['title']} at {_fmt(next_run)}",
              detail={"id": item["id"], "every_minutes": every_minutes})
    return describe(get(item["id"]))


def get(schedule_id: str) -> dict | None:
    with closing(_connect()) as conn:
        r = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return dict(r) if r else None


def describe(item: dict | None) -> dict | None:
    if not item:
        return None
    d = dict(item)
    d["enabled"] = bool(d["enabled"])
    d["next_run_text"] = _fmt(d.get("next_run"))
    d["last_run_text"] = _fmt(d.get("last_run"))
    d["repeat_minutes"] = round(d["every_seconds"] / 60, 1) if d.get("every_seconds") else None
    return d


def list_schedules(include_disabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM schedules"
    if not include_disabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY COALESCE(next_run, 9e18) ASC"
    with closing(_connect()) as conn:
        return [describe(dict(r)) for r in conn.execute(sql).fetchall()]


def cancel(schedule_id: str) -> bool:
    with _lock, closing(_connect()) as conn, conn:
        n = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,)).rowcount
    if n:
        audit.log("schedule_deleted", summary=schedule_id)
    return bool(n)


def set_enabled(schedule_id: str, enabled: bool) -> bool:
    with _lock, closing(_connect()) as conn, conn:
        return bool(conn.execute("UPDATE schedules SET enabled = ? WHERE id = ?",
                                 (1 if enabled else 0, schedule_id)).rowcount)


def due(now: float | None = None) -> list[dict]:
    now = now or time.time()
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ? "
            "ORDER BY next_run", (now,)).fetchall()]


def advance(item: dict, *, ran: bool, run_id: str | None = None, status: str = "started",
            now: float | None = None) -> None:
    """After firing (or skipping) a due schedule: compute its next run, or
    disable a one-off."""
    now = now or time.time()
    nxt = None
    if item.get("every_seconds"):
        nxt = item["next_run"]
        while nxt <= now:
            nxt += item["every_seconds"]
        if item.get("until") and nxt > item["until"]:
            nxt = None
    with _lock, closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE schedules SET next_run = ?, enabled = ?, last_run = COALESCE(?, last_run), "
            "last_status = ?, last_run_id = COALESCE(?, last_run_id), run_count = run_count + ? WHERE id = ?",
            (nxt, 1 if nxt else 0, now if ran else None, status, run_id, 1 if ran else 0, item["id"]))


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

class SchedulerLoop:
    """Fires due schedules. `on_task(item)` starts a background workflow and
    returns its run_id; `on_notify(item)` delivers a timer."""

    def __init__(self, on_task, on_notify, poll_seconds: float = 5.0):
        self._on_task = on_task
        self._on_notify = on_notify
        self._poll = poll_seconds
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="scheduler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.tick()
            except Exception as e:
                log(f"tick failed: {e}")

    def tick(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        fired = []
        for item in due(now):
            late_hours = (now - item["next_run"]) / 3600
            if late_hours > CATCH_UP_HOURS:
                log(f"skipping stale occurrence of {item['id']} ({late_hours:.1f}h late)")
                advance(item, ran=False, status="skipped_stale", now=now)
                continue
            try:
                if item["kind"] == "notify":
                    self._on_notify(item)
                    advance(item, ran=True, status="delivered", now=now)
                else:
                    run_id = self._on_task(item)
                    advance(item, ran=True, run_id=run_id, status="started", now=now)
                fired.append(item["id"])
                audit.log("schedule_fired", summary=item["title"], detail={"id": item["id"]})
            except Exception as e:
                log(f"schedule {item['id']} failed to start: {e}")
                advance(item, ran=False, status=f"error: {e}"[:200], now=now)
        return fired
