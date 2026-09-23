"""
signals.py -- what each strategy says right now.

A backtest tells you what a rule would have done. This tells you what it
says today, across a list of symbols, which is the part that makes a
strategy an algorithm rather than a story: the same code decides, every
bar, without anyone's mood involved.

For each symbol and strategy it asks the strategy twice -- once as if flat,
once as if already holding -- because most rules answer differently
depending on where you stand ("buy below 30, sell above 60" holds through
the middle). That gives three honest states:

    enter   it would open a position today
    hold    it would stay in, but wouldn't start here
    out     it wants nothing to do with this

and a "changed" flag when today's answer differs from yesterday's, which is
the only line worth acting on.

Nothing here places an order. Jugnu's paper accounts execute these same
signals on live prices with imaginary money -- see paper.py.
"""
from __future__ import annotations

from .data import load
from .engine import Context
from .strategies import get, REGISTRY


def _target(spec, bars, closes, index: int, position: float) -> float:
    ctx = Context(bars, closes, index, position, 10_000.0, 10_000.0, dict(spec.defaults), 0.0)
    try:
        return max(0.0, min(1.0, float(spec.run(ctx) or 0.0)))
    except Exception:
        return 0.0


def state(spec, bars, closes, index: int) -> str:
    if _target(spec, bars, closes, index, 0.0) >= 0.5:
        return "enter"
    if _target(spec, bars, closes, index, 1.0) >= 0.5:
        return "hold"
    return "out"


def for_symbol(symbol: str, strategies: list[str] | None = None, interval: str = "1d") -> list[dict]:
    bars = load(symbol, interval)
    if len(bars) < 30:
        raise ValueError(f"not enough history for {symbol}")
    closes = [b.close for b in bars]
    names = strategies or [n for n in sorted(REGISTRY) if n != "hold"]
    out = []
    for name in names:
        spec = get(name)
        now = state(spec, bars, closes, len(bars) - 1)
        before = state(spec, bars, closes, len(bars) - 2)
        out.append({
            "symbol": symbol.upper(), "strategy": name,
            "state": now, "was": before, "changed": now != before,
            "price": closes[-1], "date": bars[-1].date,
        })
    return out


def scan(symbols: list[str], strategies: list[str] | None = None,
         interval: str = "1d") -> list[dict]:
    rows = []
    for symbol in symbols:
        try:
            rows += for_symbol(symbol, strategies, interval)
        except Exception as e:
            rows.append({"symbol": symbol.upper(), "strategy": "—", "error": str(e)[:60]})
    return rows


def summary(rows: list[dict]) -> str:
    changed = [r for r in rows if r.get("changed")]
    if not changed:
        return "Nothing changed today: every strategy says the same as yesterday."
    entries = [r for r in changed if r["state"] == "enter"]
    exits = [r for r in changed if r["state"] == "out"]
    parts = []
    if entries:
        parts.append("would open: " + ", ".join(f"{r['strategy']} on {r['symbol']}" for r in entries))
    if exits:
        parts.append("would close: " + ", ".join(f"{r['strategy']} on {r['symbol']}" for r in exits))
    return "Changed today -- " + "; ".join(parts) + "."
