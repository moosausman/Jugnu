"""
strategies.py -- the strategies themselves, and the registry the CLI and
Jugnu's tools look them up in.

A strategy is a function of one Context that returns a target weight:
0 = in cash, 1 = fully invested, -1 = fully short (only when shorting is
switched on). It may look at ctx.bars[:ctx.i + 1] and nothing later.

To add one: write the function, describe its parameters, register it. It
then works everywhere -- CLI, tools, sweeps, paper trading -- with no other
changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .engine import Context


@dataclass(frozen=True)
class Spec:
    name: str
    run: Callable[[Context], float]
    description: str
    defaults: dict = field(default_factory=dict)
    ranges: dict = field(default_factory=dict)      # sensible sweep ranges: name -> (low, high, step)


REGISTRY: dict[str, Spec] = {}


def register(spec: Spec) -> Spec:
    REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> Spec:
    key = (name or "").strip().lower()
    if key not in REGISTRY:
        raise KeyError(f"No strategy called '{name}'. Available: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[key]


def describe() -> list[dict]:
    return [{"name": s.name, "description": s.description, "defaults": s.defaults,
             "ranges": s.ranges} for s in sorted(REGISTRY.values(), key=lambda s: s.name)]


# ---------------------------------------------------------------------------
# Indicators (plain Python: these run over a few thousand bars, not millions)
# ---------------------------------------------------------------------------

def sma(values: list[float], length: int) -> float | None:
    if length <= 0 or len(values) < length:
        return None
    return sum(values[-length:]) / length


def rsi(values: list[float], length: int = 14) -> float | None:
    if len(values) <= length:
        return None
    gains, losses = 0.0, 0.0
    for previous, current in zip(values[-length - 1:-1], values[-length:]):
        change = current - previous
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    if losses == 0:
        return 100.0
    strength = (gains / length) / (losses / length)
    return 100 - (100 / (1 + strength))


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def _hold(ctx: Context) -> float:
    return 1.0


def _sma_cross(ctx: Context) -> float:
    closes = ctx.closes
    fast = sma(closes, int(ctx.param("fast", 20)))
    slow = sma(closes, int(ctx.param("slow", 100)))
    if fast is None or slow is None:
        return 0.0
    return 1.0 if fast > slow else 0.0


def _rsi_reversion(ctx: Context) -> float:
    closes = ctx.closes
    value = rsi(closes, int(ctx.param("length", 14)))
    if value is None:
        return 0.0
    buy_below = float(ctx.param("buy_below", 30))
    sell_above = float(ctx.param("sell_above", 55))
    if value <= buy_below:
        return 1.0
    if value >= sell_above:
        return 0.0
    return 1.0 if ctx.position else 0.0      # in between: keep whatever we hold


def _breakout(ctx: Context) -> float:
    """Donchian-style: buy new highs, leave on new lows. The classic trend
    follower, and a useful foil to mean reversion -- they fail in opposite
    markets, which is worth seeing."""
    closes = ctx.closes
    lookback = int(ctx.param("lookback", 55))
    exit_lookback = int(ctx.param("exit", 20))
    if len(closes) < max(lookback, exit_lookback) + 1:
        return 0.0
    price = closes[-1]
    if price >= max(closes[-lookback - 1:-1]):
        return 1.0
    if price <= min(closes[-exit_lookback - 1:-1]):
        return 0.0
    return 1.0 if ctx.position else 0.0


def _volatility_target(ctx: Context) -> float:
    """Hold the market, but size the position so its volatility stays near a
    target. Shows that position sizing alone changes a return profile."""
    closes = ctx.closes
    lookback = int(ctx.param("lookback", 20))
    if len(closes) < lookback + 1:
        return 0.0
    returns = [(closes[i] / closes[i - 1]) - 1 for i in range(len(closes) - lookback, len(closes))]
    realised = stdev(returns)
    if realised <= 0:
        return 1.0
    target = float(ctx.param("target_vol", 0.02))
    return max(0.0, min(1.0, target / realised))


register(Spec("hold", _hold, "Buy on day one and never trade again. The benchmark every strategy has to beat."))
register(Spec("sma", _sma_cross, "Hold while the fast moving average is above the slow one.",
              {"fast": 20, "slow": 100}, {"fast": (5, 60, 5), "slow": (50, 250, 25)}))
register(Spec("rsi", _rsi_reversion, "Buy when RSI says oversold, sell once it recovers.",
              {"length": 14, "buy_below": 30, "sell_above": 55},
              {"length": (7, 28, 7), "buy_below": (15, 40, 5), "sell_above": (50, 75, 5)}))
register(Spec("breakout", _breakout, "Buy new highs, leave on new lows (trend following).",
              {"lookback": 55, "exit": 20}, {"lookback": (20, 120, 20), "exit": (10, 60, 10)}))
register(Spec("volatility", _volatility_target, "Always invested, but sized so volatility stays near a target.",
              {"lookback": 20, "target_vol": 0.02}, {"lookback": (10, 60, 10)}))
