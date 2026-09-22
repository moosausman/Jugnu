"""
appapi.py -- what the Trading window in the app asks for.

bridge.py forwards {"type": "trading", "action": ...} here and emits what
comes back. Same engine as the CLI and the tools, so the window can't show
a different number than the chat did.

Series are thinned before they're sent: a ten-year daily backtest is 2,500
points, and the window is 900 pixels wide.
"""
from __future__ import annotations

from . import analysis, metrics, paper, render, store, strategies
from .data import DataError, load

MAX_POINTS = 700


def _thin(values: list, limit: int = MAX_POINTS) -> list:
    if len(values) <= limit:
        return values
    step = len(values) / limit
    return [values[min(len(values) - 1, int(i * step))] for i in range(limit)]


def _params(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    out: dict = {}
    for piece in str(raw or "").replace(",", " ").split():
        if "=" in piece:
            name, value = piece.split("=", 1)
            try:
                out[name] = int(value)
            except ValueError:
                try:
                    out[name] = float(value)
                except ValueError:
                    out[name] = value
    return out


def handle(request: dict) -> dict:
    action = (request.get("action") or "backtest").strip().lower()
    try:
        if action == "strategies":
            return {"action": action, "ok": True, "strategies": strategies.describe()}
        if action == "backtest":
            return _backtest(request)
        if action == "sweep":
            return _sweep(request)
        if action == "runs":
            return {"action": action, "ok": True, "runs": store.list_runs(int(request.get("limit") or 20))}
        if action == "paper":
            return _paper(request)
        return {"action": action, "ok": False, "error": f"Unknown trading action '{action}'."}
    except (DataError, KeyError, ValueError) as e:
        return {"action": action, "ok": False, "error": str(e)}


def _backtest(request: dict) -> dict:
    symbol = (request.get("symbol") or "AAPL").upper()
    strategy = request.get("strategy") or "sma"
    interval = request.get("interval") or "1d"
    bars = load(symbol, interval, start=request.get("start") or None, end=request.get("end") or None)
    result = analysis.backtest(
        bars, strategy, _params(request.get("params")), symbol=symbol, interval=interval,
        cash=float(request.get("cash") or 10_000), commission=float(request.get("commission") or 1.0),
        slippage_bps=float(request.get("slippage_bps") or 5.0))
    costs = analysis.cost_sensitivity(bars, strategy, _params(request.get("params")), symbol=symbol,
                                      interval=interval, cash=float(request.get("cash") or 10_000))
    notes = metrics.warnings(result.metrics, result.benchmark_metrics, cost_curve=costs,
                             bars=len(bars), trades=result.metrics["trades"])
    carlo = analysis.monte_carlo(result)
    run_id = store.save_run(result, notes)
    return {
        "action": "backtest", "ok": True, "run_id": run_id,
        "symbol": symbol, "strategy": result.strategy, "params": result.params,
        "from": result.dates[0], "to": result.dates[-1],
        "cash": result.cash, "slippage_bps": result.slippage_bps, "commission": result.commission,
        "metrics": result.metrics, "benchmark_metrics": result.benchmark_metrics,
        "dates": _thin(result.dates), "equity": [round(v, 2) for v in _thin(result.equity)],
        "benchmark": [round(v, 2) for v in _thin(result.benchmark)],
        "trades": [{"date": t.date, "side": t.side, "shares": round(t.shares, 4),
                    "price": round(t.price, 4), "pnl": None if t.pnl is None else round(t.pnl, 2)}
                   for t in result.trades[-200:]],
        "rows": [{"label": label, "strategy": mine, "benchmark": theirs}
                 for label, mine, theirs in metrics.table(result)],
        "cost_curve": {f"{k:g}": round(v, 4) for k, v in costs.items()},
        "monte_carlo": carlo,
        "warnings": notes,
        "text": render.compact(result, notes),
    }


def _sweep(request: dict) -> dict:
    symbol = (request.get("symbol") or "AAPL").upper()
    strategy = request.get("strategy") or "sma"
    interval = request.get("interval") or "1d"
    bars = load(symbol, interval, start=request.get("start") or None, end=request.get("end") or None)
    result = analysis.sweep(bars, strategy, symbol=symbol, interval=interval,
                            slippage_bps=float(request.get("slippage_bps") or 5.0))
    return {
        "action": "sweep", "ok": True, "symbol": symbol, "strategy": strategy,
        "runs": result["runs"], "robustness": result["robustness"],
        "benchmark_return": result["benchmark_return"], "median_return": result["median_return"],
        "best": result["best"],
        "rows": [{"params": row["params"], "return": round(row["return"], 4),
                  "sharpe": round(row["sharpe"], 2), "trades": row["trades"]}
                 for row in result["rows"][:60]],
    }


def _paper(request: dict) -> dict:
    sub = (request.get("paper_action") or "status").strip().lower()
    if sub == "start":
        account = paper.start(request.get("symbol") or "AAPL", request.get("strategy") or "sma",
                              _params(request.get("params")),
                              cash=float(request.get("cash") or 10_000),
                              interval=request.get("interval") or "1d")
        return {"action": "paper", "ok": True, "accounts": paper.all_status(), "started": account["id"]}
    if sub == "stop":
        paper.stop(request.get("account_id") or "")
        return {"action": "paper", "ok": True, "accounts": paper.all_status()}
    if sub == "step":
        paper.step_all()
    return {"action": "paper", "ok": True, "accounts": paper.all_status()}
