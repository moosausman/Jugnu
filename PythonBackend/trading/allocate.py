"""
allocate.py -- how much of each thing to hold.

Mean-variance optimisation is famously unusable out of sample: it needs the
inverse of a covariance matrix estimated from noise, and it answers by
betting everything on whichever asset's estimate happened to be flattering.
The methods here all avoid that inverse.

    equal_weight        the benchmark everything has to beat, and often
                        doesn't
    inverse_variance    risk parity's simple form: quiet assets get more
    hierarchical        HRP -- cluster first, split risk down the tree, so
                        correlated assets compete with each other rather
                        than with everything at once
    kelly               how much leverage the edge itself justifies
    vol_target          the leverage that holds portfolio volatility at a
                        chosen level

HRP is Lopez de Prado's (2016). The idea worth keeping: correlation
structure is a tree, not a flat matrix, and allocating down the tree needs
no inversion and no expected returns at all -- so it cannot be wrecked by
getting expected returns wrong, which everyone does.

Pure Python: this runs in the agent account too.
"""
from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Covariance
# ---------------------------------------------------------------------------

def covariance(series: dict[str, list[float]]) -> tuple[list[str], list[list[float]]]:
    """Sample covariance over the overlapping window of every series."""
    names = sorted(series)
    length = min(len(series[n]) for n in names) if names else 0
    if length < 3:
        return names, []
    trimmed = {n: series[n][-length:] for n in names}
    means = {n: sum(trimmed[n]) / length for n in names}
    matrix = []
    for a in names:
        row = []
        for b in names:
            row.append(sum((trimmed[a][i] - means[a]) * (trimmed[b][i] - means[b])
                           for i in range(length)) / (length - 1))
        matrix.append(row)
    return names, matrix


def correlation(cov: list[list[float]]) -> list[list[float]]:
    n = len(cov)
    sd = [math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0 for i in range(n)]
    out = []
    for i in range(n):
        row = []
        for j in range(n):
            row.append(cov[i][j] / (sd[i] * sd[j]) if sd[i] > 0 and sd[j] > 0 else (1.0 if i == j else 0.0))
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# The easy ones
# ---------------------------------------------------------------------------

def equal_weight(names: list[str]) -> dict[str, float]:
    return {n: 1.0 / len(names) for n in names} if names else {}


def inverse_variance(names: list[str], cov: list[list[float]]) -> dict[str, float]:
    weights = [1.0 / cov[i][i] if cov[i][i] > 0 else 0.0 for i in range(len(names))]
    total = sum(weights)
    if total <= 0:
        return equal_weight(names)
    return {names[i]: weights[i] / total for i in range(len(names))}


# ---------------------------------------------------------------------------
# Hierarchical risk parity
# ---------------------------------------------------------------------------

def _distance(corr: list[list[float]]) -> list[list[float]]:
    """Lopez de Prado's metric: d = sqrt(0.5 * (1 - rho)). A proper distance,
    so clustering on it means something."""
    n = len(corr)
    return [[math.sqrt(max(0.0, 0.5 * (1.0 - corr[i][j]))) for j in range(n)] for i in range(n)]


def _single_linkage_order(dist: list[list[float]]) -> list[int]:
    """Cluster with single linkage and return the leaves in tree order --
    the quasi-diagonalisation step, which puts similar assets adjacent."""
    n = len(dist)
    if n <= 1:
        return list(range(n))
    nodes = {i: [i] for i in range(n)}          # cluster id -> member leaves
    while len(nodes) > 1:
        keys = list(nodes)
        best, pair = None, None
        for x in range(len(keys)):
            for y in range(x + 1, len(keys)):
                a, b = keys[x], keys[y]
                gap = min(dist[i][j] for i in nodes[a] for j in nodes[b])
                if best is None or gap < best:
                    best, pair = gap, (a, b)
        a, b = pair
        nodes[a] = nodes[a] + nodes[b]          # order preserved: this IS the seriation
        del nodes[b]
    return next(iter(nodes.values()))


