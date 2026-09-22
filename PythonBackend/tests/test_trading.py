"""The paper-trading simulator: the engine's honesty rules, the metrics, the
analysis tools, paper accounts, and the tools Jugnu calls.

Everything here runs on synthetic bars -- no network, no market data -- so
the arithmetic is checkable by hand."""

import _env  # noqa: F401  (isolates data dir; must be first)

import math
import unittest

from trading import analysis, engine, metrics, paper, store, strategies
from trading.data import Bar


def bars_from(prices, start_ts=1_600_000_000, step=86_400, spread=0.0):
    """One bar per price. open == close by default, so a fill price equals
    the price you can read off the list."""
    out = []
    for i, price in enumerate(prices):
        out.append(Bar(start_ts + i * step, price, price * (1 + spread), price * (1 - spread), price, 1000.0))
    return out


class EngineRulesTests(unittest.TestCase):
    def test_fills_at_the_next_bar_open_never_the_current_one(self):
        # Flat, then a jump. A strategy that turns on at bar 1 must pay
        # bar 2's price -- the jump -- not bar 1's.
        bars = bars_from([100, 100, 200, 200])
        def buy_at_bar_one(ctx):
            return 1.0 if ctx.i >= 1 else 0.0
        result = engine.run(bars, buy_at_bar_one, cash=1000, commission=0, slippage_bps=0)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].price, 200)        # the jump, not 100
        self.assertAlmostEqual(result.final, 1000, places=6)  # so it earns nothing

    def test_a_strategy_cannot_see_the_future(self):
        bars = bars_from([100, 100, 200, 100])
        def cheat(ctx):
            return 1.0 if ctx.bars[ctx.i + 1].close > ctx.bar.close else 0.0
        with self.assertRaises(IndexError):
            engine.run(bars, cheat, cash=1000)

    def test_costs_are_actually_deducted(self):
        bars = bars_from([100, 100, 100, 100])
        result = engine.run(bars, lambda ctx: 1.0, cash=1000, commission=5.0, slippage_bps=100)
        # Buys 1000/100 = ~10 shares at 101 (100bps slippage), pays $5.
        self.assertLess(result.final, 1000)
        self.assertAlmostEqual(result.trades[0].price, 101.0, places=6)

    def test_a_rising_hold_does_not_churn(self):
        # Without a rebalance band, holding a riser re-buys every bar.
        bars = bars_from([100 * (1.01 ** i) for i in range(200)])
        result = engine.run(bars, lambda ctx: 1.0, cash=10_000, commission=1.0, slippage_bps=5)
        self.assertLessEqual(len(result.trades), 3)

    def test_equity_curve_matches_cash_plus_position(self):
        bars = bars_from([100, 110, 120, 130])
        result = engine.run(bars, lambda ctx: 1.0, cash=1000, commission=0, slippage_bps=0)
        shares = 1000 / 110                       # bought at bar 1's open
        self.assertAlmostEqual(result.equity[-1], shares * 130, places=6)

    def test_benchmark_pays_the_same_costs(self):
        bars = bars_from([100, 100, 100])
        curve = engine.buy_and_hold(bars, cash=1000, commission=10.0, slippage_bps=0)
        self.assertAlmostEqual(curve[-1], 990, places=6)

    def test_refuses_a_series_too_short_to_simulate(self):
        with self.assertRaises(ValueError):
            engine.run(bars_from([100, 100]), lambda ctx: 1.0)


