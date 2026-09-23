"""
data.py -- price history, cached locally.

Two free sources, no API keys and no extra packages:
  Yahoo's chart endpoint   stocks, ETFs, TSX (RY.TO), and crypto (BTC-USD)
  Binance's klines         crypto, back years, down to the minute

Everything lands in one SQLite table and is served from there afterwards, so
a backtest you run twenty times hits the network once. Bars are immutable
history: only the last one can change (today, still trading), so a refresh
only ever re-fetches the tail.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ._http import USER_AGENT as _USER_AGENT, HttpError, get_json

_TIMEOUT = 25

# Intervals we support, and how many of them make a year -- annualising
# returns needs this, and it differs per market (crypto never closes).
INTERVALS = {"1d": 1, "1h": 24, "1wk": 1}


@dataclass(frozen=True)
class Bar:
    ts: int          # epoch seconds, bar open time
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def date(self) -> str:
        return dt.datetime.fromtimestamp(self.ts, dt.timezone.utc).strftime("%Y-%m-%d")

    @property
    def stamp(self) -> str:
        return dt.datetime.fromtimestamp(self.ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


class DataError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_connections: dict[int, sqlite3.Connection] = {}


def db_path() -> Path:
    base = Path(os.environ.get("JUGNU_DATA_DIR") or Path.home() / "Library" / "Application Support" / "Jugnu")
    base.mkdir(parents=True, exist_ok=True)
    return base / "trading.db"


def connect() -> sqlite3.Connection:
    """One connection per thread: SQLite objects aren't shareable, and the
    backend calls this from tool threads."""
    key = threading.get_ident()
    with _lock:
        conn = _connections.get(key)
        if conn is None:
            conn = sqlite3.connect(db_path(), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            _init(conn)
            _connections[key] = conn
        return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, interval, ts)
        );
        CREATE TABLE IF NOT EXISTS fetches (
            symbol TEXT NOT NULL, interval TEXT NOT NULL, fetched REAL,
            PRIMARY KEY (symbol, interval)
        );
    """)
    conn.commit()


