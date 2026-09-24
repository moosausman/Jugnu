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
        symbol: Ticker. Anything Yahoo lists: US ("AAPL"), an exchange
            suffix for elsewhere ("SHOP.TO" Toronto, "BP.L" London,
            "SAP.DE" Frankfurt, "7203.T" Tokyo, "RELIANCE.NS" India), an
            index ("^GSPC"), a future ("GC=F" gold, "CL=F" crude), FX
            ("EURUSD=X"), or crypto ("BTC-USD"). There is no fixed list.
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
        symbol: Ticker. Anything Yahoo lists: US ("AAPL"), an exchange
            suffix for elsewhere ("SHOP.TO" Toronto, "BP.L" London,
            "SAP.DE" Frankfurt, "7203.T" Tokyo, "RELIANCE.NS" India), an
            index ("^GSPC"), a future ("GC=F" gold, "CL=F" crude), FX
            ("EURUSD=X"), or crypto ("BTC-USD"). There is no fixed list.
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
        symbol: Ticker. Anything Yahoo lists: US ("AAPL"), an exchange
            suffix for elsewhere ("SHOP.TO" Toronto, "BP.L" London,
            "SAP.DE" Frankfurt, "7203.T" Tokyo, "RELIANCE.NS" India), an
            index ("^GSPC"), a future ("GC=F" gold, "CL=F" crude), FX
            ("EURUSD=X"), or crypto ("BTC-USD"). There is no fixed list.
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
        symbol: Ticker, for action=start. Any market Yahoo lists.
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


