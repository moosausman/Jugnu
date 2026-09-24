"""
capacity.py -- what the strategy costs once the orders are big enough to
move the price, and how much money it can hold before the edge is gone.

A backtest fills at the close for any size. Reality charges you twice:

    the spread          paid on every trade, whatever the size
    market impact       paid because your own buying pushes the price up

Impact is not linear. The square-root law -- impact grows with the square
root of the fraction of daily volume you take -- has held across equities,
futures and crypto for decades, and it is the model every execution desk
starts from:

    impact = k * sigma * sqrt(Q / ADV)

with sigma the daily volatility, Q the shares traded and ADV the average
daily volume. k is around 0.3-1.0 for equities, and rather than guess it,
`calibrate()` MEASURES it: it sweeps the matching engine in execution.py at
five sizes and fits the exponent and the coefficient. On AAPL, NVDA and SPY
the exponent comes out 0.52-0.53 with an R-squared of 1.00, which is the
square-root law arriving as a result rather than an assumption. DEFAULT_K
stays at the conservative 1.0 for when there is no intraday data to fit.

What that buys you is the number that actually decides whether a strategy
is worth running: CAPACITY. A rule that returns 40% a year on $10,000 and
loses money on $50m is a hobby. This finds where that line is, by charging
the real cost at each size until the edge disappears.

Nothing here is a promise about live fills. It is a cost model, and a
deliberately unkind one.
"""
from __future__ import annotations

import math

DEFAULT_K = 1.0
# A conservative half-spread for a liquid US large cap, in basis points.
# Small caps and crypto are wider; this is not a substitute for real quotes.
DEFAULT_HALF_SPREAD_BPS = 1.0


