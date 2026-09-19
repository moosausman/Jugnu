"""
semantic_memory.py -- longitudinal memory for Jugnu.

memory.py handles the two layers Jugnu already had: the raw conversation
(working memory) and explicit facts the user stated outright. This module
adds the layers that make Jugnu notice things nobody told it directly:

  1. Timeline   -- an append-only, timestamped log of everything that
                   happens: user messages, assistant replies, tool calls,
                   searches. This is the raw substrate everything else
                   below is built from. Nothing is ever overwritten here.

  2. Searches   -- a running frequency table pulled off the timeline, so
                   "asked about X" becomes "has asked about X 6 times,
                   most recently yesterday."

  3. Habits     -- a short, human-readable digest recomputed from the
                   timeline (recurring search topics, peak activity hours),
                   meant to be injected into the system prompt exactly like
                   facts_as_string() already is.

  4. Semantic   -- optional. If a local embedding model is reachable
                   through Ollama, notable events are embedded and can be
                   recalled by *meaning* ("what did I ask about scheduling
                   a while back?") instead of exact words. If no embedding
                   model is installed, recall() quietly falls back to
                   keyword overlap -- nothing breaks, and nothing has to be
                   installed up front to use this module.

Storage: one SQLite file living next to conversation.json / user_facts.json
in the same Application Support directory memory.py already uses. SQLite is
stdlib, transactional, and lets "top searches" / "peak hour" fall out of a
plain SQL query instead of re-parsing a growing JSON blob on every turn.

Everything here is best-effort and silent on failure by design: memory
should never be the reason a chat turn breaks. If you want a turn to hard
fail when memory writes fail, remove the try/except in record_event.
"""

import os
import sys
import json
import time
import sqlite3
import datetime
import re
import threading
import queue
import atexit
from collections import Counter
from contextlib import closing

import numpy as np

from memory import APP_SUPPORT_DIR
import llm_client

DB_FILE = APP_SUPPORT_DIR / "semantic_memory.db"

# Flip off with JUGNU_ENABLE_EMBEDDINGS=0 to run keyword-only, or point at a
# different local model with JUGNU_EMBED_MODEL (read by llm_client, not
# here -- see default_embed_model(), the one place this is now defined, so
# this and search.py's embed call can never drift out of sync again).
# Nothing else needs to change either way -- recall() degrades
# automatically if the model isn't pulled.
EMBEDDINGS_ENABLED = os.environ.get("JUGNU_ENABLE_EMBEDDINGS", "1") != "0"

# Tri-state cache so we don't retry a broken embedding model on every single
# message: None = untested yet, True/False = known good/bad for this run.
_embeddings_available = None

_WORD_RE = re.compile(r"[a-z0-9]+")


def log(message: str) -> None:
    print(f"[semantic_memory] {message}", file=sys.stderr, flush=True)


