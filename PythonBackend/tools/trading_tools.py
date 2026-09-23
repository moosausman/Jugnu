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


def open_trading_terminal(count: int = 1, session: str = "", commands: list = None) -> dict:
    """Open the Jugnu trading terminal in Terminal windows -- several at
    once, tiled, each already running whatever you tell it to.

    This is the main way to put work on screen. "Open 4 terminals and
    backtest a different stock in each" is one call: count=4 with four
    commands. Windows tile across the display automatically when count > 1.

    Terminal commands you can pass (same language the terminal itself
    speaks, `;` chains several in one window):
        q NVDA                    quote and sparkline
        ch AAPL 5m 120            price chart, 5-minute bars
        watch BTC-USD 1m          a live chart that refreshes itself
        bt sma AAPL 2015          backtest a strategy vs buy & hold
        vs sma rsi MSFT           strategies side by side
        risk sma TSLA             drawdowns, VaR, beta, trade stats
        sweep rsi SPY             every parameter combination, as a heatmap
        wf sma QQQ 5              walk forward: tune, then trade blind
        mc sma AAPL               Monte Carlo: the same trades, reshuffled
        costs sma AAPL            what slippage does to it
        pf sma:AAPL, rsi:SPY      a portfolio of sleeves, rebalanced
        screen rsi<35 vol>2       scan the watchlist
        corr AAPL MSFT NVDA       how they move together
        signals                   what every strategy says right now
        fa AAPL                   fundamentals (OpenBB) · `opt AAPL` options
        why TSLA                  what moved it today
        build buy BTC when RSI<30 writes a real strategy and runs it
        paper start sma AAPL      a simulated account on live prices
        alert NVDA < 200          tell me when

    Args:
        count: How many terminal windows to open (1-8).
        session: "jugnu" opens them on the Jugnu account's desktop (watch
            them with Jugnu's Desk), "me" opens them here. Left empty, they
            open wherever this task is running -- which is what the account
            picker in Jugnu's window selects.
        commands: One command line per window, in order, e.g.
            ["bt sma AAPL 2015", "bt sma MSFT 2015", "bt rsi NVDA 2015",
             "bt sma TSLA 2015"]. Fewer than `count` is fine; the rest open
            empty. Omit it entirely to just open terminals.
    """
    import subprocess
    from pathlib import Path

    from trading import remote

    from . import macbridge

    count = max(1, min(8, int(count or 1)))
    if isinstance(commands, str):
        commands = [commands]
    commands = [str(c) for c in (commands or []) if str(c).strip()][:count]
    # Follow the task's account unless told otherwise. Everything else Jugnu
    # does goes through MacBridge, which routes by session automatically;
    # this tool opens a window itself, so it has to ask.
    where = (session or "").strip().lower() or macbridge.active_session()
    if where in ("jugnu", "agent", "other", "second"):
        try:
            opened = remote.open_terminals(count, commands)
        except remote.RemoteError as e:
            return _fail(str(e))
        return {"ok": True, "opened": opened, "running": commands,
                "where": "the Jugnu account",
                "note": "Watch them on Jugnu's Desk (⌘⇧D)."}

    script = Path(__file__).resolve().parent.parent / "jugnu"
    if not script.exists():
        return _fail(f"The terminal launcher isn't where I expected ({script}).")
    places = remote.grid(count, _screen()) if count > 1 else []
    for index in range(count):
        command = commands[index] if index < len(commands) else ""
        arguments = " " + remote._shell_quote(command) if command else ""
        lines = ['tell application "Terminal"', "  activate",
                 f'  do script "clear && \'{script}\' term{arguments}"', "  delay 0.35"]
        if places:
            left, top, right, bottom = places[index]
            lines.append(f"  try\n    set bounds of front window to "
                         f"{{{left}, {top}, {right}, {bottom}}}\n  end try")
        else:
            lines.append("  try\n    set number of columns of front window to 108\n"
                         "    set number of rows of front window to 34\n  end try")
        lines.append("end tell")
        result = subprocess.run(["osascript", "-e", "\n".join(lines)],
                                capture_output=True, text=True, timeout=45)
        if result.returncode != 0:
            return _fail((result.stderr or "couldn't open Terminal").strip()[:200])
    return {"ok": True, "opened": count, "running": commands, "where": "your account"}


def run_in_trading_terminal(command: str, session: str = "", window: int = 1) -> dict:
    """Type a command into a trading terminal that is ALREADY OPEN, as if the
    person sitting there had typed it. Use this whenever they refer to a
    terminal that's open -- "run signals on the terminal open in jugnu",
    "backtest TSLA in the second window" -- instead of answering from your
    own tools, which puts the answer somewhere they aren't looking.

    Takes the same commands as open_trading_terminal (see its list); `;`
    chains several. If no terminal is open it opens one running the command,
    so the request still lands.

    Args:
        command: The terminal command, e.g. "signals" or "bt sma AAPL 2015".
        session: "jugnu" for the Jugnu account's desktop, "me" for here.
            Empty follows the account this task is running in.
        window: Which open terminal, 1 being the first. 0 sends it to every
            open trading terminal at once.
    """
    import subprocess

    from trading import remote

    from . import macbridge

    command = " ".join(str(command or "").split())
    if not command:
        return _fail("Tell me which command to run.")
    where = (session or "").strip().lower() or macbridge.active_session()
    remote_side = where in ("jugnu", "agent", "other", "second")
    place = "the Jugnu account" if remote_side else "your account"
    try:
        if remote_side:
            sent = remote.type_into_terminals(command, window)
        else:
            script = remote.type_script(command, window)
            done = subprocess.run(["osascript", "-e", script], capture_output=True,
                                  text=True, timeout=45)
            if done.returncode != 0:
                return _fail((done.stderr or "couldn't reach Terminal").strip()[:200])
            out = done.stdout.strip()
            sent = int(out.split()[-1]) if out.startswith("sent") else 0
    except remote.RemoteError as e:
        return _fail(str(e))
    if sent:
        return {"ok": True, "ran": command, "terminals": sent, "where": place}
    # Nothing open: opening one running it beats reporting failure.
    opened = open_trading_terminal(1, session, [command])
    if not opened.get("ok"):
        return opened
    return {"ok": True, "ran": command, "terminals": 1, "where": place,
            "note": "No terminal was open there, so I opened one running it."}


def _screen() -> tuple:
    """This display, in the coordinates AppleScript uses for window bounds."""
    import subprocess
    try:
        out = subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=10).stdout
        x0, y0, x1, y1 = [int(float(n)) for n in out.split(",")]
        return (x0, y0 + 25, x1, y1 - 60)
    except Exception:
        return (0, 25, 1512, 900)


def past_backtests(limit: int = 10) -> dict:
    """List backtests run earlier, newest first, with what each one found.

    Args:
        limit: How many to return.
    """
    runs = store.list_runs(max(1, min(50, int(limit))))
    return {"ok": True, "runs": [{**r, "return": round(r["return"] or 0, 4),
                                  "benchmark_return": round(r["benchmark_return"] or 0, 4)}
                                 for r in runs]}
