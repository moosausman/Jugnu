"""
trading_tools.py -- the paper-trading simulator, as tools Jugnu can call.

    "backtest a 20/100 crossover on AAPL since 2015"
    "does that survive realistic costs?"
    "start paper trading it and tell me how it does"

Everything is simulation. There is no brokerage connection, no API key and
no order anywhere in this file, so every tool here is read-risk: it looks at
public prices and does arithmetic. The one thing they deliberately never do
is tell the user what to buy -- they report what a simulation did, warnings
and all, and the warnings are part of the return value so the model can't
quietly leave them out.
"""
from __future__ import annotations

from trading import analysis, metrics, paper, render, store, strategies
from trading.data import DataError, load


# The daily job that keeps paper accounts current. Written as an
# instruction rather than a hook, so it goes through the normal turn:
# Jugnu updates the accounts and then tells the user in its own words.
_DAILY_TITLE = "Paper trading check"
_DAILY_INSTRUCTION = ("Update my paper trading accounts (paper_trade with action=step) and tell me in "
                      "two lines how each one is doing against buy-and-hold, including any trade it "
                      "made since yesterday. Say nothing else.")


def _ensure_daily_check() -> str | None:
    """One daily check for all accounts, added the first time an account is
    started and never duplicated."""
    try:
        import scheduler
        for item in scheduler.list_schedules():
            if (item.get("title") or "") == _DAILY_TITLE:
                return None
        item = scheduler.add(_DAILY_INSTRUCTION, in_minutes=60 * 18, every_minutes=1440,
                             kind="task", title=_DAILY_TITLE)
        return item.get("next_run_text") if isinstance(item, dict) else None
    except Exception:
        return None       # a scheduler problem must never stop a paper account


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def _params(params) -> dict:
    if not params:
        return {}
    if isinstance(params, dict):
        return params
    out = {}
    for piece in str(params).replace(",", " ").split():
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        try:
            out[name] = int(value)
        except ValueError:
            try:
                out[name] = float(value)
            except ValueError:
                out[name] = value
    return out


def list_strategies() -> dict:
    """List the trading strategies available to backtest or paper trade.

    Use this whenever the user asks what strategies exist, what they have,
    or what they can test -- the answer is this list, not a guess, and it
    includes any strategy they wrote themselves.
    """
    return {"ok": True, "strategies": strategies.describe()}


def backtest_strategy(symbol: str, strategy: str = "sma", start: str = "", end: str = "",
                      params: str = "", cash: float = 10000.0, slippage_bps: float = 5.0,
                      commission: float = 1.0, interval: str = "1d") -> dict:
    """Backtest a trading strategy on historical prices. Simulation only -- no real money.

    The result ALWAYS includes buy-and-hold over the same period, with the
    same costs, as `benchmark_return`. Never run a second backtest with
    strategy="hold" to compare -- that answer is already in the first one.

    Args:
        symbol: Ticker, e.g. AAPL, RY.TO (Toronto), BTC-USD.
        strategy: Which strategy to run:
            "sma" -- moving-average crossover (a "20/100 crossover" is
                sma with params "fast=20 slow=100"),
            "rsi" -- buy oversold, sell recovered (params length, buy_below, sell_above),
            "breakout" -- buy new highs, exit new lows (params lookback, exit),
            "volatility" -- always invested, sized to a volatility target,
            "hold" -- buy and never trade; only for "what if I just held?",
            or the name of a strategy the user wrote with `build`.
        start: Optional first day, YYYY-MM-DD.
        end: Optional last day, YYYY-MM-DD.
        params: Strategy parameters as "name=value" pairs, e.g. "fast=20 slow=100"
            for a 20/100 crossover, or "length=14 buy_below=30" for RSI. Pass the
            numbers the user actually named; leave empty for the defaults.
        cash: Starting cash for the simulation.
        slippage_bps: Assumed slippage per trade in basis points (5 = 0.05%).
        commission: Assumed commission per trade, in dollars.
        interval: Bar size: 1d, 1h or 1wk.
    """
    try:
        bars = load(symbol, interval, start=start or None, end=end or None)
        result = analysis.backtest(bars, strategy, _params(params), symbol=symbol.upper(),
                                   interval=interval, cash=cash, commission=commission,
                                   slippage_bps=slippage_bps)
        costs = analysis.cost_sensitivity(bars, strategy, _params(params), symbol=symbol.upper(),
                                          interval=interval, cash=cash, commission=commission)
    except (DataError, KeyError, ValueError) as e:
        return _fail(str(e))
    notes = metrics.warnings(result.metrics, result.benchmark_metrics, cost_curve=costs,
                             bars=len(bars), trades=result.metrics["trades"])
    run_id = store.save_run(result, notes)
    mine = result.metrics
    return {
        "ok": True,
        "run_id": run_id,
        "symbol": result.symbol,
        "strategy": result.strategy,
        "params": result.params,
        "from": result.dates[0],
        "to": result.dates[-1],
        "return": round(mine["total_return"], 4),
        "benchmark_return": round(result.benchmark_metrics["total_return"], 4),
        "cagr": round(mine["cagr"], 4),
        "max_drawdown": round(mine["max_drawdown"], 4),
        "sharpe": round(mine["sharpe"], 2),
        "trades": mine["trades"],
        "win_rate": None if mine["win_rate"] is None else round(mine["win_rate"], 3),
        "final_value": round(mine["final"], 2),
        "return_at_slippage": {f"{k:g}bps": round(v, 4) for k, v in costs.items()},
        "warnings": notes,
        "summary": render.compact(result, notes),
    }


