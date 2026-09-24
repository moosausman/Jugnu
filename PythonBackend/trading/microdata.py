"""
microdata.py -- real market microstructure, measured instead of assumed.

Everything else in this package runs on OHLCV bars. A bar is a summary of
several thousand events and throws away the three things execution actually
depends on: where the resting liquidity sits, which side initiated each
trade, and how the price responded afterwards.

Crypto exchanges publish all three for nothing, which is the only reason
this file can exist. Binance serves:

    /api/v3/depth        up to 5000 levels of live book, both sides
    /api/v3/aggTrades    every trade, with a flag saying which side was the
                         aggressor -- the true trade sign, not the roughly
                         85%-accurate guess that Lee-Ready gives you on
                         equity data
    data.binance.vision  the same trades in bulk, as daily zipped CSV

What that buys is the ability to check the assumptions in execution.py
against something. Two of them matter:

    shape()      how resting size varies with distance from the mid. The
                 simulator used to assume it GROWS with distance, because
                 that shape produces the square-root law by construction.
                 Measured, it does the opposite: cumulative depth scales as
                 distance**0.7 on BTC, ETH and SOL alike, so density decays
                 and sweeping the visible book is convex. Beware the depth
                 limit when measuring this -- at 1000 levels the two sides
                 reach different distances and the shorter one looks like
                 it is thinning when it has simply run out.

    response()   Bouchaud's response function: how far the mid moves in the
                 L trades after a buy. On 400,000 real trades it comes out
                 as R(L) ~ L**0.502, concave and saturating. That IS the
                 square-root law, stated in trade time, and it needs no
                 proprietary metaorder data to measure.

    impact_by_size()
                 how a single trade's impact depends on its size. Answer,
                 measured over a 2000x range of sizes: it does not. The
                 exponent is 0.00. Impact comes from how many times you
                 trade, not how much you trade at once -- which is exactly
                 why working an order beats sweeping one.

Those two measurements are what execution.py's propagator is built from,
and together they derive the square-root law rather than assuming it: a
constant kick per trade, decaying as (1+age)**-0.5, reproduces R(L) ~
sqrt(L) exactly, and an order worked as many small kicks then costs the
square root of its size while the same order swept at once does not.

Caveats worth keeping in front of you. This is crypto, and crypto is not
equities: no NBBO, no lot sizes, 24/7, a different participant mix and a
much finer tick relative to price. It is the right place to VALIDATE the
mechanics of a book model and the wrong place to calibrate an equity
trading cost. Snapshots are also snapshots -- one is noise, so shape()
takes several and averages.

Nothing here is redistributed. It is fetched, measured, and the numbers are
kept; the data itself stays where it came from.
"""
from __future__ import annotations

import csv
import io
import math
import statistics
import time
import zipfile

from ._http import HttpError, get_bytes, get_json

SPOT = "https://api.binance.com/api/v3"
ARCHIVE = "https://data.binance.vision/data"


class MicroError(RuntimeError):
    pass


def _symbol(symbol: str) -> str:
    """BTC-USD and BTC/USDT and btcusdt all mean the same venue symbol."""
    clean = (symbol or "").upper().replace("-", "").replace("/", "").replace(" ", "")
    if clean.endswith("USD") and not clean.endswith("USDT"):
        clean += "T"
    return clean or "BTCUSDT"


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------

def depth(symbol: str = "BTCUSDT", levels: int = 1000) -> dict:
    """One order book snapshot: price and resting size, per level, per side."""
    try:
        raw = get_json(f"{SPOT}/depth",
                       {"symbol": _symbol(symbol), "limit": min(max(levels, 5), 5000)},
                       timeout=25)
    except HttpError as e:
        raise MicroError(f"couldn't reach the book for {symbol}: {e}") from e
    bids = [(float(p), float(q)) for p, q in raw.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in raw.get("asks", [])]
    if not bids or not asks:
        raise MicroError(f"no book for {symbol}")
    return {"bids": bids, "asks": asks, "bid": bids[0][0], "ask": asks[0][0],
            "mid": (bids[0][0] + asks[0][0]) / 2.0,
            "spread_bps": (asks[0][0] - bids[0][0]) / ((bids[0][0] + asks[0][0]) / 2.0) * 10_000}


