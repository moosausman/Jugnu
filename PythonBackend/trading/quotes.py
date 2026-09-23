"""
quotes.py -- live prices for the terminal's market panels.

Yahoo for equities and ETFs, Binance for crypto (it answers in tens of
milliseconds and never rate-limits a watchlist-sized request). Fetched in
parallel and cached for a few seconds, so typing in the terminal never
waits on the network and a redraw costs nothing.

Nothing here is a trading feed: these are delayed, best-effort public
quotes, good enough to look at and not good enough to trade on. The
terminal says so.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ._http import get_json
from .data import connect

CACHE_SECONDS = 12
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="quote")
_lock = threading.Lock()
_cache: dict[str, tuple[float, "Quote"]] = {}

DEFAULT_MARKETS = ["SPY", "QQQ", "BTC-USD", "ETH-USD"]
DEFAULT_WATCHLIST = ["NVDA", "AAPL", "TSLA", "AMD"]


@dataclass
class Quote:
    symbol: str
    price: float = 0.0
    change: float = 0.0          # absolute, since the previous close
    change_pct: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    average_volume: float = 0.0
    currency: str = "USD"
    spark: list[float] = field(default_factory=list)
    error: str = ""

    @property
    def up(self) -> bool:
        return self.change_pct >= 0

    @property
    def volume_ratio(self) -> float:
        """Today's volume against its recent average -- the cheapest signal
        there is that something is going on with a name."""
        return self.volume / self.average_volume if self.average_volume > 0 else 0.0


def _is_crypto(symbol: str) -> bool:
    upper = symbol.upper()
    return upper.endswith(("-USD", "USDT", "-USDT"))


def _binance(symbol: str) -> Quote:
    pair = symbol.replace("-", "").upper()
    if pair.endswith("USD"):
        pair += "T"
    ticker = get_json("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": pair}, timeout=8)
    klines = get_json("https://api.binance.com/api/v3/klines",
                      params={"symbol": pair, "interval": "1h", "limit": 48}, timeout=8)
    price = float(ticker["lastPrice"])
    change = float(ticker["priceChange"])
    return Quote(symbol=symbol.upper(), price=price, change=change,
                 change_pct=float(ticker["priceChangePercent"]),
                 open=float(ticker["openPrice"]), high=float(ticker["highPrice"]),
                 low=float(ticker["lowPrice"]), volume=float(ticker["volume"]),
                 average_volume=float(ticker["volume"]),
                 spark=[float(k[4]) for k in klines])


def _yahoo(symbol: str) -> Quote:
    payload = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                       params={"range": "5d", "interval": "15m"}, timeout=10)
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"no quote for {symbol}")
    meta = result.get("meta", {})
    closes = [c for c in ((result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []) if c]
    price = float(meta.get("regularMarketPrice") or (closes[-1] if closes else 0.0))
    previous = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
    return Quote(symbol=symbol.upper(), price=price, change=price - previous,
                 change_pct=((price / previous - 1) * 100) if previous else 0.0,
                 open=float(meta.get("regularMarketOpen") or 0.0),
                 high=float(meta.get("regularMarketDayHigh") or 0.0),
                 low=float(meta.get("regularMarketDayLow") or 0.0),
                 volume=float(meta.get("regularMarketVolume") or 0.0),
                 average_volume=_average_volume(symbol),
                 currency=meta.get("currency", "USD"),
                 spark=closes[-96:])


def _average_volume(symbol: str) -> float:
    """From the daily bars already cached for backtests -- no extra request."""
    try:
        rows = connect().execute(
            "SELECT volume FROM bars WHERE symbol=? AND interval='1d' ORDER BY ts DESC LIMIT 30",
            (symbol.upper(),)).fetchall()
        volumes = [r["volume"] for r in rows if r["volume"]]
        return sum(volumes) / len(volumes) if volumes else 0.0
    except Exception:
        return 0.0


def one(symbol: str, max_age: float = CACHE_SECONDS) -> Quote:
    symbol = symbol.strip().upper()
    with _lock:
        cached = _cache.get(symbol)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
    try:
        quote = _binance(symbol) if _is_crypto(symbol) else _yahoo(symbol)
    except Exception as e:
        quote = Quote(symbol=symbol, error=str(e)[:60])
    with _lock:
        _cache[symbol] = (time.time(), quote)
    return quote


def many(symbols: list[str], max_age: float = CACHE_SECONDS) -> list[Quote]:
    if not symbols:
        return []
    return list(_pool.map(lambda s: one(s, max_age), symbols))


def cached(symbol: str) -> Quote | None:
    with _lock:
        entry = _cache.get(symbol.strip().upper())
    return entry[1] if entry else None


def warm(symbols: list[str]) -> None:
    """Fetch in the background; the caller keeps drawing the old values."""
    for symbol in symbols:
        _pool.submit(one, symbol)


def news(symbol: str, count: int = 8) -> list[dict]:
    """Recent headlines for a symbol, from Yahoo's free search endpoint --
    a general web search returns encyclopaedia pages when what you want is
    what happened this morning."""
    try:
        items = get_json("https://query1.finance.yahoo.com/v1/finance/search",
                         params={"q": symbol, "newsCount": count, "quotesCount": 0},
                         timeout=10).get("news") or []
    except Exception:
        return []
    out = []
    for item in items[:count]:
        published = item.get("providerPublishTime")
        out.append({"title": item.get("title", ""), "publisher": item.get("publisher", ""),
                    "link": item.get("link", ""),
                    "when": time.strftime("%d %b %H:%M", time.localtime(published)) if published else "",
                    "age_hours": (time.time() - published) / 3600 if published else None})
    return out


# ---------------------------------------------------------------------------
# Watchlist (kept in the same SQLite file as everything else)
# ---------------------------------------------------------------------------

def _init() -> None:
    connect().execute("CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, added REAL)")
    connect().commit()


def watchlist() -> list[str]:
    _init()
    rows = connect().execute("SELECT symbol FROM watchlist ORDER BY added").fetchall()
    return [r["symbol"] for r in rows] or list(DEFAULT_WATCHLIST)


def watch_add(symbol: str) -> list[str]:
    _init()
    current = connect().execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]
    if current == 0:
        for i, name in enumerate(DEFAULT_WATCHLIST):      # keep the defaults once edited
            connect().execute("INSERT OR IGNORE INTO watchlist VALUES (?,?)", (name, time.time() + i))
    connect().execute("INSERT OR REPLACE INTO watchlist VALUES (?,?)", (symbol.strip().upper(), time.time()))
    connect().commit()
    return watchlist()


def watch_remove(symbol: str) -> list[str]:
    _init()
    connect().execute("DELETE FROM watchlist WHERE symbol=?", (symbol.strip().upper(),))
    connect().commit()
    return watchlist()