# --- storage setup ---------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL NOT NULL,
                kind      TEXT NOT NULL,   -- user_message | assistant_message | tool_call | search
                text      TEXT NOT NULL,
                metadata  TEXT,            -- JSON blob: tool name, args, etc. Nullable.
                embedding TEXT             -- JSON-encoded float list. Null if not embedded.
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_counts (
                query      TEXT PRIMARY KEY,   -- lowercased/stripped
                display    TEXT NOT NULL,      -- last-seen original casing, for display
                count      INTEGER NOT NULL DEFAULT 0,
                first_seen REAL NOT NULL,
                last_seen  REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_kind ON timeline(kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_ts ON timeline(ts)")


_init_db()


# --- embeddings (optional layer) -------------------------------------------

def _embed(text: str):
    """Return a float-list embedding for text, or None if the semantic
    layer isn't available. Tries both the current and the older Ollama
    embeddings API shape, since installed client versions vary."""
    global _embeddings_available

    if not EMBEDDINGS_ENABLED or not text or not text.strip():
        return None
    if _embeddings_available is False:
        return None

    try:
        import ollama  # noqa: F401 -- kept local since this is the one place
        # we need to distinguish "no embedding model pulled" (fall back to
        # keyword recall) from "provider misconfigured" (that should surface
        # loudly, not be swallowed the same way). llm_client still does the
        # actual call; this import is only used for the AttributeError shim
        # below on older ollama-python versions.
        try:
            resp = llm_client.embed(input=text, role="EMBED")
            vector = resp["embeddings"][0]
        except AttributeError:
            resp = ollama.embeddings(model=llm_client.default_embed_model(), prompt=text)
            vector = resp["embedding"]
        _embeddings_available = True
        return vector
    except Exception as e:
        if _embeddings_available is None:
            log(
                f"embeddings unavailable ({e}); falling back to keyword "
                f"recall. Run `ollama pull {llm_client.default_embed_model()}` "
                f"to enable semantic recall, or set JUGNU_EMBED_MODEL to a "
                f"model you have."
            )
        _embeddings_available = False
        return None


def _cosine_against(query_vector, matrix) -> "list[float]":
    """Cosine similarity of one query vector against every row of `matrix`,
    in a single vectorized pass.

    This used to be a scalar _cosine(a, b) called once per candidate row,
    each call running three pure-Python generator loops over ~768 floats.
    At 500 candidate rows that's well over a million interpreted float
    operations, on the critical path of every single turn (build_context
    calls recall() before the planner ever sees the message). numpy is
    already a hard dependency -- search.py has used it for exactly this
    since the beginning -- so there was never a reason for this one to be
    the slow copy.
    """
    if matrix.size == 0:
        return []
    q = np.asarray(query_vector, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0:
        return [0.0] * matrix.shape[0]
    row_norms = np.linalg.norm(matrix, axis=1)
    # Guard zero-norm rows rather than letting them divide to nan/inf --
    # a degenerate stored embedding shouldn't poison the whole ranking.
    safe = np.where(row_norms == 0.0, 1.0, row_norms)
    scores = (matrix @ q) / (safe * q_norm)
    scores[row_norms == 0.0] = 0.0
    return scores.tolist()


def _keyword_score(query_words: set, text: str) -> float:
    text_words = set(_WORD_RE.findall(text.lower()))
    if not text_words or not query_words:
        return 0.0
    return len(query_words & text_words) / len(query_words)


# --- background write queue -------------------------------------------
#
# record_event() runs an embedding call (a real model round-trip -- can be
# hundreds of ms to a few seconds, more if the embedding model isn't
# already loaded) on every single user message, assistant reply, and tool
# call. bridge.py's on_tool_call fires this synchronously from *inside*
# the agent's tool loop, which means every tool call an agent makes was
# paying that latency before it could even see the tool's result. Memory
# is explicitly best-effort (see module docstring), so there's no reason
# any of this needs to happen on the caller's critical path: writes are
# queued and drained by a single background thread, serialized (SQLite
# doesn't want concurrent writers anyway) in the order they were recorded.

_write_queue: "queue.Queue" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker_loop, name="semantic-memory-writer", daemon=True).start()
        _worker_started = True


def _worker_loop() -> None:
    while True:
        job = _write_queue.get()
        try:
            job()
        except Exception as e:
            log(f"background write failed: {e}")
        finally:
            _write_queue.task_done()


def _flush(timeout: float = 2.0) -> None:
    """Best-effort drain on process exit. Bounded wait, not a hard
    guarantee -- a message logged in the last instant before quit can
    still be lost, same as before this change (record_event was already
    silent-on-failure by design)."""
    deadline = time.time() + timeout
    while not _write_queue.empty() and time.time() < deadline:
        time.sleep(0.05)


atexit.register(_flush)


# --- writing to the timeline -------------------------------------------

def record_event(kind: str, text: str, metadata: dict | None = None) -> None:
    """Append one entry to the timeline. Embeds it opportunistically when
    the semantic layer is up; always stored either way, so keyword recall
    and habit stats work even with embeddings off. Runs on the background
    writer thread -- returns immediately."""
    if not text or not text.strip():
        return
    _ensure_worker()
    _write_queue.put(lambda: _record_event_sync(kind, text, metadata))


def _record_event_sync(kind: str, text: str, metadata: dict | None) -> None:
    try:
        vector = _embed(text)
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO timeline (ts, kind, text, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
                (
                    time.time(),
                    kind,
                    text,
                    json.dumps(metadata) if metadata else None,
                    json.dumps(vector) if vector else None,
                ),
            )
    except Exception as e:
        # Memory should never be the reason a chat turn breaks.
        log(f"failed to record event ({kind}): {e}")


def record_search(query: str) -> None:
    """Track a search query in the running frequency table *and* the
    timeline, so repeated interests surface as habits over time. Also
    runs on the background writer thread."""
    if not query or not query.strip():
        return
    q = query.strip()
    normalized = q.lower()
    now = time.time()
    _ensure_worker()
    _write_queue.put(lambda: _record_search_sync(normalized, q, now))
    record_event("search", q, metadata={"normalized": normalized})


def _record_search_sync(normalized: str, display: str, now: float) -> None:
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("""
                INSERT INTO search_counts (query, display, count, first_seen, last_seen)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(query) DO UPDATE SET
                    count = count + 1,
                    display = excluded.display,
                    last_seen = excluded.last_seen
            """, (normalized, display, now, now))
    except Exception as e:
        log(f"failed to record search: {e}")


def record_activity(app: str, window_title: str, duration_seconds: float) -> None:
    """Logs one completed stretch of frontmost-app time (see
    activity_log.py's ActivityLogger). Stored on the same timeline as
    everything else, under kind="activity" -- no new table needed, and it
    rides along with prune_timeline() and recall() for free. Skips
    sub-minute blips (a stray Spotlight tap, an accidental Cmd+Tab) so the
    timeline holds real stretches of focus, not every flicker."""
    if not app or duration_seconds < 60:
        return
    text = f"{app}" + (f" — {window_title}" if window_title else "")
    record_event("activity", text, metadata={
        "app": app, "window_title": window_title,
        "duration_seconds": round(duration_seconds),
    })


# --- reading it back: recall, searches, habits ------------------------

def recall(query: str, k: int = 5, lookback_days: int = 365) -> list[dict]:
    """Return up to k past timeline entries most relevant to `query`, most
    relevant first. Uses embedding similarity when the semantic layer is
    available, otherwise keyword overlap. Looks at the most recent 500
    events within lookback_days -- plenty for a personal assistant's scale,
    and keeps this cheap enough to run on every turn."""
    if not query or not query.strip():
        return []

    cutoff = time.time() - lookback_days * 86400
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT ts, kind, text, embedding FROM timeline WHERE ts >= ? ORDER BY ts DESC LIMIT 500",
            (cutoff,),
        ).fetchall()

    if not rows:
        return []

    query_vector = _embed(query)
    scored = []

    if query_vector:
        # Collect first, score once. Rows whose stored embedding has a
        # different dimension than the current query vector are skipped
        # rather than crashing the matmul -- that happens for real whenever
        # JUGNU_EMBED_MODEL changes, since old rows keep the old model's
        # dimensionality forever and nothing migrates them.
        expected_dim = len(query_vector)
        embedded_rows, vectors = [], []
        for row in rows:
            if not row["embedding"]:
                continue
            try:
                vector = json.loads(row["embedding"])
            except (json.JSONDecodeError, TypeError):
                continue
            if len(vector) != expected_dim:
                continue
            embedded_rows.append(row)
            vectors.append(vector)

        if vectors:
            matrix = np.asarray(vectors, dtype=np.float32)
            for score, row in zip(_cosine_against(query_vector, matrix), embedded_rows):
                if score > 0:
                    scored.append((score, row))
    else:
        query_words = set(_WORD_RE.findall(query.lower()))
        for row in rows:
            score = _keyword_score(query_words, row["text"])
            if score > 0:
                scored.append((score, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "when": datetime.datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M"),
            "kind": row["kind"],
            "text": row["text"],
            "score": round(score, 3),
        }
        for score, row in scored[:k]
    ]


def relevant_memories_as_string(query: str, k: int = 5) -> str:
    """Formatted for direct injection into the system prompt, same pattern
    as facts_as_string()."""
    hits = recall(query, k=k)
    if not hits:
        return ""
    lines = [f"- [{h['when']}, {h['kind']}] {h['text']}" for h in hits]
    return "Possibly relevant things from before:\n" + "\n".join(lines)


def top_searches(limit: int = 10, min_count: int = 2) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT display AS query, count, last_seen FROM search_counts "
            "WHERE count >= ? ORDER BY count DESC LIMIT ?",
            (min_count, limit),
        ).fetchall()
    return [dict(r) for r in rows]


