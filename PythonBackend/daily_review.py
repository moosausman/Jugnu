"""
daily_review.py -- builds Jugnu's end-of-day recap: what today looked
like, and a handful of concrete recommendations grounded in it -- what you
worked on, what you kept searching for, what showed up in your notes and
reminders/calendar.

Assembled from data Jugnu already has (activity_log.py, semantic_memory.py)
plus two small on-demand agent tasks (Notion, calendar/reminders), reusing
the same executor.run_task() pipeline every other task in Jugnu already
runs through.

Reachable two ways:

1. Scheduled -- bridge.py's DailyReviewScheduler fires this once a day at
   JUGNU_DAILY_REVIEW_TIME and emits it as a "digest" message.
2. On demand -- the planner routes to the "daily_review" pseudo-agent, or
   bridge.py receives {"type": "daily_review"} directly.

Safari browsing history is best-effort. It requires Full Disk Access and
degrades to an empty section if unavailable or incompatible.
"""

import datetime
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import semantic_memory as smem
from executor import run_task
from memory import persona_reply
from planner import Task


CORE_DATA_EPOCH_OFFSET = 978307200
INCLUDE_BROWSER_HISTORY = (
    os.environ.get("JUGNU_INCLUDE_BROWSER_HISTORY", "1") != "0"
)
_SAFARI_HISTORY_DB = Path.home() / "Library" / "Safari" / "History.db"
_warned_history_permission = False


def log(message: str) -> None:
    print(f"[daily_review] {message}", file=sys.stderr, flush=True)


def _todays_browsing_history(limit: int = 25) -> str:
    global _warned_history_permission

    if not INCLUDE_BROWSER_HISTORY or not _SAFARI_HISTORY_DB.exists():
        return ""

    try:
        # Read-only + immutable lets this coexist with Safari's connection.
        uri = f"file:{_SAFARI_HISTORY_DB}?mode=ro&immutable=1"
        start_of_day = (
            datetime.datetime.combine(
                datetime.date.today(), datetime.time.min
            ).timestamp()
            - CORE_DATA_EPOCH_OFFSET
        )

        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT history_items.url AS url, history_visits.title AS title
                FROM history_visits
                JOIN history_items
                  ON history_visits.history_item = history_items.id
                WHERE history_visits.visit_time >= ?
                ORDER BY history_visits.visit_time DESC
                LIMIT ?
                """,
                (start_of_day, limit),
            ).fetchall()
    except Exception as exc:
        if not _warned_history_permission:
            log(
                "couldn't read Safari history -- likely no Full Disk Access "
                "granted yet (System Settings -> Privacy & Security -> Full "
                "Disk Access), or Safari's schema has changed. Skipping "
                f"browsing history in today's recap. ({exc})"
            )
            _warned_history_permission = True
        return ""

    if not rows:
        return ""

    lines = [f"- {row['title'] or row['url']}" for row in rows]
    return "Pages visited today:\n" + "\n".join(lines)


def _gather_agent_section(agent_name: str, description: str) -> str:
    """Run one best-effort task through Jugnu's existing agent pipeline."""
    task = Task(id=1, agent=agent_name, description=description)
    try:
        return run_task(task, shared_memory={})
    except Exception as exc:
        log(f"{agent_name} section failed: {exc}")
        return ""


def _synthesis_prompt(sections: dict[str, str]) -> str:
    lines = ["Here's everything gathered about today:\n"]

    for label, content in sections.items():
        if content:
            lines.append(f"--- {label} ---\n{content}\n")

    lines.append(
        "Write today's recap in two parts, using only what's above -- don't "
        "invent anything that isn't there, and if a whole section came back "
        "empty, don't mention its absence.\n\n"
        "(1) What the day looked like -- a few sentences.\n\n"
        "(2) 2-4 concrete recommendations for tomorrow, each tied to "
        "something specific above -- never generic productivity advice. "
        "Weight repeated or recurring searches especially heavily here: a "
        "query that keeps coming up is one of the strongest signals "
        "available for what's actually still unresolved and worth "
        "recommending action on -- a reminder, a saved comparison, "
        "revisiting a decision, or answering it outright if it has been "
        "searched more than once without being settled."
    )

    return "\n".join(lines)


def build_report() -> str:
    """Gather today's data and make one AGENT-model synthesis call."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        notion_future = pool.submit(
            _gather_agent_section,
            "notion",
            "Get today's journal entry (if any) and list any notes or pages "
            "created or updated today.",
        )
        calendar_future = pool.submit(
            _gather_agent_section,
            "calendar",
            "List today's calendar events, and any reminders due today or "
            "completed today.",
        )
        notion_section = notion_future.result()
        calendar_section = calendar_future.result()

    sections = {
        "Time on screen today": smem.activity_summary_today(),
        "Searched today": smem.searches_today(),
        "Repeated searches lately": smem.recent_search_patterns(),
        "Recurring patterns": smem.get_habit_summary(),
        "Notes and journal": notion_section,
        "Reminders and calendar": calendar_section,
        "Pages visited today": _todays_browsing_history(),
    }

    if not any(sections.values()):
        return ""

    return persona_reply(_synthesis_prompt(sections))
