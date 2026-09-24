"""
book.py -- a limit order book that matches like a real one.

The engine everywhere else in this package fills you at a bar's open for
whatever size you asked for. That is fine for deciding whether a signal has
any information in it, and useless for deciding whether you could have
traded on it. This is the other half: a book with price-time priority,
where an order joins the back of a queue at its price and waits.

What that buys is the three things a bar-close fill cannot show you:

    queue position     you are behind everyone already resting at your
                       price. If the level trades 10,000 shares and 8,000
                       were ahead of you, you got 2,000 -- not all of it
    partial fills      an order is a series of fills at different prices,
                       not one number
    adverse selection  the passive fills you get are the ones you did not
                       want. You are bid at 99 and get filled precisely
                       when the market is about to go to 98

Prices are integers internally -- ticks, not floats -- because a book that
compares floats for equality eventually crosses itself over a rounding
error and nothing afterwards makes sense.

Orders carry an `owner`. The simulator in execution.py posts anonymous
liquidity as "market" and your orders as "mine", so the same engine matches
both and nothing about your order is special to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

BUY, SELL = "buy", "sell"


@dataclass
class Order:
    side: str
    qty: float
    price: float | None = None        # None is a market order
    tif: str = "GTC"                  # GTC rests, IOC takes what it can, FOK is all or nothing
    owner: str = "market"
    id: int = 0
    ts: int = 0
    filled: float = 0.0
    queue_ahead: float = 0.0          # resting quantity in front of it when it joined

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled)

    @property
    def done(self) -> bool:
        return self.remaining <= 1e-9


@dataclass
class Fill:
    order_id: int
    owner: str
    side: str
    price: float
    qty: float
    ts: int
    role: str                          # "taker" crossed the spread, "maker" was resting
    mid_before: float = 0.0            # for measuring what the fill cost, and what it caught


@dataclass
class Level:
    ticks: int
    orders: list = field(default_factory=list)     # FIFO: first in, first filled

    @property
    def size(self) -> float:
        return sum(o.remaining for o in self.orders)


class Book:
    """One instrument's resting orders, with price-time priority."""

    def __init__(self, tick: float = 0.01):
        self.tick = tick
        self.bids: dict[int, Level] = {}
        self.asks: dict[int, Level] = {}
        self.clock = 0
        self._ids = count(1)
        self.trades: list[Fill] = []

    # -- prices ------------------------------------------------------------

    def to_ticks(self, price: float) -> int:
        return int(round(price / self.tick))

    def to_price(self, ticks: int) -> float:
        return round(ticks * self.tick, 10)

    @property
    def best_bid(self) -> float | None:
        live = [t for t, lv in self.bids.items() if lv.size > 1e-9]
        return self.to_price(max(live)) if live else None

    @property
    def best_ask(self) -> float | None:
        live = [t for t, lv in self.asks.items() if lv.size > 1e-9]
        return self.to_price(min(live)) if live else None

    @property
    def mid(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return bid if ask is None else ask
        return (bid + ask) / 2.0

    @property
    def spread(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        return None if bid is None or ask is None else ask - bid

    def depth(self, side: str, levels: int = 5) -> list[tuple[float, float]]:
        book = self.bids if side == BUY else self.asks
        live = sorted((t for t, lv in book.items() if lv.size > 1e-9), reverse=(side == BUY))
        return [(self.to_price(t), book[t].size) for t in live[:levels]]

    # -- submitting --------------------------------------------------------

    def submit(self, order: Order) -> list[Fill]:
        """Match what can be matched; rest the remainder if the order says so."""
        self.clock += 1
        order.id = order.id or next(self._ids)
        order.ts = self.clock
        mid_before = self.mid or (order.price or 0.0)

        if order.tif == "FOK" and not self._fillable(order):
            return []

        fills = self._match(order, mid_before)
        if order.remaining > 1e-9 and order.tif == "GTC" and order.price is not None:
            self._rest(order)
        return fills

    def cancel(self, order_id: int) -> bool:
        for book in (self.bids, self.asks):
            for level in book.values():
                for resting in level.orders:
                    if resting.id == order_id:
                        level.orders.remove(resting)
                        return True
        return False

    def cancel_owner(self, owner: str) -> int:
        removed = 0
        for book in (self.bids, self.asks):
            for level in book.values():
                keep = [o for o in level.orders if o.owner != owner]
                removed += len(level.orders) - len(keep)
                level.orders = keep
        return removed

    # -- the matching itself ------------------------------------------------

    def _opposite(self, side: str) -> dict:
        return self.asks if side == BUY else self.bids

    def _crosses(self, side: str, limit: int | None, level_ticks: int) -> bool:
        if limit is None:
            return True                                   # a market order takes anything
        return level_ticks <= limit if side == BUY else level_ticks >= limit

    def _fillable(self, order: Order) -> bool:
        """Enough resting size at acceptable prices for a fill-or-kill."""
        book = self._opposite(order.side)
        limit = None if order.price is None else self.to_ticks(order.price)
        available = sum(lv.size for t, lv in book.items() if self._crosses(order.side, limit, t))
        return available + 1e-9 >= order.qty

    def _match(self, order: Order, mid_before: float) -> list[Fill]:
        book = self._opposite(order.side)
        limit = None if order.price is None else self.to_ticks(order.price)
        fills: list[Fill] = []
        while order.remaining > 1e-9:
            live = [t for t, lv in book.items()
                    if lv.size > 1e-9 and self._crosses(order.side, limit, t)]
            if not live:
                break
            best = min(live) if order.side == BUY else max(live)
            level = book[best]
            price = self.to_price(best)
            # FIFO within the level: whoever rested first is filled first.
            while order.remaining > 1e-9 and level.orders:
                resting = level.orders[0]
                if resting.done:
                    level.orders.pop(0)
                    continue
                traded = min(order.remaining, resting.remaining)
                order.filled += traded
                resting.filled += traded
                fills.append(Fill(order.id, order.owner, order.side, price, traded,
                                  self.clock, "taker", mid_before))
                fills.append(Fill(resting.id, resting.owner, resting.side, price, traded,
                                  self.clock, "maker", mid_before))
                # Everyone still queued behind this order moves up.
                for behind in level.orders[1:]:
                    behind.queue_ahead = max(0.0, behind.queue_ahead - traded)
                if resting.done:
                    level.orders.pop(0)
        self.trades += [f for f in fills if f.role == "taker"]
        return fills

    def _rest(self, order: Order) -> None:
        book = self.bids if order.side == BUY else self.asks
        ticks = self.to_ticks(order.price)
        level = book.setdefault(ticks, Level(ticks))
        order.queue_ahead = level.size          # everyone already here is in front
        level.orders.append(order)

    # -- housekeeping -------------------------------------------------------

    def prune(self) -> None:
        for book in (self.bids, self.asks):
            for ticks in [t for t, lv in book.items() if lv.size <= 1e-9]:
                del book[ticks]

    def snapshot(self) -> dict:
        return {"bid": self.best_bid, "ask": self.best_ask, "mid": self.mid,
                "spread": self.spread,
                "bids": self.depth(BUY), "asks": self.depth(SELL)}