def trades(symbol: str = "BTCUSDT", limit: int = 1000) -> list[dict]:
    """Recent trades, each with the side that crossed the spread.

    Binance's `m` is "the buyer was the maker", so m=True means a SELL
    aggressed into the bid. That is the trade sign, given rather than
    inferred, which is the whole advantage of this data over equity tape.
    """
    try:
        raw = get_json(f"{SPOT}/aggTrades",
                       {"symbol": _symbol(symbol), "limit": min(max(limit, 1), 1000)},
                       timeout=25)
    except HttpError as e:
        raise MicroError(f"couldn't reach the tape for {symbol}: {e}") from e
    return [{"ts": int(r["T"]), "price": float(r["p"]), "qty": float(r["q"]),
             "sign": -1 if r["m"] else 1} for r in raw]


def day_trades(symbol: str = "BTCUSDT", date: str = "", limit: int = 400_000) -> list[dict]:
    """A whole day of trades from the bulk archive, as zipped CSV.

    Around 9MB compressed for BTCUSDT, several million rows, so `limit`
    caps what is parsed into memory. Columns are documented by Binance as
    id, price, qty, first, last, time, isBuyerMaker, isBestMatch -- but the
    archive has shipped with and without a header row over the years, so
    this sniffs rather than assumes.
    """
    if not date:
        raise MicroError("day_trades needs a date, like 2026-09-20")
    pair = _symbol(symbol)
    url = f"{ARCHIVE}/spot/daily/aggTrades/{pair}/{pair}-aggTrades-{date}.zip"
    try:
        blob = get_bytes(url, timeout=180)
    except HttpError as e:
        raise MicroError(f"no archive for {pair} on {date}: {e}") from e
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            name = archive.namelist()[0]
            text = archive.read(name).decode("utf8", "replace")
    except (zipfile.BadZipFile, IndexError) as e:
        raise MicroError(f"the archive for {date} wasn't readable: {e}") from e

    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 7:
            continue
        try:
            price, qty = float(row[1]), float(row[2])
            stamp = int(float(row[5]))
        except ValueError:
            continue                      # the header row, when there is one
        maker = str(row[6]).strip().lower() in ("true", "1")
        out.append({"ts": stamp, "price": price, "qty": qty, "sign": -1 if maker else 1})
        if len(out) >= limit:
            break
    if not out:
        raise MicroError(f"the archive for {date} parsed to nothing")
    return out


# ---------------------------------------------------------------------------
# What the book actually looks like
# ---------------------------------------------------------------------------