HABIT_LOOKBACK_DAYS = 90


def _hour_bucket_counts(kind: str | None = None, lookback_days: int = HABIT_LOOKBACK_DAYS) -> Counter:
    """Counts timeline events by hour-of-day -- the basis for 'usually
    active around 8am' style observations.

    Bounded two ways now. It used to be a bare `SELECT ts FROM timeline`
    with no WHERE and no LIMIT: a full table scan, run on every turn via
    get_habit_summary() -> build_context(), over a table that only ever
    grows. Both bounds matter for a different reason:

      - `ts >= cutoff` keeps the cost flat as history accumulates, and is
        also just more correct. "Most active around 09:00" drawn from all
        time is a claim about who the user used to be; drawn from the last
        90 days it's a claim about who they are now, which is the only
        version worth putting in a prompt.
      - The index on timeline(ts) makes that range scan cheap, and
        idx_timeline_kind covers the `kind` filter -- both already existed
        and neither was being used by this query.
    """
    cutoff = time.time() - lookback_days * 86400
    query = "SELECT ts FROM timeline WHERE ts >= ?"
    params: tuple = (cutoff,)
    if kind:
        query += " AND kind = ?"
        params = (cutoff, kind)
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    return Counter(datetime.datetime.fromtimestamp(r["ts"]).hour for r in rows)