def stress_test_strategy(symbol: str, strategy: str = "sma", start: str = "",
                         checks: str = "all") -> dict:
    """Find out whether a backtest is evidence or a coincidence.

    A good backtest means almost nothing on its own. This runs the checks a
    quant desk would run before believing one, and each can fail it:

        real      probabilistic Sharpe, a block-bootstrap confidence
                  interval, and the track record length the Sharpe would
                  need to clear zero
        overfit   probability of backtest overfitting -- how often picking
                  the best parameters in-sample picks a below-median
                  performer out-of-sample. Above 0.5 is worse than a coin
        deflated  the Sharpe after charging for every parameter set tried
                  to find it, against what pure luck would have produced
        factor    alpha against market, size, value and momentum exposures,
                  with Newey-West t-statistics. Most "edges" are beta
        capacity  the fund size at which square-root market impact eats the
                  whole return

    Use it whenever the user asks whether a strategy is any good, is real,
    would work with real money, or would survive scrutiny -- and before
    ever describing a backtest as promising.

    Args:
        symbol: Ticker. Anything Yahoo lists: US ("AAPL"), an exchange
            suffix for elsewhere ("SHOP.TO" Toronto, "BP.L" London,
            "SAP.DE" Frankfurt, "7203.T" Tokyo, "RELIANCE.NS" India), an
            index ("^GSPC"), a future ("GC=F" gold, "CL=F" crude), FX
            ("EURUSD=X"), or crypto ("BTC-USD"). There is no fixed list.
        strategy: Strategy name; `list_strategies` has them.
        start: Optional start date, e.g. "2015-01-01".
        checks: "all", or a comma-separated subset of
            real, overfit, deflated, factor, capacity.
    """
    from trading import allocate, capacity as cap, factors, quant  # noqa: F401
    from trading.data import periods_per_year

    symbol = (symbol or "").upper().strip()
    wanted = ({"real", "overfit", "deflated", "factor", "capacity"}
              if checks.strip().lower() in ("", "all")
              else {c.strip().lower() for c in checks.split(",")})
    try:
        bars = load(symbol, "1d", start=start or None)
        result = analysis.backtest(bars, strategy, symbol=symbol)
    except (DataError, ValueError, KeyError) as e:
        return _fail(str(e))

    equity = result.equity
    rets = [equity[i] / equity[i - 1] - 1.0 if equity[i - 1] > 0 else 0.0
            for i in range(1, len(equity))]
    per_year = periods_per_year(symbol, "1d")
    moments = quant.moments(rets)
    sr = moments["mean"] / moments["sd"] if moments["sd"] > 0 else 0.0
    out = {"ok": True, "symbol": symbol, "strategy": strategy,
           "sharpe_annual": round(quant.annualise(sr, per_year), 3),
           "return": round(result.metrics["total_return"], 4),
           "benchmark_return": round(result.benchmark_metrics["total_return"], 4),
           "bars": moments["n"], "verdict": []}

    if "real" in wanted:
        boot = quant.bootstrap_sharpe(rets, runs=400)
        out["real"] = {
            "p_sharpe_above_zero": round(quant.probabilistic_sharpe(
                sr, moments["n"], moments["skew"], moments["kurtosis"]), 3),
            "skew": round(moments["skew"], 2), "kurtosis": round(moments["kurtosis"], 1),
            "sharpe_ci": [round(quant.annualise(boot["low"], per_year), 2),
                          round(quant.annualise(boot["high"], per_year), 2)] if boot["enough"] else None,
        }
        if boot.get("enough") and boot["low"] <= 0:
            out["verdict"].append("The bootstrap interval for the Sharpe includes zero.")

    if wanted & {"overfit", "deflated"}:
        try:
            grid = analysis.sweep_returns(bars, strategy, symbol=symbol)
        except ValueError as e:
            grid = None
            out["sweep_error"] = str(e)
        if grid:
            if "overfit" in wanted:
                pbo = quant.cscv_pbo(grid["matrix"], splits=10)
                if pbo.get("enough"):
                    out["overfit"] = {"pbo": round(pbo["pbo"], 3),
                                      "configurations": pbo["configurations"]}
                    if pbo["pbo"] > 0.5:
                        out["verdict"].append(
                            f"PBO {pbo['pbo']:.0%}: tuning this on past data selects parameters "
                            "that then underperform.")
            if "deflated" in wanted:
                sharpes = [quant.sharpe([row[c] for row in grid["matrix"]])
                           for c in range(grid["configurations"])]
                # Charge for every configuration ever tested on this symbol,
                # not just this sweep's. The trial count that matters is the
                # whole search, and the ledger has been keeping it.
                history = store.search_history(symbol)
                pooled = history["sharpes"] or sharpes
                trials = max(history["trials"], grid["configurations"])
                spread = quant.moments(pooled)
                report = quant.deflated_sharpe(max(sharpes), moments["n"], moments["skew"],
                                               moments["kurtosis"], trials, spread["sd"] ** 2)
                out["deflated"] = {"p_real": round(report["deflated"], 3),
                                   "trials": report["trials"],
                                   "trials_this_sweep": grid["configurations"],
                                   "strategies_tried": history["strategies"],
                                   "sharpe_needed_annual": round(quant.annualise(
                                       quant.required_sharpe(trials, spread["sd"] ** 2, moments["n"],
                                                             moments["skew"], moments["kurtosis"]),
                                       per_year), 2),
                                   "luck_hurdle_annual": round(
                                       quant.annualise(report["hurdle"], per_year), 2)}
                if report["deflated"] < 0.5:
                    out["verdict"].append(
                        "The best parameter set does not beat what luck would produce from "
                        f"{report['trials']} tries across this instrument's whole search history.")

    if "factor" in wanted:
        attribution = factors.attribute(result.dates[1:], rets, start=start or None)
        if attribution.get("ok"):
            out["factor"] = {
                "alpha_annual": round(attribution["alpha_annual"], 4),
                "alpha_t": round(attribution["alpha_t"], 2),
                "alpha_p": round(attribution["alpha_p"], 3),
                "r2": round(attribution["r2"], 3),
                "loadings": {k: round(v["coefficient"], 3)
                             for k, v in attribution["loadings"].items()},
            }
            if attribution["alpha_p"] >= 0.05:
                out["verdict"].append(
                    f"Alpha is not distinguishable from zero (t={attribution['alpha_t']:.2f}); "
                    "the returns are the factor exposures.")
        else:
            out["factor"] = {"error": attribution.get("error")}

    if "capacity" in wanted:
        curve = cap.curve(result, bars, result.metrics["cagr"])
        if curve.get("ok"):
            solved = curve["solved_capacity"]
            out["capacity"] = {
                "usd": None if solved == float("inf") else round(solved),
                "dollar_adv": round(curve["dollar_adv"]),
            }
            if solved != float("inf") and solved < 1e6:
                out["verdict"].append(
                    f"Market impact eats the edge above about ${solved:,.0f}.")

    if not out["verdict"]:
        out["verdict"].append("Nothing here disqualifies it, which is rarer than it sounds.")
    return out