class MetricsTests(unittest.TestCase):
    def test_doubling_over_a_year(self):
        equity = [100 * (2 ** (i / 252)) for i in range(253)]
        summary = metrics.summarise(equity, periods_per_year=252)
        self.assertAlmostEqual(summary["total_return"], 1.0, places=2)
        self.assertAlmostEqual(summary["cagr"], 1.0, places=1)
        self.assertGreater(summary["sharpe"], 5)          # a perfectly smooth double

    def test_drawdown_is_the_worst_peak_to_trough(self):
        summary = metrics.summarise([100, 150, 75, 120], periods_per_year=252)
        self.assertAlmostEqual(summary["max_drawdown"], -0.5, places=6)

    def test_warnings_say_the_uncomfortable_things(self):
        mine = {"total_return": 0.2, "max_drawdown": -0.6, "sharpe": 0.4}
        theirs = {"total_return": 0.9}
        notes = " ".join(metrics.warnings(mine, theirs, cost_curve={0.0: 0.2, 20.0: -0.1},
                                          bars=3000, trades=5))
        self.assertIn("Underperforms buy & hold", notes)
        self.assertIn("20bps", notes)
        self.assertIn("too few to tell skill from luck", notes)

    def test_short_history_is_called_out(self):
        notes = metrics.warnings({"total_return": 1.0, "max_drawdown": -0.1, "sharpe": 2.0},
                                 {"total_return": 0.1}, bars=100, trades=20)
        self.assertTrue(any("meaningless" in n for n in notes))


