"""The Jugnu terminal: the strategy checker that has to hold (the model
writes code and this is what stops it mattering), the watchlist, the
explanations, and the command table.

No network and no model here -- the two model-backed paths are exercised by
hand; what's tested is everything that must be true before they run."""

import _env  # noqa: F401  (isolates data dir; must be first)

import unittest
from pathlib import Path

from trading import authoring, explain, quotes, strategies
from trading import terminal as term
from trading.analysis import backtest
from trading.data import Bar

GOOD = '''
DEFAULTS = {}


def strategy(ctx):
    length = int(ctx.param("length", 14))
    if len(ctx.closes) < length + 1:
        return 0.0
    value = rsi(ctx.closes, length)
    if ctx.position and ctx.entry_price and ctx.bar.close < ctx.entry_price * 0.98:
        return 0.0
    return 1.0 if value < ctx.param("buy_below", 30) else 0.0
'''


def bars_from(prices, start_ts=1_600_000_000):
    return [Bar(start_ts + i * 86_400, p, p, p, p, 1000.0) for i, p in enumerate(prices)]


class StrategyCheckerTests(unittest.TestCase):
    """Everything here is a way a generated strategy could reach outside its
    box. Each one must be refused before it runs, not after."""

    def rejected(self, body: str) -> str:
        source = f"def strategy(ctx):\n{body}\n"
        with self.assertRaises(authoring.StrategyError) as caught:
            authoring.check(source)
        return str(caught.exception)

    def test_accepts_an_ordinary_strategy_and_finds_its_parameters(self):
        params = authoring.check(GOOD)
        self.assertEqual(params["length"], 14)
        self.assertEqual(params["buy_below"], 30)

    def test_no_imports(self):
        self.assertIn("imports", self.rejected("    import os\n    return 0.0"))
        self.assertIn("imports", self.rejected("    from os import system\n    return 0.0"))

    def test_no_file_or_process_access(self):
        self.assertIn("open()", self.rejected("    open('/etc/passwd')\n    return 0.0"))
        self.assertIn("eval()", self.rejected("    eval('1+1')\n    return 0.0"))
        self.assertIn("exec()", self.rejected("    exec('x=1')\n    return 0.0"))

    def test_no_dunder_escapes(self):
        problem = self.rejected("    return ctx.__class__.__mro__[0]")
        self.assertTrue("__class__" in problem or "isn't available" in problem)

    def test_no_unknown_attributes(self):
        self.assertIn("isn't available", self.rejected("    return ctx.broker.submit()"))

    def test_no_unbounded_loops(self):
        self.assertIn("while", self.rejected("    while True:\n        pass\n    return 0.0"))

    def test_syntax_errors_are_explained(self):
        self.assertIn("isn't valid Python", self.rejected("    return ("))

    def test_it_runs_with_nothing_dangerous_in_scope(self):
        function, _ = authoring.compile_strategy(GOOD)
        result = backtest(bars_from([100 - i % 7 for i in range(80)]), "hold", symbol="TEST")
        self.assertIsNotNone(result)          # engine still fine
        self.assertTrue(callable(function))

    def test_a_checked_strategy_actually_backtests(self):
        spec = authoring.install("probe", GOOD, authoring.compile_strategy(GOOD)[0],
                                 {"length": 14, "buy_below": 30}, "test strategy")
        try:
            self.assertIn("probe", strategies.REGISTRY)
            result = backtest(bars_from([100 + (i % 11) * 2 for i in range(200)]), spec.name,
                              symbol="TEST")
            self.assertGreaterEqual(result.metrics["trades"], 0)
        finally:
            strategies.REGISTRY.pop(spec.name, None)
            Path(authoring.CUSTOM_DIR / f"{spec.name}.py").unlink(missing_ok=True)

    def test_reloading_saved_strategies_registers_them(self):
        spec = authoring.install("reloadme", GOOD, authoring.compile_strategy(GOOD)[0], {}, "x")
        try:
            strategies.REGISTRY.pop(spec.name, None)
            self.assertIn("reloadme", authoring.load_saved())
            self.assertIn("reloadme", strategies.REGISTRY)
        finally:
            strategies.REGISTRY.pop("reloadme", None)
            Path(authoring.CUSTOM_DIR / "reloadme.py").unlink(missing_ok=True)


