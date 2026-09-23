"""
risk.py -- the numbers that come after "what was the return".

Return and Sharpe describe a curve. These describe what holding it would
have felt like and where it can hurt you: how long you spent under water,
what the bad days look like, how much of the move was just the market, and
whether one trade carried the whole result.

Everything is computed from the equity curve and the trade list the engine
already produced -- nothing is re-simulated, so nothing can disagree.
"""
from __future__ import annotations

import math

from .data import Bar, periods_per_year
from .engine import Result


def _returns(equity: list[float]) -> list[float]:
    return [(equity[i] / equity[i - 1]) - 1 for i in range(1, len(equity)) if equity[i - 1] > 0]


def drawdown_series(equity: list[float]) -> list[float]:
    peak, out = equity[0] if equity else 1.0, []
    for value in equity:
        peak = max(peak, value)
        out.append(value / peak - 1 if peak else 0.0)
    return out


def underwater(equity: list[float], per_year: float) -> dict:
    """How long the curve spent below its previous peak -- the thing people
    actually quit during."""
    series = drawdown_series(equity)
    longest = current = 0
    total_under = 0
    worst_index = 0
    for i, value in enumerate(series):
        if value < -1e-9:
            current += 1
            total_under += 1
            longest = max(longest, current)
            if value <= min(series):
                worst_index = i
        else:
            current = 0
    days = per_year and (longest / per_year * 365) or longest
    return {"longest_bars": longest, "longest_days": round(days),
            "share_under_water": total_under / len(series) if series else 0.0,
            "still_under_water": series[-1] < -1e-9 if series else False,
            "current_drawdown": series[-1] if series else 0.0,
            "worst_at": worst_index}


def ulcer_index(equity: list[float]) -> float:
    """Root mean square of the drawdowns: one number for "how much time did
    this spend hurting", which max drawdown alone hides."""
    series = drawdown_series(equity)
    if not series:
        return 0.0
    return math.sqrt(sum((d * 100) ** 2 for d in series) / len(series))


def value_at_risk(equity: list[float], confidence: float = 0.95) -> tuple[float, float]:
    """(VaR, CVaR) per bar at `confidence`, from the actual distribution --
    no normal assumption, because returns aren't."""
    returns = sorted(_returns(equity))
    if len(returns) < 20:
        return 0.0, 0.0
    cut = max(0, int((1 - confidence) * len(returns)) - 1)
    var = returns[cut]
    tail = returns[:cut + 1]
    return var, (sum(tail) / len(tail) if tail else var)


def beta_alpha(equity: list[float], benchmark: list[float], per_year: float) -> tuple[float, float]:
    """How much of the ride was just the market (beta), and what was left
    over annualised (alpha)."""
    mine, theirs = _returns(equity), _returns(benchmark)
    n = min(len(mine), len(theirs))
    if n < 20:
        return 0.0, 0.0
    mine, theirs = mine[-n:], theirs[-n:]
    mean_mine = sum(mine) / n
    mean_theirs = sum(theirs) / n
    covariance = sum((a - mean_mine) * (b - mean_theirs) for a, b in zip(mine, theirs)) / (n - 1)
    variance = sum((b - mean_theirs) ** 2 for b in theirs) / (n - 1)
    beta = covariance / variance if variance else 0.0
    alpha = (mean_mine - beta * mean_theirs) * per_year
    return beta, alpha


def rolling_sharpe(equity: list[float], per_year: float, window: int = 252) -> list[float]:
    returns = _returns(equity)
    if len(returns) < window + 1:
        return []
    out = []
    for end in range(window, len(returns) + 1):
        chunk = returns[end - window:end]
        mean = sum(chunk) / window
        spread = math.sqrt(sum((r - mean) ** 2 for r in chunk) / (window - 1))
        out.append((mean / spread * math.sqrt(per_year)) if spread else 0.0)
    return out


def yearly(result: Result) -> list[dict]:
    """Return per calendar year, strategy against benchmark. The table that
    shows a strategy 'works' because of one extraordinary year."""
    rows: dict[str, dict] = {}
    previous_mine = previous_theirs = None
    for date, mine, theirs in zip(result.dates, result.equity, result.benchmark):
        year = date[:4]
        row = rows.setdefault(year, {"year": year, "start_mine": previous_mine if previous_mine else mine,
                                     "start_theirs": previous_theirs if previous_theirs else theirs})
        row["end_mine"], row["end_theirs"] = mine, theirs
        previous_mine, previous_theirs = mine, theirs
    out = []
    for year in sorted(rows):
        row = rows[year]
        out.append({
            "year": year,
            "strategy": row["end_mine"] / row["start_mine"] - 1 if row["start_mine"] else 0.0,
            "benchmark": row["end_theirs"] / row["start_theirs"] - 1 if row["start_theirs"] else 0.0,
        })
    return out


