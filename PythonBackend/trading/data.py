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

# How many minutes each bar covers. Day trading needs the short ones, and
# annualising a return needs to know how many of them make a year -- which
# differs per market, because crypto never closes (1440 minutes a day)
# while a US session is 390.
BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 0, "1wk": 0}
INTERVALS = BAR_MINUTES          # kept as the name the rest of the code knows

# How far back each source will go for a given bar size. Yahoo refuses more.
YAHOO_SPAN_DAYS = {"1m": 7, "5m": 59, "15m": 59, "30m": 59, "1h": 720}


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
def _spacing(interval: str) -> tuple[float, float]:
    """What gap between bars is plausible for this interval. Markets close,
    so the median gap for a 5-minute bar is 5 minutes but the mean isn't."""
    if interval == "1d":
        return 20 * 3600, 5 * 86_400
    if interval == "1wk":
        return 5 * 86_400, 12 * 86_400
    minutes = BAR_MINUTES.get(interval, 60)
    return minutes * 50, minutes * 90


def _check_spacing(symbol: str, interval: str, bars: list[Bar]) -> None:
    if len(bars) < 20:
        return
    gaps = sorted(bars[i + 1].ts - bars[i].ts for i in range(len(bars) - 1))
    median = gaps[len(gaps) // 2]
    low, high = _spacing(interval)
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
    span = YAHOO_SPAN_DAYS.get(interval)
    period1 = 0 if span is None else now - span * 86_400
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


# How much intraday history is worth pulling. Binance will happily serve
# minute bars back to 2017, which is 4 million rows and, paged from the
# start, never reaches today -- the reason a 5-minute chart of BTC ended in
# 2019 the first time.
_BINANCE_SPAN_DAYS = {"1m": 30, "5m": 180, "15m": 365, "30m": 730, "1h": 1460}


def _binance(symbol: str, interval: str) -> list[Bar]:
    pair = symbol.replace("-", "").replace("/", "").upper()
    if pair.endswith("USD"):
        pair += "T"                      # BTC-USD -> BTCUSDT, the liquid pair
    bars: list[Bar] = []
    span = _BINANCE_SPAN_DAYS.get(interval)
    start = 0 if span is None else int((time.time() - span * 86_400) * 1000)
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
    """Bars in a year. 252 days for markets that close, 365 for crypto --
    and for intraday, how many bars fit in a session: 1440 minutes a day
    for crypto, 390 for a US equity session. Sharpe and CAGR are badly
    wrong if this is."""
    crypto = _is_crypto(symbol)
    days = 365.0 if crypto else 252.0
    if interval == "1wk":
        return days / 7
    minutes = BAR_MINUTES.get(interval, 0)
    if not minutes:                       # daily
        return days
    session = 1440.0 if crypto else 390.0
    return days * max(1.0, session / minutes)


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
    age = time.time() - _fetched_at(symbol, interval)
    minutes = BAR_MINUTES.get(interval, 0)
    stale = age > (6 * 3600 if interval in ("1d", "1wk") else max(30.0, minutes * 30))
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


def recent(symbol: str, interval: str = "1m", count: int = 300) -> list[Bar]:
    """Just the last `count` bars, fetched directly.

    A live chart can't call load(refresh=True): that re-pulls the whole
    span -- thirty days of minute bars is forty round trips and a rewrite
    of the cache, every tick. This asks the source for the tail only, and
    merges it into whatever is already cached."""
    symbol = symbol.strip().upper()
    count = max(10, min(1000, count))
    try:
        if _is_crypto(symbol) and interval != "1wk":
            pair = symbol.replace("-", "").upper()
            if pair.endswith("USD"):
                pair += "T"
            rows = get_json("https://api.binance.com/api/v3/klines",
                            params={"symbol": pair, "interval": interval, "limit": count},
                            timeout=_TIMEOUT)
            fresh = [Bar(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]),
                         float(r[4]), float(r[5])) for r in rows]
        else:
            minutes = BAR_MINUTES.get(interval, 0) or 1440
            days = max(1, int(count * minutes / 390) + 2)
            now = int(time.time())
            payload = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                               params={"period1": now - days * 86_400, "period2": now + 86_400,
                                       "interval": interval}, timeout=_TIMEOUT)
            result = ((payload.get("chart") or {}).get("result") or [None])[0]
            if not result:
                return _cached(symbol, interval)[-count:]
            stamps = result.get("timestamp") or []
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            fresh = []
            for i, ts in enumerate(stamps):
                row = [quote.get(k, [None] * len(stamps))[i]
                       for k in ("open", "high", "low", "close", "volume")]
                if any(v is None for v in row[:4]):
                    continue
                fresh.append(Bar(int(ts), float(row[0]), float(row[1]), float(row[2]),
                                 float(row[3]), float(row[4] or 0)))
    except HttpError:
        return _cached(symbol, interval)[-count:]
    if not fresh:
        return _cached(symbol, interval)[-count:]
    conn = connect()
    conn.executemany(
        "INSERT OR REPLACE INTO bars (symbol, interval, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(symbol, interval, b.ts, b.open, b.high, b.low, b.close, b.volume) for b in fresh])
    conn.commit()
    return _cached(symbol, interval)[-count:]


def latest(symbol: str, interval: str = "1d") -> Bar:
    return load(symbol, interval, refresh=True)[-1]
