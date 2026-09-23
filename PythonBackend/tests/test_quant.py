"""Portfolios, deep risk metrics, the scanner, jobs, alerts, and the pieces
that let a simulation run in the other macOS account.

Synthetic bars throughout: every number here is checkable by hand, and
nothing touches the network."""

import _env  # noqa: F401  (isolates data dir; must be first)

import math
import time
import unittest

from trading import alerts, jobs, portfolio, quotes, risk, screen, signals, sources
from trading.analysis import backtest
from trading.data import Bar


def bars_from(prices, start_ts=1_600_000_000):
    return [Bar(start_ts + i * 86_400, p, p * 1.01, p * 0.99, p, 1000.0) for i, p in enumerate(prices)]


def rising(n=400, rate=1.004):
    return [100 * (rate ** i) for i in range(n)]


def sawtooth(n=400, amplitude=20, period=6):
    return [100 + amplitude * math.sin(i / period) for i in range(n)]


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.result = backtest(bars_from(sawtooth()), "sma", {"fast": 3, "slow": 10}, symbol="TEST")

    def test_drawdown_series_matches_the_worst_fall(self):
        series = risk.drawdown_series([100, 120, 60, 90])
        self.assertAlmostEqual(min(series), -0.5, places=6)

    def test_underwater_counts_the_time_not_just_the_depth(self):
        water = risk.underwater([100, 90, 95, 99, 101], per_year=252)
        self.assertEqual(water["longest_bars"], 3)
        self.assertFalse(water["still_under_water"])
        self.assertAlmostEqual(water["share_under_water"], 0.6, places=6)

    def test_ulcer_is_zero_for_a_curve_that_never_falls(self):
        self.assertEqual(risk.ulcer_index([100, 110, 120]), 0.0)
        self.assertGreater(risk.ulcer_index([100, 60, 120]), 0)

    def test_var_is_the_tail_not_the_average(self):
        var, cvar = risk.value_at_risk(rising(300))
        self.assertLessEqual(cvar, var)

    def test_beta_of_a_curve_against_itself_is_one(self):
        curve = rising(300)
        beta, alpha = risk.beta_alpha(curve, curve, per_year=252)
        self.assertAlmostEqual(beta, 1.0, places=6)
        self.assertAlmostEqual(alpha, 0.0, places=6)

    def test_yearly_table_splits_by_calendar_year(self):
        rows = risk.yearly(backtest(bars_from(rising(800)), "hold", symbol="TEST"))
        self.assertGreaterEqual(len(rows), 2)
        for row in rows:
            self.assertIn("benchmark", row)

    def test_trade_stats_describe_the_round_trips(self):
        stats = risk.trade_stats(self.result)
        if stats["round_trips"]:
            self.assertIn("payoff", stats)
            self.assertGreaterEqual(stats["win_rate"], 0.0)
            self.assertLessEqual(stats["win_rate"], 1.0)
            self.assertGreaterEqual(stats["median_hold_days"], 0)

    def test_full_and_verdicts_never_throw(self):
        deep = risk.full(self.result)
        self.assertIn("calmar", deep)
        self.assertIsInstance(risk.verdicts(deep, self.result), list)

    def test_one_trade_carrying_everything_is_called_out(self):
        deep = {"trades": {"round_trips": 6, "best_trade_share": 0.8,
                           "best_trade": {"date": "2021-01-04", "pnl": 900}, "profit_factor": 3},
                "underwater": {"share_under_water": 0.2, "longest_days": 10},
                "rolling_sharpe": {"negative_share": 0.1}, "beta": 0.2, "yearly": []}
        notes = " ".join(risk.verdicts(deep, self.result))
        self.assertIn("One trade", notes)