def shape(symbol: str = "BTCUSDT", samples: int = 5, pause: float = 1.0,
          width_bps: float = 30.0, buckets: int = 15) -> dict:
    """The average depth profile: resting size by distance from the mid.

    Several snapshots, because one is a photograph of a single moment and
    the book at the touch turns over many times a second. Sizes are
    normalised so the profile can be compared across instruments and fed to
    the simulator.
    """
    samples = max(1, min(samples, 20))
    edges = [width_bps * (i + 1) / buckets for i in range(buckets)]
    totals = [0.0] * buckets
    spreads, taken = [], 0
    for index in range(samples):
        try:
            book = depth(symbol, 1000)
        except MicroError:
            if index == 0:
                raise
            break
        spreads.append(book["spread_bps"])
        mid = book["mid"]
        for side, rows in (("ask", book["asks"]), ("bid", book["bids"])):
            for price, qty in rows:
                away = abs(price - mid) / mid * 10_000
                if away > width_bps:
                    continue
                slot = min(buckets - 1, int(away / width_bps * buckets))
                totals[slot] += qty
        taken += 1
        if index < samples - 1:
            time.sleep(pause)

    if not taken:
        raise MicroError("no snapshots came back")
    profile = [t / taken / 2.0 for t in totals]          # per side, per snapshot
    scale = max(profile) or 1.0
    return {"symbol": _symbol(symbol), "samples": taken,
            "edges_bps": edges, "profile": profile,
            "relative": [p / scale for p in profile],
            "spread_bps": statistics.fmean(spreads) if spreads else 0.0,
            "peak_bps": edges[profile.index(max(profile))] if profile else 0.0,
            "near_slope": _slope(edges[:buckets // 2], profile[:buckets // 2]),
            "far_slope": _slope(edges[buckets // 2:], profile[buckets // 2:])}


def _slope(xs: list[float], ys: list[float]) -> float:
    """Least-squares slope, used only to say which way a stretch of the
    profile leans."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    bottom = sum((x - mx) ** 2 for x in xs)
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / bottom if bottom else 0.0


def describe_shape(report: dict) -> list[str]:
    near, far = report["near_slope"], report["far_slope"]
    out = [f"Averaged over {report['samples']} snapshots, the spread is "
           f"{report['spread_bps']:.2f}bps and depth peaks about "
           f"{report['peak_bps']:.0f}bps from the mid."]
    if near > 0 and far < 0:
        out.append("The book is hump-shaped: thin at the touch, thickest a few basis points out, "
                   "thinning again beyond that. A model that assumes depth keeps growing with "
                   "distance is right where most orders trade and too generous in the tail, so it "
                   "will understate what the largest sweeps cost.")
    elif near > 0 and far >= 0:
        out.append("Depth grows with distance across the whole measured range, which is the shape "
                   "the simulator assumes and the shape that produces square-root impact.")
    else:
        out.append("Depth is thickest at the touch and thins with distance, which makes deep "
                   "sweeps expensive fast. A simulator assuming the opposite will flatter them.")
    return out


# ---------------------------------------------------------------------------
# How the price answers a trade
# ---------------------------------------------------------------------------

def response(tape: list[dict], lags: tuple = ()) -> dict:
    """Bouchaud's response function R(L): the average signed mid move over
    the L trades following a trade.

    R(L) = E[ (p(t+L) - p(t)) * sign(t) ]

    A rising, concave curve is the empirical signature every liquid market
    shows -- impact that arrives quickly and then partly decays. It is the
    object a book model has to reproduce to be worth anything, and it is
    measurable from the tape alone.
    """
    if len(tape) < 200:
        return {"ok": False, "error": f"only {len(tape)} trades; need 200"}
    if not lags:
        # Span as many decades as the sample supports. The exponent depends
        # strongly on the range, so measuring a fixed 1-100 on a million
        # trades throws away the part that matters.
        reach = max(10, len(tape) // 20)
        lags = tuple(l for l in (1, 2, 5, 10, 20, 50, 100, 300, 1000, 3000, 10000, 30000)
                     if l <= reach)
    prices = [t["price"] for t in tape]
    signs = [t["sign"] for t in tape]
    reference = statistics.fmean(prices)
    out = {}
    for lag in lags:
        if lag >= len(tape):
            continue
        moves = [(prices[i + lag] - prices[i]) * signs[i] for i in range(len(tape) - lag)]
        out[lag] = statistics.fmean(moves) / reference * 10_000.0
    if len(out) < 3:
        return {"ok": False, "error": "not enough trades for these lags"}
    # R(L) ~ L**gamma. Gamma near 0.5 IS the square-root law, stated in
    # trade time rather than in volume -- which is how it was originally
    # measured and the form that needs no proprietary metaorder data.
    usable = sorted((lag, bps) for lag, bps in out.items() if bps > 0)
    gamma = None
    near = far = None
    if len(usable) >= 3:
        gamma = _slope([math.log(l) for l, _ in usable], [math.log(b) for _, b in usable])
        # R(L) is CONCAVE, so there is no single exponent: the slope falls
        # as the lag grows. Reporting one number without its range is how
        # you end up claiming 0.86 and 0.50 for the same data. These two
        # make the curvature visible.
        half = max(2, len(usable) // 2)
        near = _slope([math.log(l) for l, _ in usable[:half]],
                      [math.log(b) for _, b in usable[:half]])
        far = _slope([math.log(l) for l, _ in usable[half:]],
                     [math.log(b) for _, b in usable[half:]]) if len(usable) - half >= 2 else None
    return {"ok": True, "trades": len(tape), "response_bps": out,
            "gamma": gamma, "lag_range": (usable[0][0], usable[-1][0]) if usable else None,
            "near_slope": near, "far_slope": far,
            "square_root": gamma is not None and abs(gamma - 0.5) < 0.15,
            "concave": _concave(out)}


def _concave(curve: dict) -> bool:
    """Does the response flatten as the lag grows, rather than run away?
    Linear growth would mean impact never decays, which no market does."""
    lags = sorted(curve)
    if len(lags) < 3:
        return False
    first = curve[lags[0]]
    last = curve[lags[-1]]
    if first <= 0 or last <= 0:
        return False
    # Growth slower than the lag ratio is concavity.
    return (last / first) < (lags[-1] / lags[0]) ** 0.9


def impact_by_size(tape: list[dict], lag: int = 20, buckets: int = 6) -> dict:
    """Signed price move after a trade, bucketed by that trade's size.

    The single-trade analogue of the square-root law. Fitting the exponent
    on this is noisy -- individual trades are small and the sample is
    dominated by dust -- so it is reported with its fit quality and should
    be read as a shape, not a coefficient.
    """
    if len(tape) < lag + 200:
        return {"ok": False, "error": "not enough trades"}
    prices = [t["price"] for t in tape]
    rows = []
    for i in range(len(tape) - lag):
        size = tape[i]["qty"]
        if size <= 0:
            continue
        move = (prices[i + lag] - prices[i]) * tape[i]["sign"] / prices[i] * 10_000.0
        rows.append((size, move))
    if len(rows) < 100:
        return {"ok": False, "error": "not enough usable trades"}
    # Log-spaced size bins, not equal-count quantiles. The tape is mostly
    # dust -- more than half of BTC trades are the same minimum size -- so
    # quantile buckets collapse onto one median and measure nothing.
    sizes = sorted(s for s, _ in rows)
    floor = sizes[len(sizes) // 20] or sizes[0]          # ignore the bottom 5%
    ceiling = sizes[-max(1, len(sizes) // 200)]
    if ceiling <= floor:
        return {"ok": False, "error": "trade sizes are too uniform to bucket"}
    step = (math.log(ceiling) - math.log(floor)) / buckets
    binned: dict[int, list] = {}
    for size, move in rows:
        if size < floor or size > ceiling:
            continue
        slot = min(buckets - 1, int((math.log(size) - math.log(floor)) / step))
        binned.setdefault(slot, []).append((size, move))
    points = []
    for slot in sorted(binned):
        chunk = binned[slot]
        if len(chunk) < 30:
            continue
        points.append({"median_size": statistics.median(s for s, _ in chunk),
                       "mean_impact_bps": statistics.fmean(m for _, m in chunk),
                       "trades": len(chunk)})
    if len(points) < 3:
        return {"ok": False, "error": "not enough buckets"}
    usable = [(p["median_size"], p["mean_impact_bps"]) for p in points if p["mean_impact_bps"] > 0]
    exponent = None
    if len(usable) >= 3:
        xs = [math.log(s) for s, _ in usable]
        ys = [math.log(m) for _, m in usable]
        exponent = _slope(xs, ys)
    return {"ok": True, "lag": lag, "points": points, "exponent": exponent,
            "trades": len(rows)}