def daily_volatility(bars: list) -> float:
    closes = [b.close for b in bars if b.close > 0]
    if len(closes) < 3:
        return 0.0
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def average_dollar_volume(bars: list, window: int = 60) -> float:
    """Median dollar volume, not mean: one earnings day should not make a
    thin name look liquid."""
    recent = [b for b in bars[-window:] if b.volume and b.close > 0]
    if not recent:
        return 0.0
    values = sorted(b.volume * b.close for b in recent)
    return values[len(values) // 2]


def impact_bps(notional: float, dollar_adv: float, volatility: float,
               k: float = DEFAULT_K, half_spread_bps: float = DEFAULT_HALF_SPREAD_BPS) -> float:
    """Round-trip-free, one-side cost of trading `notional` dollars, in bps."""
    if dollar_adv <= 0 or notional <= 0:
        return half_spread_bps
    participation = notional / dollar_adv
    return half_spread_bps + k * volatility * math.sqrt(participation) * 10_000.0


def participation(notional: float, dollar_adv: float) -> float:
    return notional / dollar_adv if dollar_adv > 0 else float("inf")


def calibrate(bars: list, sizes: tuple = (), interval_vol: float | None = None) -> dict:
    """Fit this instrument's impact coefficient by trading through the book.

    The square-root law says cost = k * sigma * sqrt(participation). The
    exponent is theory; k is not, and the default of 1.0 above is a
    conservative guess. This measures both: it sweeps a ladder of sizes
    through the matching engine in execution.py and fits

        log(cost) = log(a) + b * log(participation)

    by least squares. `b` should come out near 0.5 -- it is the check that
    the simulated book has the shape a real one has -- and `a` gives k.

    Slow (it runs a full simulation per size), so the analytic model stays
    the one used for capacity curves over years. This is what calibrates it.
    """
    import math
    import statistics

    from . import execution

    if len(bars) < 5:
        return {"ok": False, "error": "need at least five bars to calibrate"}
    volume = sum(max(b.volume, 1.0) for b in bars)
    closes = [b.close for b in bars]
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 3:
        return {"ok": False, "error": "not enough price history"}
    vol = interval_vol or statistics.stdev(rets) * math.sqrt(len(bars))
    if vol <= 0:
        return {"ok": False, "error": "no volatility in this window"}

    sizes = sizes or tuple(volume * p for p in (0.002, 0.005, 0.01, 0.02, 0.04))
    points = []
    for size in sizes:
        run = execution.execute(bars, "buy", size, "market")
        cost = run.get("spread_paid_bps")
        # Only fits where the order actually completed: a partial fill
        # measures the cost of the part that was cheap enough to get done.
        if cost and cost > 0 and run["fill_rate"] > 0.98:
            points.append((size / volume, cost))
    if len(points) < 3:
        return {"ok": False, "error": "the book could not absorb enough of the ladder to fit"}

    xs = [math.log(p) for p, _ in points]
    ys = [math.log(c) for _, c in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denominator = sum((x - mx) ** 2 for x in xs)
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denominator if denominator else 0.0
    intercept = my - slope * mx
    predicted = [intercept + slope * x for x in xs]
    ss_res = sum((ys[i] - predicted[i]) ** 2 for i in range(n))
    ss_tot = sum((y - my) ** 2 for y in ys)
    # cost_bps = exp(intercept) * part^slope, and cost_bps = k*sigma*sqrt(part)*1e4
    k = math.exp(intercept) / (vol * 10_000.0) if vol > 0 else 0.0
    return {"ok": True, "k": k, "exponent": slope,
            "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0,
            "volatility": vol, "points": points,
            "square_root": abs(slope - 0.5) < 0.15}


def cost_of_run(result, bars: list, aum: float, k: float = DEFAULT_K,
                half_spread_bps: float = DEFAULT_HALF_SPREAD_BPS) -> dict:
    """What this backtest's trades would have cost at `aum` dollars.

    The backtest's own trades are scaled to the new size: each fill's
    notional becomes the same FRACTION of equity it was originally, but of
    `aum` instead. That keeps the strategy's shape and changes only the
    money behind it -- which is the thing capacity is about.
    """
    volatility = daily_volatility(bars)
    dollar_adv = average_dollar_volume(bars)
    if dollar_adv <= 0:
        return {"ok": False, "error": "no volume data for this symbol"}

    total_bps, total_cost, worst, notionals = 0.0, 0.0, 0.0, []
    for trade in result.trades:
        if trade.equity <= 0:
            continue
        fraction = (trade.shares * trade.price) / trade.equity
        notional = fraction * aum
        if notional <= 0:
            continue
        bps = impact_bps(notional, dollar_adv, volatility, k, half_spread_bps)
        total_bps += bps
        total_cost += notional * bps / 10_000.0
        worst = max(worst, participation(notional, dollar_adv))
        notionals.append(notional)

    fills = len(notionals)
    years = max(len(result.equity) / 252.0, 1e-9)
    drag = (total_cost / aum) / years if aum > 0 else 0.0
    return {"ok": True, "aum": aum, "fills": fills,
            "average_bps": total_bps / fills if fills else 0.0,
            "dollar_cost": total_cost,
            "annual_drag": drag,
            "worst_participation": worst,
            "dollar_adv": dollar_adv, "daily_volatility": volatility,
            "median_notional": sorted(notionals)[len(notionals) // 2] if notionals else 0.0}


def curve(result, bars: list, gross_cagr: float, sizes: list[float] | None = None,
          k: float = DEFAULT_K) -> dict:
    """Net CAGR at a ladder of fund sizes, and the size where the edge dies.

    `gross_cagr` is the backtest's own CAGR, which already carries the flat
    commission and slippage the engine charged. This adds the size-dependent
    part on top, so the smallest rung should look close to the backtest.
    """
    sizes = sizes or [1e4, 1e5, 1e6, 1e7, 5e7, 1e8, 5e8, 1e9]
    rungs, capacity = [], None
    for aum in sizes:
        cost = cost_of_run(result, bars, aum, k)
        if not cost.get("ok"):
            return cost
        net = gross_cagr - cost["annual_drag"]
        rungs.append({"aum": aum, "net_cagr": net, "annual_drag": cost["annual_drag"],
                      "average_bps": cost["average_bps"],
                      "participation": cost["worst_participation"]})
        if capacity is None and net <= 0:
            capacity = aum
    # Where does the drag eat the whole edge? Solve it directly rather than
    # reading it off the ladder: drag scales with sqrt(aum), so
    #   gross = c * sqrt(aum)  ->  aum = (gross / c)^2 * reference
    reference = rungs[0]
    if reference["annual_drag"] > 0 and gross_cagr > 0:
        scale = gross_cagr / reference["annual_drag"]
        solved = reference["aum"] * scale * scale
    else:
        solved = float("inf")
    return {"ok": True, "rungs": rungs, "gross_cagr": gross_cagr,
            "capacity": capacity, "solved_capacity": solved,
            "dollar_adv": cost["dollar_adv"], "daily_volatility": cost["daily_volatility"]}


def verdict(report: dict) -> list[str]:
    if not report.get("ok"):
        return [report.get("error", "no capacity estimate")]
    out = []
    solved = report["solved_capacity"]
    if math.isinf(solved):
        out.append("No positive edge to spend, so capacity is not the binding problem here.")
    else:
        out.append(f"The edge pays for its own market impact up to roughly {_money(solved)}. "
                   "Past that the square-root cost eats the whole return.")
    tightest = max(report["rungs"], key=lambda r: r["participation"])
    if tightest["participation"] > 0.1:
        out.append(f"At {_money(tightest['aum'])} a single order is "
                   f"{tightest['participation'] * 100:.0f}% of a day's volume -- far past what could be "
                   "filled at anything like these prices.")
    out.append(f"Median daily volume is {_money(report['dollar_adv'])}, "
               f"daily volatility {report['daily_volatility'] * 100:.2f}%.")
    return out


def _money(value: float) -> str:
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= cut:
            return f"${value / cut:.1f}{suffix}"
    return f"${value:,.0f}"
