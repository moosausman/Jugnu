"""
trading -- a paper-trading simulator: historical backtests, honest metrics,
and strategies run forward on live prices with imaginary money.

It is deliberately not a trading system. There is no brokerage connection,
no API keys, no order routing, and nothing here can move money. Jugnu's
tools expose it as read-only analysis, which is all it is.

    from trading import backtest, load
    result = backtest(load("AAPL", start="2015-01-01"), "sma", symbol="AAPL")
    print(result.metrics["total_return"], result.benchmark_metrics["total_return"])

Three faces on one engine: `python -m trading` (CLI), Jugnu's tools
(tools/trading_tools.py), and the Trading window in the app.
"""
from .analysis import backtest, cost_sensitivity, monte_carlo, sweep, walk_forward
from .data import DataError, load, periods_per_year
from .engine import Bar, Context, Result, Trade, run
from .strategies import REGISTRY, describe, get, register

__all__ = [
    "backtest", "cost_sensitivity", "monte_carlo", "sweep", "walk_forward",
    "load", "periods_per_year", "DataError",
    "Bar", "Context", "Result", "Trade", "run",
    "REGISTRY", "describe", "get", "register",
]