def execution_cost(symbol: str, quantity: float, side: str = "buy",
                   interval: str = "5m", bars: int = 40) -> dict:
    """What it would actually cost to get an order done, by trading it
    through a simulated limit order book.

    Six ways to work the same order -- market (cross now), twap, vwap, pov
    (take a share of volume), passive (rest at the touch), limit (rest at a
    price) -- each reported with how much it filled and what it cost.

    The cost is implementation shortfall against the arrival price, and it
    charges the shares that did NOT trade at where the price finished.
    Without that, an order that never fills looks free and wins every
    comparison. `spread_paid_bps` is what crossing cost: negative means the
    order was paid for providing liquidity.

    The book and the matching are genuine -- price-time priority, queue
    position, partial fills. The order flow is reconstructed from OHLCV
    bars rather than tick data, so the ranking and the rough magnitude are
    meaningful and the third decimal is not.

    Use this when asked what a trade would cost, whether an order is too
    big, or how to work one -- not for whether a strategy is any good,
    which is stress_test_strategy.

    Args:
        symbol: Ticker. Anything Yahoo lists: US ("AAPL"), an exchange
            suffix for elsewhere ("SHOP.TO" Toronto, "BP.L" London,
            "SAP.DE" Frankfurt, "7203.T" Tokyo, "RELIANCE.NS" India), an
            index ("^GSPC"), a future ("GC=F" gold, "CL=F" crude), FX
            ("EURUSD=X"), or crypto ("BTC-USD"). There is no fixed list.
        quantity: Shares to trade.
        side: "buy" or "sell".
        interval: Bar size for the window -- "1m", "5m", "1h", "1d".
        bars: How many bars to work the order across.
    """
    from trading import execution

    symbol = (symbol or "").upper().strip()
    side = (side or "buy").lower().strip()
    if side not in ("buy", "sell"):
        return _fail('side must be "buy" or "sell".')
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        return _fail("quantity must be a number of shares.")
    if quantity <= 0:
        return _fail("quantity must be positive.")
    try:
        window = load(symbol, interval)[-max(2, min(int(bars), 200)):]
    except (DataError, ValueError) as e:
        return _fail(str(e))
    if len(window) < 2:
        return _fail(f"Not enough {interval} history for {symbol}.")

    report = execution.compare(window, side, quantity)
    traded = sum(b.volume for b in window)
    algos = {}
    for name, info in report["algos"].items():
        algos[name] = {"fill_rate": info["fill_rate"],
                       "average_price": info["average_price"],
                       "cost_bps": info["total_shortfall_bps"],
                       "spread_paid_bps": info["spread_paid_bps"],
                       "maker_share": info["maker_share"],
                       "fills": info["fills"]}
    cheapest = min(algos, key=lambda k: algos[k]["cost_bps"])
    return {"ok": True, "symbol": symbol, "side": side, "quantity": quantity,
            "window": f"{len(window)}x{interval}", "window_volume": round(traded),
            "participation": round(quantity / traded, 6) if traded else None,
            "algos": algos, "cheapest": cheapest,
            "notes": execution.verdict(report)}


