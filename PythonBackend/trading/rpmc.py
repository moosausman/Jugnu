"""
rpmc.py -- the Rotman Portfolio Management Competition, as a set of
constraints and an objective.

The competition is unusual in a way that changes what is worth doing. It
scores the SHARPE RATIO, not the return, over about five months, with the
book forced to be nearly fully invested and no position over a fifth of it.
Under those constraints you are a diversified, ~95%-invested portfolio
whatever you do, and over a hundred-odd trading days the Sharpe is decided
far more by controlling volatility than by picking well. That is why the
allocators here default to minimum variance rather than to maximum
expected Sharpe: the second needs expected returns, and estimating those
from five months of history is how portfolios blow up out of sample.

What this module does:

    listing()      is a symbol eligible? US and Canadian listings only,
                   checked against the exchange the data provider reports
                   rather than against a constituent list that goes stale
    benchmark()    the five-ETF benchmark, rebuilt from its start date
    compliance()   the three rules, checked: no position over 20%, cash
                   under 10%, a trade at least every fourteen days
    optimise()     weights under those constraints, by projected gradient
    review()       all of it at once, which is what the terminal shows

What this module deliberately does NOT do: place orders. The competition
runs on an external platform under the participant's own credentials, and
nothing here logs into it, types into it or trades on it. It produces a
target book and a list of what is out of line; a person enters the orders.
Competition rules on automated trading are the participant's to read.
"""
from __future__ import annotations

import datetime as dt
import math

from ._http import HttpError, get_json
from .data import DataError, load

# The benchmark as published, with the platform's ticker on the left and the
# one the data actually lives under on the right. IEMG.K is a StockTrak
# venue suffix and does not resolve anywhere else.
BENCHMARK = [
    ("SPY", "SPY", 0.35, "SPDR S&P 500"),
    ("XIC.TO", "XIC.TO", 0.15, "iShares Core S&P/TSX Capped Composite"),
    ("IEMG.K", "IEMG", 0.20, "iShares Core MSCI Emerging Markets"),
    ("XBB.TO", "XBB.TO", 0.20, "iShares Core Canadian Universe Bond"),
    ("GLD", "GLD", 0.10, "SPDR Gold Shares"),
]

START = "2026-10-09"          # competition opens
END = "2027-03-12"            # closes 6pm EST

MAX_POSITION = 0.20           # of portfolio value, at any point
MAX_CASH = 0.10               # of portfolio value, from the opening date
TRADE_EVERY_DAYS = 14         # at least one trade in any fourteen-day window

# Where an eligible security trades. The rules say US and Canadian
# exchanges; the timezone is the most reliable thing the feed reports.
ELIGIBLE_ZONES = {"America/New_York", "America/Toronto"}


class RpmcError(RuntimeError):
    pass


def yahoo_symbol(ticker: str) -> str:
    """The platform's ticker, as the data provider spells it."""
    ticker = (ticker or "").strip().upper()
    for platform, feed, _, _ in BENCHMARK:
        if ticker == platform:
            return feed
    # StockTrak appends a venue letter to some US listings; nobody else does.
    if ticker.endswith(".K") and ticker.count(".") == 1:
        return ticker[:-2]
    return ticker


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def listing(ticker: str) -> dict:
    """Which exchange a symbol trades on, and whether the rules allow it."""
    symbol = yahoo_symbol(ticker)
    try:
        payload = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                           {"range": "5d", "interval": "1d"}, timeout=20)
        meta = payload["chart"]["result"][0]["meta"]
    except (HttpError, KeyError, IndexError, TypeError) as e:
        return {"ticker": ticker, "symbol": symbol, "ok": False,
                "error": f"couldn't identify {symbol}: {str(e)[:80]}"}
    zone = meta.get("exchangeTimezoneName", "")
    return {"ticker": ticker, "symbol": symbol, "ok": True,
            "exchange": meta.get("fullExchangeName", "?"),
            "timezone": zone, "currency": meta.get("currency", "?"),
            "eligible": zone in ELIGIBLE_ZONES,
            "why": "" if zone in ELIGIBLE_ZONES else
                   f"listed on {meta.get('fullExchangeName', zone)}; the rules allow "
                   "US and Canadian exchanges only"}


# ---------------------------------------------------------------------------
# The benchmark
# ---------------------------------------------------------------------------

