"""The statistics that decide whether a backtest is evidence.

These tests check arithmetic against values that are known independently --
published normal and t quantiles, numpy's least squares where it is
available, and the theoretical behaviour of each estimator under a null it
is supposed to detect. Nothing here touches the network.
"""

import _env  # noqa: F401  (isolates data dir; must be first)

import math
import random
import unittest

from trading import allocate, capacity, factors, quant


class DistributionTests(unittest.TestCase):
    """Hand-written special functions, against published values."""

    def test_normal_quantiles(self):
        for p, want in [(0.975, 1.959964), (0.95, 1.644854), (0.5, 0.0),
                        (0.01, -2.326348), (0.001, -3.090232)]:
            self.assertAlmostEqual(quant.norm_ppf(p), want, places=5, msg=f"p={p}")

    def test_normal_round_trip(self):
        for x in (-2.5, -0.4, 0.0, 1.3, 2.9):
            self.assertAlmostEqual(quant.norm_ppf(quant.norm_cdf(x)), x, places=6)

    def test_t_distribution_against_a_table(self):
        # Two-sided critical values every statistics table carries.
        for t, df, want in [(2.228139, 10, 0.05), (3.169273, 10, 0.01),
                            (2.085963, 20, 0.05), (1.959964, 10 ** 7, 0.05)]:
            self.assertAlmostEqual(quant.t_pvalue(t, df), want, places=4, msg=f"t={t} df={df}")

    def test_moments_of_a_known_sample(self):
        rng = random.Random(1)
        xs = [rng.gauss(0, 1) for _ in range(60000)]
        m = quant.moments(xs)
        self.assertAlmostEqual(m["mean"], 0.0, places=1)
        self.assertAlmostEqual(m["sd"], 1.0, places=1)
        self.assertAlmostEqual(m["skew"], 0.0, places=1)
        self.assertAlmostEqual(m["kurtosis"], 3.0, places=0)   # raw, not excess


class SharpeEvidenceTests(unittest.TestCase):
    def test_fat_tails_widen_the_error_bar(self):
        thin = quant.sharpe_standard_error(0.1, 500, 0.0, 3.0)
        fat = quant.sharpe_standard_error(0.1, 500, 0.0, 12.0)
        self.assertGreater(fat, thin)

    def test_a_short_record_is_not_evidence(self):
        short = quant.probabilistic_sharpe(0.15, 20, 0.0, 3.0)
        long = quant.probabilistic_sharpe(0.15, 2000, 0.0, 3.0)
        self.assertLess(short, long)
        self.assertGreater(long, 0.99)

    def test_the_hurdle_rises_with_the_number_of_trials(self):
        bars = [quant.expected_max_sharpe(n, 0.01) for n in (2, 10, 100, 1000)]
        self.assertEqual(bars, sorted(bars))
        self.assertEqual(quant.expected_max_sharpe(1, 0.01), 0.0)

    def test_searching_harder_lowers_the_odds_the_winner_is_real(self):
        odds = [quant.deflated_sharpe(0.08, 1000, 0.0, 3.0, trials, 0.002)["deflated"]
                for trials in (1, 10, 100, 1000)]
        self.assertEqual(odds, sorted(odds, reverse=True))

    def test_a_losing_strategy_never_becomes_significant(self):
        self.assertEqual(quant.min_track_record(-0.01, 500, 0.0, 3.0), float("inf"))
        self.assertTrue(math.isfinite(quant.min_track_record(0.05, 500, 0.0, 3.0)))

    def test_bootstrap_brackets_the_observed_sharpe(self):
        rng = random.Random(5)
        series = [rng.gauss(0.001, 0.01) for _ in range(600)]
        out = quant.bootstrap_sharpe(series, runs=300)
        self.assertTrue(out["enough"])
        self.assertLess(out["low"], out["sharpe"])
        self.assertGreater(out["high"], out["sharpe"])

    def test_the_bootstrap_keeps_the_block_structure(self):
        # Every resample is the same length as the original and made of its
        # own values -- a block bootstrap, not a parametric draw.
        original = [0.1, -0.2, 0.3, -0.4, 0.5, -0.6, 0.7, -0.8]
        for sample in quant.stationary_bootstrap(original, runs=20)[:5]:
            self.assertEqual(len(sample), len(original))
            self.assertTrue(set(sample) <= set(original))


