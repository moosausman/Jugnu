"""
engine.py -- the simulator. Paper only: nothing here can reach a broker.

The one rule everything else depends on:

    a strategy sees bars up to and including the bar it decides on, and
    its order fills at the NEXT bar's open, minus costs.

Homemade backtesters usually fill at the same bar's close (or worse, at the
low), which quietly invents money that couldn't have been made. The fill
price here is always a price that came after the decision.

Strategies return a target weight (0..1 long-only, -1..1 with shorting
allowed), not orders. Sizing, rebalancing and cost accounting then happen in
one place, so two strategies can be compared on equal terms.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .data import Bar


@dataclass
class Trade:
    ts: int
    date: str
    side: str            # buy | sell
    shares: float
    price: float         # what we actually paid, slippage included
    cost: float          # commission on this fill
    equity: float        # portfolio value right after the fill
    pnl: float | None = None      # realised P&L, on the closing side of a round trip


class View:
    """A read-only window over a list that stops at the current bar.

    Handing a strategy the whole series and *trusting* it not to look ahead
    is how backtests end up profitable by accident. Reaching past the
    current bar raises here instead, so a peeking strategy fails loudly
    rather than printing a wonderful number.
    """
    __slots__ = ("_data", "_n")

    def __init__(self, data, n: int):
        self._data = data
        self._n = max(0, n)

    def __len__(self) -> int:
        return self._n

    def __iter__(self):
        return iter(self._data[:self._n])

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self._data[:self._n][index]
        if index < 0:
            index += self._n
        if not 0 <= index < self._n:
            raise IndexError("a strategy can only see bars up to the one it's deciding on")
        return self._data[index]

    def __repr__(self) -> str:
        return f"View({self._n} of {len(self._data)})"


@dataclass
class Context:
    """What a strategy is allowed to see: history up to now, and where it
    currently stands. `bars` and `closes` end at the current bar."""
    _bars: list[Bar]
    _closes: list[float]
    i: int
    position: float
    cash: float
    equity: float
    params: dict

    @property
    def bars(self) -> View:
        return View(self._bars, self.i + 1)

    @property
    def bar(self) -> Bar:
        return self._bars[self.i]

    @property
    def closes(self) -> View:
        return View(self._closes, self.i + 1)

    def param(self, name: str, default=None):
        return self.params.get(name, default)


Strategy = Callable[[Context], float]


@dataclass
class Result:
    symbol: str
    strategy: str
    params: dict
    interval: str
    bars: int
    start: str
    end: str
    cash: float
    commission: float
    slippage_bps: float
    equity: list[float] = field(default_factory=list)          # marked at every bar close
    dates: list[str] = field(default_factory=list)
    benchmark: list[float] = field(default_factory=list)       # buy & hold, same costs
    trades: list[Trade] = field(default_factory=list)
    exposure: float = 0.0
    metrics: dict = field(default_factory=dict)
    benchmark_metrics: dict = field(default_factory=dict)

    @property
    def final(self) -> float:
        return self.equity[-1] if self.equity else self.cash


def run(bars: list[Bar], strategy: Strategy, *, name: str = "strategy", symbol: str = "",
        interval: str = "1d", params: dict | None = None, cash: float = 10_000.0,
        commission: float = 1.0, slippage_bps: float = 5.0, allow_short: bool = False,
        rebalance_band: float = 0.05) -> Result:
    """Walks the bars once and returns everything that happened."""
    if len(bars) < 3:
        raise ValueError("Not enough price history to simulate (need at least 3 bars).")
    params = dict(params or {})
    slip = slippage_bps / 10_000.0

    closes = [b.close for b in bars]      # computed once; the View slices it per bar
    position = 0.0          # shares (negative when short)
    book_cash = cash
    avg_cost = 0.0
    trades: list[Trade] = []
    equity_curve: list[float] = []
    dates: list[str] = []
    invested_bars = 0

    for i, bar in enumerate(bars):
        equity = book_cash + position * bar.close
        equity_curve.append(equity)
        dates.append(bar.stamp if interval != "1d" else bar.date)
        if position:
            invested_bars += 1

        if i + 1 >= len(bars):
            break                      # nothing left to fill against; the last bar only marks

        target = strategy(Context(bars, closes, i, position, book_cash, equity, params))
        try:
            target = float(target)
        except (TypeError, ValueError):
            target = 0.0
        lower = -1.0 if allow_short else 0.0
        target = max(lower, min(1.0, target))

        # Trade when the *target* moves, not when the price drifts. Without
        # this band, holding a rising stock at weight 1.0 re-buys a sliver
        # every single bar: hundreds of pointless trades, all paying
        # commission, and a flattering win rate made of dust.
        held_weight = (position * bar.close / equity) if equity > 0 else 0.0
        if abs(target - held_weight) < rebalance_band:
            continue

        fill_bar = bars[i + 1]
        # Commission and slippage come out of the same pot, so size against
        # what's actually spendable -- otherwise "fully invested" overdraws.
        spendable = max(0.0, equity - commission)
        wanted_shares = (target * spendable) / (fill_bar.open * (1 + slip)) if fill_bar.open > 0 else 0.0
        delta = wanted_shares - position
        if abs(delta * fill_bar.open) < max(1.0, 0.001 * equity):
            continue                       # dust: not worth a commission

        price = fill_bar.open * (1 + slip) if delta > 0 else fill_bar.open * (1 - slip)
        book_cash -= delta * price + commission
        realised = None
        if delta > 0:
            if position >= 0:
                avg_cost = ((avg_cost * position) + price * delta) / (position + delta)
            position += delta
        else:
            closing = min(abs(delta), max(position, 0.0))
            if closing > 0:
                realised = (price - avg_cost) * closing - commission
            position += delta
            if position <= 0:
                avg_cost = price if position < 0 else 0.0
        filled_equity = book_cash + position * fill_bar.open
        trades.append(Trade(fill_bar.ts, fill_bar.date, "buy" if delta > 0 else "sell",
                            abs(delta), price, commission, filled_equity, realised))

    result = Result(symbol=symbol or name, strategy=name, params=params, interval=interval,
                    bars=len(bars), start=dates[0], end=dates[-1], cash=cash,
                    commission=commission, slippage_bps=slippage_bps,
                    equity=equity_curve, dates=dates, trades=trades,
                    exposure=invested_bars / len(bars) if bars else 0.0)
    result.benchmark = buy_and_hold(bars, cash, commission, slippage_bps)
    return result


def buy_and_hold(bars: list[Bar], cash: float, commission: float, slippage_bps: float) -> list[float]:
    """The bar everything is measured against: bought at the second bar's
    open (the first decision a strategy could also have made), same costs."""
    entry = bars[1].open * (1 + slippage_bps / 10_000.0)
    shares = (cash - commission) / entry if entry > 0 else 0.0
    left = cash - commission - shares * entry
    return [cash if i == 0 else left + shares * bar.close for i, bar in enumerate(bars)]
