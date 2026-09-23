"""
paper.py -- running a strategy forward on live prices, with imaginary money.

A paper account replays the same engine rules against bars as they arrive:
the decision is made on a closed bar, and the fill happens at the next bar's
open, with the same costs. So a paper account and a backtest of the same
period agree -- which is the whole point of having one engine.

`step()` is idempotent per bar: calling it twice in a day does nothing the
second time. That matters because the scheduler may run it more than once,
and a doubled fill would quietly corrupt the account.

Nothing here connects to a broker. There are no keys, no orders, no money.
"""
from __future__ import annotations

import time

from . import store
from .data import load
from .engine import Context
from .strategies import get

START_CASH = 10_000.0
SLIPPAGE_BPS = 5.0
COMMISSION = 1.0
REBALANCE_BAND = 0.05


def start(symbol: str, strategy: str, params: dict | None = None, cash: float = START_CASH,
          interval: str = "1d") -> dict:
    spec = get(strategy)
    load(symbol, interval)                      # fail here if the symbol is wrong, not tomorrow
    merged = {**spec.defaults, **(params or {})}
    account = store.open_paper(symbol, spec.name, merged, cash, interval)
    # A new account starts NOW. Without this it would replay the whole
    # history on its first step and report years of imaginary gains as if
    # they had just happened.
    bars = load(symbol, interval)
    store.update_paper(account["id"], last_ts=bars[-2].ts if len(bars) > 1 else 0)
    return step(account["id"])                  # then take today's position


def stop(account_id: str) -> bool:
    return store.close_paper(account_id)


def step(account_id: str) -> dict:
    """Brings one account up to date with the latest bars."""
    account = store.paper_account(account_id)
    if account is None:
        raise KeyError(f"No paper account {account_id}")
    spec = get(account["strategy"])
    bars = load(account["symbol"], account["interval"], refresh=True)
    if len(bars) < 3:
        return status(account_id)

    cash = account["cash"]
    position = account["position"]
    avg_cost = account["avg_cost"]
    trades = list(account["trades"])
    last_ts = account["last_ts"] or 0
    slip = SLIPPAGE_BPS / 10_000.0
    closes = [b.close for b in bars]

    # Decide on every closed bar we haven't acted on yet, filling at the
    # next bar's open -- exactly as the backtester does.
    for i in range(len(bars) - 1):
        decision_bar, fill_bar = bars[i], bars[i + 1]
        if fill_bar.ts <= last_ts:
            continue
        equity = cash + position * decision_bar.close
        target = spec.run(Context(bars, closes, i, position, cash, equity, account["params"], avg_cost))
        target = max(0.0, min(1.0, float(target or 0.0)))
        last_ts = fill_bar.ts
        held_weight = (position * decision_bar.close / equity) if equity > 0 else 0.0
        if abs(target - held_weight) < REBALANCE_BAND:
            continue                       # same rule as the backtester
        # Size against what's actually spendable: the commission and the
        # slippage have to come out of the same pot, or a "fully invested"
        # account ends up with slightly negative cash.
        spendable = max(0.0, equity - COMMISSION)
        wanted = (target * spendable) / (fill_bar.open * (1 + slip)) if fill_bar.open > 0 else 0.0
        delta = wanted - position
        if abs(delta * fill_bar.open) < max(1.0, 0.001 * equity):
            continue
        price = fill_bar.open * (1 + slip) if delta > 0 else fill_bar.open * (1 - slip)
        cash -= delta * price + COMMISSION
        pnl = None
        if delta > 0:
            avg_cost = ((avg_cost * position) + price * delta) / (position + delta) if position + delta else price
        else:
            closing = min(abs(delta), max(position, 0.0))
            if closing:
                pnl = (price - avg_cost) * closing - COMMISSION
        position += delta
        if position <= 1e-9:
            position, avg_cost = 0.0, 0.0
        trades.append({"ts": fill_bar.ts, "date": fill_bar.date, "side": "buy" if delta > 0 else "sell",
                       "shares": round(abs(delta), 6), "price": round(price, 4),
                       "pnl": None if pnl is None else round(pnl, 2)})

    store.update_paper(account_id, cash=cash, position=position, avg_cost=avg_cost,
                       last_ts=last_ts, trades=trades)
    return status(account_id)


def step_all() -> list[dict]:
    """What the scheduled daily check calls."""
    out = []
    for account in store.paper_accounts():
        try:
            out.append(step(account["id"]))
        except Exception as e:                  # one bad symbol mustn't stop the rest
            out.append({"id": account["id"], "symbol": account["symbol"], "error": str(e)})
    return out


def status(account_id: str) -> dict:
    account = store.paper_account(account_id)
    if account is None:
        raise KeyError(f"No paper account {account_id}")
    bars = load(account["symbol"], account["interval"])
    price = bars[-1].close
    equity = account["cash"] + account["position"] * price
    start_cash = account["start_cash"] or START_CASH
    # Buy-and-hold means "if you'd bought when this account opened", not
    # since the dawn of the price history.
    opened = account["created"] or 0
    first = next((b for b in bars if b.ts >= opened), bars[-1])
    held_since = next((t for t in reversed(account["trades"]) if t["side"] == "buy"), None)
    return {
        "id": account["id"], "symbol": account["symbol"], "strategy": account["strategy"],
        "params": account["params"], "active": bool(account["active"]),
        "cash": round(account["cash"], 2), "position": round(account["position"], 6),
        "price": price, "equity": round(equity, 2),
        "return": equity / start_cash - 1 if start_cash else 0.0,
        "buy_and_hold_return": price / first.open - 1 if first.open else 0.0,
        "in_market": account["position"] > 0,
        "since": account["trades"][0]["date"] if account["trades"] else None,
        "last_trade": account["trades"][-1] if account["trades"] else None,
        "held_since": held_since["date"] if held_since and account["position"] > 0 else None,
        "trades": len(account["trades"]),
        "updated": time.time(),
    }


def all_status() -> list[dict]:
    out = []
    for account in store.paper_accounts():
        try:
            out.append(status(account["id"]))
        except Exception as e:
            out.append({"id": account["id"], "symbol": account["symbol"], "error": str(e)})
    return out