def measure_microstructure(symbol: str = "BTCUSDT", date: str = "") -> dict:
    """Measure a REAL order book and a REAL trade tape, and check the
    simulator's assumptions against them.

    Free and public for crypto only (Binance), so this takes BTCUSDT,
    ETHUSDT, SOLUSDT and the like -- not AAPL. Equity depth and quote data
    is licensed and this does not have it.

    What comes back:
        depth_exponent   how cumulative resting size grows with distance
                         from the mid. 1.0 means constant density, 2.0
                         means density growing with distance. Measured, it
                         is about 0.7, so the book THINS and sweeping it is
                         convex.
        response         the average signed mid move L trades after a
                         trade, with the slope across the measured lag
                         range. The slope is about 0.5 across four decades,
                         which is the square-root law in trade time -- but
                         the curve is concave, so the slope depends on the
                         range and is reported with it.
        size_exponent    how a single trade's impact depends on its size.
                         Measured: it does not, at all.

    Use it when asked whether the cost model is realistic, what real market
    impact looks like, or to justify a number the execution simulator gave.

    Args:
        symbol: A Binance pair -- "BTCUSDT", "ETHUSDT", "SOLUSDT".
        date: Optional "YYYY-MM-DD" to pull a whole day from the bulk
            archive, which is hundreds of thousands of trades. Without it
            only the last thousand trades are used, which is a few minutes
            and too short to measure the long lags.
    """
    from trading import execution, microdata

    try:
        book = microdata.depth(symbol, 5000)
    except microdata.MicroError as e:
        return _fail(str(e))
    mid = book["mid"]
    points, cumulative = [], 0.0
    for price, qty in book["asks"]:
        away = (price - mid) / mid * 10_000
        cumulative += qty
        if away > 0.5:
            points.append((away, cumulative))
    exponent = None
    if len(points) > 20:
        import math
        xs = [math.log(d) for d, _ in points]
        ys = [math.log(q) for _, q in points]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        bottom = sum((x - mx) ** 2 for x in xs)
        exponent = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / bottom if bottom else None

    try:
        tape = (microdata.day_trades(symbol, date, limit=300_000) if date
                else microdata.trades(symbol, 1000))
    except microdata.MicroError as e:
        return {"ok": True, "symbol": symbol, "spread_bps": round(book["spread_bps"], 4),
                "levels": len(book["asks"]), "depth_exponent": exponent,
                "tape_error": str(e)}

    curve = microdata.response(tape)
    sized = microdata.impact_by_size(tape)
    out = {"ok": True, "symbol": symbol, "trades": len(tape),
           "spread_bps": round(book["spread_bps"], 4),
           "levels": len(book["asks"]),
           "book_reach_bps": round(points[-1][0], 1) if points else None,
           "depth_exponent": round(exponent, 3) if exponent is not None else None,
           "model_shape_exponent": execution.MEASURED_SHAPE}
    if curve.get("ok"):
        out["response"] = {
            "slope": round(curve["gamma"], 3) if curve["gamma"] is not None else None,
            "lag_range": curve["lag_range"],
            "short_lag_slope": round(curve["near_slope"], 3) if curve["near_slope"] is not None else None,
            "long_lag_slope": round(curve["far_slope"], 3) if curve["far_slope"] is not None else None,
            "concave": curve["concave"],
            "note": ("The curve is concave, so there is no single exponent: the slope falls as the "
                     "lag grows. The 0.5 quoted as the square-root law is the chord across roughly "
                     "four decades, not a power law at every scale."),
        }
        if len(tape) < 50_000:
            out["response"]["warning"] = (
                f"{len(tape):,} trades is a few minutes and cannot reach the long lags. "
                "Pass a date to pull a full day.")
    if sized.get("ok") and sized.get("exponent") is not None:
        out["size_exponent"] = round(sized["exponent"], 3)
    return out