def _cached(symbol: str, interval: str) -> list[Bar]:
    rows = connect().execute(
        "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? AND interval=? ORDER BY ts",
        (symbol, interval)).fetchall()
    return [Bar(r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


# Roughly how far apart bars of each interval should sit, in seconds. A
# source that quietly coarsens the data (Yahoo does) is caught here instead
# of silently turning a daily backtest into a quarterly one.
_SPACING = {"1d": (20 * 3600, 5 * 86400), "1h": (1800, 4 * 86400), "1wk": (5 * 86400, 12 * 86400)}


def _check_spacing(symbol: str, interval: str, bars: list[Bar]) -> None:
    if len(bars) < 20:
        return
    gaps = sorted(bars[i + 1].ts - bars[i].ts for i in range(len(bars) - 1))
    median = gaps[len(gaps) // 2]
    low, high = _SPACING[interval]
    if not low <= median <= high:
        raise DataError(
            f"{symbol}: the source returned bars about {median / 86400:.0f} days apart for a "
            f"'{interval}' request, not the interval asked for. Refusing to backtest on them.")


def _store(symbol: str, interval: str, bars: list[Bar]) -> None:
    conn = connect()
    # A fetch always brings the full history for that interval, so replace
    # rather than merge -- otherwise bars from an earlier, coarser fetch
    # would mix in with the good ones.
    conn.execute("DELETE FROM bars WHERE symbol=? AND interval=?", (symbol, interval))
    conn.executemany(
        "INSERT OR REPLACE INTO bars (symbol, interval, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(symbol, interval, b.ts, b.open, b.high, b.low, b.close, b.volume) for b in bars])
    conn.execute("INSERT OR REPLACE INTO fetches (symbol, interval, fetched) VALUES (?,?,?)",
                 (symbol, interval, time.time()))
    conn.commit()


def _fetched_at(symbol: str, interval: str) -> float:
    row = connect().execute("SELECT fetched FROM fetches WHERE symbol=? AND interval=?",
                            (symbol, interval)).fetchone()
    return row["fetched"] if row else 0.0


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _yahoo(symbol: str, interval: str) -> list[Bar]:
    # Explicit epochs, never range="max": asked for max, Yahoo silently
    # hands back QUARTERLY bars for a daily request -- which sails straight
    # through a backtest and produces nonsense. period1/period2 gives the
    # real daily history. Intraday is capped at ~2 years either way.
    now = int(time.time())
    period1 = 0 if interval in ("1d", "1wk") else now - 720 * 86400
    try:
        payload = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                           params={"period1": period1, "period2": now + 86400,
                                   "interval": interval, "includePrePost": "false"},
                           timeout=_TIMEOUT)
    except HttpError as e:
        raise DataError(f"Couldn't reach Yahoo for {symbol}: {e}") from e
    chart = (payload.get("chart") or {})
    if chart.get("error"):
        raise DataError(f"{symbol}: {chart['error'].get('description') or chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise DataError(f"No data for {symbol}. Check the ticker (TSX needs .TO, e.g. RY.TO).")
    result = results[0]
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, ts in enumerate(stamps):
        row = [quote.get(k, [None] * len(stamps))[i] for k in ("open", "high", "low", "close", "volume")]
        if any(v is None for v in row[:4]):
            continue        # holidays and halts come back as nulls
        bars.append(Bar(int(ts), float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4] or 0)))
    return bars


def _binance(symbol: str, interval: str) -> list[Bar]:
    pair = symbol.replace("-", "").replace("/", "").upper()
    if pair.endswith("USD"):
        pair += "T"                      # BTC-USD -> BTCUSDT, the liquid pair
    bars: list[Bar] = []
    start = 0
    while True:
        try:
            rows = get_json("https://api.binance.com/api/v3/klines",
                            params={"symbol": pair, "interval": interval,
                                    "limit": 1000, "startTime": start},
                            timeout=_TIMEOUT)
        except HttpError as e:
            raise DataError(f"Couldn't reach Binance for {pair}: {e}") from e
        if not rows:
            break
        for row in rows:
            bars.append(Bar(int(row[0]) // 1000, float(row[1]), float(row[2]), float(row[3]),
                            float(row[4]), float(row[5])))
        if len(rows) < 1000:
            break
        start = int(rows[-1][0]) + 1
        if len(bars) > 200_000:
            break
    return bars


def _is_crypto(symbol: str) -> bool:
    upper = symbol.upper()
    return upper.endswith(("-USD", "USDT", "-USDT")) or "/" in upper


def periods_per_year(symbol: str, interval: str) -> float:
    """Trading periods in a year -- 252 days for markets that close, 365 for
    crypto. Sharpe and CAGR are wrong by 20% if this is wrong."""
    days = 365.0 if _is_crypto(symbol) else 252.0
    return days * INTERVALS.get(interval, 1)


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def load(symbol: str, interval: str = "1d", start: str | None = None, end: str | None = None,
         refresh: bool = False, source: str | None = None) -> list[Bar]:
    """Bars for `symbol`, from the cache when it's fresh enough.

    start/end are "YYYY-MM-DD" and inclusive. Raises DataError with something
    a person can act on when the symbol is wrong or the source is down."""
    symbol = symbol.strip().upper()
    if interval not in INTERVALS:
        raise DataError(f"interval must be one of {', '.join(INTERVALS)}")
    bars = _cached(symbol, interval)
    stale = time.time() - _fetched_at(symbol, interval) > (6 * 3600 if interval == "1d" else 900)
    if refresh or not bars or stale:
        picked = source or ("binance" if _is_crypto(symbol) and interval != "1wk" else "yahoo")
        try:
            fetched = _binance(symbol, interval) if picked == "binance" else _yahoo(symbol, interval)
        except DataError:
            if bars:
                fetched = []          # keep serving what we have rather than failing a backtest
            elif picked == "binance":
                fetched = _yahoo(symbol, interval)   # crypto Yahoo also has, just coarser
            else:
                raise
        if fetched:
            _check_spacing(symbol, interval, fetched)
            _store(symbol, interval, fetched)
            bars = _cached(symbol, interval)
    if not bars:
        raise DataError(f"No price history for {symbol}.")
    return window(bars, start, end)


def window(bars: list[Bar], start: str | None, end: str | None) -> list[Bar]:
    def epoch(day: str, endish: bool) -> int:
        value = dt.datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        if endish:
            value += dt.timedelta(days=1)
        return int(value.timestamp())
    low = epoch(start, False) if start else None
    high = epoch(end, True) if end else None
    return [b for b in bars if (low is None or b.ts >= low) and (high is None or b.ts < high)]


def latest(symbol: str, interval: str = "1d") -> Bar:
    return load(symbol, interval, refresh=True)[-1]
