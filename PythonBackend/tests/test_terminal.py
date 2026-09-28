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
        # Abbreviated above 100k so columns stay narrow in an 80-wide window.
        self.assertEqual(term.money(12.5), "12.50")
        self.assertEqual(term.money(4210.0), "4,210")
        self.assertEqual(term.money(112421.3), "112.4k")
        self.assertEqual(term.money(4.95e12, "$"), "$4,950.0bn")

    def test_nothing_a_panel_draws_is_wider_than_the_window(self):
        from trading import ui
        rows = ui.table(["symbol", "price", "change", "note"],
                        [["AAPL", "339.75", "+2.54%", "a long trailing note " * 4]],
                        width=80, trim=[3, 2])
        for line in rows:
            self.assertLessEqual(ui.width_of(line), 80)
        for line in ui.note("word " * 80, 80):
            self.assertLessEqual(ui.width_of(line), 80)
        for line in ui.chart([([1.0, 5.0, 3.0, 9.0], "█", "")], width=80, height=4):
            self.assertLessEqual(ui.width_of(line), 80)
        for line in ui.stats([(f"label{i}", f"value{i}") for i in range(9)], width=80):
            self.assertLessEqual(ui.width_of(line), 80)

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


class BarSizeTests(unittest.TestCase):
    """`bt sma NVDA 5m` is a day-trading backtest. It used to be a daily
    one: the bar size parsed as nothing at all and was silently dropped,
    which is the worst way for a tool to be wrong."""

    def parse(self, args):
        terminal = term.Terminal.__new__(term.Terminal)
        terminal.session = term.Session()
        return terminal._parse_run(args), terminal.session.interval

    def test_a_bar_size_is_recognised(self):
        for size in ("1m", "5m", "15m", "30m", "1h", "1d"):
            (_, symbol, _, _), interval = self.parse(["sma", "NVDA", size])
            self.assertEqual(interval, size)
            self.assertEqual(symbol, "NVDA")

    def test_it_defaults_to_daily(self):
        (_, _, _, _), interval = self.parse(["sma", "NVDA"])
        self.assertEqual(interval, "1d")

    def test_a_bar_size_is_not_mistaken_for_a_year_or_a_parameter(self):
        (_, _, start, params), interval = self.parse(["sma", "NVDA", "5m", "2015", "fast=10"])
        self.assertEqual(interval, "5m")
        self.assertEqual(start, "2015-01-01")
        self.assertEqual(params, {"fast": 10})

    def test_intraday_annualisation_uses_the_session_length(self):
        """A 5-minute Sharpe annualised with 252 would be wrong by a factor
        of nine. Equities get a 6.5-hour day, crypto a 24-hour one."""
        from trading.data import periods_per_year
        self.assertEqual(periods_per_year("NVDA", "1d"), 252)
        self.assertEqual(periods_per_year("NVDA", "5m"), 252 * 78)
        self.assertEqual(periods_per_year("BTC-USD", "5m"), 365 * 288)

    def test_the_remote_runner_is_told_the_bar_size(self):
        """appapi already read an interval out of the request; the terminal
        never put one in, so every run sent to the other account was daily
        whatever had been typed."""
        import re
        source = open(term.__file__.replace(".pyc", ".py")).read()
        for action in ("backtest", "sweep", "walk_forward", "risk"):
            match = re.search(r'\{"action": "' + action + r'".{0,320}?\}', source, re.S)
            self.assertIsNotNone(match, action)
            self.assertIn("interval", match.group(0), f"{action} payload drops the bar size")


class OpeningManyTests(unittest.TestCase):
    """Jugnu opening four terminals side by side, each running its own
    simulation -- the tiling arithmetic and the startup commands."""

    def test_windows_tile_without_overlapping_or_leaving_the_screen(self):
        from trading import remote
        screen = (0, 25, 1512, 900)
        for count in range(1, 9):
            places = remote.grid(count, screen)
            self.assertEqual(len(places), count)
            for left, top, right, bottom in places:
                self.assertGreater(right, left)
                self.assertGreater(bottom, top)
                self.assertGreaterEqual(left, screen[0])
                self.assertLessEqual(right, screen[2])
                self.assertLessEqual(bottom, screen[3])
            for i, a in enumerate(places):
                for b in places[i + 1:]:
                    apart = (min(a[2], b[2]) <= max(a[0], b[0])      # side by side
                             or min(a[3], b[3]) <= max(a[1], b[1]))  # or stacked
                    self.assertTrue(apart, f"{count} windows overlap: {a} {b}")

    def test_four_windows_are_two_by_two(self):
        from trading import remote
        places = remote.grid(4, (0, 0, 1000, 800))
        self.assertEqual(places[0], (0, 0, 500, 400))
        self.assertEqual(places[3], (500, 400, 1000, 800))

    def test_a_command_with_a_quote_cannot_break_out_of_the_shell(self):
        """A symbol is model-written text going into a shell command line.
        Quoted properly, the shell hands it over whole instead of running
        the second half of it."""
        import subprocess
        from trading import remote
        nasty = "bt sma AAPL'; echo pwned; echo '"
        seen = subprocess.run(["sh", "-c", f"printf %s {remote._shell_quote(nasty)}"],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(seen.stdout, nasty)
        self.assertNotIn("pwned", seen.stdout.replace("echo pwned", ""))

    def test_every_command_jugnu_is_told_about_exists(self):
        """The tool's docstring is the only place the model learns what the
        terminal understands. A command listed there that doesn't exist is a
        failure the user sees as `no command 'mc'`."""
        from tools.trading_tools import open_trading_terminal
        doc = open_trading_terminal.__doc__
        block = doc.split("Terminal commands you can pass")[1].split("Args:")[0]
        named = [line.split()[0] for line in block.splitlines()
                 if line.startswith("    ") and line.strip()]
        self.assertIn("bt", named)
        for name in named:
            self.assertIn(name, term.COMMANDS, f"the docstring promises `{name}`")

    def test_startup_commands_run_before_the_prompt(self):
        ran = []
        terminal = term.Terminal.__new__(term.Terminal)
        terminal.run_command = ran.append
        for command in ["bt sma AAPL 2015; help", "signals"]:
            for piece in command.split(";"):
                if piece.strip():
                    terminal.run_command(piece.strip())
        self.assertEqual(ran, ["bt sma AAPL 2015", "help", "signals"])


if __name__ == "__main__":
    unittest.main()