def rotman_competition(action: str = "status", symbols: list = None,
                       positions: dict = None, cash: float = 0.0,
                       last_trade: str = "", objective: str = "minvar") -> dict:
    """The Rotman Portfolio Management Competition: its rules, its
    benchmark, and weights that satisfy both.

    The competition scores the SHARPE RATIO over five months, with cash
    capped under 10%, no position over 20%, and a penalty for going more
    than fourteen days without trading. Under those constraints the Sharpe
    is decided far more by controlling volatility than by picking well,
    which is why the sizing defaults to minimum variance.

    This never places an order. It produces a target book and a list of
    what is out of line; the person enters the trades on the competition's
    own platform, and their rules on automated assistance are theirs to
    read.

    Args:
        action: One of
            "status"    dates, rules, benchmark composition
            "check"     test a book against the three rules; needs
                        `positions` and `cash`, and `last_trade` for the
                        fortnightly rule
            "optimise"  weights for `symbols` under the constraints
            "benchmark" the five-ETF benchmark's performance
            "eligible"  are `symbols` allowed? US and Canadian listings only
        symbols: Tickers, for "optimise" and "eligible".
        positions: {ticker: market value}, signed, for "check". A short is
            negative.
        cash: Cash in dollars, for "check".
        last_trade: "YYYY-MM-DD" of the last trade, for "check".
        objective: "minvar" (default), "parity", or "sharpe".
    """
    import datetime as _dt

    from trading import rpmc

    action = (action or "status").strip().lower()
    if isinstance(symbols, str):
        symbols = [s for s in symbols.replace(",", " ").split() if s]
    symbols = [str(s).strip().upper() for s in (symbols or [])]

    if action in ("status", "rules"):
        today = _dt.date.today()
        opens = _dt.date.fromisoformat(rpmc.START)
        closes = _dt.date.fromisoformat(rpmc.END)
        phase = ("not started" if today < opens
                 else "running" if today <= closes else "finished")
        return {"ok": True, "phase": phase, "opens": rpmc.START, "closes": rpmc.END,
                "days_until_open": max(0, (opens - today).days),
                "days_left": max(0, (closes - today).days) if phase == "running" else None,
                "rules": {"max_position": rpmc.MAX_POSITION, "max_cash": rpmc.MAX_CASH,
                          "trade_every_days": rpmc.TRADE_EVERY_DAYS},
                "benchmark": {p: {"weight": w, "name": n, "loads_as": f}
                              for p, f, w, n in rpmc.BENCHMARK},
                "scoring": "50% Sharpe ratio ranking, 50% presentation",
                "note": "Nothing here places orders; the trades are entered by hand."}

    if action in ("check", "book"):
        if not positions:
            return _fail("Give me the book as {ticker: market value}, negative for a short.")
        clean = {str(k).upper(): float(v) for k, v in positions.items()}
        report = rpmc.compliance(clean, float(cash or 0.0), last_trade)
        if not report.get("ok"):
            return _fail(report["error"])
        return {"ok": True, "compliant": report["compliant"],
                "equity": round(report["equity"], 2),
                "cash_share": round(report["cash_share"], 4),
                "gross_leverage": round(report["leverage"], 3),
                "days_since_trade": report["days_since_trade"],
                "positions": [{"ticker": p["ticker"], "share": round(p["share"], 4),
                               "side": p["side"]} for p in report["positions"]],
                "breaches": report["breaches"], "warnings": report["warnings"],
                "advice": rpmc.advice(report)}

    if action in ("optimise", "optimize", "weights", "size"):
        if len(symbols) < 2:
            return _fail("Give me at least two tickers to weight.")
        found = rpmc.candidates(symbols)
        if len(found["series"]) < 2:
            return _fail("Not enough eligible securities loaded. Rejected: "
                         + "; ".join(f"{k} ({v})" for k, v in found["rejected"].items()))
        try:
            result = rpmc.optimise(found["series"], objective=objective)
        except rpmc.RpmcError as e:
            return _fail(str(e))
        return {"ok": True, "objective": result["objective"],
                "weights": {k: round(v, 4) for k, v in result["weights"].items()},
                "cash": round(result["cash"], 4),
                "largest_position": round(result["largest"], 4),
                "volatility_annual": round(result["volatility_annual"], 4),
                "sharpe_annual_in_sample": round(result["sharpe_annual"], 3),
                "rejected": found["rejected"] or None,
                "caveat": ("These are in-sample figures from past returns; the Sharpe is what the "
                           "book WOULD have scored, not a forecast. Minimum variance is the default "
                           "because covariance is estimable over a few months and expected returns "
                           "are not.")}

    if action in ("benchmark", "bench"):
        report = rpmc.benchmark(symbols[0] if symbols else rpmc.START)
        if not report.get("ok"):
            return {"ok": True, "not_started": report.get("not_started", False),
                    "message": report.get("error"), "legs": report.get("legs")}
        from trading import quant
        return {"ok": True, "start": report["start"], "end": report["end"],
                "days": report["days"],
                "total_return": round(report["total_return"], 4),
                "sharpe_annual": round(quant.annualise(quant.sharpe(report["returns"]), 252), 3),
                "legs": report["legs"], "missing": report["missing"] or None}

    if action in ("eligible", "allowed"):
        if not symbols:
            return _fail("Give me tickers to check.")
        return {"ok": True, "checked": [rpmc.listing(t) for t in symbols],
                "rule": "US and Canadian exchanges only"}

    return _fail(f'No RPMC action "{action}". Use status, check, optimise, benchmark or eligible.')


def compare_allocations(symbols: list, start: str = "") -> dict:
    """Weight a basket three ways and report what each does to portfolio risk.

    Equal weight, inverse variance (risk parity's simple form), and
    hierarchical risk parity, which clusters correlated names so three
    versions of the same trade share one slot instead of taking three.
    None of them needs expected returns or a matrix inverse, which is why
    they survive out of sample where mean-variance optimisation does not.

    Args:
        symbols: Tickers, two to twelve, e.g. ["SPY", "QQQ", "TLT", "GLD"].
            Any market Yahoo lists, mixed freely.
        start: Optional start date.
    """
    from trading import allocate

    if isinstance(symbols, str):
        symbols = [s for s in symbols.replace(",", " ").split() if s]
    tickers = [str(s).upper().strip() for s in (symbols or [])][:12]
    if len(tickers) < 2:
        return _fail("Give me at least two symbols.")
    series, failed = {}, {}
    for ticker in tickers:
        try:
            closes = [b.close for b in load(ticker, "1d", start=start or None)]
            series[ticker] = [closes[i] / closes[i - 1] - 1.0
                              for i in range(1, len(closes)) if closes[i - 1] > 0]
        except (DataError, ValueError) as e:
            failed[ticker] = str(e)[:80]
    if len(series) < 2:
        return _fail("Couldn't load enough of those: " + "; ".join(f"{k} ({v})" for k, v in failed.items()))
    report = allocate.summarise(list(series), series)
    if not report.get("ok"):
        return _fail(report.get("error", "couldn't build the covariance"))
    schemes = {label: {"weights": {k: round(v, 4) for k, v in info["weights"].items()},
                       "annual_volatility": round(info["annual_vol"], 4)}
               for label, info in report["schemes"].items()}
    calmest = min(schemes, key=lambda k: schemes[k]["annual_volatility"])
    return {"ok": True, "symbols": report["names"], "schemes": schemes,
            "lowest_risk": calmest, "skipped": failed or None}


