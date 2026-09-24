"""
quant.py -- the arithmetic that decides whether a backtest means anything.

Everything else in this package answers "what would this rule have made?".
This module answers the harder question a desk actually asks: "how much of
that is luck, and how much did you spend searching for it?"

A Sharpe of 1.5 found after trying two hundred parameter sets is not the
same number as a Sharpe of 1.5 someone wrote down in advance, and the
difference is quantifiable. That is what most of this file does:

    probabilistic_sharpe   is this Sharpe above a threshold, given how few
                           observations and how fat the tails are?
    deflated_sharpe        the same question after charging you for every
                           strategy you tried before this one
    min_track_record       how long a record you need before the number is
                           evidence rather than an anecdote
    cscv_pbo               the probability that an in-sample winner is a
                           below-median performer out of sample
    bootstrap_sharpe       a confidence interval that survives the fact
                           that returns are serially dependent
    purged_splits          cross-validation folds that cannot leak, because
                           the bars adjacent to the test set are removed

Two conventions, because mixing them up silently produces nonsense:

  * Sharpe ratios here are PER PERIOD (per bar), not annualised. Annualise
    only for display, with `annualise()`.
  * Kurtosis is the raw fourth moment (3.0 for a normal), not excess.

No third-party imports. These run in the agent account too, where the
interpreter is whatever macOS ships and nothing is installed.

References: Bailey & Lopez de Prado, "The Deflated Sharpe Ratio" (2014) and
"The Probability of Backtest Overfitting" (2015); Politis & Romano (1994)
for the stationary bootstrap; Lo (2002) for the Sharpe standard error.
"""
from __future__ import annotations

import math
import random
from itertools import combinations

EULER = 0.5772156649015329


