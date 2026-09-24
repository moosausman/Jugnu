"""
store.py -- saved backtests and paper accounts, in the same SQLite file the
price cache uses.

Runs are kept so "what did that backtest say?" survives restarts, the app
can list them, and a paper account can point back at the run that started
it. Nothing here touches money: a paper account is a row with a cash number
in it.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from .data import connect
from .engine import Result


def _init() -> None:
    connect().executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, created REAL, symbol TEXT, strategy TEXT, params TEXT,
            interval TEXT, start TEXT, end TEXT, cash REAL, commission REAL, slippage REAL,
            metrics TEXT, benchmark TEXT, equity TEXT, dates TEXT, trades TEXT, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS trials (
            fingerprint TEXT PRIMARY KEY, created REAL, symbol TEXT, strategy TEXT,
            params TEXT, interval TEXT, start TEXT, end TEXT, bars INTEGER,
            sharpe REAL, total_return REAL
        );
        CREATE INDEX IF NOT EXISTS trials_symbol ON trials (symbol, strategy);
        CREATE TABLE IF NOT EXISTS paper (
            id TEXT PRIMARY KEY, created REAL, symbol TEXT, strategy TEXT, params TEXT,
            interval TEXT, cash REAL, start_cash REAL, position REAL, avg_cost REAL,
            last_ts INTEGER, active INTEGER DEFAULT 1, trades TEXT, note TEXT
        );
    """)
    connect().commit()


# ---------------------------------------------------------------------------
# The search ledger
# ---------------------------------------------------------------------------
# Every configuration ever evaluated, one row each, so the deflated Sharpe
# can charge for the WHOLE search rather than for whichever sweep happens to
# be on screen. Deflating by one grid is the convenient number; this is the
# honest one.
#
# Keyed by fingerprint, so re-running the same test on the same window is
# one trial however many times you do it -- you learned nothing new the
# second time. Change the parameters, the symbol or the window and it is a
# new trial, because that is a new place you looked.

def _fingerprint(symbol: str, strategy: str, params: dict, interval: str,
                 start: str, end: str) -> str:
    seed = "|".join([symbol.upper(), strategy, interval, start, end,
                     json.dumps(params, sort_keys=True, default=str)])
    return hashlib.sha1(seed.encode("utf8")).hexdigest()[:16]


def record_trial(symbol: str, strategy: str, params: dict, interval: str,
                 start: str, end: str, bars: int, sharpe: float, total_return: float) -> None:
    """One evaluated configuration. Cheap enough to call from the inside of
    a sweep -- it is a single INSERT OR IGNORE against a primary key."""
    if not symbol or not strategy:
        return
    try:
        _init()
        connect().execute(
            "INSERT OR IGNORE INTO trials (fingerprint, created, symbol, strategy, params, "
            "interval, start, end, bars, sharpe, total_return) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_fingerprint(symbol, strategy, params, interval, start, end), time.time(),
             symbol.upper(), strategy, json.dumps(params, sort_keys=True, default=str),
             interval, start, end, bars, sharpe, total_return))
        connect().commit()
    except Exception:
        # A ledger that breaks a backtest is worse than a ledger with a gap.
        pass


def search_history(symbol: str = "", strategy: str = "") -> dict:
    """How hard you have looked, and what you found while looking.

    Scoped to a symbol by default: every strategy and parameter set you
    tried on that instrument is one search for an edge in it, and that is
    the count the deflation wants. Narrow to a strategy when you only want
    that family.
    """
    _init()
    where, args = [], []
    if symbol:
        where.append("symbol = ?")
        args.append(symbol.upper())
    if strategy:
        where.append("strategy = ?")
        args.append(strategy)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = connect().execute(
        f"SELECT sharpe, created, strategy FROM trials{clause}", args).fetchall()
    if not rows:
        return {"trials": 0, "sharpes": [], "strategies": [], "first": None, "last": None}
    sharpes = [r["sharpe"] for r in rows if r["sharpe"] is not None]
    stamps = [r["created"] for r in rows]
    return {"trials": len(rows), "sharpes": sharpes,
            "strategies": sorted({r["strategy"] for r in rows}),
            "first": min(stamps), "last": max(stamps)}