class OverfittingTests(unittest.TestCase):
    def test_pbo_is_a_coin_flip_on_worthless_strategies(self):
        """The defining property: if no configuration has an edge, picking
        the in-sample winner should be no better than picking at random.

        Averaged over matrices -- one matrix's 252 combinations share a
        single realisation, so a single PBO has a wide spread.
        """
        values = []
        for trial in range(12):
            rng = random.Random(500 + trial)
            matrix = [[rng.gauss(0, 0.01) for _ in range(15)] for _ in range(320)]
            values.append(quant.cscv_pbo(matrix, splits=8)["pbo"])
        mean = sum(values) / len(values)
        self.assertGreater(mean, 0.3)
        self.assertLess(mean, 0.7)

    def test_pbo_finds_a_genuine_edge(self):
        rng = random.Random(9)
        matrix = [[rng.gauss(0, 0.01) for _ in range(15)] for _ in range(320)]
        for row in matrix:
            row[4] = rng.gauss(0.005, 0.01)        # one configuration really is better
        self.assertLess(quant.cscv_pbo(matrix, splits=8)["pbo"], 0.2)

    def test_pbo_declines_politely_when_it_cannot_run(self):
        self.assertFalse(quant.cscv_pbo([[0.1, 0.2]], splits=8)["enough"])

    def test_purged_folds_never_touch_the_test_window(self):
        for train, test in quant.purged_splits(1000, folds=5, embargo=0.02):
            self.assertFalse(set(train) & set(test))
            nearest = min(abs(a - b) for a in train for b in (test[0], test[-1]))
            self.assertGreater(nearest, 1, "training bars sit right against the test set")

    def test_every_bar_is_tested_once(self):
        tested = []
        for _, test in quant.purged_splits(600, folds=6, embargo=0.0):
            tested += test
        self.assertEqual(sorted(tested), list(range(600)))

    def test_reality_check_is_unimpressed_by_noise(self):
        rng = random.Random(3)
        family = [[rng.gauss(0, 0.01) for _ in range(300)] for _ in range(8)]
        out = quant.reality_check(family[0], family[1:], runs=200)
        self.assertGreater(out["p_value"], 0.05)