# ---------------------------------------------------------------------------
# Distributions, by hand
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, then one
    Halley step -- good to roughly 1e-15, which is far more than we need)."""
    if not 0.0 < p < 1.0:
        raise ValueError("norm_ppf needs 0 < p < 1")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    low, high = 0.02425, 1 - 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    elif p > high:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    else:
        q, r = p - 0.5, (p - 0.5) ** 2
        x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    error = norm_cdf(x) - p
    density = math.exp(-x * x / 2) / math.sqrt(2 * math.pi)
    if density > 0:
        step = error / density
        x -= step / (1 + x * step / 2)
    return x


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1.0) < 1e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                          + b * math.log(1.0 - x) + a * math.log(x)) * _betacf(b, a, 1.0 - x) / b


def t_pvalue(t: float, df: float) -> float:
    """Two-sided p-value for a t statistic. Exact, not a normal approximation:
    with 40 monthly observations the difference decides significance."""
    if df <= 0:
        return 1.0
    return betainc(df / 2.0, 0.5, df / (df + t * t))


# ---------------------------------------------------------------------------
# Moments
# ---------------------------------------------------------------------------

def moments(returns: list[float]) -> dict:
    """Mean, standard deviation, skew and RAW kurtosis (3.0 = normal)."""
    n = len(returns)
    if n < 2:
        return {"n": n, "mean": 0.0, "sd": 0.0, "skew": 0.0, "kurtosis": 3.0}
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    sd = math.sqrt(m2) if m2 > 0 else 0.0
    if sd <= 0:
        return {"n": n, "mean": mean, "sd": 0.0, "skew": 0.0, "kurtosis": 3.0}
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    # Sample standard deviation for the Sharpe, population moments for the
    # shape -- the combination the PSR papers use.
    return {"n": n, "mean": mean,
            "sd": math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1)),
            "skew": m3 / sd ** 3, "kurtosis": m4 / sd ** 4}


def sharpe(returns: list[float]) -> float:
    """Per-period Sharpe. Annualise for display, never for these tests."""
    m = moments(returns)
    return m["mean"] / m["sd"] if m["sd"] > 0 else 0.0


def annualise(per_period_sharpe: float, periods_per_year: float) -> float:
    return per_period_sharpe * math.sqrt(max(periods_per_year, 1e-9))


def sharpe_standard_error(sr: float, n: int, skew: float, kurtosis: float) -> float:
    """Lo (2002) with the Mertens correction: the error bar on a Sharpe is
    wider when returns are skewed or fat-tailed, which they always are."""
    if n < 3:
        return float("inf")
    variance = (1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr) / (n - 1)
    return math.sqrt(max(variance, 1e-18))


# ---------------------------------------------------------------------------
# Is this Sharpe real?
# ---------------------------------------------------------------------------

def probabilistic_sharpe(sr: float, n: int, skew: float, kurtosis: float,
                         benchmark: float = 0.0) -> float:
    """P(true Sharpe > `benchmark`) given the observed one and the shape of
    the returns. A Sharpe of 2 over 30 bars is not evidence; this says so."""
    error = sharpe_standard_error(sr, n, skew, kurtosis)
    if not math.isfinite(error) or error <= 0:
        return 0.0
    return norm_cdf((sr - benchmark) / error)


def expected_max_sharpe(trials: int, sr_variance: float) -> float:
    """The best Sharpe you'd expect from `trials` WORTHLESS strategies whose
    Sharpes vary by `sr_variance`. This is the bar a real strategy has to
    clear, and it rises with every parameter set you tried."""
    if trials < 2 or sr_variance <= 0:
        return 0.0
    spread = math.sqrt(sr_variance)
    return spread * ((1 - EULER) * norm_ppf(1.0 - 1.0 / trials)
                     + EULER * norm_ppf(1.0 - 1.0 / (trials * math.e)))


def deflated_sharpe(sr: float, n: int, skew: float, kurtosis: float,
                    trials: int, sr_variance: float) -> dict:
    """The Deflated Sharpe Ratio: the probability this Sharpe is real once
    you account for having searched `trials` of them.

    `sr_variance` is the variance of the per-period Sharpes ACROSS those
    trials -- a sweep hands you exactly that, which is why `sweep` feeds
    this. Without it there is no way to know how wide the search was.
    """
    bar = expected_max_sharpe(trials, sr_variance)
    return {"deflated": probabilistic_sharpe(sr, n, skew, kurtosis, benchmark=bar),
            "hurdle": bar, "sharpe": sr, "trials": trials, "n": n}


def min_track_record(sr: float, n: int, skew: float, kurtosis: float,
                     target: float = 0.0, confidence: float = 0.95) -> float:
    """How many observations you'd need for this Sharpe to beat `target` at
    `confidence`. Returns inf when the Sharpe is at or below the target --
    no amount of data makes a losing rule significant."""
    if sr <= target:
        return float("inf")
    z = norm_ppf(confidence)
    shape = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    return 1.0 + max(shape, 1e-12) * (z / (sr - target)) ** 2


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def stationary_bootstrap(returns: list[float], runs: int = 1000,
                         mean_block: float = 0.0, seed: int = 7) -> list[list[float]]:
    """Politis-Romano: resample in geometrically-sized blocks so serial
    structure survives. Shuffling single returns would quietly destroy the
    autocorrelation that makes a drawdown a drawdown."""
    n = len(returns)
    if n < 4:
        return []
    if mean_block <= 0:
        mean_block = max(2.0, n ** (1.0 / 3.0))     # the usual rule of thumb
    p = 1.0 / mean_block
    rng = random.Random(seed)
    out = []
    for _ in range(runs):
        sample, index = [], rng.randrange(n)
        while len(sample) < n:
            sample.append(returns[index])
            index = rng.randrange(n) if rng.random() < p else (index + 1) % n
        out.append(sample)
    return out


def bootstrap_sharpe(returns: list[float], runs: int = 1000,
                     confidence: float = 0.95, seed: int = 7) -> dict:
    """A confidence interval for the Sharpe that respects serial dependence."""
    draws = sorted(sharpe(s) for s in stationary_bootstrap(returns, runs, seed=seed))
    if not draws:
        return {"enough": False}
    tail = (1.0 - confidence) / 2.0

    def pick(q: float) -> float:
        return draws[max(0, min(len(draws) - 1, int(q * len(draws))))]

    observed = sharpe(returns)
    return {"enough": True, "sharpe": observed, "runs": len(draws),
            "low": pick(tail), "median": pick(0.5), "high": pick(1 - tail),
            "p_negative": sum(1 for d in draws if d <= 0) / len(draws)}


# ---------------------------------------------------------------------------
# Overfitting
# ---------------------------------------------------------------------------

def cscv_pbo(matrix: list[list[float]], splits: int = 10, max_combos: int = 400) -> dict:
    """Probability of Backtest Overfitting, by combinatorially symmetric
    cross-validation.

    `matrix` is one column per configuration you tried and one row per
    period, holding that configuration's return in that period -- a sweep's
    full output, not just its winner.

    The procedure: cut the timeline into `splits` blocks, take every way of
    using half of them as "in sample", pick the configuration that wins in
    sample, and see where it ranks out of sample. PBO is how often the
    in-sample winner lands in the bottom half of the out-of-sample ranking.
    Near 0.5 means your selection process carries no information at all.
    """
    rows = len(matrix)
    configs = len(matrix[0]) if rows else 0
    if configs < 2 or rows < splits * 2 or splits < 4:
        return {"enough": False, "reason": "needs at least 4 even splits and 2 configurations"}
    if splits % 2:
        splits -= 1

    size = rows // splits
    blocks = [list(range(i * size, (i + 1) * size)) for i in range(splits)]
    logits, beaten = [], 0
    every = list(combinations(range(splits), splits // 2))
    if len(every) > max_combos:                      # C(16,8) is 12870; sample it
        every = random.Random(7).sample(every, max_combos)

    for chosen in every:
        inside = [i for b in chosen for i in blocks[b]]
        outside = [i for b in range(splits) if b not in chosen for i in blocks[b]]
        in_sample = [sharpe([matrix[i][c] for i in inside]) for c in range(configs)]
        out_sample = [sharpe([matrix[i][c] for i in outside]) for c in range(configs)]
        best = max(range(configs), key=lambda c: in_sample[c])
        # Rank of the in-sample winner among the out-of-sample results.
        rank = sum(1 for value in out_sample if value <= out_sample[best])
        omega = rank / (configs + 1.0)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logits.append(math.log(omega / (1 - omega)))
        if out_sample[best] < sorted(out_sample)[configs // 2]:
            beaten += 1

    return {"enough": True, "pbo": beaten / len(logits), "combinations": len(logits),
            "configurations": configs, "median_logit": sorted(logits)[len(logits) // 2]}


def purged_splits(n: int, folds: int = 5, embargo: float = 0.01) -> list[tuple[list[int], list[int]]]:
    """K-fold splits for a time series: the training bars that touch the test
    window are dropped (purged), and a further `embargo` fraction after it is
    dropped too.

    Plain K-fold on prices leaks -- a label built from tomorrow's return
    trains on a bar the test set is about to use. This is what makes a
    cross-validated backtest honest rather than decorative.
    """
    if n < folds * 2 or folds < 2:
        return []
    size = n // folds
    gap = max(1, int(n * embargo))
    out = []
    for k in range(folds):
        start = k * size
        stop = n if k == folds - 1 else (k + 1) * size
        test = list(range(start, stop))
        train = [i for i in range(n) if i < start - gap or i >= stop + gap]
        if train and test:
            out.append((train, test))
    return out


def reality_check(best: list[float], others: list[list[float]],
                  runs: int = 500, seed: int = 7) -> dict:
    """White's Reality Check: is the best strategy better than the best of a
    family of alternatives, or is it just the luckiest draw?

    Bootstraps the whole family together so the comparison keeps the
    cross-sectional correlation between strategies.
    """
    if not best or not others:
        return {"enough": False}
    n = len(best)
    family = [best] + [o for o in others if len(o) == n]
    if len(family) < 2:
        return {"enough": False}
    observed = max(sum(series) / n for series in family) * math.sqrt(n)
    rng = random.Random(seed)
    mean_of = [sum(series) / n for series in family]
    worse = 0
    for _ in range(runs):
        index = [rng.randrange(n) for _ in range(n)]
        draw = max((sum(series[i] for i in index) / n) - mean_of[k]
                   for k, series in enumerate(family)) * math.sqrt(n)
        if draw >= observed:
            worse += 1
    return {"enough": True, "p_value": worse / runs, "statistic": observed,
            "strategies": len(family)}