def _cluster_variance(cov: list[list[float]], items: list[int]) -> float:
    """Variance of a cluster held at inverse-variance weights."""
    weights = [1.0 / cov[i][i] if cov[i][i] > 0 else 0.0 for i in items]
    total = sum(weights)
    if total <= 0:
        return 0.0
    weights = [w / total for w in weights]
    return sum(weights[a] * cov[items[a]][items[b]] * weights[b]
               for a in range(len(items)) for b in range(len(items)))


def hierarchical(names: list[str], cov: list[list[float]]) -> dict[str, float]:
    """HRP weights: cluster, order, then split risk down the tree."""
    n = len(names)
    if n == 0:
        return {}
    if n == 1:
        return {names[0]: 1.0}
    order = _single_linkage_order(_distance(correlation(cov)))
    weights = {i: 1.0 for i in order}
    groups = [order]
    while groups:
        nxt = []
        for group in groups:
            if len(group) < 2:
                continue
            half = len(group) // 2
            left, right = group[:half], group[half:]
            var_left, var_right = _cluster_variance(cov, left), _cluster_variance(cov, right)
            total = var_left + var_right
            share = 1.0 - var_left / total if total > 0 else 0.5
            for i in left:
                weights[i] *= share
            for i in right:
                weights[i] *= 1.0 - share
            nxt += [left, right]
        groups = nxt
    total = sum(weights.values())
    return {names[i]: weights[i] / total for i in order} if total > 0 else equal_weight(names)


# ---------------------------------------------------------------------------
# Leverage
# ---------------------------------------------------------------------------

def kelly(returns: list[float], cap: float = 4.0) -> dict:
    """The Kelly fraction, f* = mean / variance, and the half-Kelly most
    desks actually run.

    Full Kelly maximises long-run growth and is far too violent to hold:
    it accepts arbitrarily deep drawdowns on the way. Half Kelly gives up
    a quarter of the growth for most of the volatility.
    """
    n = len(returns)
    if n < 10:
        return {"enough": False}
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if variance <= 0:
        return {"enough": False}
    raw = mean / variance
    return {"enough": True, "full": max(min(raw, cap), -cap),
            "half": max(min(raw / 2.0, cap), -cap), "uncapped": raw,
            "mean": mean, "variance": variance}


def vol_target(returns: list[float], target_annual: float = 0.15,
               periods_per_year: float = 252.0, cap: float = 3.0) -> dict:
    """The constant leverage that would have held volatility at the target.

    Backward-looking by construction -- a live version has to estimate the
    volatility from a trailing window, which is a different and harder
    thing. This says what the strategy's natural size is, not what to do.
    """
    n = len(returns)
    if n < 10:
        return {"enough": False}
    mean = sum(returns) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1))
    if sd <= 0:
        return {"enough": False}
    realised = sd * math.sqrt(periods_per_year)
    return {"enough": True, "realised_vol": realised, "target": target_annual,
            "leverage": max(0.0, min(target_annual / realised, cap))}


def summarise(names: list[str], series: dict[str, list[float]]) -> dict:
    """Every scheme side by side, with what each portfolio's volatility
    would have been -- the only fair way to compare allocators."""
    names, cov = covariance(series)
    if not cov:
        return {"ok": False, "error": "not enough overlapping history"}
    schemes = {
        "equal": equal_weight(names),
        "inverse variance": inverse_variance(names, cov),
        "hierarchical": hierarchical(names, cov),
    }
    out = {}
    for label, weights in schemes.items():
        order = [weights.get(n, 0.0) for n in names]
        variance = sum(order[a] * cov[a][b] * order[b]
                       for a in range(len(names)) for b in range(len(names)))
        out[label] = {"weights": weights,
                      "annual_vol": math.sqrt(max(variance, 0.0)) * math.sqrt(252.0),
                      "concentration": sum(w * w for w in order)}
    return {"ok": True, "names": names, "schemes": out}