class SearchLedgerTests(unittest.TestCase):
    """The trial count that makes deflation honest across a whole project."""

    def setUp(self):
        from trading import store
        store.forget_trials()

    def record(self, **over):
        from trading import store
        row = {"symbol": "AAPL", "strategy": "sma", "params": {"fast": 20},
               "interval": "1d", "start": "2015-01-02", "end": "2020-01-02",
               "bars": 1000, "sharpe": 0.04, "total_return": 1.2}
        row.update(over)
        store.record_trial(**row)

    def test_the_same_test_twice_is_one_trial(self):
        """Re-running a backtest teaches you nothing new, so it cannot cost
        you significance a second time."""
        from trading import store
        self.record()
        self.record()
        self.record()
        self.assertEqual(store.search_history("AAPL")["trials"], 1)

    def test_a_different_parameter_window_or_symbol_is_a_new_trial(self):
        from trading import store
        self.record()
        self.record(params={"fast": 30})
        self.record(end="2021-01-02")
        self.record(symbol="MSFT")
        self.assertEqual(store.search_history("AAPL")["trials"], 3)
        self.assertEqual(store.search_history("MSFT")["trials"], 1)
        self.assertEqual(store.search_history()["trials"], 4)

    def test_history_scopes_to_a_strategy_when_asked(self):
        from trading import store
        self.record()
        self.record(strategy="rsi", params={"length": 14})
        self.assertEqual(store.search_history("AAPL")["trials"], 2)
        self.assertEqual(store.search_history("AAPL", "rsi")["trials"], 1)
        self.assertEqual(store.search_history("AAPL")["strategies"], ["rsi", "sma"])

    def test_running_a_backtest_writes_to_the_ledger(self):
        from trading import store
        from trading.analysis import backtest
        from trading.data import Bar
        bars = [Bar(ts=1_600_000_000 + i * 86400, open=100 + i, high=101 + i,
                    low=99 + i, close=100 + i, volume=1e6) for i in range(200)]
        backtest(bars, "sma", {"fast": 5, "slow": 20}, symbol="TEST")
        self.assertEqual(store.search_history("TEST")["trials"], 1)

    def test_forget_is_scoped(self):
        from trading import store
        self.record()
        self.record(symbol="MSFT")
        store.forget_trials("AAPL")
        self.assertEqual(store.search_history("AAPL")["trials"], 0)
        self.assertEqual(store.search_history("MSFT")["trials"], 1)

    def test_the_bar_rises_as_you_search(self):
        bars = [quant.required_sharpe(n, 0.002, 2000) for n in (1, 10, 100, 1000)]
        self.assertEqual(bars, sorted(bars))

    def test_required_sharpe_inverts_the_deflated_sharpe(self):
        """The two have to agree, or the number shown as "the bar" is not
        the bar the verdict is using."""
        for trials in (5, 50, 500):
            need = quant.required_sharpe(trials, 0.002, 1500, confidence=0.95)
            self.assertAlmostEqual(
                quant.deflated_sharpe(need, 1500, 0.0, 3.0, trials, 0.002)["deflated"],
                0.95, places=3, msg=f"trials={trials}")


class RegressionTests(unittest.TestCase):
    def test_inverse_is_a_real_inverse(self):
        matrix = [[4.0, 1.2, 0.3], [1.2, 3.0, 0.7], [0.3, 0.7, 2.5]]
        inverse = factors._invert(matrix)
        for i in range(3):
            for j in range(3):
                product = sum(matrix[i][k] * inverse[k][j] for k in range(3))
                self.assertAlmostEqual(product, 1.0 if i == j else 0.0, places=10)

    def test_collinear_columns_are_refused_not_fudged(self):
        self.assertIsNone(factors._invert([[1.0, 2.0], [2.0, 4.0]]))
        column = [float(i) for i in range(80)]
        self.assertIsNone(factors.ols([float(i) for i in range(80)],
                                      {"a": column, "b": [2 * v for v in column]}))

    def test_ols_recovers_the_coefficients_it_was_given(self):
        rng = random.Random(11)
        n = 900
        a = [rng.gauss(0.0004, 0.011) for _ in range(n)]
        b = [rng.gauss(0.0001, 0.006) for _ in range(n)]
        y = [0.0003 + 0.80 * a[i] + 0.30 * b[i] + rng.gauss(0, 0.004) for i in range(n)]
        fit = factors.ols(y, {"A": a, "B": b}, hac=False)
        self.assertAlmostEqual(fit["terms"]["A"]["coefficient"], 0.80, places=1)
        self.assertAlmostEqual(fit["terms"]["B"]["coefficient"], 0.30, places=1)
        self.assertGreater(fit["r2"], 0.5)

    def test_newey_west_widens_errors_on_autocorrelated_residuals(self):
        rng = random.Random(12)
        n = 800
        x = [rng.gauss(0, 0.01) for _ in range(n)]
        shock = [0.0] * n
        for i in range(1, n):
            shock[i] = 0.7 * shock[i - 1] + rng.gauss(0, 0.004)
        y = [0.0003 + 0.8 * x[i] + shock[i] for i in range(n)]
        plain = factors.ols(y, {"X": x}, hac=False)["terms"]["alpha"]["error"]
        corrected = factors.ols(y, {"X": x}, hac=True)["terms"]["alpha"]["error"]
        self.assertGreater(corrected, plain,
                           "autocorrelated residuals must not look like independent ones")

    def test_a_short_sample_is_refused(self):
        self.assertIsNone(factors.ols([1.0, 2.0, 3.0], {"a": [1.0, 2.0, 3.0]}))