def benchmark(start: str = START, end: str = "") -> dict:
    """The five-ETF benchmark, bought at its start date and held.

    Weights are struck on the closing prices of the start date, as the rules
    specify, and then left alone -- it is a buy-and-hold basket, not a
    rebalanced one, so its weights drift exactly as the published one does.
    """
    today = dt.date.today().isoformat()
    if start > today:
        # The competition has not opened. Saying "no history" would be true
        # and useless; what the person wants to know is how long they have.
        days = (dt.date.fromisoformat(start) - dt.date.fromisoformat(today)).days
        return {"ok": False, "not_started": True, "start": start, "days_until": days,
                "legs": {p: {"name": n, "weight": w} for p, _, w, n in BENCHMARK},
                "error": f"the competition opens on {start}, {days} days from now; "
                         "the benchmark is struck on that day's closes",
                "missing": []}

    legs, missing = {}, []
    for platform, feed, weight, name in BENCHMARK:
        try:
            bars = [b for b in load(feed, "1d", start=start) if not end or b.date <= end]
        except DataError as e:
            missing.append(f"{platform} ({str(e)[:40]})")
            continue
        if len(bars) < 2:
            missing.append(f"{platform} (no history from {start})")
            continue
        legs[platform] = {"name": name, "weight": weight, "symbol": feed,
                          "dates": [b.date for b in bars], "closes": [b.close for b in bars]}
    if not legs:
        raise RpmcError("none of the benchmark's ETFs could be loaded: " + "; ".join(missing))

    # Align on the dates every surviving leg has -- exchanges keep different
    # holidays, and Toronto and New York do not close on the same days.
    common = sorted(set.intersection(*(set(leg["dates"]) for leg in legs.values())))
    if len(common) < 2:
        raise RpmcError("the benchmark's ETFs have no overlapping trading days yet")
    index = {leg: {d: c for d, c in zip(legs[leg]["dates"], legs[leg]["closes"])} for leg in legs}
    total_weight = sum(leg["weight"] for leg in legs.values())

    values = []
    for date in common:
        value = sum(legs[leg]["weight"] / total_weight * index[leg][date] / index[leg][common[0]]
                    for leg in legs)
        values.append(value)
    returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]
    return {"ok": True, "start": common[0], "end": common[-1], "days": len(common),
            "dates": common, "values": values, "returns": returns,
            "total_return": values[-1] - 1.0,
            "legs": {k: {"name": v["name"], "weight": v["weight"]} for k, v in legs.items()},
            "missing": missing,
            "note": ("weights renormalised over the legs that loaded"
                     if abs(total_weight - 1.0) > 1e-9 else "")}


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def compliance(positions: dict, cash: float, last_trade: str = "",
               today: str = "") -> dict:
    """Check a book against the three hard rules.

    `positions` is {ticker: market value}, signed -- a short is negative.
    Concentration is measured on ABSOLUTE value, because a 30% short is a
    30% position however the sign reads.
    """
    today = today or dt.date.today().isoformat()
    gross = sum(abs(v) for v in positions.values())
    equity = sum(positions.values()) + cash
    if equity <= 0:
        return {"ok": False, "error": "portfolio value must be positive"}

    breaches, warnings = [], []
    sizes = []
    for ticker, value in sorted(positions.items(), key=lambda kv: -abs(kv[1])):
        share = abs(value) / equity
        sizes.append({"ticker": ticker, "value": value, "share": share,
                      "side": "short" if value < 0 else "long"})
        if share > MAX_POSITION + 1e-9:
            breaches.append(f"{ticker} is {share:.1%} of the portfolio, over the {MAX_POSITION:.0%} cap")
        elif share > MAX_POSITION - 0.02:
            warnings.append(f"{ticker} is {share:.1%}, close to the {MAX_POSITION:.0%} cap")

    cash_share = cash / equity
    if cash_share > MAX_CASH + 1e-9:
        breaches.append(f"cash is {cash_share:.1%} of the portfolio, over the {MAX_CASH:.0%} limit")
    elif cash_share > MAX_CASH - 0.02:
        warnings.append(f"cash is {cash_share:.1%}, close to the {MAX_CASH:.0%} limit")

    days_since = None
    if last_trade:
        try:
            days_since = (dt.date.fromisoformat(today) - dt.date.fromisoformat(last_trade)).days
        except ValueError:
            warnings.append(f"couldn't read the last trade date {last_trade!r}")
        else:
            if days_since > TRADE_EVERY_DAYS:
                breaches.append(f"{days_since} days since the last trade, past the "
                                f"{TRADE_EVERY_DAYS}-day rule -- this is a scored penalty")
            elif days_since >= TRADE_EVERY_DAYS - 3:
                warnings.append(f"{days_since} days since the last trade; "
                                f"{TRADE_EVERY_DAYS - days_since} left before a penalty")

    return {"ok": True, "equity": equity, "gross": gross, "cash": cash,
            "cash_share": cash_share, "invested_share": 1.0 - cash_share,
            "leverage": gross / equity, "positions": sizes,
            "days_since_trade": days_since,
            "breaches": breaches, "warnings": warnings,
            "compliant": not breaches}


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def project(vector: list[float], total: float, low: float, high: float) -> list[float]:
    """Nearest point to `vector` with the right sum and every element inside
    [low, high].

    w_i = clip(v_i - theta, low, high), and the sum falls monotonically as
    theta rises, so one bisection finds it. This is the step that makes a
    constrained optimiser out of an unconstrained one.
    """
    n = len(vector)
    if n == 0:
        return []
    if not low * n - 1e-12 <= total <= high * n + 1e-12:
        raise RpmcError(f"no allocation can sum to {total:.2f} with {n} names "
                        f"capped between {low:.0%} and {high:.0%}")
    lo, hi = min(vector) - high - 1.0, max(vector) - low + 1.0
    for _ in range(200):
        theta = (lo + hi) / 2.0
        current = sum(min(max(v - theta, low), high) for v in vector)
        if current > total:
            lo = theta
        else:
            hi = theta
    theta = (lo + hi) / 2.0
    return [min(max(v - theta, low), high) for v in vector]


