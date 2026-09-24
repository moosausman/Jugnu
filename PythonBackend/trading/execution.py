"""
execution.py -- what it would actually have cost to get the trade done.

capacity.py answers this with a formula: impact grows with the square root
of the volume you take. This answers it by trading. It drives the matching
engine in book.py through a simulated session and reports what came back --
fill by fill, with the spread paid, the queue waited in, and the fills that
arrived precisely because the price was about to move against you.

The honest caveat, stated once and loudly: THIS IS DRIVEN BY BARS, NOT BY
TICKS. Real execution research uses message-by-message market data. What
happens here is that each OHLCV bar is expanded into a plausible path --
open to low to high to close on an up bar, the reverse on a down bar --
with the bar's volume spread across it and the aggressor side leaning the
way the price is going. The book is real, the matching is real, the queue
is real; the order flow is a reconstruction. It will tell you that crossing
the spread on every trade is expensive and that resting passively means
sometimes not trading at all, and it will get the sign and the rough size
of those effects right. It cannot tell you your fill rate on a venue.

Five ways to get a trade done, which is the comparison worth having:

    market      cross immediately. Certain, and you pay for it
    limit       rest at a price and wait. Free, if it fills
    passive     join the near touch and follow it. The realistic default
    twap        equal slices through the window
    vwap        slices weighted by each bar's volume
    pov         take a fixed share of whatever trades

Written against the same constraint as everything else here: no
third-party packages, so it runs in the agent account too.
"""
from __future__ import annotations

import math
import random

from .book import BUY, SELL, Book, Order

MINE = "mine"
CROWD = "market"