class AllocationTests(unittest.TestCase):
    def series(self):
        rng = random.Random(3)
        n = 500
        one = [rng.gauss(0, 0.01) for _ in range(n)]
        return {
            "A1": [one[i] + rng.gauss(0, 0.002) for i in range(n)],
            "A2": [one[i] + rng.gauss(0, 0.002) for i in range(n)],
            "A3": [one[i] + rng.gauss(0, 0.002) for i in range(n)],
            "LONE": [rng.gauss(0, 0.004) for _ in range(n)],
        }

    def test_weights_are_a_portfolio(self):
        series = self.series()
        report = allocate.summarise(list(series), series)
        for scheme in report["schemes"].values():
            self.assertAlmostEqual(sum(scheme["weights"].values()), 1.0, places=9)
            self.assertTrue(all(w >= 0 for w in scheme["weights"].values()))

    def test_hrp_does_not_buy_the_same_trade_three_times(self):
        """Three copies of one factor should share roughly one slot between
        them, not take three. That is the whole point of clustering first."""
        series = self.series()
        report = allocate.summarise(list(series), series)
        hrp = report["schemes"]["hierarchical"]["weights"]
        equal = report["schemes"]["equal"]["weights"]
        cluster = hrp["A1"] + hrp["A2"] + hrp["A3"]
        self.assertLess(cluster, equal["A1"] + equal["A2"] + equal["A3"])
        self.assertGreater(hrp["LONE"], equal["LONE"])

    def test_hrp_has_the_lowest_volatility_here(self):
        series = self.series()
        report = allocate.summarise(list(series), series)
        vols = {k: v["annual_vol"] for k, v in report["schemes"].items()}
        self.assertEqual(min(vols, key=vols.get), "hierarchical")

    def test_inverse_variance_prefers_the_quiet_asset(self):
        names, cov = allocate.covariance({"LOUD": [0.05, -0.05] * 50, "QUIET": [0.005, -0.005] * 50})
        weights = allocate.inverse_variance(names, cov)
        self.assertGreater(weights["QUIET"], weights["LOUD"])

    def test_kelly_halves(self):
        rng = random.Random(2)
        out = allocate.kelly([rng.gauss(0.0002, 0.02) for _ in range(400)])
        self.assertAlmostEqual(out["half"], out["full"] / 2, places=9)

    def test_vol_target_scales_the_right_way(self):
        rng = random.Random(6)
        calm = allocate.vol_target([rng.gauss(0, 0.004) for _ in range(400)], 0.15)
        wild = allocate.vol_target([rng.gauss(0, 0.03) for _ in range(400)], 0.15)
        self.assertGreater(calm["leverage"], wild["leverage"])


class ImpactTests(unittest.TestCase):
    def test_cost_grows_with_the_square_root_of_size(self):
        """Quadrupling the order should roughly double the impact, not
        quadruple it -- that is what the square-root law says."""
        adv, vol = 1e9, 0.02
        small = capacity.impact_bps(1e6, adv, vol, half_spread_bps=0.0)
        large = capacity.impact_bps(4e6, adv, vol, half_spread_bps=0.0)
        self.assertAlmostEqual(large / small, 2.0, places=6)

    def test_the_spread_is_paid_even_on_a_tiny_order(self):
        self.assertAlmostEqual(capacity.impact_bps(1.0, 1e12, 0.02, half_spread_bps=1.5), 1.5, places=3)

    def test_a_thinner_name_costs_more_for_the_same_order(self):
        liquid = capacity.impact_bps(1e6, 1e10, 0.02)
        thin = capacity.impact_bps(1e6, 1e7, 0.02)
        self.assertGreater(thin, liquid)

    def test_median_dollar_volume_ignores_one_frantic_day(self):
        class FakeBar:
            def __init__(self, volume):
                self.volume, self.close = volume, 100.0
        bars = [FakeBar(1000) for _ in range(30)] + [FakeBar(10_000_000)]
        self.assertLess(capacity.average_dollar_volume(bars), 200_000)


