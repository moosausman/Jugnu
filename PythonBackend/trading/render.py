"""
render.py -- turning a run into something readable, in one place, so the
CLI, Jugnu's chat replies and the saved report all say the same thing.

The equity chart is drawn with block characters: no plotting library, works
over SSH, and pastes into a chat window intact.
"""
from __future__ import annotations

from . import metrics
from .engine import Result

_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 60) -> str:
    if not values:
        return ""
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    low, high = min(sampled), max(sampled)
    if high - low < 1e-12:
        return _BLOCKS[0] * len(sampled)
    return "".join(_BLOCKS[min(len(_BLOCKS) - 1, int((v - low) / (high - low) * (len(_BLOCKS) - 1)))]
                   for v in sampled)


def chart(result: Result, width: int = 64, height: int = 9) -> str:
    """Strategy equity against buy & hold, same axes -- the comparison is
    the point, so they're never drawn apart."""
    def sample(series: list[float]) -> list[float]:
        step = max(1, len(series) // width)
        return series[::step][:width]

    strategy, benchmark = sample(result.equity), sample(result.benchmark)
    low = min(min(strategy), min(benchmark))
    high = max(max(strategy), max(benchmark))
    if high - low < 1e-9:
        return ""
    rows = []
    for level in range(height, 0, -1):
        top = low + (high - low) * level / height
        bottom = low + (high - low) * (level - 1) / height
        line = ""
        for i in range(len(strategy)):
            here = bottom <= strategy[i] < top or (level == height and strategy[i] >= top)
            bench = i < len(benchmark) and (bottom <= benchmark[i] < top
                                            or (level == height and benchmark[i] >= top))
            line += "█" if here else ("·" if bench else " ")
        label = f"{top:>9,.0f}"
        rows.append(f"{label} │{line}")
    rows.append(" " * 10 + "└" + "─" * len(strategy))
    rows.append(" " * 11 + f"{result.dates[0]}" + " " * max(1, len(strategy) - 21) + f"{result.dates[-1]}")
    return "\n".join(rows)


def summary(result: Result, *, notes: list[str] | None = None, width: int = 64,
            with_chart: bool = True) -> str:
    params = " ".join(f"{k}={v}" for k, v in result.params.items())
    head = f"{result.symbol} · {result.strategy}({params}) · {result.dates[0]} → {result.dates[-1]}"
    costs = f"{result.slippage_bps:g}bps slippage, ${result.commission:g}/trade, ${result.cash:,.0f} start"
    lines = [head, costs, ""]
    lines.append(f"{'':<16}{'strategy':>14}{'buy & hold':>14}")
    for label, mine, theirs in metrics.table(result):
        lines.append(f"{label:<16}{mine:>14}{theirs:>14}")
    for note in notes or []:
        lines.append(f"  ⚠ {note}")
    if with_chart:
        drawing = chart(result, width=width)
        if drawing:
            lines += ["", drawing, "", "  █ strategy   · buy & hold"]
    return "\n".join(lines)


def compact(result: Result, notes: list[str] | None = None) -> str:
    """A few lines for a chat reply: the numbers that decide, plus the
    warnings, plus one sparkline."""
    mine, theirs = result.metrics, result.benchmark_metrics
    lines = [
        f"**{result.symbol} · {result.strategy}** {result.dates[0]} → {result.dates[-1]}"
        f" ({result.slippage_bps:g}bps, ${result.commission:g}/trade)",
        f"- Return **{metrics.format_percent(mine['total_return'])}** vs buy & hold "
        f"{metrics.format_percent(theirs['total_return'])}",
        f"- Max drawdown {metrics.format_percent(mine['max_drawdown'])} · Sharpe {mine['sharpe']:.2f}"
        f" · {mine['trades']} trades"
        + (f" · {mine['win_rate'] * 100:.0f}% winners" if mine["win_rate"] is not None else ""),
        f"- Ends at ${mine['final']:,.0f} from ${result.cash:,.0f}",
    ]
    for note in notes or []:
        lines.append(f"- ⚠ {note}")
    lines.append("`" + sparkline(result.equity) + "`")
    return "\n".join(lines)


def cost_table(curve: dict[float, float]) -> str:
    return "\n".join(f"  {level:>5g}bps  {metrics.format_percent(total):>9}"
                     for level, total in sorted(curve.items()))


def heatmap(sweep_result: dict, width: int = 2) -> str:
    """Sweeps over one or two parameters print as a grid; more than that
    prints as a ranked list, because a 4-D heatmap helps nobody."""
    rows = sweep_result["rows"]
    names = sorted(rows[0]["params"].keys())
    if len(names) != 2:
        lines = ["  " + " ".join(f"{k}={v}" for k, v in r["params"].items())
                 + f"   return {metrics.format_percent(r['return'])}  sharpe {r['sharpe']:.2f}"
                 for r in rows[:12]]
        return "\n".join(lines)
    x_name, y_name = names
    xs = sorted({r["params"][x_name] for r in rows})
    ys = sorted({r["params"][y_name] for r in rows})
    lookup = {(r["params"][x_name], r["params"][y_name]): r for r in rows}
    scale = " .:-=+*#%@"
    best = max(r["return"] for r in rows)
    worst = min(r["return"] for r in rows)
    span = max(best - worst, 1e-9)
    lines = [f"  {y_name} ↓ / {x_name} →   " + " ".join(f"{x:>{width}}" for x in xs)]
    for y in ys:
        cells = []
        for x in xs:
            row = lookup.get((x, y))
            if row is None:
                cells.append(" " * width)
                continue
            mark = scale[min(len(scale) - 1, int((row["return"] - worst) / span * (len(scale) - 1)))]
            cells.append(f"{mark:>{width}}")
        lines.append(f"  {y:>{len(y_name) + 12}} " + " ".join(cells))
    lines.append(f"  worst {metrics.format_percent(worst)}  →  best {metrics.format_percent(best)}"
                 f"   (benchmark {metrics.format_percent(sweep_result['benchmark_return'])})")
    return "\n".join(lines)
