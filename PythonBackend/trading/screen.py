"""
screen.py -- scanning a list of symbols for the states worth looking at.

Not a stock screener in the "find me winners" sense. It computes the same
handful of facts for every name -- where it sits in its year, what RSI
says, whether today's volume is unusual, how far it is from its moving
averages, how violent it's been -- and lets you filter on them.

    screen                     everything on the watchlist
    screen rsi<30              oversold
    screen vol>2 near_high>0.95

Everything is computed from bars already cached for backtests, so a scan of
twenty names costs nothing after the first run.
"""
from __future__ import annotations

import re

from .data import load
from .strategies import rsi, sma, stdev

FIELDS = {
    "price": "last close",
    "change": "% change on the day",
    "rsi": "14-day RSI (under 30 oversold, over 70 overbought)",
    "vol": "today's volume against its 30-day average",
    "near_high": "close as a fraction of the 52-week high (1.0 = at the high)",
    "near_low": "close as a fraction of the 52-week low",
    "above_sma50": "% above (or below) the 50-day average",
    "above_sma200": "% above (or below) the 200-day average",
    "mom20": "% change over 20 bars",
    "mom120": "% change over 120 bars",
    "atr": "average daily range as a % of price -- how violent it is",
    "trend": "1 when the 50-day is above the 200-day, else 0",
}

_FILTER = re.compile(r"([a-z_0-9]+)\s*(>=|<=|>|<|=)\s*(-?[\d.]+)")


def facts(symbol: str, interval: str = "1d") -> dict:
    bars = load(symbol, interval)
    if len(bars) < 30:
        raise ValueError(f"not enough history for {symbol}")
    closes = [b.close for b in bars]
    year = bars[-252:] if len(bars) >= 252 else bars
    highs = [b.high for b in year]
    lows = [b.low for b in year]
    ranges = [(b.high - b.low) / b.close for b in bars[-20:] if b.close]
    volumes = [b.volume for b in bars[-31:-1] if b.volume]
    fifty = sma(closes, 50)
    two_hundred = sma(closes, 200)
    price = closes[-1]
    return {
        "symbol": symbol.upper(),
        "price": price,
        "change": (price / closes[-2] - 1) * 100 if len(closes) > 1 and closes[-2] else 0.0,
        "rsi": rsi(closes, 14) or 50.0,
        "vol": (bars[-1].volume / (sum(volumes) / len(volumes))) if volumes else 0.0,
        "near_high": price / max(highs) if highs else 0.0,
        "near_low": price / min(lows) if lows and min(lows) else 0.0,
        "above_sma50": ((price / fifty - 1) * 100) if fifty else 0.0,
        "above_sma200": ((price / two_hundred - 1) * 100) if two_hundred else 0.0,
        "mom20": (price / closes[-21] - 1) * 100 if len(closes) > 21 else 0.0,
        "mom120": (price / closes[-121] - 1) * 100 if len(closes) > 121 else 0.0,
        "atr": (sum(ranges) / len(ranges) * 100) if ranges else 0.0,
        "trend": 1.0 if (fifty and two_hundred and fifty > two_hundred) else 0.0,
        "volatility": stdev([(closes[i] / closes[i - 1]) - 1 for i in range(-20, 0)]) * 100,
    }


def parse_filters(text: str) -> list[tuple[str, str, float]]:
    out = []
    for name, operator, value in _FILTER.findall((text or "").lower()):
        if name in FIELDS or name == "volatility":
            out.append((name, operator, float(value)))
    return out


def passes(row: dict, filters: list[tuple[str, str, float]]) -> bool:
    for name, operator, value in filters:
        actual = row.get(name)
        if actual is None:
            return False
        if operator == ">" and not actual > value:
            return False
        if operator == ">=" and not actual >= value:
            return False
        if operator == "<" and not actual < value:
            return False
        if operator == "<=" and not actual <= value:
            return False
        if operator == "=" and abs(actual - value) > 1e-9:
            return False
    return True


def scan(symbols: list[str], filters: str = "", interval: str = "1d") -> list[dict]:
    parsed = parse_filters(filters)
    rows = []
    for symbol in symbols:
        try:
            row = facts(symbol, interval)
        except Exception as e:
            rows.append({"symbol": symbol.upper(), "error": str(e)[:60]})
            continue
        row["flags"] = flags(row)
        if passes(row, parsed):
            rows.append(row)
    return rows


def flags(row: dict) -> list[str]:
    """The short words the terminal prints beside a row: what's notable."""
    out = []
    if row["rsi"] <= 30:
        out.append("oversold")
    elif row["rsi"] >= 70:
        out.append("overbought")
    if row["vol"] >= 2:
        out.append("volume spike")
    if row["near_high"] >= 0.98:
        out.append("52w high")
    elif row["near_low"] <= 1.02:
        out.append("52w low")
    if row["trend"] and row["above_sma50"] > 0:
        out.append("uptrend")
    elif not row["trend"] and row["above_sma50"] < 0:
        out.append("downtrend")
    if row["atr"] >= 5:
        out.append("wild")
    return out