class StrategyTests(unittest.TestCase):
    def test_registry_exposes_each_strategy_with_defaults(self):
        names = {s["name"] for s in strategies.describe()}
        self.assertTrue({"sma", "rsi", "hold", "breakout"} <= names)
        self.assertEqual(strategies.get("SMA ").name, "sma")
        with self.assertRaises(KeyError):
            strategies.get("nope")

    def test_sma_crossover_follows_the_trend(self):
        rising = bars_from(list(range(100, 400)))
        result = analysis.backtest(rising, "sma", {"fast": 5, "slow": 20}, symbol="TEST",
                                   commission=0, slippage_bps=0)
        self.assertGreater(result.metrics["total_return"], 0)
        self.assertGreater(result.metrics["exposure"], 0.5)

    def test_indicators(self):
        self.assertAlmostEqual(strategies.sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(strategies.sma([1], 5))
        self.assertEqual(strategies.rsi([1, 2, 3, 4, 5, 6], 3), 100.0)   # only gains


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        # A sawtooth: mean reversion should do something, trend following shouldn't.
        self.bars = bars_from([100 + 20 * math.sin(i / 6) for i in range(400)])

    def test_costs_make_returns_worse_monotonically(self):
        curve = analysis.cost_sensitivity(self.bars, "sma", {"fast": 3, "slow": 10}, symbol="TEST",
                                          levels=(0.0, 10.0, 50.0))
        self.assertGreaterEqual(curve[0.0], curve[10.0])
        self.assertGreaterEqual(curve[10.0], curve[50.0])

    def test_sweep_reports_the_best_and_how_lonely_it_is(self):
        result = analysis.sweep(self.bars, "sma", {"fast": (3, 9, 3), "slow": (12, 24, 6)},
                                symbol="TEST")
        self.assertEqual(result["runs"], 9)
        self.assertIn("params", result["best"])
        self.assertGreaterEqual(result["robustness"], 0.0)
        self.assertLessEqual(result["robustness"], 1.0)

    def test_sweep_refuses_an_enormous_grid(self):
        with self.assertRaises(ValueError):
            analysis.sweep(self.bars, "sma", {"fast": (1, 50, 1), "slow": (1, 50, 1)},
                           symbol="TEST", limit=100)

    def test_walk_forward_reports_out_of_sample_separately(self):
        result = analysis.walk_forward(self.bars, "sma", {"fast": (3, 9, 3), "slow": (12, 24, 6)},
                                       symbol="TEST", folds=3)
        self.assertIn("out_of_sample_return", result)
        self.assertEqual(len(result["folds"]), 2)
        for fold in result["folds"]:
            self.assertIn("in_sample_return", fold)

    def test_monte_carlo_needs_enough_trades(self):
        result = analysis.backtest(bars_from([100, 100, 110, 100]), "hold", symbol="TEST")
        self.assertFalse(analysis.monte_carlo(result)["enough_trades"])

    def test_monte_carlo_brackets_the_actual_result(self):
        result = analysis.backtest(self.bars, "sma", {"fast": 3, "slow": 10}, symbol="TEST")
        carlo = analysis.monte_carlo(result, runs=200)
        if carlo["enough_trades"]:
            self.assertLessEqual(carlo["p05"], carlo["p50"])
            self.assertLessEqual(carlo["p50"], carlo["p95"])


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.bars = bars_from([100 + i for i in range(120)])
        self._real_load = paper.load
        paper.load = lambda symbol, interval="1d", **kw: self.bars
        store.open_paper("SETUP", "hold", {}, 1.0)      # ensures the tables exist
        store.connect().execute("DELETE FROM paper")
        store.connect().commit()

    def tearDown(self):
        paper.load = self._real_load

    def test_account_takes_a_position_and_tracks_it(self):
        status = paper.start("TEST", "hold", cash=1000)
        self.assertTrue(status["in_market"])
        self.assertGreater(status["equity"], 0)
        self.assertEqual(status["strategy"], "hold")

    def test_stepping_twice_does_not_trade_twice(self):
        status = paper.start("TEST", "hold", cash=1000)
        before = status["trades"]
        again = paper.step(status["id"])
        self.assertEqual(again["trades"], before)

    def test_stopping_hides_it_from_the_active_list(self):
        status = paper.start("TEST", "hold", cash=1000)
        self.assertTrue(paper.stop(status["id"]))
        self.assertNotIn(status["id"], [a["id"] for a in paper.all_status()])

    def test_a_new_account_starts_now_instead_of_replaying_history(self):
        # It used to backfill every bar it had ever seen, so a new account
        # opened with $1,000 immediately reported years of imaginary gains.
        status = paper.start("TEST", "hold", cash=1000)
        self.assertLessEqual(status["equity"], 1010)
        self.assertGreater(status["equity"], 950)
        self.assertLessEqual(status["trades"], 1)

    def test_paper_follows_the_same_rules_as_a_backtest(self):
        status = paper.start("TEST", "sma", {"fast": 3, "slow": 10}, cash=1000)
        store.update_paper(status["id"], last_ts=0, cash=1000.0, position=0.0,
                           avg_cost=0.0, trades=[])
        replayed = paper.step(status["id"])
        backtested = analysis.backtest(self.bars, "sma", {"fast": 3, "slow": 10}, symbol="TEST",
                                       cash=1000, commission=paper.COMMISSION,
                                       slippage_bps=paper.SLIPPAGE_BPS)
        self.assertEqual(replayed["trades"], len(backtested.trades))
        self.assertAlmostEqual(replayed["equity"], backtested.final, delta=backtested.final * 0.02)

    def test_cash_never_goes_negative(self):
        status = paper.start("TEST", "hold", cash=1000)
        store.update_paper(status["id"], last_ts=0, cash=1000.0, position=0.0, avg_cost=0.0, trades=[])
        paper.step(status["id"])
        self.assertGreaterEqual(store.paper_account(status["id"])["cash"], 0)


class ToolTests(unittest.TestCase):
    """The layer Jugnu actually calls."""

    def setUp(self):
        from tools import trading_tools
        self.tools = trading_tools
        self.bars = bars_from([100 + 10 * math.sin(i / 5) + i * 0.3 for i in range(500)])
        self._real_load = trading_tools.load
        trading_tools.load = lambda symbol, interval="1d", **kw: self.bars

    def tearDown(self):
        self.tools.load = self._real_load

    def test_backtest_tool_returns_a_summary_and_the_benchmark(self):
        out = self.tools.backtest_strategy(symbol="TEST", strategy="sma")
        self.assertTrue(out["ok"])
        self.assertIn("return", out)
        self.assertIn("benchmark_return", out)
        self.assertIn("summary", out)
        self.assertIn("run_id", out)

    def test_unknown_strategy_is_a_clear_error(self):
        out = self.tools.backtest_strategy(symbol="TEST", strategy="wishful")
        self.assertFalse(out["ok"])
        self.assertIn("wishful", out["error"])

    def test_list_strategies(self):
        out = self.tools.list_strategies()
        self.assertTrue(out["ok"])
        self.assertTrue(any(s["name"] == "rsi" for s in out["strategies"]))

    def test_sweep_tool_warns_when_the_winner_stands_alone(self):
        out = self.tools.sweep_strategy(symbol="TEST", strategy="sma")
        self.assertTrue(out["ok"])
        self.assertIn("robustness", out)
        self.assertIn("best", out)


if __name__ == "__main__":
    unittest.main()