def _moments(series: dict[str, list[float]], shrink_mean: float,
             shrink_cov: float) -> tuple[list[str], list[float], list[list[float]]]:
    names = sorted(series)
    length = min(len(series[n]) for n in names)
    trimmed = {n: series[n][-length:] for n in names}
    means = [sum(trimmed[n]) / length for n in names]
    # Shrink the means towards their cross-sectional average. Expected
    # returns estimated from a few months of data are mostly noise, and a
    # maximum-Sharpe optimiser will happily bet the whole book on the
    # noisiest one. This is the cheap version of the standard fix.
    grand = sum(means) / len(means)
    means = [(1 - shrink_mean) * m + shrink_mean * grand for m in means]

    cov = []
    raw_means = [sum(trimmed[n]) / length for n in names]
    for i, a in enumerate(names):
        row = []
        for j, b in enumerate(names):
            row.append(sum((trimmed[a][k] - raw_means[i]) * (trimmed[b][k] - raw_means[j])
                           for k in range(length)) / max(length - 1, 1))
        cov.append(row)
    # Shrink the covariance towards its diagonal, which keeps it invertible
    # and stops the optimiser exploiting correlation estimates it cannot
    # possibly have measured well.
    for i in range(len(names)):
        for j in range(len(names)):
            if i != j:
                cov[i][j] *= (1 - shrink_cov)
    return names, means, cov


def _variance(weights: list[float], cov: list[list[float]]) -> float:
    n = len(weights)
    return sum(weights[i] * cov[i][j] * weights[j] for i in range(n) for j in range(n))