class OrderBookTests(unittest.TestCase):
    """A matching engine that gets priority wrong is worse than no engine:
    every execution number downstream would be quietly fictional."""

    def book(self):
        from trading.book import Book
        return Book(tick=0.01)

    def test_price_time_priority(self):
        from trading.book import BUY, SELL, Order
        book = self.book()
        for name, qty in (("A", 100), ("B", 200), ("C", 300)):
            book.submit(Order(SELL, qty, 10.01, owner=name))
        fills = [f for f in book.submit(Order(BUY, 250, 10.01, owner="taker"))
                 if f.role == "maker"]
        self.assertEqual([(f.owner, f.qty) for f in fills], [("A", 100.0), ("B", 150.0)])

    def test_the_queue_moves_up_as_it_is_filled(self):
        from trading.book import BUY, SELL, Order
        book = self.book()
        for qty in (100, 200, 300):
            book.submit(Order(SELL, qty, 10.01))
        book.submit(Order(BUY, 250, 10.01))
        ahead = [o.queue_ahead for o in book.asks[book.to_ticks(10.01)].orders]
        self.assertEqual(ahead, [0.0, 50.0])      # B is now at the front, C behind its remainder

    def test_a_market_order_walks_the_levels(self):
        from trading.book import BUY, SELL, Order
        book = self.book()
        for price in (10.00, 10.02, 10.05):
            book.submit(Order(SELL, 100, price))
        taker = Order(BUY, 250, None, owner="mine")
        fills = [f for f in book.submit(taker) if f.role == "taker"]
        self.assertEqual([(f.price, f.qty) for f in fills],
                         [(10.00, 100.0), (10.02, 100.0), (10.05, 50.0)])
        self.assertEqual(taker.filled, 250.0)

    def test_ioc_takes_what_it_can_and_does_not_rest(self):
        from trading.book import BUY, SELL, Order
        book = self.book()
        book.submit(Order(SELL, 50, 10.00))
        order = Order(BUY, 200, 10.00, tif="IOC")
        book.submit(order)
        self.assertEqual(order.filled, 50.0)
        self.assertEqual(book.depth(BUY), [])

    def test_fill_or_kill_is_all_or_nothing(self):
        from trading.book import BUY, SELL, Order
        book = self.book()
        book.submit(Order(SELL, 50, 10.00))
        order = Order(BUY, 200, 10.00, tif="FOK")
        book.submit(order)
        self.assertEqual(order.filled, 0.0)
        book.submit(Order(SELL, 500, 10.00))
        ok = Order(BUY, 200, 10.00, tif="FOK")
        book.submit(ok)
        self.assertEqual(ok.filled, 200.0)

    def test_the_book_never_crosses(self):
        from trading.book import BUY, SELL, Order
        rng = random.Random(0)
        book = self.book()
        for _ in range(3000):
            book.submit(Order(rng.choice([BUY, SELL]), rng.randint(1, 100),
                              round(10 + rng.gauss(0, 0.05), 2) if rng.random() < 0.85 else None,
                              tif=rng.choice(["GTC", "GTC", "IOC"])))
            if book.best_bid is not None and book.best_ask is not None:
                self.assertLess(book.best_bid, book.best_ask)

    def test_cancel_removes_it_from_the_queue(self):
        from trading.book import BUY, SELL, Order
        book = self.book()
        mine = Order(SELL, 100, 10.01, owner="mine")
        book.submit(mine)
        book.submit(Order(SELL, 100, 10.01, owner="other"))
        self.assertTrue(book.cancel(mine.id))
        fills = [f for f in book.submit(Order(BUY, 100, 10.01)) if f.role == "maker"]
        self.assertEqual([f.owner for f in fills], ["other"])


