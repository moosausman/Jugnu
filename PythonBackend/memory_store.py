"""
memory_store.py -- Jugnu's structured long-term memory.

Before this, "memory" was three unrelated things: a flat user_facts.json
(key -> value, no confidence, no history, newest write silently wins), the
semantic_memory timeline (raw events), and the skills table. Nothing knew
where a memory came from, how sure it was, whether it had been contradicted,
or when it stopped being worth keeping -- and the user had no way to see or
correct any of it short of opening the files.

This is one store with explicit kinds:

    user        stable preferences and personal facts ("prefers dark mode")
    episodic    summaries of meaningful past interactions and tasks
    semantic    knowledge extracted from experience ("CSC236 notes live in
                ~/Documents/CSC236")
    error       a failure, its cause, and what fixed it
    strategy    how a kind of task is best approached next time
    (procedural memory -- reusable workflows -- lives in skills.py; tool
    reliability in the tool_stats table below; working memory is per-run,
    in executor checkpoints)

Every memory carries:
    confidence  0..1 -- how sure Jugnu is
    source      user_stated > user_confirmed > observed > tool_result >
                reflection > inferred  (trust ranking, see SOURCE_RANK)
    scope       global, or project:/path, app:Name, task:type, session:agent
    importance  0..1 -- how much it matters when relevant
    key         optional canonical subject ("user.name", "pref.editor") used
                to detect contradictions directly
    timestamps  created / updated / last used, plus a use count

Contradictions: a new memory with the same kind+key but different content
supersedes the old one only if its source is at least as trustworthy (a
guess never overwrites something the user said). The old one isn't lost --
it's marked superseded and linked, so history can be shown and undone.
Content that is the same reinforces instead (confidence up, timestamps
bumped).

Guesses stay guesses: "inferred" memories are capped at 0.6 confidence and
labelled as unconfirmed whenever they're put in front of a model.

Forgetting: maintain() decays memories by kind-specific half-lives, and
prunes ones that are low-value (low importance x confidence x recency) and
unused. User facts never decay; they only change by being superseded or
edited.

Secrets are refused outright (redact.contains_secret) -- not stored in a
mangled form, not stored at all.
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
from dataclasses import dataclass, field, asdict

import audit
from memory import APP_SUPPORT_DIR
from redact import contains_secret, redact

DB_FILE = APP_SUPPORT_DIR / "memories.db"

KINDS = ("user", "episodic", "semantic", "error", "strategy")

SOURCE_RANK = {
    "user_stated": 6,      # the user said it
    "user_confirmed": 5,   # the user confirmed or edited it
    "observed": 4,         # directly observed on the machine (a file exists, an app is installed)
    "tool_result": 3,      # returned by a tool
    "reflection": 2,       # concluded by Jugnu's post-task reflection
    "inferred": 1,         # a guess from patterns
    "imported": 5,         # migrated from the old facts file (the user had said it)
}
INFERRED_MAX_CONFIDENCE = 0.6

# Half-life (days) of relevance by kind. None = never decays.
HALF_LIFE_DAYS = {"user": None, "episodic": 45, "semantic": 180, "error": 120, "strategy": 150}
MAX_PER_KIND = {"user": 500, "episodic": 1500, "semantic": 2000, "error": 800, "strategy": 500}
PRUNE_SCORE = 0.05

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.+#-]*")
_STOP = frozenset("""a an the and or but if then of to in on at for with by from is are was were be been being
this that these those it its as i me my you your we our they their he she his her do does did done have has had
not no yes can could should would will just so than too very about into over under what which who whom when where
why how all any each some such only own same other more most also there here up down out off again""".split())

_lock = threading.RLock()


def log(message: str) -> None:
    import sys
    print(f"[memory_store] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass
class Memory:
    id: str
    kind: str
    content: str
    key: str | None = None
    confidence: float = 0.7
    source: str = "reflection"
    scope: str = "global"
    importance: float = 0.5
    status: str = "active"          # active | superseded | archived
    tags: list = field(default_factory=list)
    data: dict = field(default_factory=dict)   # structured payload (error cause/fix, etc.)
    evidence: list = field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source_rank"] = SOURCE_RANK.get(self.source, 0)
        d["unconfirmed"] = self.source == "inferred"
        return d


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id            TEXT PRIMARY KEY,
                kind          TEXT NOT NULL,
                key           TEXT,
                content       TEXT NOT NULL,
                confidence    REAL NOT NULL,
                source        TEXT NOT NULL,
                scope         TEXT NOT NULL DEFAULT 'global',
                importance    REAL NOT NULL DEFAULT 0.5,
                status        TEXT NOT NULL DEFAULT 'active',
                tags          TEXT,
                data          TEXT,
                evidence      TEXT,
                supersedes    TEXT,
                superseded_by TEXT,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                last_used_at  REAL NOT NULL,
                use_count     INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_kind_status ON memories(kind, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_key ON memories(kind, key)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_stats (
                tool          TEXT NOT NULL,
                context       TEXT NOT NULL DEFAULT '',
                successes     INTEGER NOT NULL DEFAULT 0,
                failures      INTEGER NOT NULL DEFAULT 0,
                total_seconds REAL NOT NULL DEFAULT 0,
                error_types   TEXT,
                last_error    TEXT,
                last_used     REAL NOT NULL,
                PRIMARY KEY (tool, context)
            )
        """)