def optimise(series: dict[str, list[float]], *, objective: str = "minvar",
             cap: float = MAX_POSITION, invested: float = 1.0 - MAX_CASH,
             allow_short: bool = False, shrink_mean: float = 0.8,
             shrink_cov: float = 0.2, steps: int = 600) -> dict:
    """Weights under the competition's constraints.

    objective:
        minvar   least portfolio variance. The default, and on a five-month
                 Sharpe contest almost certainly the right one.
        sharpe   most expected Sharpe. Needs expected returns, which is the
                 weak link; the means are shrunk hard towards their average
                 to stop it betting everything on an estimate.
        parity   inverse volatility, capped. Simple and hard to embarrass.

    Projected gradient: step downhill (or uphill), then project back onto
    {sum of weights = invested, every weight within the cap}.
    """
    if len(series) < 2:
        raise RpmcError("need at least two securities")
    names, means, cov = _moments(series, shrink_mean, shrink_cov)
    n = len(names)
    low = -cap if allow_short else 0.0
    if not low * n <= invested <= cap * n:
        need = math.ceil(invested / cap)
        raise RpmcError(f"{n} names cannot be {invested:.0%} invested under a {cap:.0%} cap; "
                        f"you need at least {need}")

    if objective == "parity":
        vols = [math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0 for i in range(n)]
        raw = [1.0 / v if v > 0 else 0.0 for v in vols]
        scale = sum(raw) or 1.0
        weights = project([r / scale * invested for r in raw], invested, max(low, 0.0), cap)
    else:
        weights = project([invested / n] * n, invested, low, cap)
        step = 1.0
        for iteration in range(steps):
            variance = _variance(weights, cov)
            sigma = math.sqrt(max(variance, 1e-18))
            grad_var = [2.0 * sum(cov[i][j] * weights[j] for j in range(n)) for i in range(n)]
            if objective == "sharpe":
                edge = sum(weights[i] * means[i] for i in range(n))
                gradient = [means[i] / sigma - edge * grad_var[i] / (2 * sigma ** 3)
                            for i in range(n)]
                better = lambda w: (sum(w[i] * means[i] for i in range(n))
                                    / math.sqrt(max(_variance(w, cov), 1e-18)))
                current = better(weights)
                direction = 1.0
            else:
                gradient = grad_var
                better = lambda w: -_variance(w, cov)
                current = better(weights)
                direction = -1.0
            # Backtracking: shrink the step until it actually improves, so a
            # badly scaled gradient cannot walk the solution backwards.
            for _ in range(40):
                trial = project([weights[i] + direction * step * gradient[i] for i in range(n)],
                                invested, low, cap)
                if better(trial) > current + 1e-15:
                    weights = trial
                    step *= 1.15
                    break
                step *= 0.5
            else:
                break
            if step < 1e-14:
                break

    variance = _variance(weights, cov)
    expected = sum(weights[i] * means[i] for i in range(n))
    daily_vol = math.sqrt(max(variance, 0.0))
    return {
        "ok": True, "objective": objective,
        "weights": {names[i]: weights[i] for i in range(n)},
        "cash": 1.0 - sum(weights),
        "gross": sum(abs(w) for w in weights),
        "expected_return_annual": (1.0 + expected) ** 252 - 1.0,
        "volatility_annual": daily_vol * math.sqrt(252),
        "sharpe_annual": (expected / daily_vol * math.sqrt(252)) if daily_vol > 0 else 0.0,
        "largest": max(abs(w) for w in weights),
        "names": names,
    }


# ---------------------------------------------------------------------------
# Everything at once
# ---------------------------------------------------------------------------

def candidates(tickers: list[str], start: str = "", lookback: int = 260) -> dict:
    """Load a universe, check eligibility, and return usable return series."""
    series, rejected = {}, {}
    for ticker in tickers:
        check = listing(ticker)
        if not check.get("ok"):
            rejected[ticker] = check["error"]
            continue
        if not check["eligible"]:
            rejected[ticker] = check["why"]
            continue
        try:
            bars = load(check["symbol"], "1d", start=start or None)[-lookback:]
        except DataError as e:
            rejected[ticker] = str(e)[:70]
            continue
        if len(bars) < 40:
            rejected[ticker] = f"only {len(bars)} bars of history"
            continue
        closes = [b.close for b in bars]
        series[ticker] = [closes[i] / closes[i - 1] - 1.0
                          for i in range(1, len(closes)) if closes[i - 1] > 0]
    return {"series": series, "rejected": rejected}


def review(positions: dict, cash: float, last_trade: str = "", today: str = "") -> dict:
    """Compliance, plus how the book has done against the benchmark."""
    out = {"compliance": compliance(positions, cash, last_trade, today)}
    try:
        out["benchmark"] = benchmark()
    except (RpmcError, DataError) as e:
        out["benchmark"] = {"ok": False, "error": str(e)}
    return out


def advice(check: dict) -> list[str]:
    """What to do about it, in sentences."""
    if not check.get("ok"):
        return [check.get("error", "nothing to check")]
    out = []
    if check["breaches"]:
        out.append("Breaking the rules right now: " + "; ".join(check["breaches"]) + ".")
    elif check["warnings"]:
        out.append("Close to the line: " + "; ".join(check["warnings"]) + ".")
    else:
        out.append("Inside every rule.")
    if check["leverage"] > 1.05:
        out.append(f"Gross exposure is {check['leverage']:.2f}x equity. Leverage is allowed, but it "
                   "scales volatility as fast as it scales return and the score is a Sharpe ratio, "
                   "so it does not help the number you are judged on.")
    if check["cash_share"] < 0.02:
        out.append("Almost no cash. The rule is a ceiling, not a floor, but a book with none has no "
                   "room to make the fortnightly trade without selling something first.")
    return out