class Session:
    """A book kept alive across bars, with a crowd trading in it."""

    def __init__(self, tick: float = 0.01, levels: int = 6, spread_ticks: int = 1,
                 depth_share: float = 0.35, seed: int = 7):
        """`depth_share` is how much of a step's volume rests at each level.
        Higher means a deeper, more forgiving book."""
        self.book = Book(tick=tick)
        self.levels = levels
        self.spread_ticks = max(1, spread_ticks)
        self.depth_share = depth_share
        self.rng = random.Random(seed)
        self.my_fills: list = []
        self.mid_history: list[float] = []

    # -- the crowd ---------------------------------------------------------

    def _refresh(self, mid: float, size: float) -> None:
        """Re-post the crowd's resting liquidity around `mid`.

        The crowd's old orders are pulled and replaced; yours are left
        exactly where they are. New crowd orders at your price arrive
        AFTER you, so they queue behind you -- which is what actually
        happens when you have been resting a while.
        """
        self.book.cancel_owner(CROWD)
        self.book.prune()
        half = self.spread_ticks * self.book.tick / 2.0
        for level in range(self.levels):
            offset = half + level * self.book.tick
            resting = max(1.0, size * self.depth_share * (0.75 ** level))
            self.book.submit(Order(BUY, resting, round(mid - offset, 10), owner=CROWD))
            self.book.submit(Order(SELL, resting, round(mid + offset, 10), owner=CROWD))

    def _aggress(self, volume: float, buy_share: float, reference: float) -> None:
        """Send the crowd's market orders. `buy_share` leans with the price:
        a rising step is mostly buyers lifting offers.

        They carry a protection limit a few ticks out. Without one, a step
        whose volume exceeds the visible depth sweeps the entire book, and
        a resting buy order at $1 on a $100 stock gets filled -- which is
        not a market, it is an artefact of only modelling six levels.
        """
        band = (self.levels + 2) * self.book.tick
        buys = volume * buy_share
        sells = volume - buys
        if buys > 0:
            self._collect(self.book.submit(
                Order(BUY, buys, round(reference + band, 10), tif="IOC", owner=CROWD)))
        if sells > 0:
            self._collect(self.book.submit(
                Order(SELL, sells, round(reference - band, 10), tif="IOC", owner=CROWD)))

    def _collect(self, fills: list) -> None:
        for fill in fills:
            if fill.owner == MINE:
                self.my_fills.append(fill)

    # -- time --------------------------------------------------------------

    def path(self, bar, steps: int) -> list[float]:
        """A plausible walk through one bar. Up bars dip first, down bars
        pop first -- the usual convention, and the one that decides whether
        a resting order got touched."""
        waypoints = ([bar.open, bar.low, bar.high, bar.close] if bar.close >= bar.open
                     else [bar.open, bar.high, bar.low, bar.close])
        out, legs = [], len(waypoints) - 1
        per_leg = max(1, steps // legs)
        for leg in range(legs):
            start, stop = waypoints[leg], waypoints[leg + 1]
            for i in range(per_leg):
                out.append(start + (stop - start) * (i + 1) / per_leg)
        return out or [bar.close]

    def run_bar(self, bar, steps: int = 8, on_step=None) -> None:
        prices = self.path(bar, steps)
        per_step = max(bar.volume, 1.0) / max(len(prices), 1)
        span = max(bar.high - bar.low, self.book.tick)
        previous = bar.open
        for price in prices:
            self._refresh(price, per_step)
            # Flow leans the way the price moved, which is what makes a
            # passive fill arrive just before the move continues.
            lean = 0.5 + 0.5 * max(-1.0, min(1.0, (price - previous) / (span / len(prices))))
            self._aggress(per_step, max(0.05, min(0.95, lean)), price)
            self.mid_history.append(self.book.mid or price)
            if on_step:
                on_step(self)
            previous = price


# ---------------------------------------------------------------------------
# Getting a trade done
# ---------------------------------------------------------------------------

def _shortfall(side: str, arrival: float, average: float) -> float:
    """Implementation shortfall in basis points, positive meaning worse.
    Buying above where the price was when you decided is a cost; selling
    below it is the same cost with the sign flipped."""
    if not arrival:
        return 0.0
    move = (average - arrival) if side == BUY else (arrival - average)
    return move / arrival * 10_000.0


def _total_shortfall(side: str, wanted: float, got: float, arrival: float,
                     average: float, final: float) -> float:
    """Perold's implementation shortfall: the shares you traded charged at
    what you paid, and the shares you DIDN'T trade charged at where the
    price ended up.

    Without the second half, never trading is free and always wins. The
    opportunity cost of a limit order that sat there while the price ran
    away is the whole reason anyone crosses a spread.
    """
    if not arrival or wanted <= 0:
        return 0.0
    sign = 1.0 if side == BUY else -1.0
    traded = got * (average - arrival) * sign if got else 0.0
    missed = (wanted - got) * (final - arrival) * sign
    return (traded + missed) / (arrival * wanted) * 10_000.0


def _report(side: str, wanted: float, arrival: float, session: Session,
            interval_vwap: float) -> dict:
    fills = session.my_fills
    got = sum(f.qty for f in fills)
    final = session.mid_history[-1] if session.mid_history else arrival
    if got <= 0:
        # No average price exists, so no cost does either. Saying "0 bps" or
        # computing one against a price of zero would both be lies.
        return {"requested": wanted, "filled": 0.0, "fill_rate": 0.0,
                "arrival": round(arrival, 6), "average_price": None,
                "shortfall_bps": None, "vs_vwap_bps": None, "spread_paid_bps": None,
                "maker_share": None, "fills": 0, "post_fill_drift_bps": None,
                "total_shortfall_bps": round(
                    _total_shortfall(side, wanted, 0.0, arrival, 0.0, final), 2)}
    average = sum(f.price * f.qty for f in fills) / got
    maker = sum(f.qty for f in fills if f.role == "maker")
    # Against the arrival mid this includes everything the market did while
    # you traded. Against the window's VWAP it is much closer to what the
    # execution itself cost, which is the number to compare algorithms on.
    sign = 1.0 if side == BUY else -1.0
    spread_paid = sum((f.price - (f.mid_before or f.price)) * sign * f.qty
                      for f in fills) / got / (average or 1.0) * 10_000.0
    later = session.mid_history[-1] if session.mid_history else average
    drift = sum((later - f.price) * sign * f.qty for f in fills) / got / (average or 1.0) * 10_000.0
    return {
        "requested": wanted, "filled": round(got, 4),
        "fill_rate": round(got / wanted, 4) if wanted else 0.0,
        "arrival": round(arrival, 6), "average_price": round(average, 6),
        "shortfall_bps": round(_shortfall(side, arrival, average), 2),
        "total_shortfall_bps": round(
            _total_shortfall(side, wanted, got, arrival, average, final), 2),
        "vs_vwap_bps": round(_shortfall(side, interval_vwap, average), 2) if interval_vwap else None,
        "spread_paid_bps": round(spread_paid, 2),
        "maker_share": round(maker / got, 4),
        "fills": len(fills),
        "post_fill_drift_bps": round(drift, 2),
    }


def execute(bars: list, side: str, quantity: float, algo: str = "passive", *,
            rate: float = 0.1, limit_price: float | None = None,
            steps: int = 8, seed: int = 7, **book_args) -> dict:
    """Work `quantity` through `bars` with `algo`, and report what it cost.

    Args:
        bars: the window to trade across -- a handful of bars, not years.
        side: "buy" or "sell".
        quantity: shares.
        algo: market | limit | passive | twap | vwap | pov.
        rate: for pov, the share of volume to take.
        limit_price: for limit; defaults to the arrival touch.
    """
    if not bars or quantity <= 0:
        return {"ok": False, "error": "nothing to trade"}
    session = Session(seed=seed, **book_args)
    session._refresh(bars[0].open, max(bars[0].volume, 1.0) / steps)
    arrival = session.book.mid or bars[0].open
    total_volume = sum(max(b.volume, 1.0) for b in bars)
    remaining = quantity
    placed = 0

    for index, bar in enumerate(bars):
        if remaining <= 1e-9:
            break
        before = sum(f.qty for f in session.my_fills)

        if algo == "market":
            # Cross for everything available now, and keep sweeping on later
            # bars if the book could not absorb it. Giving up after one bar
            # would flatter it: an unfilled market order is not a cheap one.
            session._collect(session.book.submit(
                Order(side, remaining, None, tif="IOC", owner=MINE)))
        elif algo in ("twap", "vwap", "pov"):
            if algo == "twap":
                slice_qty = quantity / len(bars)
            elif algo == "vwap":
                slice_qty = quantity * max(bar.volume, 1.0) / total_volume
            else:
                slice_qty = max(bar.volume, 1.0) * rate
            slice_qty = min(remaining, slice_qty)
            if slice_qty > 0:
                session._collect(session.book.submit(
                    Order(side, slice_qty, None, tif="IOC", owner=MINE)))
        elif algo == "limit":
            if placed == 0:                      # posted once, and lived with
                price = limit_price if limit_price is not None else (
                    session.book.best_bid if side == BUY else session.book.best_ask)
                if price:
                    session.book.submit(Order(side, remaining, price, owner=MINE))
                    placed += 1
        elif algo == "passive":
            session.book.cancel_owner(MINE)      # re-join the touch as it moves
            price = session.book.best_bid if side == BUY else session.book.best_ask
            if price:
                session.book.submit(Order(side, remaining, price, owner=MINE))
                placed += 1

        session.run_bar(bar, steps=steps)
        remaining = quantity - sum(f.qty for f in session.my_fills)
        if algo == "market" and sum(f.qty for f in session.my_fills) == before and index > 2:
            break                                 # the book simply cannot absorb it

    traded = sum(max(b.volume, 1.0) * (b.high + b.low + b.close) / 3.0 for b in bars)
    interval_vwap = traded / total_volume if total_volume else 0.0
    out = _report(side, quantity, arrival, session, interval_vwap)
    out.update({"ok": True, "algo": algo, "side": side, "bars": len(bars)})
    return out


def compare(bars: list, side: str, quantity: float, *, rate: float = 0.1,
            steps: int = 8, seed: int = 7) -> dict:
    """Every algorithm on the same window and the same order, which is the
    only way the numbers mean anything relative to each other."""
    out = {}
    for algo in ("market", "twap", "vwap", "pov", "passive", "limit"):
        out[algo] = execute(bars, side, quantity, algo, rate=rate, steps=steps, seed=seed)
    return {"ok": True, "side": side, "quantity": quantity, "bars": len(bars), "algos": out}


def verdict(report: dict) -> list[str]:
    """What the comparison says, in sentences."""
    algos = report.get("algos") or {}
    done = {k: v for k, v in algos.items() if v.get("ok")}
    if not done:
        return ["nothing executed"]
    out = []
    crossed = done.get("market")
    if crossed and crossed["fill_rate"] > 0.99:
        out.append(f"Crossing the spread for the whole order cost "
                   f"{crossed['shortfall_bps']:+.1f}bps against arrival "
                   f"({crossed['spread_paid_bps']:+.1f} of that was the spread itself). "
                   "That is the price of certainty.")
    elif crossed:
        out.append(f"Even crossing the spread only got {crossed['fill_rate'] * 100:.0f}% done -- "
                   "the order is large against what this book can absorb, and no algorithm "
                   "fixes that.")
    ranked = sorted(done.items(), key=lambda kv: kv[1]["total_shortfall_bps"])
    best, info = ranked[0]
    out.append(f"Cheapest overall was {best} at {info['total_shortfall_bps']:+.1f}bps, counting the "
               f"shares it did not trade at where the price finished ({info['fill_rate'] * 100:.0f}% "
               "filled). That is the comparison that does not reward refusing to trade.")
    for name, info in sorted(done.items(), key=lambda kv: kv[1]["fill_rate"]):
        if info["fill_rate"] <= 0.99 and name != "market":
            price = (f"at {info['vs_vwap_bps']:+.1f}bps vs VWAP"
                     if info["vs_vwap_bps"] is not None else "and never traded at all")
            out.append(f"{name} got {info['fill_rate'] * 100:.0f}% done {price} -- a good price on "
                       "the part you traded is not the same as having traded.")
    passive = done.get("passive")
    if passive and passive["fills"] and (passive["post_fill_drift_bps"] or 0) < -1:
        out.append(f"Passive fills were followed by {passive['post_fill_drift_bps']:.1f}bps of drift "
                   "the wrong way. That is adverse selection: resting orders are filled by whoever "
                   "is right about the next move.")
    return out