_init()


def _row_to_memory(row: sqlite3.Row) -> Memory:
    def _j(value, default):
        if not value:
            return default
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return Memory(
        id=row["id"], kind=row["kind"], key=row["key"], content=row["content"],
        confidence=row["confidence"], source=row["source"], scope=row["scope"],
        importance=row["importance"], status=row["status"], tags=_j(row["tags"], []),
        data=_j(row["data"], {}), evidence=_j(row["evidence"], []),
        supersedes=row["supersedes"], superseded_by=row["superseded_by"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        last_used_at=row["last_used_at"], use_count=row["use_count"],
    )


def _insert(conn, m: Memory) -> None:
    conn.execute(
        "INSERT INTO memories (id, kind, key, content, confidence, source, scope, importance, status, tags, "
        "data, evidence, supersedes, superseded_by, created_at, updated_at, last_used_at, use_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (m.id, m.kind, m.key, m.content, m.confidence, m.source, m.scope, m.importance, m.status,
         json.dumps(m.tags), json.dumps(m.data), json.dumps(m.evidence), m.supersedes, m.superseded_by,
         m.created_at, m.updated_at, m.last_used_at, m.use_count),
    )


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalize_key(key: str | None) -> str | None:
    if not key:
        return None
    k = re.sub(r"[^a-z0-9_.]+", "_", str(key).strip().lower()).strip("_.")
    return k[:80] or None


