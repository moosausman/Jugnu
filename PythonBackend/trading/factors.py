"""
factors.py -- how much of a return is skill, and how much is just exposure.

A strategy that is long equities in a bull market has a wonderful Sharpe
and no alpha. The only way to tell the difference is to regress its returns
on the things it might merely be tracking, and look at what is left over.

    r_strategy - r_f = alpha + b1*MKT + b2*SMB + b3*HML + b4*MOM + e

Alpha is the intercept: the part no exposure explains. What matters is not
its size but its t-statistic -- and its t-statistic computed with standard
errors that survive autocorrelation, because strategy returns are not
independent draws.

The factors here are ETF SPREADS, not the Fama-French research factors:

    MKT   SPY minus cash            the market
    SMB   IWM minus SPY             small minus big
    HML   VTV minus VUG             value minus growth
    MOM   MTUM minus SPY            momentum

That is an honest approximation and not the real thing: the ETF spreads are
tradeable, start in the 2000s-2010s, and carry fees and tracking error the
academic factors do not. Treat a loading as "this is roughly what it is
tracking", not as a published exposure.

Ordinary least squares, the matrix inverse and the Newey-West covariance
are all written out here, because this has to run in the agent account
where nothing is installed.
"""
from __future__ import annotations

import math

from .data import load
from .quant import t_pvalue

# The spread definitions above, as (long, short) tickers. A short leg of
# None means the leg is just cash.
FACTORS = {
    "MKT": ("SPY", None),
    "SMB": ("IWM", "SPY"),
    "HML": ("VTV", "VUG"),
    "MOM": ("MTUM", "SPY"),
}


# ---------------------------------------------------------------------------
# Linear algebra, by hand
# ---------------------------------------------------------------------------

def _invert(matrix: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan with partial pivoting. Small k only, which is all a
    factor model needs."""
    n = len(matrix)
    work = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda r: abs(work[r][column]))
        if abs(work[pivot][column]) < 1e-12:
            return None                      # singular: collinear factors
        work[column], work[pivot] = work[pivot], work[column]
        scale = work[column][column]
        work[column] = [v / scale for v in work[column]]
        for row in range(n):
            if row != column and work[row][column] != 0.0:
                factor = work[row][column]
                work[row] = [a - factor * b for a, b in zip(work[row], work[column])]
    return [row[n:] for row in work]


def _newey_west_lags(n: int) -> int:
    """Newey & West's own rule of thumb for the truncation lag."""
    return max(1, int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0))))