def trade_stats(result: Result, bars: list[Bar] | None = None) -> dict:
    """What the trades themselves looked like: hold times, the spread of
    wins and losses, and whether one trade carried everything."""
    closed = [t for t in result.trades if t.pnl is not None]
    if not closed:
        return {"round_trips": 0}
    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [t.pnl for t in closed if t.pnl <= 0]
    total = sum(t.pnl for t in closed)
    best = max(closed, key=lambda t: t.pnl)

    holds = []
    entry_ts = None
    for trade in result.trades:
        if trade.side == "buy" and entry_ts is None:
            entry_ts = trade.ts
        elif trade.side == "sell" and entry_ts is not None:
            holds.append((trade.ts - entry_ts) / 86_400)
            entry_ts = None

    streak = worst_streak = 0
    for trade in closed:
        streak = streak + 1 if trade.pnl <= 0 else 0
        worst_streak = max(worst_streak, streak)

    return {
        "round_trips": len(closed),
        "win_rate": len(wins) / len(closed),
        "average_win": sum(wins) / len(wins) if wins else 0.0,
        "average_loss": sum(losses) / len(losses) if losses else 0.0,
        "payoff": (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) if wins and losses else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses and sum(losses) else 0.0,
        "expectancy": total / len(closed),
        "best_trade": {"date": best.date, "pnl": best.pnl},
        "best_trade_share": (best.pnl / total) if total else 0.0,
        "worst_losing_streak": worst_streak,
        "median_hold_days": sorted(holds)[len(holds) // 2] if holds else 0.0,
        "longest_hold_days": max(holds) if holds else 0.0,
    }


def full(result: Result, bars: list[Bar] | None = None) -> dict:
    """Everything above, for one run."""
    per_year = periods_per_year(result.symbol, result.interval)
    mine = result.metrics
    var, cvar = value_at_risk(result.equity)
    beta, alpha = beta_alpha(result.equity, result.benchmark, per_year)
    water = underwater(result.equity, per_year)
    rolling = rolling_sharpe(result.equity, per_year, window=int(min(per_year, len(result.equity) // 3) or 20))
    calmar = (mine["cagr"] / abs(mine["max_drawdown"])) if mine["max_drawdown"] else 0.0
    return {
        "calmar": calmar,
        "ulcer": ulcer_index(result.equity),
        "var95": var, "cvar95": cvar,
        "beta": beta, "alpha": alpha,
        "underwater": water,
        "rolling_sharpe": {"min": min(rolling) if rolling else 0.0,
                           "max": max(rolling) if rolling else 0.0,
                           "last": rolling[-1] if rolling else 0.0,
                           "negative_share": (sum(1 for r in rolling if r < 0) / len(rolling))
                           if rolling else 0.0},
        "yearly": yearly(result),
        "trades": trade_stats(result, bars),
    }


def verdicts(deep: dict, result: Result) -> list[str]:
    """The uncomfortable readings, in words. Same job as metrics.warnings,
    one level deeper."""
    notes = []
    trades = deep.get("trades", {})
    if trades.get("round_trips", 0) and trades.get("best_trade_share", 0) > 0.5:
        notes.append(f"One trade ({trades['best_trade']['date']}) produced "
                     f"{trades['best_trade_share'] * 100:.0f}% of all the profit. Remove it and there's "
                     f"barely a strategy left.")
    if deep["underwater"]["share_under_water"] > 0.7:
        notes.append(f"Under water {deep['underwater']['share_under_water'] * 100:.0f}% of the time, "
                     f"the longest stretch {deep['underwater']['longest_days']} days.")
    if deep["rolling_sharpe"]["negative_share"] > 0.4:
        notes.append(f"Rolling Sharpe was negative {deep['rolling_sharpe']['negative_share'] * 100:.0f}% "
                     f"of the time -- it only works in stretches.")
    if abs(deep["beta"]) > 0.9 and result.metrics["total_return"] < result.benchmark_metrics["total_return"]:
        notes.append(f"Beta {deep['beta']:.2f}: it's taking essentially the market's risk while "
                     f"earning less than the market.")
    positive_years = [y for y in deep["yearly"] if y["strategy"] > 0]
    if deep["yearly"] and len(positive_years) <= len(deep["yearly"]) / 3:
        notes.append(f"Only {len(positive_years)} of {len(deep['yearly'])} years were positive.")
    if trades.get("profit_factor") and trades["profit_factor"] < 1.1:
        notes.append(f"Profit factor {trades['profit_factor']:.2f} -- the wins barely cover the losses.")
    return notes
