"""
terminal.py -- the Jugnu terminal. Type a command, get an answer.

    python -m trading term

A full screen with live markets at the top, your work in the middle, and a
command line at the bottom. No mouse, no menus, no dependencies: raw ANSI,
so it works over SSH and starts instantly.

Two things make it Jugnu's rather than another ticker:

  every result explains itself, in plain words, with no model call --
  a number you can't interpret is worse than no number; and

  `build buy BTC when RSI drops under 30` writes a real strategy file,
  checks it, runs it, and then argues with the result.

Commands that touch the model (`ask`, `why`, `explain`, `build`) say so
before they run. Everything else is local arithmetic and takes milliseconds.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import sys
import termios
import threading
import time
import tty
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import (alerts, analysis, authoring, explain, jobs, metrics, paper, portfolio, quotes,
               remote, render, risk, screen, sources, store, strategies)
from .data import DataError, load

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[38;5;244m"
FAINT = "\x1b[38;5;240m"
INK = "\x1b[38;5;253m"
CLAY = "\x1b[38;5;209m"
GREEN = "\x1b[38;5;114m"
RED = "\x1b[38;5;203m"
AMBER = "\x1b[38;5;179m"
BAR = "\x1b[48;5;236m"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def visible(text: str) -> int:
    return len(_ANSI.sub("", text))


def fit(text: str, width: int) -> str:
    """Pad or truncate to an exact visible width, keeping colours intact."""
    length = visible(text)
    if length <= width:
        return text + " " * (width - length)
    out, count = "", 0
    i = 0
    while i < len(text) and count < width - 1:
        if text[i] == "\x1b":
            end = text.find("m", i)
            if end == -1:
                break
            out += text[i:end + 1]
            i = end + 1
            continue
        out += text[i]
        count += 1
        i += 1
    return out + "…" + RESET


def money(value: float, width: int = 0) -> str:
    text = f"{value:,.2f}" if abs(value) < 1000 else f"{value:,.0f}"
    return text.rjust(width) if width else text


def pct(value: float, width: int = 0, colour: bool = True) -> str:
    text = f"{value:+.2f}%"
    text = text.rjust(width) if width else text
    if not colour:
        return text
    return (GREEN if value >= 0 else RED) + text + RESET


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class Session:
    lines: list[str] = field(default_factory=list)      # the work area, newest last
    scroll: int = 0
    symbol: str = "AAPL"
    strategy: str = "sma"
    last_result = None
    last_sweep: dict | None = None
    busy: str = ""
    status: str = ""
    history: list[str] = field(default_factory=list)
    last_portfolio: dict | None = None
    where: str = "local"          # where the next heavy command runs


class Terminal:
    REFRESH = 10.0            # seconds between quote refreshes

    def __init__(self):
        self.session = Session()
        self.buffer = ""
        self.history_index = 0
        self.running = True
        self.jobs = ThreadPoolExecutor(max_workers=3, thread_name_prefix="term")
        self.runner = jobs.Runner(workers=4, on_change=self._job_changed)
        self.lock = threading.Lock()
        # Which job the calling thread belongs to. Several run at once, so
        # "everything written since I started" is not a thing -- output has
        # to be attributed as it happens.
        self.current = threading.local()
        self.last_alert_check = 0.0
        self.dirty = True
        self.last_refresh = 0.0
        self.markets = list(quotes.DEFAULT_MARKETS)

    # -- output ------------------------------------------------------------

    def write(self, *lines: str) -> None:
        entry = getattr(self.current, "job", None)
        with self.lock:
            for line in lines:
                pieces = str(line).split("\n")
                self.session.lines.extend(pieces)
                if entry is not None:
                    entry.lines.extend(pieces)
            del self.session.lines[:-2000]
            self.session.scroll = 0
            self.dirty = True

    def para(self, text: str, indent: str = "  ", colour: str = DIM) -> None:
        """Wrap prose to the window instead of letting fit() cut it off."""
        import textwrap
        for line in textwrap.wrap(text, max(40, _width() - len(indent) - 2)) or [""]:
            self.write(f"{indent}{colour}{line}{RESET}")

    def rule(self, label: str = "") -> None:
        width = shutil.get_terminal_size((100, 30)).columns - 2
        text = f"{DIM}{'─' * 2} {CLAY}{label}{RESET} " if label else f"{DIM}{'─' * 4}"
        self.write(fit(text, width).rstrip() + f"{DIM}" + "─" * max(0, width - visible(text)) + RESET)

    def status(self, message: str) -> None:
        self.session.status = message
        self.dirty = True

    # -- the frame ---------------------------------------------------------

    def draw(self) -> None:
        size = shutil.get_terminal_size((100, 30))
        width, height = max(72, size.columns), max(20, size.lines)
        rows: list[str] = []
        rows.append(self._title(width))
        rows += self._markets(width)
        strip = self._jobs_strip(width)
        if strip:
            rows.append(strip)
        rows.append(f"{FAINT}{'─' * width}{RESET}")

        work_height = height - len(rows) - 2
        with self.lock:
            lines = list(self.session.lines)
            scroll = self.session.scroll
        end = len(lines) - scroll
        window = lines[max(0, end - work_height):end]
        rows += [fit(line, width) for line in window]
        rows += [" " * width] * max(0, work_height - len(window))

        rows.append(self._status(width))
        rows.append(self._prompt(width))
        sys.stdout.write("\x1b[H" + "\n".join(fit(r, width) for r in rows[:height]))
        sys.stdout.write("\x1b[J")
        # Park the cursor at the end of what's typed.
        sys.stdout.write(f"\x1b[{height};{min(width, visible(self._prompt(width, cursor=True)) + 1)}H")
        sys.stdout.flush()

    def _title(self, width: int) -> str:
        import llm_client
        clock = time.strftime("%a %d %b  %H:%M:%S")
        mode = llm_client.model_mode()
        left = f"{BAR}{BOLD}{CLAY} JUGNU TERMINAL {RESET}{BAR}{DIM} paper · simulation only{RESET}"
        right = f"{BAR}{DIM}{mode}  {clock} {RESET}"
        gap = max(1, width - visible(left) - visible(right))
        return left + BAR + " " * gap + right

    def _markets(self, width: int) -> list[str]:
        watch = quotes.watchlist()
        market_quotes = [quotes.cached(s) or quotes.Quote(symbol=s) for s in self.markets]
        watch_quotes = [quotes.cached(s) or quotes.Quote(symbol=s) for s in watch]
        half = width // 2 - 1
        left = [f"{DIM}MARKETS{RESET}"] + [self._quote_line(q, half) for q in market_quotes]
        right = [f"{DIM}WATCHLIST{RESET}"] + [self._quote_line(q, half, volume=True) for q in watch_quotes]
        rows = []
        for i in range(max(len(left), len(right))):
            a = left[i] if i < len(left) else ""
            b = right[i] if i < len(right) else ""
            rows.append(fit(a, half) + f"{FAINT}│{RESET}" + fit(b, width - half - 1))
        return rows

    def _jobs_strip(self, width: int) -> str:
        """One line showing what's running, so several simulations at once
        are visible rather than implied."""
        entries = [j for j in self.runner.all() if not j.done or j.seconds < 20]
        if not entries:
            return ""
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 10) % 10]
        cells = []
        for entry in entries[-5:]:
            mark = (f"{CLAY}{spin}{RESET}" if not entry.done
                    else (f"{GREEN}✓{RESET}" if entry.status == "done" else f"{RED}✗{RESET}"))
            place = f"{FAINT}@jugnu{RESET}" if entry.where == "jugnu" else ""
            cells.append(f"{mark} {FAINT}[{entry.id}]{RESET} {DIM}{entry.label[:30]}{RESET}"
                         f" {FAINT}{entry.seconds:.0f}s{RESET}{place}")
        return " " + "  ".join(cells)

    def _quote_line(self, quote: quotes.Quote, width: int, volume: bool = False) -> str:
        if quote.error:
            return f"{quote.symbol:<9}{DIM}unavailable{RESET}"
        if not quote.price:
            return f"{quote.symbol:<9}{FAINT}…{RESET}"
        arrow = f"{GREEN}▲{RESET}" if quote.up else f"{RED}▼{RESET}"
        line = f"{INK}{quote.symbol:<9}{RESET}{money(quote.price):>10}  {pct(quote.change_pct, 7)} {arrow}"
        if volume and quote.volume_ratio:
            flag = f" {AMBER}⚠{RESET}" if quote.volume_ratio >= 2 else ""
            line += f"  {DIM}vol {quote.volume_ratio:.1f}×{RESET}{flag}"
        return fit(line, width)

    def _status(self, width: int) -> str:
        if self.session.busy:
            spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 10) % 10]
            text = f"{CLAY}{spin} {self.session.busy}{RESET}"
        else:
            text = f"{DIM}{self.session.status or 'ready'}{RESET}"
        right = f"{FAINT}F1 help · ↑ history · ^C quit{RESET}"
        gap = max(1, width - visible(text) - visible(right) - 2)
        return " " + text + " " * gap + right + " "

    def _prompt(self, width: int, cursor: bool = False) -> str:
        return f"{CLAY}> {RESET}{INK}{self.buffer}{RESET}" if not cursor else f"> {self.buffer}"

    # -- input -------------------------------------------------------------

    def key(self, char: str) -> None:
        if char in ("\r", "\n"):
            command, self.buffer = self.buffer.strip(), ""
            self.history_index = 0
            if command:
                self.session.history.append(command)
                self.run_command(command)
        elif char in ("\x7f", "\b"):
            self.buffer = self.buffer[:-1]
        elif char == "\x15":                      # ctrl-U
            self.buffer = ""
        elif char == "\x0c":                      # ctrl-L
            with self.lock:
                self.session.lines.clear()
        elif char == "\x1b":                      # escape sequence
            rest = _read_escape()
            if rest == "[A":
                self._recall(-1)
            elif rest == "[B":
                self._recall(1)
            elif rest in ("[5~", "[6~"):
                with self.lock:
                    step = 10 if rest == "[5~" else -10
                    self.session.scroll = max(0, min(len(self.session.lines) - 5,
                                                     self.session.scroll + step))
            elif rest in ("OP", "[11~"):          # F1
                self.cmd_help([])
        elif char.isprintable():
            self.buffer += char
        self.dirty = True

    def _recall(self, direction: int) -> None:
        history = self.session.history
        if not history:
            return
        self.history_index = max(-len(history), min(0, self.history_index + direction))
        self.buffer = "" if self.history_index == 0 else history[self.history_index]

    # -- running -----------------------------------------------------------

    def background(self, label: str, fn) -> None:
        """Quick work (a quote, a chart): runs off the input loop so the
        terminal never freezes, but isn't worth a job entry."""
        def task():
            self.session.busy = label
            self.dirty = True
            try:
                fn()
            except (DataError, ValueError, KeyError, authoring.StrategyError,
                    remote.RemoteError) as e:
                self.write(f"{RED}{e}{RESET}")
            except Exception as e:                # a bug here must not kill the terminal
                self.write(f"{RED}{type(e).__name__}: {e}{RESET}")
            finally:
                self.session.busy = ""
                self.dirty = True
        self.jobs.submit(task)

    def job(self, label: str, fn, *, where: str | None = None) -> None:
        """Heavy work becomes a numbered job: the prompt comes straight back,
        several can run at once, and `job N` re-prints what it produced."""
        where = where or self.session.where
        def work(entry):
            self.current.job = entry
            try:
                fn(entry)
            finally:
                self.current.job = None
        entry = self.runner.submit(label, work, where=where)
        self.write(f"{FAINT}[{entry.id}] {label}"
                   + (f" · on the Jugnu account" if where == "jugnu" else "") + f"{RESET}")

    def _job_changed(self, entry) -> None:
        if entry.status == "failed":
            self.write(f"{RED}[{entry.id}] {entry.label}: {entry.error}{RESET}")
        elif entry.status == "done" and entry.seconds > 3:
            self.status(f"[{entry.id}] {entry.label} finished in {entry.seconds:.0f}s")
        self.dirty = True

    def refresh_quotes(self) -> None:
        symbols = self.markets + quotes.watchlist()
        self.jobs.submit(lambda: (quotes.many(symbols, max_age=self.REFRESH - 1), self._touch()))

    def _touch(self) -> None:
        self.dirty = True

    def loop(self) -> None:
        old = termios.tcgetattr(sys.stdin)
        try:
            tty.setraw(sys.stdin.fileno())
            sys.stdout.write("\x1b[?1049h\x1b[?25h\x1b[2J")
            self.banner()
            while self.running:
                if time.time() - self.last_refresh > self.REFRESH:
                    self.last_refresh = time.time()
                    self.refresh_quotes()
                if time.time() - self.last_alert_check > 60:
                    self.last_alert_check = time.time()
                    self.jobs.submit(self._check_alerts)
                if self.dirty or self.session.busy:
                    self.dirty = False
                    self.draw()
                ready, _, _ = select.select([sys.stdin], [], [], 0.12)
                if ready:
                    char = sys.stdin.read(1)
                    if char == "\x03":            # ctrl-C
                        break
                    self.key(char)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
            sys.stdout.write("\x1b[?1049l\x1b[?25h")
            sys.stdout.flush()

    def _check_alerts(self) -> None:
        for fired in alerts.check():
            self.write(f"{AMBER}⚑ alert{RESET} {INK}{alerts.describe(fired)}{RESET}")
            alerts.notify(fired)

    def banner(self) -> None:
        loaded = authoring.load_saved()
        self.write(
            f"{BOLD}{CLAY}Jugnu terminal{RESET}  {DIM}paper trading and research. Nothing here can place a real order.{RESET}",
            "",
            f"  {INK}bt sma AAPL 2015{RESET}      {DIM}backtest, with buy & hold beside it{RESET}",
            f"  {INK}build buy BTC when RSI drops under 30, sell above 60{RESET}",
            f"                       {DIM}writes a real strategy, checks it, runs it{RESET}",
            f"  {INK}pf sma:AAPL, breakout:BTC-USD{RESET}  {DIM}a portfolio of both, rebalanced{RESET}",
            f"  {INK}risk sma AAPL{RESET}        {DIM}drawdowns, VaR, beta, year by year{RESET}",
            f"  {INK}screen rsi<35{RESET}        {DIM}scan your watchlist{RESET}",
            f"  {INK}on jugnu{RESET}             {DIM}run the heavy work in the other account{RESET}",
            f"  {INK}help{RESET}                 {DIM}everything else{RESET}",
            "")
        if loaded:
            self.write(f"{DIM}loaded your strategies: {', '.join(loaded)}{RESET}", "")

    # -- commands ----------------------------------------------------------

    def run_command(self, command: str) -> None:
        self.write(f"{FAINT}> {command}{RESET}")
        name, *rest = command.split()
        name = name.lower()
        handler = COMMANDS.get(name)
        if handler is None:
            near = ", ".join(sorted(k for k in COMMANDS if k.startswith(name[:2]))[:5])
            self.write(f"{RED}no command '{name}'{RESET}" + (f"{DIM} — did you mean: {near}{RESET}" if near else ""))
            return
        handler(self, rest)

    # each command is a method; COMMANDS maps names to them at the bottom

    def cmd_help(self, args: list[str]) -> None:
        groups = [
            ("prices", [
                ("q SYM", "quote, day's range, sparkline"),
                ("ch SYM [days]", "price chart"),
                ("wl [add|rm] SYM", "watchlist"),
                ("mkt [SYM…]", "what sits in the markets panel"),
                ("screen [filters]", "scan the watchlist: `screen rsi<35 vol>2`"),
                ("corr [SYM…]", "how they move together"),
                ("alert NVDA < 200", "tell me when · `alert` lists, `unalert N` removes"),
            ]),
            ("research", [
                ("bt STRAT SYM [year]", "backtest against buy & hold"),
                ("vs STRAT STRAT SYM", "several strategies side by side"),
                ("pf sma:AAPL, rsi:SPY", "portfolio of sleeves, rebalanced"),
                ("risk STRAT SYM", "drawdowns, VaR, beta, year by year, trade stats"),
                ("sweep STRAT SYM", "every parameter combination as a heatmap"),
                ("wf STRAT SYM [folds]", "walk forward: tune, then trade blind"),
                ("costs STRAT SYM", "what slippage does to it"),
                ("replay STRAT SYM", "watch it trade, bar by bar"),
                ("trades · runs · y", "the trade list, saved runs, the year table"),
            ]),
            ("paper", [
                ("paper start STRAT SYM", "a simulated account on live prices"),
                ("paper [step|stop ID]", "update, or close one"),
            ]),
            ("strategies", [
                ("strats · show NAME", "what exists, and its source"),
                ("build DESCRIPTION", "write one from a sentence            ·model"),
            ]),
            ("asking", [
                ("ask QUESTION", "interrogate the last result          ·model"),
                ("why SYM", "what moved it today                  ·model"),
                ("explain [term]", "plain English, free · `explain sharpe`"),
            ]),
            ("elsewhere", [
                ("on jugnu / on me", "run the heavy work in the other account"),
                ("jobs · job N", "what's running · re-print one"),
                ("fa SYM · opt SYM", "fundamentals, options (needs OpenBB)"),
                ("source [install]", "where data comes from"),
            ]),
        ]
        for title, rows in groups:
            self.rule(title)
            for left, right in rows:
                self.write(f"  {INK}{left:<24}{RESET}{DIM}{right}{RESET}")
        self.write("")
        self.para("·model marks the four commands that spend a model call. Everything else is "
                  "local arithmetic. Heavy commands become numbered jobs, so you can start several "
                  "and keep working.")
        self.write(f"  {FAINT}^L clears · ^C quits · ↑ history · PgUp/PgDn scrolls{RESET}")

    def cmd_quote(self, args: list[str]) -> None:
        symbol = (args[0] if args else self.session.symbol).upper()
        self.session.symbol = symbol
        self.background(f"{symbol}…", lambda: self._quote(symbol))

    def _quote(self, symbol: str) -> None:
        quote = quotes.one(symbol, max_age=2)
        if quote.error:
            self.write(f"{RED}{symbol}: {quote.error}{RESET}")
            return
        self.rule(symbol)
        arrow = f"{GREEN}▲{RESET}" if quote.up else f"{RED}▼{RESET}"
        self.write(f"  {BOLD}{INK}{money(quote.price)}{RESET} {arrow}  {pct(quote.change_pct)}"
                   f"  {DIM}({quote.change:+,.2f}){RESET}")
        if quote.open:
            self.write(f"  {DIM}open{RESET} {money(quote.open)}   {DIM}high{RESET} {money(quote.high)}"
                       f"   {DIM}low{RESET} {money(quote.low)}   {DIM}volume{RESET} {quote.volume:,.0f}"
                       + (f"  {AMBER}{quote.volume_ratio:.1f}× normal{RESET}"
                          if quote.volume_ratio >= 1.8 else ""))
        if quote.spark:
            self.write(f"  {CLAY}{render.sparkline(quote.spark, 64)}{RESET}  {DIM}recent{RESET}")

    def cmd_chart(self, args: list[str]) -> None:
        symbol = (args[0] if args else self.session.symbol).upper()
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 180
        self.session.symbol = symbol
        def work():
            bars = load(symbol)[-days:]
            closes = [b.close for b in bars]
            self.rule(f"{symbol} · {len(bars)} days")
            self.write(_price_chart(closes, bars[0].date, bars[-1].date, width=_width() - 12))
            change = closes[-1] / closes[0] - 1
            self.write(f"  {DIM}{bars[0].date} → {bars[-1].date}{RESET}   {pct(change * 100)}"
                       f"   {DIM}low{RESET} {money(min(closes))}  {DIM}high{RESET} {money(max(closes))}")
        self.background(f"{symbol} chart…", work)

    def cmd_watchlist(self, args: list[str]) -> None:
        if args and args[0] in ("add", "+"):
            for symbol in args[1:]:
                quotes.watch_add(symbol)
        elif args and args[0] in ("rm", "remove", "-"):
            for symbol in args[1:]:
                quotes.watch_remove(symbol)
        self.write(f"{DIM}watchlist: {', '.join(quotes.watchlist())}{RESET}")
        self.refresh_quotes()

    def cmd_markets(self, args: list[str]) -> None:
        if args:
            self.markets = [a.upper() for a in args][:6]
        self.refresh_quotes()
        self.write(f"{DIM}markets: {', '.join(self.markets)}{RESET}")

    # -- research ----------------------------------------------------------

    def _parse_run(self, args: list[str]) -> tuple[str, str, str, dict]:
        strategy = args[0] if args else self.session.strategy
        symbol = (args[1] if len(args) > 1 else self.session.symbol).upper()
        start = ""
        params: dict = {}
        for token in args[2:]:
            if "=" in token:
                key, value = token.split("=", 1)
                try:
                    params[key] = int(value)
                except ValueError:
                    try:
                        params[key] = float(value)
                    except ValueError:
                        params[key] = value
            elif re.fullmatch(r"\d{4}", token):
                start = f"{token}-01-01"
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
                start = token
        self.session.strategy, self.session.symbol = strategy, symbol
        return strategy, symbol, start, params

    def cmd_backtest(self, args: list[str]) -> None:
        strategy, symbol, start, params = self._parse_run(args)
        if self.session.where == "jugnu":
            self._remote({"action": "backtest", "symbol": symbol, "strategy": strategy,
                          "start": start, "params": params},
                         f"bt {strategy} {symbol}", self._show_json_result)
            return

        def work(entry):
            bars = load(symbol, start=start or None)
            result = analysis.backtest(bars, strategy, params, symbol=symbol)
            costs = analysis.cost_sensitivity(bars, strategy, params, symbol=symbol)
            notes = metrics.warnings(result.metrics, result.benchmark_metrics, cost_curve=costs,
                                     bars=len(bars), trades=result.metrics["trades"])
            self.session.last_result = result
            store.save_run(result, notes)
            self._show_result(result, notes, costs)
        self.job(f"bt {strategy} {symbol}", work)

    def _show_json_result(self, reply: dict) -> None:
        """A result that came back from the Jugnu account: same layout, from
        JSON rather than objects."""
        mine, theirs = reply["metrics"], reply["benchmark_metrics"]
        won = mine["total_return"] > theirs["total_return"]
        self.rule(f"{reply['symbol']} · {reply['strategy']} {FAINT}(ran in the Jugnu account){RESET}")
        self.write(
            f"  {DIM}{reply['from']} → {reply['to']}   {reply['slippage_bps']:g}bps slippage{RESET}",
            f"  {BOLD}strategy{RESET} {(GREEN if won else INK)}{mine['total_return'] * 100:+.1f}%{RESET}"
            f"    {DIM}buy & hold{RESET} {(INK if won else GREEN)}{theirs['total_return'] * 100:+.1f}%"
            f"{RESET}    {DIM}ends at{RESET} ${mine['final']:,.0f}",
            f"  {DIM}sharpe{RESET} {mine['sharpe']:.2f}   {DIM}max drawdown{RESET} "
            f"{RED}{mine['max_drawdown'] * 100:.1f}%{RESET}   {DIM}trades{RESET} {mine['trades']}")
        for note in reply.get("warnings", []):
            self.write(f"  {AMBER}⚠ {note}{RESET}")
        if reply.get("equity") and reply.get("benchmark"):
            self.write("")
            self.write(_plot([(reply["benchmark"], "·", FAINT), (reply["equity"], "█", CLAY)],
                             9, _width() - 12, reply["dates"][0], reply["dates"][-1]))
            self.write(f"{' ' * 11}{CLAY}█ strategy{RESET}   {FAINT}· buy & hold{RESET}")
        curve = reply.get("cost_curve") or {}
        if curve:
            cells = "   ".join(f"{DIM}{level}bps{RESET} "
                               + (GREEN if value > 0 else RED) + f"{value * 100:+.0f}%" + RESET
                               for level, value in sorted(curve.items(), key=lambda kv: float(kv[0])))
            self.write("", f"  {DIM}at other slippage:{RESET} {cells}")

    def _show_result(self, result, notes: list[str], costs: dict | None = None) -> None:
        mine, theirs = result.metrics, result.benchmark_metrics
        self.rule(f"{result.symbol} · {result.strategy}"
                  f"({' '.join(f'{k}={v}' for k, v in result.params.items())})")
        won = mine["total_return"] > theirs["total_return"]
        self.write(
            f"  {DIM}{result.dates[0]} → {result.dates[-1]}   "
            f"{result.slippage_bps:g}bps slippage, ${result.commission:g}/trade{RESET}",
            f"  {BOLD}strategy{RESET} {(GREEN if won else INK)}{mine['total_return'] * 100:+.1f}%{RESET}"
            f"    {DIM}buy & hold{RESET} {(INK if won else GREEN)}{theirs['total_return'] * 100:+.1f}%{RESET}"
            f"    {DIM}ends at{RESET} ${mine['final']:,.0f}")
        self.write(f"  {DIM}sharpe{RESET} {mine['sharpe']:.2f} {FAINT}vs {theirs['sharpe']:.2f}{RESET}"
                   f"   {DIM}max drawdown{RESET} {RED}{mine['max_drawdown'] * 100:.1f}%{RESET}"
                   f"   {DIM}trades{RESET} {mine['trades']}"
                   + (f"   {DIM}win rate{RESET} {mine['win_rate'] * 100:.0f}%" if mine["win_rate"] else "")
                   + f"   {DIM}invested{RESET} {mine['exposure'] * 100:.0f}%")
        for note in notes:
            self.write(f"  {AMBER}⚠ {note}{RESET}")
        self.write("")
        self.write(_equity_chart(result, width=_width() - 12))
        if costs:
            cells = "   ".join(f"{DIM}{level:g}bps{RESET} "
                               + (GREEN if value > 0 else RED) + f"{value * 100:+.0f}%" + RESET
                               for level, value in sorted(costs.items()))
            self.write("", f"  {DIM}at other slippage:{RESET} {cells}")
        self.write("")
        for line in explain.plain(result):
            self.para(line)
        self.write("", f"  {FAINT}`explain` for more, `ask <question>` to interrogate it, "
                       f"`trades` for the list{RESET}")

    def cmd_sweep(self, args: list[str]) -> None:
        strategy, symbol, start, _ = self._parse_run(args)
        if self.session.where == "jugnu":
            def show(reply):          # not `render`: that's the module this file uses
                self.rule(f"{reply['symbol']} · {reply['strategy']} · {reply['runs']} combinations "
                          f"{FAINT}(Jugnu account){RESET}")
                best = reply["best"]
                self.write(f"  {DIM}best{RESET} "
                           + " ".join(f"{k}={v}" for k, v in best["params"].items())
                           + f"   {GREEN}{best['return'] * 100:+.1f}%{RESET}"
                           f"   {DIM}sharpe{RESET} {best['sharpe']:.2f}")
                for line in explain.plain_sweep(reply):
                    self.para(line)
            self._remote({"action": "sweep", "symbol": symbol, "strategy": strategy, "start": start},
                         f"sweep {strategy} {symbol}", show)
            return

        def work(entry):
            bars = load(symbol, start=start or None)
            result = analysis.sweep(bars, strategy, symbol=symbol)
            self.session.last_sweep = result
            self.rule(f"{symbol} · {strategy} · {result['runs']} combinations")
            self.write(render.heatmap(result))
            best = result["best"]
            self.write("", f"  {DIM}best{RESET} "
                           f"{' '.join(f'{k}={v}' for k, v in best['params'].items())}"
                           f"   {GREEN}{best['return'] * 100:+.1f}%{RESET}"
                           f"   {DIM}sharpe{RESET} {best['sharpe']:.2f}")
            for line in explain.plain_sweep(result):
                self.para(line)
        self.job(f"sweep {strategy} {symbol}", work)

    def cmd_walkforward(self, args: list[str]) -> None:
        strategy, symbol, start, _ = self._parse_run(args)
        folds = next((int(a) for a in args[2:] if a.isdigit() and len(a) <= 2), 4)
        if self.session.where == "jugnu":
            def show(reply):
                self.rule(f"{reply['symbol']} · walk forward {FAINT}(Jugnu account){RESET}")
                for fold in reply["folds"]:
                    self.write(f"  {DIM}{fold['from']} → {fold['to']}{RESET}   "
                               f"{DIM}tuned{RESET} {fold['in_sample_return'] * 100:+7.1f}%   "
                               f"{BOLD}blind{RESET} {fold['out_of_sample_return'] * 100:+7.1f}%   "
                               f"{DIM}hold{RESET} {fold['benchmark_return'] * 100:+7.1f}%")
                self.write("", f"  {BOLD}out of sample{RESET} "
                               f"{reply['out_of_sample_return'] * 100:+.1f}%"
                               f"   {DIM}buy & hold{RESET} {reply['benchmark_return'] * 100:+.1f}%")
            self._remote({"action": "walk_forward", "symbol": symbol, "strategy": strategy,
                          "start": start, "folds": folds},
                         f"wf {strategy} {symbol}", show)
            return
        def work(entry):
            bars = load(symbol, start=start or None)
            result = analysis.walk_forward(bars, strategy, symbol=symbol, folds=folds)
            self.rule(f"{symbol} · {strategy} · walk forward")
            for fold in result["folds"]:
                params = " ".join(f"{k}={v}" for k, v in fold["params"].items())
                self.write(f"  {DIM}{fold['from']} → {fold['to']}{RESET}  {params:<24}"
                           f"{DIM}tuned{RESET} {fold['in_sample_return'] * 100:+7.1f}%   "
                           f"{BOLD}blind{RESET} "
                           + (GREEN if fold["out_of_sample_return"] > 0 else RED)
                           + f"{fold['out_of_sample_return'] * 100:+7.1f}%" + RESET
                           + f"   {DIM}hold{RESET} {fold['benchmark_return'] * 100:+7.1f}%")
            beat = result["beat_benchmark"]
            self.write("", f"  {BOLD}out of sample{RESET} "
                           + (GREEN if beat else RED)
                           + f"{result['out_of_sample_return'] * 100:+.1f}%" + RESET
                           + f"   {DIM}buy & hold{RESET} {result['benchmark_return'] * 100:+.1f}%")
            self.para("Tuned settings only ever traded data they had never seen. "
                      "This is the number that counts.")
            if not result["consistent"]:
                self.write(f"  {AMBER}⚠ at least one blind stretch lost money{RESET}")
        self.job(f"wf {strategy} {symbol}", work)

    def cmd_costs(self, args: list[str]) -> None:
        strategy, symbol, start, params = self._parse_run(args)
        def work(entry):
            bars = load(symbol, start=start or None)
            curve = analysis.cost_sensitivity(bars, strategy, params, symbol=symbol,
                                              levels=(0.0, 2.0, 5.0, 10.0, 20.0, 50.0))
            self.rule(f"{symbol} · {strategy} · cost sensitivity")
            best = max(abs(v) for v in curve.values()) or 1
            for level, value in sorted(curve.items()):
                length = int(abs(value) / best * 40)
                bar = (GREEN if value > 0 else RED) + ("█" * length) + RESET
                self.write(f"  {DIM}{level:>5g}bps{RESET} {value * 100:>8.1f}%  {bar}")
            self.write("")
            self.para("Every trade pays the spread. A strategy that only works at zero cost "
                      "doesn't work.")
        self.job(f"costs {strategy} {symbol}", work)

    def cmd_replay(self, args: list[str]) -> None:
        strategy, symbol, start, params = self._parse_run(args)
        speed = next((int(a) for a in args[2:] if a.isdigit() and len(a) <= 3), 40)
        def work(entry):
            bars = load(symbol, start=start or None)
            result = analysis.backtest(bars, strategy, params, symbol=symbol)
            self.session.last_result = result
            trades = {t.date: t for t in result.trades}
            self.rule(f"{symbol} · {strategy} · replay")
            anchor = len(self.session.lines)
            for i in range(2, len(result.equity), max(1, len(result.equity) // 220)):
                if not self.running:
                    return
                window = result.equity[:i]
                date = result.dates[i - 1]
                trade = trades.get(date)
                with self.lock:
                    del self.session.lines[anchor:]
                self.write(
                    f"  {DIM}{result.dates[0]} → {date}{RESET}   "
                    f"{BOLD}${window[-1]:,.0f}{RESET}  {pct((window[-1] / window[0] - 1) * 100)}"
                    f"   {DIM}hold{RESET} ${result.benchmark[i - 1]:,.0f}",
                    f"  {CLAY}{render.sparkline(window, _width() - 10)}{RESET}",
                    f"  {(GREEN if trade and trade.side == 'buy' else RED) if trade else DIM}"
                    + (f"{trade.side.upper()} {trade.shares:.4f} @ {trade.price:,.2f}" if trade else "")
                    + RESET)
                time.sleep(1.0 / speed)
            with self.lock:
                del self.session.lines[anchor:]
            self._show_result(result, metrics.warnings(result.metrics, result.benchmark_metrics,
                                                       bars=len(bars),
                                                       trades=result.metrics["trades"]))
        self.job(f"replay {strategy} {symbol}", work)

    def cmd_years(self, args: list[str]) -> None:
        result = self.session.last_result
        if result is None:
            self.write(f"{DIM}nothing backtested yet{RESET}")
            return
        self.rule(f"{result.symbol} · {result.strategy} · year by year")
        self._yearly_table(risk.yearly(result))

    def cmd_trades(self, args: list[str]) -> None:
        result = self.session.last_result
        if result is None:
            self.write(f"{DIM}nothing backtested yet — try `bt sma AAPL 2015`{RESET}")
            return
        self.rule(f"{result.symbol} · {len(result.trades)} trades")
        for trade in result.trades[-40:]:
            colour = GREEN if trade.side == "buy" else RED
            pnl = ""
            if trade.pnl is not None:
                pnl = f"   {(GREEN if trade.pnl >= 0 else RED)}{trade.pnl:+10,.2f}{RESET}"
            self.write(f"  {DIM}{trade.date}{RESET}  {colour}{trade.side.upper():<4}{RESET}"
                       f" {trade.shares:>12.4f} @ {money(trade.price):>10}{pnl}")

    def cmd_runs(self, args: list[str]) -> None:
        rows = store.list_runs(15)
        if not rows:
            self.write(f"{DIM}no saved runs yet{RESET}")
            return
        self.rule("saved runs")
        for row in rows:
            when = time.strftime("%d %b %H:%M", time.localtime(row["created"]))
            params = " ".join(f"{k}={v}" for k, v in row["params"].items())
            self.write(f"  {FAINT}{row['id']}{RESET} {DIM}{when}{RESET}  {row['symbol']:<9}"
                       f"{row['strategy']:<9}{params:<22}"
                       f"{(GREEN if row['return'] > row['benchmark_return'] else INK)}"
                       f"{row['return'] * 100:+8.1f}%{RESET}"
                       f"  {DIM}hold {row['benchmark_return'] * 100:+.1f}%{RESET}")

    def cmd_strategies(self, args: list[str]) -> None:
        self.rule("strategies")
        for spec in strategies.describe():
            defaults = " ".join(f"{k}={v}" for k, v in spec["defaults"].items())
            self.write(f"  {INK}{spec['name']:<12}{RESET}{DIM}{spec['description']}{RESET}")
            if defaults:
                self.write(f"  {'':<12}{FAINT}{defaults}{RESET}")

    def cmd_show(self, args: list[str]) -> None:
        if not args:
            self.write(f"{DIM}show which strategy? try `strats`{RESET}")
            return
        source = authoring.source_of(args[0].lower())
        if source is None:
            self.write(f"{DIM}{args[0]} is built in — see trading/strategies.py{RESET}")
            return
        self.rule(f"custom/{args[0].lower()}.py")
        for line in source.split("\n"):
            self.write(f"  {FAINT}{line}{RESET}")

    def cmd_paper(self, args: list[str]) -> None:
        action = (args[0] if args else "status").lower()
        def work():
            if action == "start":
                strategy = args[1] if len(args) > 1 else self.session.strategy
                symbol = (args[2] if len(args) > 2 else self.session.symbol).upper()
                cash = float(args[3]) if len(args) > 3 else 10_000.0
                account = paper.start(symbol, strategy, cash=cash)
                self.write(f"  {GREEN}opened{RESET} {account['id']}  {symbol} · {strategy}"
                           f"  ${account['equity']:,.2f}  {DIM}imaginary money{RESET}")
            elif action == "stop" and len(args) > 1:
                self.write(f"  {DIM}{'stopped' if paper.stop(args[1]) else 'no such account'}{RESET}")
            elif action == "step":
                paper.step_all()
                self.write(f"  {DIM}brought up to date{RESET}")
            accounts = paper.all_status()
            if not accounts:
                self.write(f"{DIM}no paper accounts. `paper start sma AAPL` opens one.{RESET}")
                return
            self.rule("paper accounts")
            for account in accounts:
                if "error" in account:
                    self.write(f"  {RED}{account['symbol']}: {account['error']}{RESET}")
                    continue
                where = (f"{GREEN}holding{RESET}" if account["in_market"] else f"{DIM}in cash{RESET}")
                self.write(f"  {FAINT}{account['id']}{RESET} {INK}{account['symbol']:<9}{RESET}"
                           f"{account['strategy']:<10} ${account['equity']:>10,.2f}"
                           f"  {pct(account['return'] * 100, 8)}"
                           f"  {DIM}hold{RESET} {pct(account['buy_and_hold_return'] * 100, 8, colour=False)}"
                           f"  {where}")
                for line in explain.plain_paper(account):
                    self.write(f"    {FAINT}{line}{RESET}")
        self.background("paper accounts…", work)

    # -- the model ---------------------------------------------------------

    def cmd_build(self, args: list[str]) -> None:
        description = " ".join(args).strip()
        if not description:
            self.write(f"{DIM}describe it: `build buy BTC when RSI drops under 30, sell above 60`{RESET}")
            return
        def work(entry):
            spec = authoring.write(description, on_status=lambda text: self.status(text))
            self.rule(f"wrote {spec.name}")
            source = authoring.source_of(spec.name) or ""
            for line in source.split("\n"):
                self.write(f"  {FAINT}{line}{RESET}")
            self.write("")
            self.para(f"checked: no imports, no file or network access, can't see future bars. "
                      f"Saved as custom/{spec.name}.py")
            self.write("")
            symbol = self.session.symbol
            bars = load(symbol)
            result = analysis.backtest(bars, spec.name, symbol=symbol)
            costs = analysis.cost_sensitivity(bars, spec.name, symbol=symbol)
            notes = metrics.warnings(result.metrics, result.benchmark_metrics, cost_curve=costs,
                                     bars=len(bars), trades=result.metrics["trades"])
            self.session.last_result = result
            store.save_run(result, notes)
            self._show_result(result, notes, costs)
            self.write("", f"  {FAINT}`sweep {spec.name} {symbol}` to see if it survives its own "
                           f"parameters, `wf {spec.name} {symbol}` to test it blind{RESET}")
        self.job(f"build: {description[:28]}", work)

    def cmd_ask(self, args: list[str]) -> None:
        question = " ".join(args).strip()
        result = self.session.last_result
        if result is None:
            self.write(f"{DIM}run a backtest first — `ask` reasons over the last one{RESET}")
            return
        def work():
            answer = explain.deep(result, question)
            self.rule("analyst")
            for line in (answer or "(no answer)").split("\n"):
                self.para(line, colour=INK)
        self.background("thinking…", work)

    def cmd_explain(self, args: list[str]) -> None:
        if args:
            meaning = explain.term(" ".join(args))
            if meaning:
                name, _, text = meaning.partition(": ")
                self.write(f"  {INK}{name}{RESET}{DIM} — {text}{RESET}")
                return
        result = self.session.last_result
        if result is None:
            self.rule("what the words mean")
            for name, meaning in explain.GLOSSARY.items():
                self.write(f"  {INK}{name}{RESET}")
                self.para(meaning, indent="      ")
            return
        def work():
            answer = explain.deep(result)
            self.rule("what happened")
            for line in (answer or "").split("\n"):
                self.para(line, colour=INK)
        self.background("thinking…", work)

    def cmd_why(self, args: list[str]) -> None:
        symbol = (args[0] if args else self.session.symbol).upper()
        def work():
            import llm_client
            from search import search_web
            quote = quotes.one(symbol, max_age=30)
            self.status("reading the news…")
            headlines = quotes.news(symbol)
            fresh = [h for h in headlines if (h["age_hours"] or 99) < 48]
            # Headlines first (a general web search hands back encyclopaedia
            # pages), then the open web for anything they missed.
            found = search_web(f"{symbol} stock why moved today") if len(fresh) < 3 else ""
            self.status("thinking…")
            messages = [
                {"role": "system", "content":
                    "You are Jugnu's market analyst writing into a terminal. At most five short "
                    "lines, no markdown. Say what the sources actually claim and who claims it. "
                    "If they don't explain the move, say that plainly. Never predict where it goes "
                    "next and never recommend buying or selling."},
                {"role": "user", "content":
                    f"{symbol} is {quote.change_pct:+.2f}% today at {quote.price:,.2f}"
                    f"{f', volume {quote.volume_ratio:.1f}x normal' if quote.volume_ratio else ''}.\n\n"
                    + "Headlines:\n" + ("\n".join(f"- [{h['when']}] {h['publisher']}: {h['title']}"
                                                  for h in headlines) or "(none)")
                    + (f"\n\nWeb search:\n{str(found)[:5000]}" if found else "")
                    + "\n\nWhy did it move? Only use what's above."}]
            answer = (llm_client.chat(messages, role="EASY")["message"].content or "").strip()
            self.rule(f"why {symbol} moved")
            self.write(f"  {money(quote.price)}  {pct(quote.change_pct)}"
                       + (f"   {AMBER}volume {quote.volume_ratio:.1f}× normal{RESET}"
                          if quote.volume_ratio >= 1.5 else ""))
            for line in (answer or "(nothing found)").split("\n"):
                self.para(line, colour=INK)
            if headlines:
                self.write("")
                for item in headlines[:5]:
                    self.write(f"  {FAINT}{item['when']:<13}{item['publisher'][:18]:<19}"
                               f"{item['title'][:70]}{RESET}")
            self.write(f"  {FAINT}headlines fetched just now — check anything that matters{RESET}")
        self.background(f"why {symbol}…", work)


    # -- portfolios, risk, scanning ----------------------------------------

    def cmd_portfolio(self, args: list[str]) -> None:
        """pf sma:AAPL, breakout:BTC-USD*2, rsi:SPY   [2019]"""
        spec = " ".join(args)
        start = ""
        match = re.search(r"\b(\d{4}(?:-\d{2}-\d{2})?)\b", spec)
        if match:
            start = match.group(1) if "-" in match.group(1) else f"{match.group(1)}-01-01"
            spec = spec.replace(match.group(1), "")
        sleeves = portfolio.parse_sleeves(spec)
        if not sleeves:
            self.write(f"{DIM}pf sma:AAPL, breakout:BTC-USD, rsi:SPY   (a *2 after one weights it){RESET}")
            return

        def work(entry):
            result = portfolio.run(sleeves, start=start or None)
            self.session.last_portfolio = result
            mine, theirs = result["metrics"], result["benchmark_metrics"]
            self.rule(f"portfolio · {len(sleeves)} sleeves · rebalanced {result['rebalance']}")
            won = mine["total_return"] > theirs["total_return"]
            self.write(f"  {BOLD}together{RESET} {(GREEN if won else INK)}"
                       f"{mine['total_return'] * 100:+.1f}%{RESET}"
                       f"    {DIM}equal-weight hold{RESET} {(INK if won else GREEN)}"
                       f"{theirs['total_return'] * 100:+.1f}%{RESET}"
                       f"    {DIM}sharpe{RESET} {mine['sharpe']:.2f}"
                       f"   {DIM}max drawdown{RESET} {RED}{mine['max_drawdown'] * 100:.1f}%{RESET}")
            self.write("")
            for sleeve in result["sleeves"]:
                self.write(f"  {INK}{sleeve['label']:<22}{RESET}{DIM}weight{RESET} "
                           f"{sleeve['weight'] * 100:>3.0f}%   "
                           + (GREEN if sleeve["return"] > 0 else RED)
                           + f"{sleeve['return'] * 100:>+9.1f}%" + RESET
                           + f"   {DIM}sharpe{RESET} {sleeve['sharpe']:>5.2f}"
                           f"   {DIM}maxdd{RESET} {sleeve['max_drawdown'] * 100:>6.1f}%"
                           f"   {DIM}trades{RESET} {sleeve['trades']}")
            self.write("")
            self.write(_plot([(result["benchmark"], "·", FAINT), (result["equity"], "█", CLAY)],
                             9, _width() - 12, result["dates"][0], result["dates"][-1]))
            self.write(f"{' ' * 11}{CLAY}█ portfolio{RESET}   {FAINT}· equal-weight hold{RESET}", "")
            self._correlation_table(result["correlation"])
            for note in result["notes"]:
                self.para(note)
        self.job(f"portfolio {len(sleeves)} sleeves", work)

    def _correlation_table(self, matrix: dict) -> None:
        labels = list(matrix)
        if len(labels) < 2:
            return
        width = max(len(label) for label in labels) + 2
        self.write(f"  {DIM}how they move together{RESET}")
        self.write("  " + " " * width + "".join(f"{label[:9]:>11}" for label in labels))
        for a in labels:
            cells = ""
            for b in labels:
                value = matrix[a][b]
                colour = INK if a == b else (RED if value > 0.8 else GREEN if value < 0.3 else DIM)
                cells += f"{colour}{value:>11.2f}{RESET}"
            self.write(f"  {DIM}{a:<{width}}{RESET}{cells}")

    def cmd_risk(self, args: list[str]) -> None:
        strategy, symbol, start, params = self._parse_run(args)
        def work(entry):
            bars = load(symbol, start=start or None)
            result = analysis.backtest(bars, strategy, params, symbol=symbol)
            deep = risk.full(result, bars)
            self.session.last_result = result
            trades = deep["trades"]
            self.rule(f"{symbol} · {strategy} · risk")
            self.write(
                f"  {DIM}calmar{RESET} {deep['calmar']:>6.2f}   {DIM}ulcer{RESET} {deep['ulcer']:>6.1f}"
                f"   {DIM}beta{RESET} {deep['beta']:>6.2f}   {DIM}alpha{RESET} "
                f"{deep['alpha'] * 100:>+6.1f}%/yr",
                f"  {DIM}worst day (95%){RESET} {RED}{deep['var95'] * 100:>6.2f}%{RESET}"
                f"   {DIM}average of the worst{RESET} {RED}{deep['cvar95'] * 100:>6.2f}%{RESET}",
                f"  {DIM}under water{RESET} {deep['underwater']['share_under_water'] * 100:>4.0f}% of the time"
                f"   {DIM}longest{RESET} {deep['underwater']['longest_days']} days"
                + (f"   {AMBER}still under water{RESET}" if deep["underwater"]["still_under_water"] else ""))
            if trades.get("round_trips"):
                self.write("",
                    f"  {DIM}round trips{RESET} {trades['round_trips']}"
                    f"   {DIM}win rate{RESET} {trades['win_rate'] * 100:.0f}%"
                    f"   {DIM}payoff{RESET} {trades['payoff']:.2f}"
                    f"   {DIM}profit factor{RESET} {trades['profit_factor']:.2f}",
                    f"  {DIM}average win{RESET} {GREEN}${trades['average_win']:,.0f}{RESET}"
                    f"   {DIM}average loss{RESET} {RED}${trades['average_loss']:,.0f}{RESET}"
                    f"   {DIM}expectancy{RESET} ${trades['expectancy']:,.0f}/trade",
                    f"  {DIM}median hold{RESET} {trades['median_hold_days']:.0f} days"
                    f"   {DIM}worst losing streak{RESET} {trades['worst_losing_streak']}")
            self.write("")
            self._yearly_table(deep["yearly"])
            for note in risk.verdicts(deep, result):
                self.para(f"⚠ {note}", colour=AMBER)
        self.job(f"risk {strategy} {symbol}", work)

    def _yearly_table(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.write(f"  {DIM}year by year{RESET}")
        for row in rows:
            width = 34
            value = row["strategy"]
            bar_len = min(width, int(abs(value) * width / 1.0)) if abs(value) < 1 else width
            colour = GREEN if value >= 0 else RED
            self.write(f"  {DIM}{row['year']}{RESET} {colour}{value * 100:>+7.1f}%{RESET} "
                       f"{colour}{'█' * bar_len}{RESET}"
                       + " " * max(0, width - bar_len)
                       + f" {FAINT}hold {row['benchmark'] * 100:+.1f}%{RESET}")

    def cmd_compare(self, args: list[str]) -> None:
        """vs sma rsi breakout AAPL 2015"""
        names = [a for a in args if a.lower() in strategies.REGISTRY]
        rest = [a for a in args if a.lower() not in strategies.REGISTRY]
        symbol = (rest[0] if rest else self.session.symbol).upper()
        start = next((f"{a}-01-01" for a in rest if re.fullmatch(r"\d{4}", a)), "")
        if not names:
            self.write(f"{DIM}vs sma rsi breakout AAPL 2015{RESET}")
            return
        def work(entry):
            bars = load(symbol, start=start or None)
            self.rule(f"{symbol} · {len(names)} strategies")
            self.write(f"  {DIM}{'strategy':<14}{'return':>10}{'sharpe':>9}{'maxdd':>9}"
                       f"{'trades':>8}{'invested':>10}{RESET}")
            rows = []
            for name in names:
                result = analysis.backtest(bars, name, symbol=symbol)
                rows.append((name, result))
            hold = rows[0][1].benchmark_metrics
            for name, result in sorted(rows, key=lambda r: -r[1].metrics["total_return"]):
                mine = result.metrics
                beat = mine["total_return"] > hold["total_return"]
                self.write(f"  {INK}{name:<14}{RESET}"
                           + (GREEN if beat else RED) + f"{mine['total_return'] * 100:>9.1f}%" + RESET
                           + f"{mine['sharpe']:>9.2f}{mine['max_drawdown'] * 100:>8.1f}%"
                           f"{mine['trades']:>8}{mine['exposure'] * 100:>9.0f}%")
            self.write(f"  {DIM}{'buy & hold':<14}{hold['total_return'] * 100:>9.1f}%"
                       f"{hold['sharpe']:>9.2f}{hold['max_drawdown'] * 100:>8.1f}%{1:>8}{100:>9.0f}%{RESET}")
            winners = [n for n, r in rows if r.metrics["total_return"] > hold["total_return"]]
            self.write("")
            self.para(f"{len(winners)} of {len(rows)} beat buying and holding "
                      + (f"({', '.join(winners)})." if winners else "-- none of them."))
        self.job(f"compare {len(names)} on {symbol}", work)

    def cmd_screen(self, args: list[str]) -> None:
        filters = " ".join(args)
        symbols = quotes.watchlist() + [s for s in self.markets if not s.endswith("-USD")]
        def work(entry):
            rows = screen.scan(sorted(set(symbols)), filters=filters)
            self.rule(f"screen · {len(rows)} of {len(set(symbols))}"
                      + (f" · {filters}" if filters else ""))
            self.write(f"  {DIM}{'symbol':<9}{'price':>10}{'chg':>8}{'rsi':>6}{'vol':>6}"
                       f"{'52w':>7}{'50d':>8}{'mom20':>8}  flags{RESET}")
            for row in rows:
                if "error" in row:
                    self.write(f"  {row['symbol']:<9}{DIM}{row['error']}{RESET}")
                    continue
                rsi_colour = GREEN if row["rsi"] <= 35 else (RED if row["rsi"] >= 65 else DIM)
                self.write(
                    f"  {INK}{row['symbol']:<9}{RESET}{money(row['price']):>10}"
                    + pct(row["change"], 8) +
                    f"{rsi_colour}{row['rsi']:>6.0f}{RESET}"
                    f"{(AMBER if row['vol'] >= 2 else DIM)}{row['vol']:>6.1f}{RESET}"
                    f"{DIM}{row['near_high'] * 100:>6.0f}%{RESET}"
                    f"{(GREEN if row['above_sma50'] > 0 else RED)}{row['above_sma50']:>+7.1f}%{RESET}"
                    f"{(GREEN if row['mom20'] > 0 else RED)}{row['mom20']:>+7.1f}%{RESET}"
                    f"  {FAINT}{', '.join(row.get('flags', []))}{RESET}")
            if not filters:
                self.write("")
                self.para("filter it: `screen rsi<35`, `screen vol>2`, `screen near_high>0.97 trend=1`")
        self.job("screening", work)

    def cmd_correlation(self, args: list[str]) -> None:
        symbols = [a.upper() for a in args] or (quotes.watchlist() + ["SPY", "BTC-USD"])
        def work(entry):
            series = {}
            for symbol in sorted(set(symbols)):
                try:
                    series[symbol] = [b.close for b in load(symbol)[-500:]]
                except Exception:
                    continue
            matrix = {a: {b: portfolio.correlation(series[a], series[b]) for b in series} for a in series}
            self.rule(f"correlation · last {min(500, max(len(v) for v in series.values()))} bars")
            self._correlation_table(matrix)
            pairs = [(a, b, matrix[a][b]) for a in matrix for b in matrix if a < b]
            if pairs:
                tight = max(pairs, key=lambda p: p[2])
                loose = min(pairs, key=lambda p: p[2])
                self.write("")
                self.para(f"{tight[0]} and {tight[1]} move most together ({tight[2]:.2f}); "
                          f"{loose[0]} and {loose[1]} least ({loose[2]:.2f}). Holding things that "
                          f"move together isn't diversification.")
        self.job("correlation", work)

    # -- fundamentals (OpenBB) ---------------------------------------------

    def cmd_fundamentals(self, args: list[str]) -> None:
        symbol = (args[0] if args else self.session.symbol).upper()
        def work(entry):
            data = sources.fundamentals(symbol)
            if not data.get("ok"):
                self.para(data.get("error", "unavailable"), colour=AMBER)
                return
            self.rule(f"{symbol} · {data.get('name') or ''}")
            cap = data.get("market_cap")
            self.write(f"  {DIM}sector{RESET} {data.get('sector') or '—'}"
                       f"   {DIM}market cap{RESET} "
                       + (f"${cap / 1e9:,.0f}bn" if cap else "—")
                       + (f"   {DIM}staff{RESET} {data['employees']:,}" if data.get("employees") else ""))
            pairs = [("P/E", data.get("pe_ratio"), "x"), ("P/B", data.get("price_to_book"), "x"),
                     ("P/S", data.get("price_to_sales"), "x"),
                     ("ROE", data.get("return_on_equity"), "%"),
                     ("net margin", data.get("net_profit_margin"), "%"),
                     ("debt/equity", data.get("debt_to_equity"), ""),
                     ("dividend", data.get("dividend_yield"), "%"),
                     ("revenue growth", data.get("revenue_growth"), "%")]
            shown = [f"{DIM}{name}{RESET} " +
                     (f"{value * 100:,.1f}%" if unit == "%" and abs(value) < 10 else f"{value:,.1f}{unit}")
                     for name, value, unit in pairs if isinstance(value, (int, float))]
            for i in range(0, len(shown), 4):
                self.write("  " + "   ".join(shown[i:i + 4]))
            if data.get("description"):
                self.write("")
                self.para(data["description"][:300], colour=FAINT)
            self.write(f"  {FAINT}via OpenBB{RESET}")
        self.job(f"fundamentals {symbol}", work)

    def cmd_options(self, args: list[str]) -> None:
        symbol = (args[0] if args else self.session.symbol).upper()
        def work(entry):
            data = sources.options(symbol)
            if not data.get("ok"):
                self.para(data.get("error", "unavailable"), colour=AMBER)
                return
            self.rule(f"{symbol} · options")
            iv = data.get("implied_volatility")
            self.write(f"  {DIM}contracts{RESET} {data['contracts']:,}"
                       f"   {DIM}calls/puts{RESET} {data['calls']:,}/{data['puts']:,}"
                       + (f"   {DIM}implied vol{RESET} {iv * 100:.1f}%" if iv else "")
                       + (f"   {DIM}put/call volume{RESET} {data['put_call_volume']:.2f}"
                          if data.get("put_call_volume") else ""))
            if data.get("expirations"):
                self.write(f"  {DIM}expiries{RESET} {', '.join(data['expirations'])}")
            if iv:
                self.write("")
                self.para(f"Implied volatility is what the options market charges for movement: "
                          f"{iv * 100:.0f}% a year is roughly {iv * 100 / 19.1:.1f}% a day.")
            self.write(f"  {FAINT}via OpenBB{RESET}")
        self.job(f"options {symbol}", work)

    def cmd_source(self, args: list[str]) -> None:
        if args and args[0] == "install":
            def work(entry):
                self.para("installing OpenBB into its own environment — a few hundred MB, "
                          "one-off. Prices keep coming from Yahoo and Binance either way.")
                ok, detail = sources.install(on_status=lambda text: self.status(text))
                self.write(f"  {(GREEN if ok else RED)}{'installed ' + detail if ok else detail}{RESET}")
            self.job("installing OpenBB", work)
            return
        status = sources.status()
        self.rule("data sources")
        self.write(f"  {INK}prices{RESET}        {DIM}Yahoo (stocks, ETFs, TSX) · Binance (crypto)"
                   f" — always on, no keys{RESET}")
        self.write(f"  {INK}news{RESET}          {DIM}Yahoo headlines, used by `why`{RESET}")
        mark = f"{GREEN}installed{RESET}" if status["installed"] else f"{AMBER}not installed{RESET}"
        self.write(f"  {INK}OpenBB{RESET}        {mark} {DIM}— fundamentals (`fa`), options (`opt`), "
                   f"estimates{RESET}")
        if not status["installed"]:
            self.write(f"  {FAINT}`source install` adds it. AGPL, so it lives outside the app in "
                       f"{status['path']}{RESET}")

    # -- the other account --------------------------------------------------

    def cmd_where(self, args: list[str]) -> None:
        """`on jugnu` sends the heavy commands to the other macOS account."""
        if args:
            target = args[0].lower()
            if target in ("jugnu", "agent", "remote"):
                if not remote.available():
                    self.write(f"{AMBER}The Jugnu account isn't answering. Log into it once and "
                               f"leave it running in the background.{RESET}")
                    return
                self.session.where = "jugnu"
                self.write(f"  {GREEN}heavy commands now run in the Jugnu account{RESET}"
                           f"  {DIM}(`on me` to come back){RESET}")
                rest = args[1:]
                if rest:
                    self.run_command(" ".join(rest))
                return
            if target in ("me", "local", "here", "primary"):
                self.session.where = "local"
                self.write(f"  {DIM}back to running here{RESET}")
                return
        self.write(f"  {DIM}running {'in the Jugnu account' if self.session.where == 'jugnu' else 'here'}"
                   f" · `on jugnu` or `on me`{RESET}")

    def _remote(self, payload: dict, label: str, render_fn) -> None:
        def work(entry):
            reply = remote.run(payload)
            if not reply.get("ok"):
                self.write(f"{RED}{reply.get('error', 'remote run failed')}{RESET}")
                return
            render_fn(reply)
        self.job(label, work, where="jugnu")

    # -- jobs ---------------------------------------------------------------

    def cmd_jobs(self, args: list[str]) -> None:
        if args and args[0] in ("clear", "clean"):
            self.write(f"  {DIM}cleared {self.runner.clear_finished()} finished jobs{RESET}")
            return
        entries = self.runner.all()
        if not entries:
            self.write(f"{DIM}nothing running. Heavy commands become jobs automatically.{RESET}")
            return
        self.rule("jobs")
        for entry in entries:
            mark = {"done": f"{GREEN}done{RESET}", "failed": f"{RED}failed{RESET}",
                    "running": f"{CLAY}running{RESET}", "queued": f"{DIM}queued{RESET}"}.get(
                        entry.status, entry.status)
            self.write(f"  {FAINT}[{entry.id}]{RESET} {mark:<20}{INK}{entry.label:<34}{RESET}"
                       f"{DIM}{entry.seconds:>6.0f}s{RESET}"
                       + (f"  {FAINT}@jugnu{RESET}" if entry.where == "jugnu" else "")
                       + (f"  {RED}{entry.error[:40]}{RESET}" if entry.error else ""))
        self.write(f"  {FAINT}`job N` re-prints one{RESET}")

    def cmd_job(self, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            self.write(f"{DIM}job N — see `jobs`{RESET}")
            return
        entry = self.runner.get(int(args[0]))
        if entry is None:
            self.write(f"{DIM}no job {args[0]}{RESET}")
            return
        self.rule(f"job {entry.id} · {entry.label} · {entry.status}")
        for line in entry.lines:
            self.write(line)
        if entry.error:
            self.write(f"  {RED}{entry.error}{RESET}")

    # -- alerts -------------------------------------------------------------

    def cmd_alert(self, args: list[str]) -> None:
        if not args:
            rows = alerts.all_alerts()
            if not rows:
                self.write(f"{DIM}no alerts. `alert NVDA < 200`, `alert AAPL rsi < 30`{RESET}")
                return
            self.rule("alerts")
            for row in rows:
                state = f"{GREEN}armed{RESET}" if row["active"] else f"{FAINT}fired{RESET}"
                self.write(f"  {FAINT}[{row['id']}]{RESET} {state}  {INK}{alerts.describe(row)}{RESET}")
            self.write(f"  {FAINT}checked every minute while this is open · `unalert N` removes one{RESET}")
            return
        try:
            rule = alerts.add(" ".join(args))
        except ValueError as e:
            self.write(f"{RED}{e}{RESET}")
            return
        self.write(f"  {GREEN}watching{RESET} {INK}{alerts.describe(rule)}{RESET}"
                   f"  {DIM}— it'll notify you once and disarm{RESET}")

    def cmd_unalert(self, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            self.write(f"{DIM}unalert N — see `alert`{RESET}")
            return
        self.write(f"  {DIM}{'removed' if alerts.remove(int(args[0])) else 'no such alert'}{RESET}")

    # -- misc --------------------------------------------------------------

    def cmd_clear(self, args: list[str]) -> None:
        with self.lock:
            self.session.lines.clear()
        self.dirty = True

    def cmd_quit(self, args: list[str]) -> None:
        self.running = False


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _width() -> int:
    return max(72, shutil.get_terminal_size((100, 30)).columns)


def _plot(series: list[tuple[list[float], str, str]], height: int, width: int,
          left: str, right: str) -> str:
    """Overlay several series in one ASCII panel. Each is (values, mark, colour)."""
    sampled = []
    for values, mark, colour in series:
        step = max(1, len(values) / width)
        sampled.append(([values[min(len(values) - 1, int(i * step))] for i in range(width)], mark, colour))
    low = min(min(v) for v, _, _ in sampled)
    high = max(max(v) for v, _, _ in sampled)
    span = (high - low) or 1
    rows = []
    for level in range(height, 0, -1):
        top = low + span * level / height
        bottom = low + span * (level - 1) / height
        line = ""
        for i in range(width):
            drawn = " "
            for values, mark, colour in sampled:
                value = values[i]
                if bottom <= value < top or (level == height and value >= top):
                    drawn = f"{colour}{mark}{RESET}"
                    break
            line += drawn
        rows.append(f"{FAINT}{top:>9,.0f} │{RESET}{line}")
    rows.append(f"{FAINT}{' ' * 10}└{'─' * width}{RESET}")
    rows.append(f"{FAINT}{' ' * 11}{left}{' ' * max(1, width - len(left) - len(right))}{right}{RESET}")
    return "\n".join(rows)


def _equity_chart(result, width: int, height: int = 9) -> str:
    return _plot([(result.benchmark, "·", FAINT), (result.equity, "█", CLAY)],
                 height, width, result.dates[0], result.dates[-1]) + \
        f"\n{' ' * 11}{CLAY}█ strategy{RESET}   {FAINT}· buy & hold{RESET}"


def _price_chart(closes: list[float], first: str, last: str, width: int, height: int = 12) -> str:
    colour = GREEN if closes[-1] >= closes[0] else RED
    return _plot([(closes, "█", colour)], height, width, first, last)


# ---------------------------------------------------------------------------

def _read_escape() -> str:
    """The rest of an escape sequence, if it's already waiting."""
    out = ""
    while select.select([sys.stdin], [], [], 0.02)[0] and len(out) < 6:
        out += sys.stdin.read(1)
        if out[-1].isalpha() or out[-1] == "~":
            break
    return out


COMMANDS = {
    "help": Terminal.cmd_help, "?": Terminal.cmd_help,
    "q": Terminal.cmd_quote, "quote": Terminal.cmd_quote,
    "ch": Terminal.cmd_chart, "chart": Terminal.cmd_chart,
    "wl": Terminal.cmd_watchlist, "watch": Terminal.cmd_watchlist,
    "mkt": Terminal.cmd_markets, "markets": Terminal.cmd_markets,
    "bt": Terminal.cmd_backtest, "backtest": Terminal.cmd_backtest,
    "sweep": Terminal.cmd_sweep,
    "wf": Terminal.cmd_walkforward, "forward": Terminal.cmd_walkforward,
    "costs": Terminal.cmd_costs,
    "replay": Terminal.cmd_replay,
    "trades": Terminal.cmd_trades,
    "runs": Terminal.cmd_runs,
    "strats": Terminal.cmd_strategies, "strategies": Terminal.cmd_strategies,
    "show": Terminal.cmd_show,
    "paper": Terminal.cmd_paper,
    "build": Terminal.cmd_build,
    "ask": Terminal.cmd_ask,
    "explain": Terminal.cmd_explain,
    "why": Terminal.cmd_why,
    "pf": Terminal.cmd_portfolio, "portfolio": Terminal.cmd_portfolio,
    "risk": Terminal.cmd_risk,
    "vs": Terminal.cmd_compare, "compare": Terminal.cmd_compare,
    "screen": Terminal.cmd_screen, "scan": Terminal.cmd_screen,
    "corr": Terminal.cmd_correlation,
    "fa": Terminal.cmd_fundamentals, "fundamentals": Terminal.cmd_fundamentals,
    "opt": Terminal.cmd_options, "options": Terminal.cmd_options,
    "source": Terminal.cmd_source, "sources": Terminal.cmd_source,
    "on": Terminal.cmd_where, "where": Terminal.cmd_where,
    "jobs": Terminal.cmd_jobs, "job": Terminal.cmd_job, "y": Terminal.cmd_years,
    "alert": Terminal.cmd_alert, "unalert": Terminal.cmd_unalert,
    "clear": Terminal.cmd_clear,
    "quit": Terminal.cmd_quit, "exit": Terminal.cmd_quit,
}


def _pick_model_mode() -> None:
    """Run from a plain shell, the terminal has no API key in its
    environment. If Claude Code is installed and logged in, use the plan --
    it costs nothing per call and needs no configuration."""
    import llm_client
    if os.environ.get("JUGNU_MODEL_MODE"):
        return
    if any(os.environ.get(name) for name in ("JUGNU_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")):
        return
    try:
        import claude_code
        if claude_code.find_cli():
            llm_client.set_mode("claude")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    if not sys.stdin.isatty():
        print("The Jugnu terminal needs a terminal. Run it directly:  python -m trading term")
        return 1
    _pick_model_mode()
    terminal = Terminal()
    terminal.refresh_quotes()
    try:
        terminal.loop()
    finally:
        terminal.running = False
        terminal.jobs.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