def ols(y: list[float], columns: dict[str, list[float]], hac: bool = True) -> dict | None:
    """Regress `y` on the named columns plus an intercept.

    Returns coefficients with standard errors, t statistics and p values.
    With `hac`, the standard errors are Newey-West: autocorrelated residuals
    otherwise make every t statistic look about twice as convincing as it is.
    """
    names = list(columns)
    n = len(y)
    if n < len(names) + 10:
        return None
    design = [[1.0] + [columns[name][i] for name in names] for i in range(n)]
    k = len(names) + 1

    xtx = [[sum(design[i][a] * design[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    xty = [sum(design[i][a] * y[i] for i in range(n)) for a in range(k)]
    inverse = _invert(xtx)
    if inverse is None:
        return None
    beta = [sum(inverse[a][b] * xty[b] for b in range(k)) for a in range(k)]
    fitted = [sum(beta[a] * design[i][a] for a in range(k)) for i in range(n)]
    residual = [y[i] - fitted[i] for i in range(n)]

    mean_y = sum(y) / n
    ss_total = sum((v - mean_y) ** 2 for v in y)
    ss_residual = sum(e * e for e in residual)
    r2 = 1.0 - ss_residual / ss_total if ss_total > 0 else 0.0

    if hac:
        lags = _newey_west_lags(n)
        meat = [[0.0] * k for _ in range(k)]
        for lag in range(lags + 1):
            weight = 1.0 if lag == 0 else 1.0 - lag / (lags + 1.0)
            block = [[0.0] * k for _ in range(k)]
            for i in range(lag, n):
                for a in range(k):
                    for b in range(k):
                        block[a][b] += design[i][a] * residual[i] * design[i - lag][b] * residual[i - lag]
            for a in range(k):
                for b in range(k):
                    meat[a][b] += weight * (block[a][b] + (block[b][a] if lag else 0.0))
        middle = [[sum(inverse[a][c] * meat[c][b] for c in range(k)) for b in range(k)] for a in range(k)]
        covariance = [[sum(middle[a][c] * inverse[c][b] for c in range(k)) for b in range(k)] for a in range(k)]
    else:
        sigma2 = ss_residual / max(n - k, 1)
        covariance = [[inverse[a][b] * sigma2 for b in range(k)] for a in range(k)]

    df = max(n - k, 1)
    terms = {}
    for index, name in enumerate(["alpha"] + names):
        error = math.sqrt(max(covariance[index][index], 0.0))
        t = beta[index] / error if error > 0 else 0.0
        terms[name] = {"coefficient": beta[index], "error": error,
                       "t": t, "p": t_pvalue(t, df)}
    return {"terms": terms, "r2": r2, "n": n, "df": df,
            "residuals": residual, "hac_lags": _newey_west_lags(n) if hac else 0}


# ---------------------------------------------------------------------------
# Building the factors
# ---------------------------------------------------------------------------

def _series(symbol: str, start: str | None) -> dict[str, float]:
    """Date -> simple return, so factors can be aligned by date rather than
    by position. Two ETFs do not always have the same number of bars."""
    bars = load(symbol, "1d", start=start)
    out = {}
    for older, newer in zip(bars, bars[1:]):
        if older.close > 0:
            out[newer.date] = newer.close / older.close - 1.0
    return out


def build(start: str | None = None, names: list[str] | None = None) -> dict[str, dict[str, float]]:
    """Each factor as a date -> return mapping."""
    wanted = names or list(FACTORS)
    cache: dict[str, dict[str, float]] = {}
    built = {}
    for name in wanted:
        long_leg, short_leg = FACTORS[name]
        for ticker in (long_leg, short_leg):
            if ticker and ticker not in cache:
                cache[ticker] = _series(ticker, start)
        longs = cache[long_leg]
        if short_leg is None:
            built[name] = dict(longs)
        else:
            shorts = cache[short_leg]
            built[name] = {d: longs[d] - shorts[d] for d in longs if d in shorts}
    return built


def attribute(dates: list[str], returns: list[float], start: str | None = None,
              names: list[str] | None = None) -> dict:
    """Regress a strategy's daily returns on the factor spreads.

    `dates` and `returns` must line up -- returns[i] is the return earned
    over the bar ending on dates[i].
    """
    factors = build(start, names)
    if not factors:
        return {"ok": False, "error": "no factor data"}
    usable = [name for name, series in factors.items() if len(series) > 50]
    if not usable:
        return {"ok": False, "error": "not enough factor history"}

    aligned_y, columns = [], {name: [] for name in usable}
    for date, value in zip(dates, returns):
        if all(date in factors[name] for name in usable):
            aligned_y.append(value)
            for name in usable:
                columns[name].append(factors[name][date])
    if len(aligned_y) < 60:
        return {"ok": False, "error": f"only {len(aligned_y)} overlapping days; need 60"}

    fit = ols(aligned_y, columns)
    if fit is None:
        return {"ok": False, "error": "the factors are collinear over this window"}
    alpha = fit["terms"]["alpha"]
    return {
        "ok": True,
        "alpha_daily": alpha["coefficient"],
        "alpha_annual": (1.0 + alpha["coefficient"]) ** 252 - 1.0,
        "alpha_t": alpha["t"],
        "alpha_p": alpha["p"],
        "loadings": {name: fit["terms"][name] for name in usable},
        "r2": fit["r2"], "days": fit["n"], "hac_lags": fit["hac_lags"],
        "factors": usable,
    }


def verdict(report: dict) -> list[str]:
    """What the regression actually means, in sentences."""
    if not report.get("ok"):
        return [report.get("error", "no attribution")]
    out = []
    t = report["alpha_t"]
    annual = report["alpha_annual"] * 100
    if report["alpha_p"] < 0.05 and t > 0:
        out.append(f"Alpha of {annual:.1f}%/yr survives the factors (t={t:.2f}, p={report['alpha_p']:.3f}). "
                   "That is the part the exposures below do not explain.")
    elif t > 0:
        out.append(f"Alpha is {annual:.1f}%/yr but not distinguishable from zero "
                   f"(t={t:.2f}, p={report['alpha_p']:.2f}). On this evidence the returns are the exposures.")
    else:
        out.append(f"Alpha is negative ({annual:.1f}%/yr). The exposures earned more than the strategy did.")
    market = report["loadings"].get("MKT")
    if market and market["p"] < 0.05:
        out.append(f"It is {market['coefficient']:.2f} beta to the market -- "
                   f"{'most' if market['coefficient'] > 0.7 else 'much'} of the ride is simply being long.")
    out.append(f"The factors explain {report['r2'] * 100:.0f}% of the variance "
               f"({report['days']} overlapping days, Newey-West lag {report['hac_lags']}).")
    return out