class PortfolioTests(unittest.TestCase):
    def test_parsing_sleeves_and_weights(self):
        sleeves = portfolio.parse_sleeves("sma:AAPL, rsi:BTC-USD*2")
        self.assertEqual([s.symbol for s in sleeves], ["AAPL", "BTC-USD"])
        self.assertEqual(sleeves[1].weight, 2.0)
        self.assertEqual(sleeves[0].strategy, "sma")

    def test_correlation_of_a_series_with_itself(self):
        curve = sawtooth(200)
        self.assertAlmostEqual(portfolio.correlation(curve, curve), 1.0, places=6)
        # A series whose every daily move is the opposite one. (Reversing a
        # curve does NOT do this -- its returns stay the same size and the
        # correlation comes out near zero, which is what caught this test.)
        mirror = [100.0]
        for i in range(1, len(curve)):
            mirror.append(mirror[-1] * (2 - curve[i] / curve[i - 1]))
        self.assertLess(portfolio.correlation(curve, mirror), -0.95)

    def test_aligning_two_calendars_forward_fills(self):
        dates, aligned = portfolio._align({
            "a": (["2021-01-01", "2021-01-02", "2021-01-03"], [1.0, 2.0, 3.0]),
            "b": (["2021-01-01", "2021-01-03"], [10.0, 30.0]),
        })
        self.assertEqual(dates, ["2021-01-01", "2021-01-02", "2021-01-03"])
        self.assertEqual(aligned["b"], [10.0, 10.0, 30.0])      # the gap holds, never drops

    def test_a_portfolio_runs_and_reports_against_holding(self):
        import trading.portfolio as module
        prices = {"AAA": bars_from(rising(300)), "BBB": bars_from(sawtooth(300))}
        original = module.load
        module.load = lambda symbol, interval="1d", **kw: prices[symbol]
        try:
            result = module.run(module.parse_sleeves("hold:AAA, sma:BBB"), cash=1000)
        finally:
            module.load = original
        self.assertEqual(len(result["sleeves"]), 2)
        self.assertEqual(len(result["equity"]), len(result["dates"]))
        self.assertIn("benchmark_metrics", result)
        self.assertIn("AAA·hold", result["correlation"])
        self.assertAlmostEqual(result["equity"][0], 1000, delta=50)

    def test_refuses_an_empty_portfolio(self):
        with self.assertRaises(ValueError):
            portfolio.run([])


class ScreenTests(unittest.TestCase):
    def setUp(self):
        import trading.screen as module
        self.module = module
        self.original = module.load
        module.load = lambda symbol, interval="1d", **kw: bars_from(
            rising(300) if symbol == "UP" else list(reversed(rising(300))))

    def tearDown(self):
        self.module.load = self.original

    def test_facts_describe_where_a_name_sits(self):
        facts = screen.facts("UP")
        self.assertGreater(facts["near_high"], 0.98)
        self.assertGreater(facts["above_sma50"], 0)
        self.assertEqual(facts["trend"], 1.0)

    def test_filters_parse_and_apply(self):
        parsed = screen.parse_filters("rsi<30 vol>2")
        self.assertEqual(len(parsed), 2)
        self.assertTrue(screen.passes({"rsi": 20, "vol": 3}, parsed))
        self.assertFalse(screen.passes({"rsi": 40, "vol": 3}, parsed))

    def test_scan_filters_and_flags(self):
        rows = screen.scan(["UP", "DOWN"])
        self.assertEqual(len(rows), 2)
        up = next(r for r in rows if r["symbol"] == "UP")
        self.assertIn("uptrend", up["flags"])
        filtered = screen.scan(["UP", "DOWN"], filters="above_sma50>0")
        self.assertEqual([r["symbol"] for r in filtered], ["UP"])

    def test_a_bad_symbol_reports_instead_of_raising(self):
        self.module.load = lambda *a, **k: (_ for _ in ()).throw(ValueError("nope"))
        rows = screen.scan(["ZZZZ"])
        self.assertIn("error", rows[0])


class JobTests(unittest.TestCase):
    def test_jobs_run_and_keep_their_own_output(self):
        runner = jobs.Runner(workers=2)
        first = runner.submit("one", lambda job: job.write("hello"))
        second = runner.submit("two", lambda job: job.write("world"))
        deadline = time.time() + 10
        while runner.running() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(first.lines, ["hello"])
        self.assertEqual(second.lines, ["world"])
        self.assertEqual(first.status, "done")
        runner.shutdown()

    def test_a_failing_job_is_recorded_not_raised(self):
        runner = jobs.Runner(workers=1)
        def explode(job):
            raise RuntimeError("boom")
        job = runner.submit("bad", explode)
        deadline = time.time() + 10
        while not job.done and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(job.status, "failed")
        self.assertIn("boom", job.error)
        runner.shutdown()

    def test_clearing_keeps_what_is_still_running(self):
        runner = jobs.Runner(workers=1)
        done = runner.submit("done", lambda job: None)
        deadline = time.time() + 5
        while not done.done and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(runner.clear_finished(), 1)
        self.assertEqual(runner.all(), [])
        runner.shutdown()


