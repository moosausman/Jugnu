"""
portfolio.py -- several sleeves at once, combined honestly.

A sleeve is one strategy on one symbol. A portfolio is a set of them with
weights, rebalanced on a schedule. This is where the interesting quant
question lives: two mediocre strategies that lose at different times can be
better together than either alone, and the only way to see it is to combine
their actual equity curves rather than their headline numbers.

Dates are aligned on the union of trading days, each sleeve forward-filled
through days its own market was shut -- otherwise a portfolio of a US stock
and BTC silently drops every weekend.

The benchmark is an equal-weight buy-and-hold of the same symbols with the
same costs, so "diversification helped" has to be proved, not assumed.
"""
from __future__ import annotations

import math

from . import metrics
from .analysis import backtest
from .data import load, periods_per_year
from .engine import Result


class Sleeve:
    def __init__(self, symbol: str, strategy: str, params: dict | None = None, weight: float = 1.0):
        self.symbol = symbol.upper()
        self.strategy = strategy
        self.params = params or {}
        self.weight = weight
        self.result: Result | None = None

    @property
    def label(self) -> str:
        return f"{self.symbol}·{self.strategy}"


def parse_sleeves(spec: str) -> list[Sleeve]:
    """"sma:AAPL, rsi:BTC-USD*2, breakout:SPY" -> sleeves (the * is a weight)."""
    sleeves = []
    for piece in spec.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        weight = 1.0
        if "*" in piece:
            piece, _, raw = piece.partition("*")
            try:
                weight = float(raw)
            except ValueError:
                weight = 1.0
        if ":" in piece:
            strategy, _, symbol = piece.partition(":")
        else:
            parts = piece.split()
            strategy, symbol = (parts + ["sma", "SPY"])[:2]
        sleeves.append(Sleeve(symbol.strip(), strategy.strip(), weight=weight))
    return sleeves


def _align(series: dict[str, tuple[list[str], list[float]]]) -> tuple[list[str], dict[str, list[float]]]:
    """One date axis, each sleeve forward-filled onto it."""
    dates = sorted({d for values in series.values() for d in values[0]})
    aligned: dict[str, list[float]] = {}
    for name, (own_dates, values) in series.items():
        lookup = dict(zip(own_dates, values))
        out, last = [], None
        for date in dates:
            last = lookup.get(date, last)
            out.append(last if last is not None else values[0])
        aligned[name] = out
    return dates, aligned


def correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    ra = [(a[i] / a[i - 1]) - 1 for i in range(1, n) if a[i - 1]]
    rb = [(b[i] / b[i - 1]) - 1 for i in range(1, n) if b[i - 1]]
    n = min(len(ra), len(rb))
    ra, rb = ra[-n:], rb[-n:]
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - mean_a) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mean_b) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else 0.0


def run(sleeves: list[Sleeve], *, start: str | None = None, end: str | None = None,
        cash: float = 10_000.0, commission: float = 1.0, slippage_bps: float = 5.0,
        rebalance: str = "monthly", interval: str = "1d") -> dict:
    """Backtests each sleeve, then combines them."""
    if not sleeves:
        raise ValueError("No sleeves to run.")
    total_weight = sum(s.weight for s in sleeves) or 1.0
    series: dict[str, tuple[list[str], list[float]]] = {}
    holds: dict[str, tuple[list[str], list[float]]] = {}

    for sleeve in sleeves:
        bars = load(sleeve.symbol, interval, start=start, end=end)
        sleeve.result = backtest(bars, sleeve.strategy, sleeve.params, symbol=sleeve.symbol,
                                 interval=interval, cash=cash, commission=commission,
                                 slippage_bps=slippage_bps)
        series[sleeve.label] = (sleeve.result.dates, sleeve.result.equity)
        holds[sleeve.label] = (sleeve.result.dates, sleeve.result.benchmark)

    dates, aligned = _align(series)
    _, aligned_holds = _align(holds)

    # Rebalance back to target weights on a schedule: between rebalances a
    # winning sleeve is allowed to grow, which is what actually happens.
    step = {"daily": 1, "weekly": 5, "monthly": 21, "quarterly": 63, "never": 10 ** 9}.get(rebalance, 21)
    equity, hold_equity = [], []
    units = {s.label: (cash * s.weight / total_weight) / aligned[s.label][0] for s in sleeves}
    hold_units = {s.label: (cash * s.weight / total_weight) / aligned_holds[s.label][0] for s in sleeves}
    rebalances = 0
    for i, _ in enumerate(dates):
        value = sum(units[s.label] * aligned[s.label][i] for s in sleeves)
        equity.append(value)
        hold_equity.append(sum(hold_units[s.label] * aligned_holds[s.label][i] for s in sleeves))
        if i and i % step == 0:
            units = {s.label: (value * s.weight / total_weight) / aligned[s.label][i] for s in sleeves}
            rebalances += 1

    per_year = periods_per_year(sleeves[0].symbol, interval)
    combined = metrics.summarise(equity, periods_per_year=per_year)
    benchmark = metrics.summarise(hold_equity, periods_per_year=per_year)

    labels = [s.label for s in sleeves]
    matrix = {a: {b: correlation(aligned[a], aligned[b]) for b in labels} for a in labels}
    best_sleeve = max(sleeves, key=lambda s: s.result.metrics["total_return"])
    diversified = (combined["max_drawdown"] > best_sleeve.result.metrics["max_drawdown"]
                   and combined["sharpe"] >= best_sleeve.result.metrics["sharpe"] * 0.95)

    return {
        "sleeves": [{"label": s.label, "symbol": s.symbol, "strategy": s.strategy,
                     "weight": s.weight / total_weight,
                     "return": s.result.metrics["total_return"],
                     "sharpe": s.result.metrics["sharpe"],
                     "max_drawdown": s.result.metrics["max_drawdown"],
                     "trades": s.result.metrics["trades"]} for s in sleeves],
        "dates": dates, "equity": equity, "benchmark": hold_equity,
        "metrics": combined, "benchmark_metrics": benchmark,
        "correlation": matrix,
        "rebalance": rebalance, "rebalances": rebalances,
        "cash": cash,
        "best_sleeve": best_sleeve.label,
        "diversification_helped": diversified,
        "notes": _notes(combined, benchmark, sleeves, matrix, diversified),
    }


def _notes(combined: dict, benchmark: dict, sleeves: list[Sleeve], matrix: dict,
           diversified: bool) -> list[str]:
    notes = []
    if combined["total_return"] < benchmark["total_return"]:
        notes.append("The portfolio lost to equal-weight buy & hold of the same symbols.")
    pairs = [(a, b, value) for a, row in matrix.items() for b, value in row.items() if a < b]
    if pairs:
        tightest = max(pairs, key=lambda p: p[2])
        if tightest[2] > 0.85:
            notes.append(f"{tightest[0]} and {tightest[1]} move together ({tightest[2]:.2f}) -- "
                         f"holding both isn't diversification, it's one bet twice.")
        loosest = min(pairs, key=lambda p: p[2])
        if loosest[2] < 0.2:
            notes.append(f"{loosest[0]} and {loosest[1]} are nearly unrelated ({loosest[2]:.2f}), "
                         f"which is where the benefit of combining them comes from.")
    best = max(sleeves, key=lambda s: s.result.metrics["total_return"])
    if best.result.metrics["total_return"] > combined["total_return"] * 1.5:
        notes.append(f"{best.label} alone beat the portfolio -- the other sleeves are dragging it down.")
    if diversified:
        notes.append("Combining them cut the worst drawdown without giving up risk-adjusted return.")
    return notes