def open_trading_terminal(count: int = 1, session: str = "", commands: list = None) -> dict:
    """Open the JUGNU TRADING TERMINAL -- the market simulator -- in Terminal
    windows, several at once, tiled, each already running what you tell it to.

    This is NOT the macOS Terminal app. "Open a terminal", "open Terminal",
    "give me a shell" mean that plain app: use open_app for those, and do
    not call this as well. Call this only when they ask for the TRADING
    terminal or the simulator, or to run one of the commands below.

    This is the main way to put work on screen. "Open 4 terminals and
    backtest a different stock in each" is one call: count=4 with four
    commands. Windows tile across the display automatically when count > 1.

    Terminal commands you can pass (the same language the terminal itself
    speaks, `;` chains several in one window):

      prices and the market
        q NVDA                    quote, day's range, sparkline
        ch AAPL 5m 120            price chart; minute bars for day trading
        watch BTC-USD 1m          a live chart that refreshes itself
        wl add NVDA               the watchlist; `wl` lists, `wl rm` removes
        mkt SPY QQQ               what sits in the markets panel
        screen rsi<35 vol>2       scan the watchlist against filters
        corr AAPL MSFT NVDA       how they move together
        signals                   what every strategy says right now
        alert NVDA < 200          tell me when; bare `alert` lists them
        unalert 1                 remove alert number 1

      backtesting
        bt sma AAPL 2015          backtest against buy & hold
        vs sma rsi MSFT           several strategies side by side
        risk sma TSLA             drawdowns, VaR, beta, year by year
        y                         the year-by-year table on its own
        trades                    every trade the last run made
        runs                      backtests saved earlier
        sweep rsi SPY             every parameter combination, as a heatmap
        wf sma QQQ 5              walk forward: tune, then trade blind
        mc sma AAPL               Monte Carlo: the same trades, reshuffled
        costs sma AAPL            what slippage does to it
        replay sma AAPL           watch it trade, bar by bar
        pf sma:AAPL, rsi:SPY      a portfolio of sleeves, rebalanced

      whether any of it is real -- the part that can FAIL a strategy
        stats sma AAPL            probabilistic Sharpe and a bootstrap CI
        pbo sma AAPL              probability the parameter search overfitted
        dsr sma AAPL              Sharpe after paying for every trial ever run
        search AAPL               the whole search ledger, and the bar it set
        forget AAPL               clear that ledger (rarely honest)
        cv sma AAPL               purged, embargoed cross-validation
        factor sma AAPL           alpha vs market, size, value, momentum

      execution and capacity
        exec AAPL 300000 buy      six execution algos through an order book
        cap sma AAPL              fund size before market impact eats the edge
        micro BTCUSDT             measure a REAL book and tape (crypto only)
        alloc SPY QQQ TLT GLD     equal vs inverse-variance vs HRP weights

      strategies, accounts, data
        strats                    what strategies exist
        show rsi                  one strategy's source
        build buy BTC when RSI<30 writes a real strategy and runs it
        paper start sma AAPL      a paper account on live prices
        fa AAPL                   fundamentals (needs OpenBB)
        opt AAPL                  options chain (needs OpenBB)
        why TSLA                  what moved it today
        explain sharpe            plain English for any term, free
        ask <question>            interrogate the last result
        source                    where the data comes from
        on jugnu                  run heavy work in the other account
        rpmc                      Rotman competition: rules and benchmark
        rpmc opt SPY XIC.TO GLD   weights under its 20% / cash / Sharpe rules
        rpmc check AAPL=250000    test a book against the competition rules
        rpmc eligible SHOP.TO     is a ticker allowed? US and Canada only

    Not worth sending: help, tour, clear, quit, jobs, job, g. They drive the
    window rather than produce an answer, and `quit` closes it.

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