class RenderingTests(unittest.TestCase):
    def test_fit_measures_visible_width_not_escape_codes(self):
        coloured = f"{term.CLAY}hello{term.RESET}"
        self.assertEqual(term.visible(coloured), 5)
        self.assertEqual(term.visible(term.fit(coloured, 12)), 12)
        self.assertEqual(term.visible(term.fit("abcdefghij", 5)), 5)

    def test_percent_is_coloured_by_direction(self):
        self.assertIn(term.GREEN, term.pct(1.2))
        self.assertIn(term.RED, term.pct(-1.2))
        self.assertNotIn(term.GREEN, term.pct(1.2, colour=False))

    def test_money_shortens_large_numbers(self):
        self.assertEqual(term.money(12.5), "12.50")
        self.assertEqual(term.money(112421.3), "112,421")

    def test_every_command_maps_to_a_method(self):
        for name, handler in term.COMMANDS.items():
            self.assertTrue(callable(handler), name)

    def test_an_unknown_command_suggests_something(self):
        terminal = term.Terminal()
        terminal.run_command("swep sma AAPL")
        text = "\n".join(terminal.session.lines)
        self.assertIn("no command", text)
        self.assertIn("sweep", text)

    def test_typing_and_history(self):
        terminal = term.Terminal()
        for char in "bt sma":
            terminal.key(char)
        self.assertEqual(terminal.buffer, "bt sma")
        terminal.key("\x7f")
        self.assertEqual(terminal.buffer, "bt sm")
        terminal.key("\x15")
        self.assertEqual(terminal.buffer, "")
        terminal.session.history.append("bt sma AAPL")
        terminal._recall(-1)
        self.assertEqual(terminal.buffer, "bt sma AAPL")


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.result = backtest(bars_from([100 + i * 0.5 for i in range(600)]), "sma",
                               {"fast": 5, "slow": 20}, symbol="TEST")

    def test_plain_english_names_the_benchmark_and_the_costs(self):
        text = " ".join(explain.plain(self.result))
        self.assertIn("buying and holding", text)
        self.assertIn("slippage", text)
        self.assertNotIn("Sharpe ratio of", text)      # no unexplained jargon

    def test_crypto_years_are_counted_at_365_not_252(self):
        crypto = backtest(bars_from([100 + i for i in range(365)]), "hold", symbol="BTC-USD")
        equity = backtest(bars_from([100 + i for i in range(365)]), "hold", symbol="AAPL")
        self.assertAlmostEqual(explain._years(crypto), 1.0, places=1)
        self.assertAlmostEqual(explain._years(equity), 1.45, places=1)

    def test_sweep_wording_changes_when_nothing_beat_holding(self):
        none_won = explain.plain_sweep({"runs": 40, "robustness": 0.0, "median_return": -0.1,
                                        "benchmark_return": 0.9, "best": {"return": 0.2}})
        self.assertIn("Not one combination", " ".join(none_won))
        broad = explain.plain_sweep({"runs": 40, "robustness": 0.8, "median_return": 1.2,
                                     "benchmark_return": 0.9, "best": {"return": 1.6}})
        self.assertIn("broad", " ".join(broad))

    def test_glossary_defines_the_words_it_prints(self):
        for word in ("sharpe", "drawdown", "slippage", "robustness"):
            self.assertIsNotNone(explain.term(word))
        self.assertIsNone(explain.term("moon"))


class WatchlistTests(unittest.TestCase):
    def test_add_and_remove(self):
        quotes.watch_add("PLTR")
        self.assertIn("PLTR", quotes.watchlist())
        quotes.watch_remove("PLTR")
        self.assertNotIn("PLTR", quotes.watchlist())

    def test_quote_of_an_unknown_symbol_fails_softly(self):
        quote = quotes.Quote(symbol="ZZZZ", error="not found")
        self.assertEqual(quote.volume_ratio, 0.0)
        self.assertTrue(quote.up)          # a zero change counts as flat, not a crash


if __name__ == "__main__":
    unittest.main()
