"""
analysis.py -- four ways of finding out whether a good backtest meant
anything. Each one exists because a single backtest can't tell you.

  cost_sensitivity  re-runs at higher slippage. Most strategies that look
                    good at 0bps are dead at 20.
  sweep             every parameter combination, so you can see whether
                    your winner is a broad plateau or one lucky pixel.
  walk_forward      pick parameters on one stretch, judge them on the next
                    stretch they never saw. The only honest tuning.
  monte_carlo       reshuffles the order of your trades a thousand times.
                    One equity curve is one sample; this is the range.
"""
from __future__ import annotations

import itertools
import random

from . import metrics
from .data import Bar, periods_per_year
from .engine import Result, run
from .strategies import get


def backtest(bars: list[Bar], strategy: str, params: dict | None = None, *, symbol: str = "",
             interval: str = "1d", cash: float = 10_000.0, commission: float = 1.0,
             slippage_bps: float = 5.0, allow_short: bool = False) -> Result:
    """One run, with its metrics and its benchmark's filled in."""
    spec = get(strategy)
    merged = {**spec.defaults, **(params or {})}
    result = run(bars, spec.run, name=spec.name, symbol=symbol, interval=interval, params=merged,
                 cash=cash, commission=commission, slippage_bps=slippage_bps, allow_short=allow_short)
    per_year = periods_per_year(symbol or "", interval)
    result.metrics = metrics.summarise(result.equity, periods_per_year=per_year,
                                       trades=result.trades, exposure=result.exposure)
    result.benchmark_metrics = metrics.summarise(result.benchmark, periods_per_year=per_year, exposure=1.0)
    return result


def cost_sensitivity(bars: list[Bar], strategy: str, params: dict | None = None, *, symbol: str = "",
                     interval: str = "1d", cash: float = 10_000.0, commission: float = 1.0,
                     levels: tuple[float, ...] = (0.0, 5.0, 20.0, 50.0)) -> dict[float, float]:
    """{slippage in bps: total return}. The shape of this is the single most
    useful thing in the whole module."""
    out: dict[float, float] = {}
    for level in levels:
        result = backtest(bars, strategy, params, symbol=symbol, interval=interval,
                          cash=cash, commission=commission, slippage_bps=level)
        out[level] = result.metrics["total_return"]
    return out


def grid(ranges: dict) -> list[dict]:
    """Every combination from {name: (low, high, step)}."""
    names, values = [], []
    for name, span in ranges.items():
        low, high, step = span
        points = []
        current = low
        while current <= high:
            points.append(round(current, 6) if isinstance(step, float) else int(current))
            current += step
        names.append(name)
        values.append(points)
    return [dict(zip(names, combo)) for combo in itertools.product(*values)]


def sweep(bars: list[Bar], strategy: str, ranges: dict | None = None, *, symbol: str = "",
          interval: str = "1d", cash: float = 10_000.0, commission: float = 1.0,
          slippage_bps: float = 5.0, limit: int = 400) -> dict:
    """Runs the grid and reports the best -- with how alone it stands.

    `robustness` is the best result's neighbourhood: the share of all
    combinations that also beat buy & hold. A winner surrounded by losers is
    noise you've curve-fitted; a winner in a field of winners is a real
    effect (or a rising market)."""
    spec = get(strategy)
    combos = grid(ranges or spec.ranges)
    if not combos:
        raise ValueError(f"{strategy} has no parameters to sweep.")
    if len(combos) > limit:
        raise ValueError(f"That grid is {len(combos)} runs; narrow it (limit {limit}).")
    rows = []
    benchmark_return = None
    for params in combos:
        result = backtest(bars, strategy, params, symbol=symbol, interval=interval, cash=cash,
                          commission=commission, slippage_bps=slippage_bps)
        benchmark_return = result.benchmark_metrics["total_return"]
        rows.append({"params": params, "return": result.metrics["total_return"],
                     "sharpe": result.metrics["sharpe"], "drawdown": result.metrics["max_drawdown"],
                     "trades": result.metrics["trades"]})
    rows.sort(key=lambda r: r["sharpe"], reverse=True)
    beating = [r for r in rows if r["return"] > (benchmark_return or 0)]
    return {
        "strategy": strategy, "symbol": symbol, "runs": len(rows),
        "benchmark_return": benchmark_return,
        "best": rows[0], "worst": rows[-1], "rows": rows,
        "robustness": len(beating) / len(rows),
        "median_return": sorted(r["return"] for r in rows)[len(rows) // 2],
    }


def walk_forward(bars: list[Bar], strategy: str, ranges: dict | None = None, *, symbol: str = "",
                 interval: str = "1d", folds: int = 4, cash: float = 10_000.0,
                 commission: float = 1.0, slippage_bps: float = 5.0) -> dict:
    """Tune on each fold, trade the next one blind. The out-of-sample total
    is the only number here worth believing."""
    spec = get(strategy)
    if folds < 2 or len(bars) < folds * 60:
        raise ValueError("Not enough history to walk forward; use a longer period or fewer folds.")
    size = len(bars) // folds
    results = []
    for fold in range(folds - 1):
        train = bars[fold * size:(fold + 1) * size]
        test = bars[(fold + 1) * size:(fold + 2) * size]
        if len(train) < 30 or len(test) < 30:
            continue
        best = sweep(train, strategy, ranges or spec.ranges, symbol=symbol, interval=interval,
                     cash=cash, commission=commission, slippage_bps=slippage_bps)["best"]
        out = backtest(test, strategy, best["params"], symbol=symbol, interval=interval,
                       cash=cash, commission=commission, slippage_bps=slippage_bps)
        results.append({
            "fold": fold + 1, "params": best["params"],
            "in_sample_return": best["return"],
            "out_of_sample_return": out.metrics["total_return"],
            "benchmark_return": out.benchmark_metrics["total_return"],
            "from": out.dates[0], "to": out.dates[-1],
        })
    if not results:
        raise ValueError("Not enough history to walk forward.")
    compounded = 1.0
    benchmark = 1.0
    for row in results:
        compounded *= 1 + row["out_of_sample_return"]
        benchmark *= 1 + row["benchmark_return"]
    return {
        "folds": results,
        "out_of_sample_return": compounded - 1,
        "benchmark_return": benchmark - 1,
        "beat_benchmark": compounded > benchmark,
        "consistent": all(r["out_of_sample_return"] > 0 for r in results),
    }


def monte_carlo(result: Result, *, runs: int = 1000, seed: int = 7) -> dict:
    """Resamples this run's own trade results, with replacement, to show the
    spread of outcomes the same edge could plausibly have produced."""
    closed = [t.pnl for t in result.trades if t.pnl is not None]
    if len(closed) < 5:
        return {"enough_trades": False, "round_trips": len(closed)}
    rng = random.Random(seed)
    finals = []
    for _ in range(runs):
        equity = result.cash
        for _ in closed:
            equity += rng.choice(closed)
        finals.append(equity / result.cash - 1)
    finals.sort()

    def percentile(p: float) -> float:
        return finals[max(0, min(len(finals) - 1, int(p * len(finals))))]

    return {
        "enough_trades": True,
        "round_trips": len(closed),
        "runs": runs,
        "p05": percentile(0.05), "p50": percentile(0.50), "p95": percentile(0.95),
        "losing_share": sum(1 for f in finals if f < 0) / len(finals),
        "actual": result.metrics["total_return"],
    }