def sweep_strategy(symbol: str, strategy: str = "sma", start: str = "", end: str = "",
                   interval: str = "1d", slippage_bps: float = 5.0) -> dict:
    """Try every parameter combination for a strategy and report the best -- and whether it's real.

    `robustness` is the share of all combinations that also beat buy-and-hold.
    A high number means the effect is broad; a low one means the best result
    is probably curve-fitted noise.

    Args:
        symbol: Ticker, e.g. AAPL or BTC-USD.
        strategy: Strategy name with parameters to sweep (sma, rsi, breakout).
        start: Optional first day, YYYY-MM-DD.
        end: Optional last day, YYYY-MM-DD.
        interval: Bar size: 1d, 1h or 1wk.
        slippage_bps: Assumed slippage per trade in basis points.
    """
    try:
        bars = load(symbol, interval, start=start or None, end=end or None)
        result = analysis.sweep(bars, strategy, symbol=symbol.upper(), interval=interval,
                                slippage_bps=slippage_bps)
    except (DataError, KeyError, ValueError) as e:
        return _fail(str(e))
    best = result["best"]
    if result["robustness"] == 0:
        verdict = "No combination beat buy-and-hold. The best one here is still worse than doing nothing."
    elif result["robustness"] < 0.25:
        verdict = "The winning settings stand almost alone, which is what curve-fitting looks like."
    else:
        verdict = "Most settings beat buy-and-hold here, so the effect isn't one lucky combination."
    return {
        "ok": True, "symbol": symbol.upper(), "strategy": strategy, "runs": result["runs"],
        "best": {"params": best["params"], "return": round(best["return"], 4),
                 "sharpe": round(best["sharpe"], 2), "trades": best["trades"]},
        "median_return": round(result["median_return"], 4),
        "benchmark_return": round(result["benchmark_return"], 4),
        "robustness": round(result["robustness"], 3),
        "verdict": verdict,
    }


def walk_forward_test(symbol: str, strategy: str = "sma", start: str = "", end: str = "",
                      folds: int = 4, interval: str = "1d") -> dict:
    """Tune a strategy on one stretch of history and trade the next stretch blind, repeatedly.

    The out-of-sample return is the only figure here worth trusting: it's what
    the strategy did on data its parameters never saw.

    Args:
        symbol: Ticker, e.g. AAPL or BTC-USD.
        strategy: Strategy name (sma, rsi, breakout).
        start: Optional first day, YYYY-MM-DD.
        end: Optional last day, YYYY-MM-DD.
        folds: How many stretches to split the history into (2-8).
        interval: Bar size: 1d, 1h or 1wk.
    """
    try:
        bars = load(symbol, interval, start=start or None, end=end or None)
        result = analysis.walk_forward(bars, strategy, symbol=symbol.upper(), interval=interval,
                                       folds=max(2, min(8, int(folds))))
    except (DataError, KeyError, ValueError) as e:
        return _fail(str(e))
    return {
        "ok": True, "symbol": symbol.upper(), "strategy": strategy,
        "out_of_sample_return": round(result["out_of_sample_return"], 4),
        "benchmark_return": round(result["benchmark_return"], 4),
        "beat_benchmark": result["beat_benchmark"],
        "every_fold_profitable": result["consistent"],
        "folds": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in fold.items()}
                  for fold in result["folds"]],
    }