class ExecutionTests(unittest.TestCase):
    def bars(self, drift=0.0, n=20):
        """Synthetic minute bars, optionally trending."""
        from trading.data import Bar
        out, price = [], 100.0
        for i in range(n):
            close = price * (1 + drift)
            out.append(Bar(ts=1_600_000_000 + i * 60, open=price,
                           high=max(price, close) * 1.001, low=min(price, close) * 0.999,
                           close=close, volume=100_000))
            price = close
        return out

    def test_crossing_the_spread_costs_more_than_resting(self):
        from trading import execution
        bars = self.bars()
        crossed = execution.execute(bars, "buy", 20_000, "market")
        rested = execution.execute(bars, "buy", 20_000, "passive")
        self.assertGreater(crossed["spread_paid_bps"], rested["spread_paid_bps"],
                           "a taker pays the spread; a maker is paid it")
        self.assertLessEqual(rested["spread_paid_bps"], 0.0)

    def test_a_bigger_sweep_gets_a_worse_price(self):
        """Measured inside ONE bar. Over many bars a large order is worked
        through a price that wanders back, which is a fact about the market
        rather than about the order; walking the book is the part that is
        always true."""
        from trading import execution
        one = self.bars(n=1)
        small = execution.execute(one, "buy", 5_000, "market")
        large = execution.execute(one, "buy", 120_000, "market")
        self.assertGreater(large["average_price"], small["average_price"])
        self.assertGreater(large["spread_paid_bps"], small["spread_paid_bps"])

    def test_not_trading_is_charged_for_in_a_trending_market(self):
        """Without an opportunity cost, an order that never fills looks
        free, and every comparison picks it."""
        from trading import execution
        rising = self.bars(drift=0.002)
        patient = execution.execute(rising, "buy", 500_000, "limit", limit_price=99.0)
        urgent = execution.execute(rising, "buy", 500_000, "market")
        self.assertLess(patient["fill_rate"], 0.5)
        self.assertGreater(patient["total_shortfall_bps"], urgent["total_shortfall_bps"],
                           "missing a rising market must cost more than crossing into it")

    def test_an_order_that_never_fills_reports_no_price(self):
        from trading import execution
        out = execution.execute(self.bars(), "buy", 1000, "limit", limit_price=1.0)
        self.assertEqual(out["fill_rate"], 0.0)
        self.assertIsNone(out["average_price"])
        self.assertIsNone(out["shortfall_bps"])
        self.assertIsNotNone(out["total_shortfall_bps"])   # not trading still has a cost

    def test_passive_fills_are_made_by_resting(self):
        from trading import execution
        out = execution.execute(self.bars(), "buy", 20_000, "passive")
        self.assertGreater(out["fill_rate"], 0.0)
        self.assertAlmostEqual(out["maker_share"], 1.0, places=6)

    def test_nothing_is_filled_beyond_what_was_asked(self):
        from trading import execution
        for algo in ("market", "twap", "vwap", "pov", "passive", "limit"):
            out = execution.execute(self.bars(), "buy", 30_000, algo)
            self.assertLessEqual(out["filled"], 30_000 + 1e-6, algo)

    def test_the_same_seed_gives_the_same_execution(self):
        from trading import execution
        bars = self.bars()
        a = execution.execute(bars, "buy", 50_000, "vwap", seed=3)
        b = execution.execute(bars, "buy", 50_000, "vwap", seed=3)
        self.assertEqual(a["average_price"], b["average_price"])


if __name__ == "__main__":
    unittest.main()