class AlertTests(unittest.TestCase):
    def setUp(self):
        for row in alerts.all_alerts():
            alerts.remove(row["id"])

    def test_parsing_the_shorthand(self):
        self.assertEqual(alerts.parse("NVDA < 200"), ("NVDA", "price", "<", 200.0))
        self.assertEqual(alerts.parse("aapl rsi < 30"), ("AAPL", "rsi", "<", 30.0))
        with self.assertRaises(ValueError):
            alerts.parse("NVDA moon 30")
        with self.assertRaises(ValueError):
            alerts.parse("nonsense")

    def test_add_list_and_remove(self):
        rule = alerts.add("NVDA < 200")
        self.assertIn(rule["id"], [a["id"] for a in alerts.all_alerts()])
        self.assertIn("NVDA price < $200", alerts.describe(rule))
        self.assertTrue(alerts.remove(rule["id"]))

    def test_an_alert_fires_once_then_disarms(self):
        rule = alerts.add("TESTSYM < 100")
        original = alerts._value
        alerts._value = lambda symbol, field: 50.0
        try:
            fired = alerts.check()
            self.assertIn(rule["id"], [f["id"] for f in fired])
            self.assertEqual(alerts.check(), [])          # disarmed, so no repeat
        finally:
            alerts._value = original


class SignalTests(unittest.TestCase):
    """What a strategy says right now -- the algo view. A rule that answers
    differently depending on whether you already hold it (most do) has to be
    asked both ways, or the answer is meaningless."""

    def setUp(self):
        import trading.signals as module
        self.module = module
        self.original = module.load
        module.load = lambda symbol, interval="1d", **kw: bars_from(
            rising(300) if symbol == "UP" else list(reversed(rising(300))))

    def tearDown(self):
        self.module.load = self.original

    def test_a_trend_follower_is_in_on_the_way_up_and_out_on_the_way_down(self):
        up = {row["strategy"]: row for row in signals.for_symbol("UP", ["sma"])}
        down = {row["strategy"]: row for row in signals.for_symbol("DOWN", ["sma"])}
        self.assertIn(up["sma"]["state"], ("enter", "hold"))
        self.assertEqual(down["sma"]["state"], "out")

    def test_every_row_says_what_it_said_yesterday(self):
        for row in signals.for_symbol("UP", ["sma", "breakout"]):
            self.assertIn(row["was"], ("enter", "hold", "out"))
            self.assertEqual(row["changed"], row["state"] != row["was"])

    def test_a_bad_symbol_is_reported_not_raised(self):
        self.module.load = lambda *a, **k: (_ for _ in ()).throw(ValueError("nope"))
        rows = signals.scan(["ZZZZ"])
        self.assertIn("error", rows[0])

    def test_the_summary_says_plainly_when_nothing_changed(self):
        self.assertIn("Nothing changed", signals.summary([{"changed": False, "state": "hold"}]))
        text = signals.summary([{"changed": True, "state": "enter", "strategy": "sma",
                                 "symbol": "AAPL"}])
        self.assertIn("would open", text)


class SourceTests(unittest.TestCase):
    def test_columnar_openbb_results_become_rows(self):
        rows = sources._flatten({"symbol": ["AAPL", "MSFT"], "name": ["Apple", "Microsoft"]})
        self.assertEqual(rows, [{"symbol": "AAPL", "name": "Apple"},
                                {"symbol": "MSFT", "name": "Microsoft"}])
        self.assertEqual(sources._flatten({"results": [{"a": 1}]}), [{"a": 1}])
        self.assertEqual(sources._flatten(None), [])

    def test_without_openbb_it_says_so_instead_of_failing(self):
        original = sources.obb
        sources.obb = lambda: None
        try:
            self.assertFalse(sources.fundamentals("AAPL")["ok"])
            self.assertIn("install", sources.fundamentals("AAPL")["error"].lower())
            self.assertFalse(sources.options("AAPL")["ok"])
        finally:
            sources.obb = original

    def test_it_lives_outside_the_app_bundle(self):
        self.assertNotIn("PythonBackend", str(sources.sidecar()))


class QuoteTests(unittest.TestCase):
    def test_crypto_is_recognised(self):
        self.assertTrue(quotes._is_crypto("BTC-USD"))
        self.assertTrue(quotes._is_crypto("ETHUSDT"))
        self.assertFalse(quotes._is_crypto("AAPL"))


if __name__ == "__main__":
    unittest.main()