def forget_trials(symbol: str = "", strategy: str = "") -> int:
    """Clear the ledger. Only honest when the old results genuinely no
    longer count -- a different dataset, say, not a disappointing number."""
    _init()
    where, args = [], []
    if symbol:
        where.append("symbol = ?")
        args.append(symbol.upper())
    if strategy:
        where.append("strategy = ?")
        args.append(strategy)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    cursor = connect().execute(f"DELETE FROM trials{clause}", args)
    connect().commit()
    return cursor.rowcount


def save_run(result: Result, notes: list[str] | None = None) -> str:
    _init()
    run_id = uuid.uuid4().hex[:10]
    connect().execute(
        "INSERT INTO runs (id, created, symbol, strategy, params, interval, start, end, cash, "
        "commission, slippage, metrics, benchmark, equity, dates, trades, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, time.time(), result.symbol, result.strategy, json.dumps(result.params),
         result.interval, result.dates[0], result.dates[-1], result.cash, result.commission,
         result.slippage_bps, json.dumps(result.metrics), json.dumps(result.benchmark_metrics),
         json.dumps([round(v, 4) for v in result.equity]), json.dumps(result.dates),
         json.dumps([t.__dict__ for t in result.trades]), json.dumps(notes or [])))
    connect().commit()
    return run_id


def list_runs(limit: int = 20) -> list[dict]:
    _init()
    rows = connect().execute(
        "SELECT id, created, symbol, strategy, params, start, end, metrics, benchmark "
        "FROM runs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        mine, theirs = json.loads(row["metrics"]), json.loads(row["benchmark"])
        out.append({"id": row["id"], "created": row["created"], "symbol": row["symbol"],
                    "strategy": row["strategy"], "params": json.loads(row["params"]),
                    "from": row["start"], "to": row["end"],
                    "return": mine.get("total_return"), "benchmark_return": theirs.get("total_return"),
                    "sharpe": mine.get("sharpe"), "max_drawdown": mine.get("max_drawdown")})
    return out


def get_run(run_id: str) -> dict | None:
    _init()
    row = connect().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    for key in ("params", "metrics", "benchmark", "equity", "dates", "trades", "notes"):
        data[key] = json.loads(data[key] or "null")
    return data


# ---------------------------------------------------------------------------
# Paper accounts
# ---------------------------------------------------------------------------

def open_paper(symbol: str, strategy: str, params: dict, cash: float, interval: str = "1d") -> dict:
    _init()
    account_id = uuid.uuid4().hex[:8]
    connect().execute(
        "INSERT INTO paper (id, created, symbol, strategy, params, interval, cash, start_cash, "
        "position, avg_cost, last_ts, active, trades, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (account_id, time.time(), symbol.upper(), strategy, json.dumps(params), interval,
         cash, cash, 0.0, 0.0, 0, json.dumps([]), ""))
    connect().commit()
    return paper_account(account_id)


def paper_account(account_id: str) -> dict | None:
    _init()
    row = connect().execute("SELECT * FROM paper WHERE id=?", (account_id,)).fetchone()
    if row is None:
        return None
    account = dict(row)
    account["params"] = json.loads(account["params"] or "{}")
    account["trades"] = json.loads(account["trades"] or "[]")
    return account


def paper_accounts(active_only: bool = True) -> list[dict]:
    _init()
    query = "SELECT * FROM paper" + (" WHERE active=1" if active_only else "") + " ORDER BY created DESC"
    accounts = []
    for row in connect().execute(query).fetchall():
        account = dict(row)
        account["params"] = json.loads(account["params"] or "{}")
        account["trades"] = json.loads(account["trades"] or "[]")
        accounts.append(account)
    return accounts


def update_paper(account_id: str, **fields) -> None:
    _init()
    if not fields:
        return
    if "trades" in fields and not isinstance(fields["trades"], str):
        fields["trades"] = json.dumps(fields["trades"])
    assignments = ", ".join(f"{key}=?" for key in fields)
    connect().execute(f"UPDATE paper SET {assignments} WHERE id=?", (*fields.values(), account_id))
    connect().commit()


def close_paper(account_id: str) -> bool:
    _init()
    cursor = connect().execute("UPDATE paper SET active=0 WHERE id=?", (account_id,))
    connect().commit()
    return cursor.rowcount > 0