def paper_trade(action: str = "status", symbol: str = "", strategy: str = "sma",
                params: str = "", account_id: str = "", cash: float = 10000.0,
                interval: str = "1d") -> dict:
    """Run a strategy forward on live prices with imaginary money, or check how one is doing.

    No brokerage and no real orders: a paper account is a simulated balance
    updated from public prices.

    Args:
        action: start, status, step (bring up to date) or stop.
        symbol: Ticker, for action=start.
        strategy: Strategy name, for action=start.
        params: Optional parameters, e.g. "fast=20 slow=100".
        account_id: Which paper account, for status/step/stop. Omit for all.
        cash: Starting cash, for action=start.
        interval: Bar size: 1d, 1h or 1wk.
    """
    action = (action or "status").strip().lower()
    try:
        if action == "start":
            if not symbol:
                return _fail("Which symbol should the paper account trade?")
            account = paper.start(symbol, strategy, _params(params), cash=cash, interval=interval)
            scheduled = _ensure_daily_check()
            return {"ok": True, "account": account,
                    "daily_check": scheduled or "already scheduled",
                    "note": "Simulated money only. Jugnu will update this daily and tell you how it goes."}
        if action == "stop":
            if not account_id:
                return _fail("Which paper account should stop? Use action=status to list them.")
            return {"ok": paper.stop(account_id), "stopped": account_id}
        if action == "step":
            return {"ok": True, "accounts": [paper.step(account_id)] if account_id else paper.step_all()}
        accounts = [paper.status(account_id)] if account_id else paper.all_status()
        return {"ok": True, "accounts": accounts,
                "note": "Simulated accounts only -- no broker, no real money." if accounts else
                        "No paper accounts yet. Start one with action=start."}
    except (DataError, KeyError, ValueError) as e:
        return _fail(str(e))


def open_trading_terminal(count: int = 1, session: str = "") -> dict:
    """Open the Jugnu trading terminal in Terminal windows.

    The terminal is where backtests, portfolios, paper accounts and the
    market screen live. Opening several lets you run separate experiments
    side by side.

    Args:
        count: How many terminal windows to open (1-8).
        session: "jugnu" opens them on the Jugnu account's desktop (watch
            them with Jugnu's Desk), "me" opens them here. Left empty, they
            open wherever this task is running -- which is what the account
            picker in Jugnu's window selects.
    """
    import subprocess
    from pathlib import Path

    from trading import remote

    from . import macbridge

    count = max(1, min(8, int(count or 1)))
    # Follow the task's account unless told otherwise. Everything else Jugnu
    # does goes through MacBridge, which routes by session automatically;
    # this tool opens a window itself, so it has to ask.
    where = (session or "").strip().lower() or macbridge.active_session()
    if where in ("jugnu", "agent", "other", "second"):
        try:
            opened = remote.open_terminals(count)
        except remote.RemoteError as e:
            return _fail(str(e))
        return {"ok": True, "opened": opened, "where": "the Jugnu account",
                "note": "Watch them on Jugnu's Desk (⌘⇧D)."}

    script = Path(__file__).resolve().parent.parent / "jugnu"
    if not script.exists():
        return _fail(f"The terminal launcher isn't where I expected ({script}).")
    for _ in range(count):
        source = ('tell application "Terminal"\n  activate\n'
                  f'  do script "clear && \'{script}\'"\n'
                  '  delay 0.3\n  try\n'
                  '    set number of columns of front window to 108\n'
                  '    set number of rows of front window to 34\n'
                  '  end try\nend tell')
        result = subprocess.run(["osascript", "-e", source], capture_output=True, text=True,
                                timeout=45)
        if result.returncode != 0:
            return _fail((result.stderr or "couldn't open Terminal").strip()[:200])
    return {"ok": True, "opened": count, "where": "your account"}


def past_backtests(limit: int = 10) -> dict:
    """List backtests run earlier, newest first, with what each one found.

    Args:
        limit: How many to return.
    """
    runs = store.list_runs(max(1, min(50, int(limit))))
    return {"ok": True, "runs": [{**r, "return": round(r["return"] or 0, 4),
                                  "benchmark_return": round(r["benchmark_return"] or 0, 4)}
                                 for r in runs]}
