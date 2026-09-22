"""
metrics.py -- what actually happened, in numbers, plus the warnings.

Every figure is computed from the equity curve the engine marked at each
bar close (costs already taken out), never from the trade list, so the
headline number can't disagree with the chart.

The warnings matter as much as the numbers. A backtest that reports +142%
and stays quiet about buy-and-hold's +487% is lying by omission, so
`warnings()` says it out loud.
"""
from __future__ import annotations

import math


def _returns(equity: list[float]) -> list[float]:
    return [(equity[i] / equity[i - 1]) - 1 for i in range(1, len(equity)) if equity[i - 1] > 0]


def summarise(equity: list[float], *, periods_per_year: float, trades: list | None = None,
              exposure: float = 0.0) -> dict:
    if len(equity) < 2 or equity[0] <= 0:
        return {"total_return": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
                "sortino": 0.0, "volatility": 0.0, "trades": 0, "win_rate": None,
                "exposure": exposure, "final": equity[-1] if equity else 0.0}
    returns = _returns(equity)
    total = equity[-1] / equity[0] - 1
    years = max(len(equity) / periods_per_year, 1e-9)
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1 if equity[-1] > 0 else -1.0

    mean = sum(returns) / len(returns) if returns else 0.0
    spread = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) if len(returns) > 1 else 0.0
    downside = [r for r in returns if r < 0]
    down_spread = math.sqrt(sum(r * r for r in downside) / len(downside)) if downside else 0.0
    scale = math.sqrt(periods_per_year)

    peak, drawdown = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)

    closed = [t for t in (trades or []) if getattr(t, "pnl", None) is not None]
    wins = [t for t in closed if t.pnl > 0]

    return {
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": drawdown,
        "volatility": spread * scale,
        "sharpe": (mean / spread * scale) if spread > 0 else 0.0,
        "sortino": (mean / down_spread * scale) if down_spread > 0 else 0.0,
        "trades": len(trades or []),
        "round_trips": len(closed),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "exposure": exposure,
        "final": equity[-1],
    }


def warnings(result_metrics: dict, benchmark_metrics: dict, *, cost_curve: dict | None = None,
             bars: int = 0, trades: int = 0) -> list[str]:
    """The things a person should be told before they get excited."""
    notes: list[str] = []
    if result_metrics["total_return"] < benchmark_metrics.get("total_return", 0):
        notes.append("Underperforms buy & hold after costs.")
    if result_metrics["total_return"] <= 0:
        notes.append("Loses money over this period.")
    if result_metrics["max_drawdown"] < -0.5:
        notes.append(f"Drops {abs(result_metrics['max_drawdown']):.0%} at its worst -- hard to sit through.")
    if cost_curve:
        for level, total in sorted(cost_curve.items()):
            if total <= 0 and result_metrics["total_return"] > 0:
                notes.append(f"Turns negative once slippage reaches {level:g}bps.")
                break
    if trades and bars and trades / max(bars, 1) > 0.25:
        notes.append("Trades on more than a quarter of all bars; costs dominate at that rate.")
    if 0 < trades < 10:
        notes.append(f"Only {trades} trades -- too few to tell skill from luck.")
    if bars and bars < 250:
        notes.append("Less than a year of data; the result is close to meaningless.")
    return notes


def format_percent(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:+.1f}%"


def table(result) -> list[tuple[str, str, str]]:
    """(label, strategy, benchmark) rows, for the CLI and the app."""
    strategy, benchmark = result.metrics, result.benchmark_metrics
    rows = [
        ("Return", format_percent(strategy["total_return"]), format_percent(benchmark["total_return"])),
        ("CAGR", format_percent(strategy["cagr"]), format_percent(benchmark["cagr"])),
        ("Max drawdown", format_percent(strategy["max_drawdown"]), format_percent(benchmark["max_drawdown"])),
        ("Volatility", format_percent(strategy["volatility"]), format_percent(benchmark["volatility"])),
        ("Sharpe", f"{strategy['sharpe']:.2f}", f"{benchmark['sharpe']:.2f}"),
        ("Sortino", f"{strategy['sortino']:.2f}", f"{benchmark['sortino']:.2f}"),
        ("Final value", f"${strategy['final']:,.0f}", f"${benchmark['final']:,.0f}"),
        ("Trades", str(strategy["trades"]), "1"),
        ("Win rate", "--" if strategy["win_rate"] is None else f"{strategy['win_rate'] * 100:.1f}%", "--"),
        ("Time invested", f"{strategy['exposure'] * 100:.0f}%", "100%"),
    ]
    return rows