def _norm_text(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


class MemoryRejected(ValueError):
    """Raised when a memory can't be stored (secret, empty, bad kind)."""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def remember(content: str, kind: str = "user", *, key: str | None = None, confidence: float = 0.7,
             source: str = "reflection", scope: str = "global", importance: float = 0.5,
             tags: list | None = None, data: dict | None = None, evidence: list | None = None,
             run_id: str | None = None) -> dict:
    """Stores a memory, handling duplicates and contradictions. Returns
    {"action": "created"|"reinforced"|"superseded"|"kept_existing", "memory": {...},
     "previous": {...}?}. Raises MemoryRejected for secrets/empty/unknown kind."""
    content = (content or "").strip()
    if not content:
        raise MemoryRejected("empty memory")
    if kind not in KINDS:
        raise MemoryRejected(f"unknown memory kind {kind!r}; expected one of {KINDS}")
    if contains_secret(content) or (data and contains_secret(json.dumps(data, default=str))):
        raise MemoryRejected("refusing to store something that looks like a secret (password, key or token)")
    if source not in SOURCE_RANK:
        source = "reflection"
    confidence = _clamp(confidence)
    if source == "inferred":
        confidence = min(confidence, INFERRED_MAX_CONFIDENCE)
    key = normalize_key(key)
    now = time.time()
    new = Memory(
        id=uuid.uuid4().hex[:12], kind=kind, content=content[:4000], key=key, confidence=confidence,
        source=source, scope=(scope or "global")[:200], importance=_clamp(importance),
        tags=list(tags or [])[:12], data=data or {}, evidence=list(evidence or [])[-10:],
        created_at=now, updated_at=now, last_used_at=now,
    )
    if run_id:
        new.evidence.append({"run_id": run_id, "ts": now})

    with _lock, closing(_connect()) as conn, conn:
        existing = _find_existing(conn, new)
        if existing is None:
            _insert(conn, new)
            result = {"action": "created", "memory": new.to_dict()}
        elif _norm_text(existing.content) == _norm_text(new.content):
            # Same thing again: reinforce rather than duplicate.
            boost = 0.15 if SOURCE_RANK[source] >= SOURCE_RANK.get(existing.source, 0) else 0.05
            conf = _clamp(max(existing.confidence, confidence) + boost * (1 - existing.confidence))
            better_source = source if SOURCE_RANK[source] > SOURCE_RANK.get(existing.source, 0) else existing.source
            if better_source == "inferred":
                conf = min(conf, INFERRED_MAX_CONFIDENCE)
            ev = (existing.evidence + new.evidence)[-10:]
            conn.execute(
                "UPDATE memories SET confidence = ?, source = ?, updated_at = ?, last_used_at = ?, "
                "importance = MAX(importance, ?), evidence = ? WHERE id = ?",
                (conf, better_source, now, now, new.importance, json.dumps(ev), existing.id),
            )
            existing.confidence, existing.source, existing.updated_at = conf, better_source, now
            result = {"action": "reinforced", "memory": existing.to_dict()}
        elif SOURCE_RANK[source] >= SOURCE_RANK.get(existing.source, 0):
            # A contradiction from an equally or more trustworthy source:
            # newest confirmed information wins, the old version is kept
            # (superseded) so it can be shown and restored.
            new.supersedes = existing.id
            _insert(conn, new)
            conn.execute("UPDATE memories SET status = 'superseded', superseded_by = ?, updated_at = ? "
                         "WHERE id = ?", (new.id, now, existing.id))
            result = {"action": "superseded", "memory": new.to_dict(), "previous": existing.to_dict()}
        else:
            # A weaker source disagrees with something more trusted. Don't
            # overwrite; lower the confidence of neither, but record the
            # disagreement on the existing memory so it can be surfaced.
            disputes = existing.data.get("disputed_by", [])
            disputes.append({"content": new.content[:300], "source": source, "ts": now})
            existing.data["disputed_by"] = disputes[-5:]
            conn.execute("UPDATE memories SET data = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(existing.data), now, existing.id))
            result = {"action": "kept_existing", "memory": existing.to_dict(),
                      "rejected": new.to_dict()}

    audit.log("memory_change", run_id=run_id, status=result["action"],
              summary=f"[{kind}] {content[:200]}", detail={"id": result["memory"]["id"], "key": key,
                                                              "source": source, "confidence": confidence})
    return result


def _find_existing(conn, new: Memory) -> Memory | None:
    """The active memory a new one would duplicate or contradict: same
    kind+key+scope if a key was given, else an exact-text duplicate."""
    if new.key:
        row = conn.execute(
            "SELECT * FROM memories WHERE kind = ? AND key = ? AND scope = ? AND status = 'active' "
            "ORDER BY updated_at DESC LIMIT 1", (new.kind, new.key, new.scope)).fetchone()
        return _row_to_memory(row) if row else None
    norm = _norm_text(new.content)
    rows = conn.execute(
        "SELECT * FROM memories WHERE kind = ? AND scope = ? AND status = 'active' AND key IS NULL "
        "ORDER BY updated_at DESC LIMIT 400", (new.kind, new.scope)).fetchall()
    for row in rows:
        if _norm_text(row["content"]) == norm:
            return _row_to_memory(row)
    return None


def update(memory_id: str, *, content: str | None = None, confidence: float | None = None,
           scope: str | None = None, importance: float | None = None, key: str | None = None,
           tags: list | None = None, status: str | None = None) -> dict:
    """A user edit. Edited memories become user_confirmed (the user has now
    vouched for them) at full confidence unless one is given."""
    with _lock, closing(_connect()) as conn, conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            raise KeyError(memory_id)
        m = _row_to_memory(row)
        if content is not None:
            content = content.strip()
            if not content:
                raise MemoryRejected("empty memory")
            if contains_secret(content):
                raise MemoryRejected("refusing to store something that looks like a secret")
            m.content = content[:4000]
        if confidence is not None:
            m.confidence = _clamp(confidence)
        elif content is not None:
            m.confidence = 1.0
        if scope is not None:
            m.scope = scope or "global"
        if importance is not None:
            m.importance = _clamp(importance)
        if key is not None:
            m.key = normalize_key(key)
        if tags is not None:
            m.tags = list(tags)[:12]
        if status in ("active", "archived"):
            m.status = status
        m.source = "user_confirmed"
        m.updated_at = time.time()
        conn.execute(
            "UPDATE memories SET content = ?, confidence = ?, scope = ?, importance = ?, key = ?, tags = ?, "
            "status = ?, source = ?, updated_at = ? WHERE id = ?",
            (m.content, m.confidence, m.scope, m.importance, m.key, json.dumps(m.tags), m.status,
             m.source, m.updated_at, m.id))
    audit.log("memory_change", status="edited", summary=f"[{m.kind}] {m.content[:200]}", detail={"id": m.id})
    return m.to_dict()


def delete(memory_id: str) -> bool:
    """Hard delete -- the user asked for it to be gone, including history."""
    with _lock, closing(_connect()) as conn, conn:
        row = conn.execute("SELECT kind, content, supersedes FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        # Re-activate what it had replaced, so deleting a wrong correction
        # restores the earlier memory rather than leaving a hole.
        if row["supersedes"]:
            conn.execute("UPDATE memories SET status = 'active', superseded_by = NULL WHERE id = ? "
                         "AND status = 'superseded'", (row["supersedes"],))
    audit.log("memory_change", status="deleted", summary=f"[{row['kind']}] {row['content'][:200]}",
              detail={"id": memory_id})
    return True


def clear(kind: str | None = None) -> int:
    with _lock, closing(_connect()) as conn, conn:
        if kind:
            cur = conn.execute("DELETE FROM memories WHERE kind = ?", (kind,))
        else:
            cur = conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM tool_stats")
    audit.log("memory_change", status="cleared", summary=f"cleared {kind or 'all'} memories")
    return cur.rowcount


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def get(memory_id: str, with_history: bool = True) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return None
        m = _row_to_memory(row)
        out = m.to_dict()
        if with_history:
            history, cursor = [], m.supersedes
            while cursor and len(history) < 20:
                prev = conn.execute("SELECT * FROM memories WHERE id = ?", (cursor,)).fetchone()
                if not prev:
                    break
                pm = _row_to_memory(prev)
                history.append(pm.to_dict())
                cursor = pm.supersedes
            out["history"] = history
        return out


def list_memories(kind: str | None = None, query: str = "", include_inactive: bool = False,
                  limit: int = 200, offset: int = 0) -> list[dict]:
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if not include_inactive:
        clauses.append("status = 'active'")
    if query:
        clauses.append("(content LIKE ? OR key LIKE ? OR tags LIKE ?)")
        params.extend([f"%{query}%"] * 3)
    sql = "SELECT * FROM memories"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    with closing(_connect()) as conn:
        return [_row_to_memory(r).to_dict() for r in conn.execute(sql, params).fetchall()]


def counts() -> dict:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT kind, status, COUNT(*) AS n FROM memories GROUP BY kind, status").fetchall()
    out: dict = {k: {"active": 0, "superseded": 0, "archived": 0} for k in KINDS}
    for r in rows:
        out.setdefault(r["kind"], {})[r["status"]] = r["n"]
    return out


def _recency(m: Memory, now: float) -> float:
    half_life = HALF_LIFE_DAYS.get(m.kind)
    if not half_life:
        return 1.0
    age_days = (now - max(m.last_used_at, m.updated_at)) / 86400
    return 0.5 ** (age_days / half_life)


def _value(m: Memory, now: float) -> float:
    """How much keeping this memory is worth right now."""
    usage = 1 + math.log1p(m.use_count) * 0.25
    return m.importance * m.confidence * _recency(m, now) * usage


def recall(query: str, kinds: tuple | list | None = None, k: int = 6, scope: str | None = None,
           min_score: float = 0.08, touch: bool = True) -> list[dict]:
    """The memories most relevant to `query`: lexical relevance (IDF-weighted
    token overlap -- works with no embedding model installed) blended with
    confidence, importance, recency and scope match."""
    kinds = tuple(kinds or KINDS)
    q_tokens = set(_tokens(query))
    now = time.time()
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM memories WHERE status = 'active' AND kind IN ({','.join('?' * len(kinds))}) "
            "ORDER BY updated_at DESC LIMIT 3000", kinds).fetchall()
        memories = [_row_to_memory(r) for r in rows]
        if not memories:
            return []
        # Document frequencies over this candidate set, for IDF weighting.
        df: dict[str, int] = {}
        token_sets = []
        for m in memories:
            toks = set(_tokens(m.content + " " + (m.key or "").replace(".", " ") + " " + " ".join(m.tags)))
            token_sets.append(toks)
            for t in toks:
                df[t] = df.get(t, 0) + 1
        n = len(memories)
        scored = []
        for m, toks in zip(memories, token_sets):
            if q_tokens:
                overlap = q_tokens & toks
                if not overlap and m.kind != "user":
                    continue
                idf = sum(math.log(1 + n / df[t]) for t in overlap)
                max_idf = sum(math.log(1 + n / max(1, df.get(t, 1))) for t in q_tokens) or 1.0
                relevance = idf / max_idf
            else:
                relevance = 0.3
            scope_bonus = 0.0
            if scope and m.scope != "global":
                scope_bonus = 0.25 if (m.scope == scope or scope.startswith(m.scope.split(":", 1)[-1])) else -0.3
            score = (0.6 * relevance + 0.2 * m.confidence + 0.1 * m.importance + 0.1 * _recency(m, now)
                     + scope_bonus)
            if m.kind == "user" and relevance == 0:
                score *= 0.35  # stable facts stay available, but rank below relevant ones
            if score >= min_score:
                scored.append((score, m))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[:k]
        if touch and top:
            conn.execute(
                f"UPDATE memories SET last_used_at = ?, use_count = use_count + 1 "
                f"WHERE id IN ({','.join('?' * len(top))})", [now] + [m.id for _, m in top])
            conn.commit()
    return [{**m.to_dict(), "score": round(s, 3)} for s, m in top]


def _format(m: dict) -> str:
    tag = ""
    if m.get("source") == "inferred":
        tag = " (my inference -- unconfirmed)"
    elif m.get("confidence", 1) < 0.5:
        tag = " (low confidence)"
    scope = f" [{m['scope']}]" if m.get("scope") and m["scope"] != "global" else ""
    return f"- {m['content']}{scope}{tag}"


def user_profile_block(limit: int = 25) -> str:
    """Stable facts about the user, most important first -- the successor
    to memory.facts_as_string()."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM memories WHERE kind = 'user' AND status = 'active' "
            "ORDER BY importance DESC, confidence DESC, updated_at DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return ""
    return "What you know about the user:\n" + "\n".join(_format(_row_to_memory(r).to_dict()) for r in rows)


def context_block(query: str, scope: str | None = None) -> str:
    """Relevant experience for a request: past episodes, knowledge, lessons
    from errors and task strategies. Fed to the planner and task agents."""
    sections = []
    groups = [
        ("Relevant past work", ("episodic",), 3),
        ("Things learned", ("semantic",), 4),
        ("Lessons from past mistakes -- avoid repeating these", ("error",), 3),
        ("What has worked for similar tasks", ("strategy",), 3),
    ]
    for title, kinds, k in groups:
        hits = recall(query, kinds=kinds, k=k, scope=scope, min_score=0.3)
        if hits:
            sections.append(f"{title}:\n" + "\n".join(_format(h) for h in hits))
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Error memory -- failures, causes, fixes
# ---------------------------------------------------------------------------

def _error_signature(tool: str, error_type: str, error: str) -> str:
    text = re.sub(r"(/[^\s'\"]+)+", "<path>", (error or "").lower())
    text = re.sub(r"\d+", "#", text)
    return normalize_key(f"{tool}.{error_type}.{' '.join(text.split()[:8])}") or tool


def record_error(tool: str, error_type: str, error: str, args: dict | None = None,
                 run_id: str | None = None) -> dict | None:
    """Remembers a failure so the same mistake can be recognised next time.
    The fix is filled in by record_fix() when a later call succeeds."""
    try:
        sig = _error_signature(tool, error_type, error)
        return remember(
            f"{tool} failed ({error_type}): {redact(error)[:300]}", kind="error", key=sig,
            confidence=0.6, source="tool_result", importance=0.5,
            data={"tool": tool, "error_type": error_type, "args": redact(json.dumps(args or {}, default=str))[:500]},
            run_id=run_id,
        )
    except MemoryRejected:
        return None


def record_fix(tool: str, error_type: str, error: str, fix: str, run_id: str | None = None) -> dict | None:
    """Attaches the resolution to an error memory: what worked instead."""
    sig = _error_signature(tool, error_type, error)
    with _lock, closing(_connect()) as conn, conn:
        row = conn.execute("SELECT * FROM memories WHERE kind = 'error' AND key = ? AND status = 'active'",
                           (sig,)).fetchone()
        if not row:
            return None
        m = _row_to_memory(row)
        m.data["fix"] = redact(fix)[:500]
        content = f"{m.content.split(' -- fix: ')[0]} -- fix: {m.data['fix']}"
        conn.execute("UPDATE memories SET data = ?, content = ?, confidence = MIN(1.0, confidence + 0.2), "
                     "importance = MAX(importance, 0.7), updated_at = ? WHERE id = ?",
                     (json.dumps(m.data), content[:4000], time.time(), m.id))
    audit.log("memory_change", run_id=run_id, status="fix_learned", summary=f"{tool}: {fix[:200]}")
    return get(m.id, with_history=False)


def known_fix(tool: str, error_type: str, error: str) -> str | None:
    """The remembered fix for this kind of failure, if one was learned."""
    sig = _error_signature(tool, error_type, error)
    with closing(_connect()) as conn:
        row = conn.execute("SELECT data FROM memories WHERE kind = 'error' AND key = ? AND status = 'active'",
                           (sig,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"] or "{}").get("fix")
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Tool memory -- reliability and selection history
# ---------------------------------------------------------------------------

def record_tool_use(tool: str, ok: bool, duration: float, error_type: str | None = None,
                    context: str = "") -> None:
    now = time.time()
    with _lock, closing(_connect()) as conn, conn:
        row = conn.execute("SELECT * FROM tool_stats WHERE tool = ? AND context = ?", (tool, context)).fetchone()
        if row is None:
            conn.execute("INSERT INTO tool_stats (tool, context, successes, failures, total_seconds, error_types, "
                         "last_error, last_used) VALUES (?, ?, 0, 0, 0, '{}', NULL, ?)", (tool, context, now))
            errors: dict = {}
        else:
            errors = json.loads(row["error_types"] or "{}")
        if not ok and error_type:
            errors[error_type] = errors.get(error_type, 0) + 1
        conn.execute(
            "UPDATE tool_stats SET successes = successes + ?, failures = failures + ?, "
            "total_seconds = total_seconds + ?, error_types = ?, last_error = COALESCE(?, last_error), "
            "last_used = ? WHERE tool = ? AND context = ?",
            (1 if ok else 0, 0 if ok else 1, float(duration or 0), json.dumps(errors),
             None if ok else error_type, now, tool, context))


def tool_stats(limit: int = 200) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT tool, SUM(successes) s, SUM(failures) f, SUM(total_seconds) t, MAX(last_used) lu "
            "FROM tool_stats GROUP BY tool ORDER BY (SUM(successes) + SUM(failures)) DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        total = (r["s"] or 0) + (r["f"] or 0)
        out.append({"tool": r["tool"], "successes": r["s"] or 0, "failures": r["f"] or 0,
                    "reliability": round((r["s"] or 0) / total, 3) if total else None,
                    "avg_seconds": round((r["t"] or 0) / total, 2) if total else None,
                    "last_used": r["lu"]})
    return out


def unreliable_tools_block(min_uses: int = 4, max_reliability: float = 0.5) -> str:
    """Tools that have mostly failed lately, so agents can prefer others."""
    bad = [t for t in tool_stats() if (t["successes"] + t["failures"]) >= min_uses
           and t["reliability"] is not None and t["reliability"] <= max_reliability]
    if not bad:
        return ""
    lines = [f"- {t['tool']}: {int(t['reliability'] * 100)}% success over {t['successes'] + t['failures']} uses"
             for t in bad[:8]]
    return "Tools that have been unreliable recently (prefer alternatives when one fits):\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def maintain() -> dict:
    """Forget low-value memories and enforce per-kind caps. Superseded
    versions older than 180 days are dropped too."""
    now = time.time()
    removed = 0
    with _lock, closing(_connect()) as conn, conn:
        rows = conn.execute("SELECT * FROM memories WHERE status = 'active' AND kind != 'user'").fetchall()
        doomed = []
        for r in rows:
            m = _row_to_memory(r)
            idle_days = (now - m.last_used_at) / 86400
            if _value(m, now) < PRUNE_SCORE and idle_days > 14:
                doomed.append(m.id)
        for kind, cap in MAX_PER_KIND.items():
            extra = conn.execute(
                "SELECT id FROM memories WHERE kind = ? AND status = 'active' "
                "ORDER BY importance * confidence DESC, last_used_at DESC LIMIT -1 OFFSET ?", (kind, cap)).fetchall()
            doomed.extend(r["id"] for r in extra)
        if doomed:
            conn.executemany("DELETE FROM memories WHERE id = ?", [(i,) for i in set(doomed)])
            removed += len(set(doomed))
        cur = conn.execute("DELETE FROM memories WHERE status = 'superseded' AND updated_at < ?",
                           (now - 180 * 86400,))
        removed += cur.rowcount
    if removed:
        audit.log("memory_change", status="pruned", summary=f"forgot {removed} low-value memories")
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Migration from the old flat facts file
# ---------------------------------------------------------------------------

def migrate_facts_file(path) -> int:
    """Imports user_facts.json (key -> value) once; the file is renamed so it
    isn't imported twice. Returns how many facts were imported."""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return 0
    try:
        facts = json.loads(p.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return 0
    n = 0
    for key, value in (facts or {}).items():
        try:
            remember(f"{key.replace('_', ' ')}: {value}", kind="user", key=f"fact.{key}", confidence=0.95,
                     source="imported", importance=0.7)
            n += 1
        except MemoryRejected:
            continue
    try:
        p.rename(p.with_suffix(".json.migrated"))
    except OSError:
        pass
    return n