def get_habit_summary(max_lines: int = 6) -> str:
    """A short digest of recurring behaviour, meant to be injected into the
    system prompt the same way facts are. Recomputing this from scratch on
    every turn is cheap at personal-assistant scale (hundreds to low
    thousands of timeline rows). If the timeline grows much larger than
    that, call this from a periodic job instead and cache the result rather
    than paying the query cost every message."""
    lines = []

    for s in top_searches(limit=max_lines):
        lines.append(f"- Has searched \"{s['query']}\" {s['count']} times")

    tool_hours = _hour_bucket_counts(kind="tool_call")
    if tool_hours:
        peak_hour, count = tool_hours.most_common(1)[0]
        if count >= 5:
            lines.append(f"- Most active around {peak_hour:02d}:00 local time")

    if not lines:
        return ""
    return "Recurring patterns noticed over time:\n" + "\n".join(lines[:max_lines])


# --- today, specifically -----------------------------------------------
# get_habit_summary()/top_searches() above are all-time, recurring-pattern
# views. daily_review.py's end-of-day recap needs a same-day slice instead
# -- these two exist for that, and only that.

def _start_of_today_ts() -> float:
    return datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp()


def activity_summary_today(min_minutes: float = 1.0) -> str:
    """Total time-per-app for today, most time first -- built from the
    'activity' rows activity_log.py's ActivityLogger writes as it watches
    the frontmost app change throughout the day. "" if activity tracking
    is off or nothing's accumulated yet."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT metadata FROM timeline WHERE kind = 'activity' AND ts >= ?",
            (_start_of_today_ts(),),
        ).fetchall()

    totals: Counter = Counter()
    for row in rows:
        if not row["metadata"]:
            continue
        meta = json.loads(row["metadata"])
        app = meta.get("app")
        if app:
            totals[app] += meta.get("duration_seconds", 0)

    lines = [
        f"- {app}: {secs / 60:.0f} min"
        for app, secs in totals.most_common()
        if secs / 60 >= min_minutes
    ]
    if not lines:
        return ""
    return "Time on screen today, by app:\n" + "\n".join(lines)


def searches_today() -> str:
    """Distinct search queries from today only, in the order they
    happened -- deliberately separate from top_searches()' all-time
    frequency table, which answers a different question ("what does the
    user search a lot") than this one ("what came up today")."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT text FROM timeline WHERE kind = 'search' AND ts >= ? ORDER BY ts",
            (_start_of_today_ts(),),
        ).fetchall()

    seen = []
    for row in rows:
        if row["text"] not in seen:
            seen.append(row["text"])
    if not seen:
        return ""
    return "Searched today:\n" + "\n".join(f"- {q}" for q in seen)


def recent_search_patterns(days: int = 7, min_count: int = 2, limit: int = 8) -> str:
    """Searches repeated within the last `days` days -- a tighter, more
    actionable signal than top_searches()' all-time counts for 'what
    should Jugnu proactively suggest right now'. Something searched 3
    times this week is a much stronger basis for a recommendation (a
    price-tracking reminder, a saved comparison, just resolving it
    outright) than something searched 3 times over the last year, which
    top_searches() would weight the same."""
    cutoff = time.time() - days * 86400
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT text, metadata FROM timeline WHERE kind = 'search' AND ts >= ?",
            (cutoff,),
        ).fetchall()

    counts: Counter = Counter()
    display: dict = {}
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        normalized = meta.get("normalized", row["text"].lower())
        counts[normalized] += 1
        display.setdefault(normalized, row["text"])

    lines = [
        f'- "{display[q]}" -- searched {n} times in the last {days} days'
        for q, n in counts.most_common(limit)
        if n >= min_count
    ]
    if not lines:
        return ""
    return f"Repeated searches (last {days} days):\n" + "\n".join(lines)


# --- housekeeping -----------------------------------------------------

PRUNE_KEEP_DAYS = int(os.environ.get("JUGNU_MEMORY_KEEP_DAYS", "180"))


def prune_timeline(keep_days: int = PRUNE_KEEP_DAYS) -> int:
    """Delete timeline rows older than keep_days. Returns rows deleted.

    Wired into maintain(), which bridge.py calls once at startup -- this
    docstring used to end with "Not called automatically by anything",
    which was true and was the bug: memory grew forever by default.
    """
    cutoff = time.time() - keep_days * 86400
    with closing(_connect()) as conn, conn:
        cur = conn.execute("DELETE FROM timeline WHERE ts < ?", (cutoff,))
        return cur.rowcount


def maintain() -> None:
    """Startup housekeeping: drop expired rows, then reclaim the pages they
    were using. Best-effort and silent on failure, like everything else in
    this module -- memory should never be the reason a launch fails.

    VACUUM matters more than the row count suggests. SQLite marks deleted
    pages free but doesn't return them to the filesystem, so a database
    that has churned through a lot of history sits at its high-water mark
    indefinitely. This one measured 19.9MB on disk while holding 350 live
    rows -- essentially all of it reclaimable.

    Runs on the background writer thread. VACUUM takes an exclusive lock
    and rewrites the whole file, which is milliseconds at this scale but
    is still not something to make a user's first turn wait behind.
    """
    def _job() -> None:
        try:
            removed = prune_timeline()
            with closing(_connect()) as conn:
                conn.execute("VACUUM")
            if removed:
                log(f"pruned {removed} timeline rows older than {PRUNE_KEEP_DAYS} days")
        except Exception as e:
            log(f"maintenance pass failed: {e}")

    _ensure_worker()
    _write_queue.put(_job)


def clear_all() -> None:
    """Wipes both tables -- timeline (events, searches, activity) and
    search_counts (the running frequency table habits are built from) --
    for the "reset memory" action (see bridge.py's "reset_memory"
    request). Flushes the background write queue first: record_event/
    record_search/record_activity all queue their actual writes rather
    than doing them inline, so a message still in flight when this is
    called could otherwise land a moment *after* the tables were emptied
    and silently survive the reset. Synchronous and blocking on purpose,
    unlike every other write in this module -- the caller wants to know
    this has actually finished before reporting success back to the user."""
    _flush()
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM timeline")
        conn.execute("DELETE FROM search_counts")
