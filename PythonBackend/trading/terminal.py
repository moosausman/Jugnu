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
               remote, render, risk, screen, sources, store, strategies, ui)
from .data import DataError, load
from .ui import GUTTER, fit, money, percent, visible_width as visible

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

RESET, BOLD = ui.RESET, ui.BOLD
DIM, FAINT, INK = ui.DIM, ui.FAINT, ui.INK
CLAY, GREEN, RED, AMBER, BAR = ui.CLAY, ui.GREEN, ui.RED, ui.AMBER, ui.BAR


def pct(value: float, width: int = 0, colour: bool = True) -> str:
    text = ui.percent(value, 2, colour=False)
    text = text.rjust(width) if width else text
    return ((GREEN if value >= 0 else RED) + text + RESET) if colour else text


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
    # Where heavy commands run. Defaults to the Jugnu account when it's up:
    # that's a whole second machine sitting idle, it keeps long sweeps off
    # the session you're typing in, and you never have to switch user.
    where: str = "local"


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
        if remote.where_am_i() == "primary" and remote.available():
            self.session.where = "jugnu"

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

    def para(self, text: str, indent: str = ui.GUTTER, colour: str = DIM,
             marker: str = "") -> None:
        """Prose, wrapped to the window rather than cut off by it."""
        self.write(*ui.note(text, _width(), marker=marker, colour=colour, indent=indent))

    def rule(self, label: str = "") -> None:
        self.write("", ui.rule(label, _width()))

    def stats(self, pairs: list[tuple[str, str]]) -> None:
        self.write(*ui.stats(pairs, _width()))

    def table(self, headers, rows, aligns: str = "", trim=None) -> None:
        self.write(*ui.table(headers, rows, _width(), aligns=aligns, trim=trim))

    def status(self, message: str) -> None:
        self.session.status = message
        self.dirty = True

    # -- the frame ---------------------------------------------------------

    def draw(self) -> None:
        size = shutil.get_terminal_size((100, 30))
        # One column short of the real width, always. Writing into the last
        # cell makes most terminals wrap to the next line, which pushed
        # every following row one line down and scattered the screen.
        width, height = max(30, size.columns - 1), max(10, size.lines)
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
        # CR before LF: the terminal is in raw mode, where "\n" moves down
        # a line without returning to column 0. Without the "\r" every row
        # started where the last one ended, which is what turned the screen
        # into a staircase of fragments. \x1b[K clears the rest of each line
        # instead of padding it, so a redraw can't leave debris behind.
        painted = []
        for row in rows[:height]:
            # Bars carry their own shade; everything else gets the page
            # colour, so the window's profile never shows through.
            background = ui.BG_BAR if row.startswith(ui.BG_BAR) else ui.BG
            painted.append(background + fit(row, width) + "\x1b[K" + ui.BG_RESET)
        sys.stdout.write("\x1b[H" + "\r\n".join(painted) + "\x1b[J" + ui.FULL_RESET)
        # Park the cursor at the end of what's typed.
        sys.stdout.write(f"\x1b[{height};{min(width, visible(self._prompt(width, cursor=True)) + 1)}H")
        sys.stdout.flush()

    def _title(self, width: int) -> str:
        clock = time.strftime("%a %d %b  %H:%M:%S")
        llm = model()
        mode = llm.model_mode() if llm else "no model"
        narrow = width < 100
        place = "@jugnu" if self.session.where == "jugnu" else "@here"
        bar = ui.BG_BAR
        left = f"{bar}{BOLD}{CLAY} JUGNU {RESET}{bar}{DIM} {place}{RESET}" if narrow else (
            f"{bar}{BOLD}{CLAY} JUGNU TERMINAL {RESET}{bar}{DIM} paper · simulation only · "
            + ("runs in the Jugnu account" if self.session.where == "jugnu" else "runs here")
            + RESET)
        clock_text = time.strftime("%H:%M") if narrow else clock
        right = f"{bar}{DIM}{mode}  {clock_text} {RESET}"
        gap = max(1, width - visible(left) - visible(right))
        return left + bar + " " * gap + right + RESET

    def _markets(self, width: int) -> list[str]:
        """Two columns in a wide window, one compact ticker in a narrow or
        short one -- an 80x24 terminal can't spare six rows for prices."""
        watch = quotes.watchlist()
        market = [quotes.cached(s) or quotes.Quote(symbol=s) for s in self.markets]
        listed = [quotes.cached(s) or quotes.Quote(symbol=s) for s in watch]
        height = shutil.get_terminal_size((100, 30)).lines

        if width < 96 or height < 28:
            cells = [self._ticker_cell(q) for q in market + listed if q.price or q.error]
            rows, line = [], ui.GUTTER
            for cell in cells:
                if ui.width_of(line) + ui.width_of(cell) + 3 > width:
                    rows.append(line)
                    line = ui.GUTTER
                line += cell + f"{FAINT}  ·  {RESET}"
            rows.append(line)
            return rows[:2]

        half = width // 2 - 1
        left = [f"{FAINT}MARKETS{RESET}"] + [self._quote_line(q, half) for q in market]
        right = [f"{FAINT}WATCHLIST{RESET}"] + [self._quote_line(q, half - 2, volume=True)
                                                for q in listed]
        rows = []
        for i in range(max(len(left), len(right))):
            a = left[i] if i < len(left) else ""
            b = right[i] if i < len(right) else ""
            rows.append(fit(a, half) + f"{FAINT}│{RESET} " + fit(b, width - half - 2))
        return rows

    def _ticker_cell(self, quote: quotes.Quote) -> str:
        if quote.error or not quote.price:
            return f"{INK}{quote.symbol}{RESET} {FAINT}—{RESET}"
        return (f"{INK}{quote.symbol}{RESET} {ui.money(quote.price)} "
                + ui.percent(quote.change_pct))

    def _quote_line(self, quote: quotes.Quote, width: int, volume: bool = False) -> str:
        if quote.error:
            return f"{INK}{quote.symbol:<9}{RESET}{FAINT}unavailable{RESET}"
        if not quote.price:
            return f"{INK}{quote.symbol:<9}{RESET}{FAINT}…{RESET}"
        line = (f"{INK}{quote.symbol:<9}{RESET}"
                + ui.pad(ui.money(quote.price), 10, ">") + "  "
                + ui.pad(ui.percent(quote.change_pct), 8, ">"))
        if volume and quote.volume_ratio >= 1.8:
            line += f"  {AMBER}{quote.volume_ratio:.1f}× vol{RESET}"
        return fit(line, width)

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

    def _status(self, width: int) -> str:
        if self.session.busy:
            spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 10) % 10]
            text = f"{CLAY}{spin} {self.session.busy}{RESET}"
        else:
            text = f"{DIM}{self.session.status or 'ready'}{RESET}"
        right = f"{FAINT}F1 help · ↑ history · ^C quit{RESET}"
        gap = max(1, width - visible(text) - visible(right) - 3)
        return f"{ui.BG_BAR} " + text + " " * gap + right + f" {RESET}"

    def _prompt(self, width: int, cursor: bool = False) -> str:
        if cursor:
            return f"  > {self.buffer}"
        return f"{ui.BG}  {CLAY}❯{RESET} {INK}{self.buffer}{RESET}"

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

    def job(self, label: str, fn, *, where: str = "local") -> None:
        """Heavy work becomes a numbered job: the prompt comes straight back,
        several can run at once, and `job N` re-prints what it produced.

        `where` is where the work actually ran, not where the session is
        pointed -- a job labelled @jugnu has to have been there."""
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
            sys.stdout.write(ui.FULL_RESET + "\x1b[?1049l\x1b[?25h")
            sys.stdout.flush()

    def _check_alerts(self) -> None:
        for fired in alerts.check():
            self.write(f"{AMBER}⚑ alert{RESET} {INK}{alerts.describe(fired)}{RESET}")
            alerts.notify(fired)

    def banner(self) -> None:
        loaded = authoring.load_saved()
        width = _width()
        self.write("", f"{ui.GUTTER}{BOLD}{CLAY}Jugnu terminal{RESET}")
        self.para("Paper trading and research. Nothing here can place a real order.",
                  colour=FAINT)
        self.write("")
        examples = [
            ("bt sma AAPL 2015", "backtest, with buy & hold beside it"),
            ("pf sma:AAPL, breakout:BTC-USD", "a portfolio of both, rebalanced"),
            ("risk sma AAPL", "drawdowns, VaR, beta, year by year"),
            ("screen rsi<35", "scan your watchlist"),
            ("build buy BTC when RSI < 30, sell above 60", "writes a real strategy and runs it"),
            ("help", "everything else"),
        ]
        self.write(*self._command_list(examples, width))
        self.write("")
        self.para("New here? Type `tour` -- it runs one real backtest and explains every number "
                  "in it.", colour=CLAY)
        if loaded:
            self.write("", f"{ui.GUTTER}{FAINT}your strategies: {', '.join(loaded)}{RESET}")
        if self.session.where == "jugnu":
            self.para("Heavy commands run in the Jugnu account, so sweeps don't slow this "
                      "session down and you never switch user. `on me` keeps them here.",
                      colour=FAINT)
        self.write("")

    @staticmethod
    def _command_list(rows: list[tuple[str, str]], width: int) -> list[str]:
        """Command and description side by side when there's room, stacked
        when there isn't. A 55-column window was truncating every line."""
        longest = max(ui.width_of(left) for left, _ in rows)
        widest = max(ui.width_of(right) for _, right in rows)
        out = []
        # Side by side only when the description fits whole: a truncated
        # explanation is worse than one on its own line.
        if longest + widest + 5 <= width:
            for left, right in rows:
                out.append(f"{ui.GUTTER}{INK}{ui.pad(left, longest + 3)}{RESET}{FAINT}{right}{RESET}")
            return out
        for left, right in rows:
            out.append(f"{ui.GUTTER}{INK}{left}{RESET}")
            out += ui.note(right, width, colour=FAINT, indent=ui.GUTTER + "  ")
        return out

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
        width = _width()
        for title, rows in groups:
            self.rule(title)
            self.write(*self._command_list(rows, width))
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
            self.para(f"{symbol}: {quote.error}", colour=RED)
            return
        self.rule(symbol)
        self.write(f"{ui.GUTTER}{BOLD}{INK}{ui.money(quote.price)}{RESET}  "
                   f"{ui.percent(quote.change_pct)}  {FAINT}({quote.change:+,.2f}){RESET}", "")
        pairs = [("open", ui.money(quote.open)), ("high", ui.money(quote.high)),
                 ("low", ui.money(quote.low)), ("volume", ui.money(quote.volume))]
        if quote.volume_ratio:
            pairs.append(("vs normal", (AMBER if quote.volume_ratio >= 1.8 else INK)
                          + f"{quote.volume_ratio:.1f}×" + RESET))
        self.stats([(label, value) for label, value in pairs if value not in ("0.00", "$0.00")])
        if quote.spark:
            self.write(f"{ui.GUTTER}{CLAY}{ui.sparkline(quote.spark, _width() - 12)}{RESET}"
                       f"  {FAINT}recent{RESET}")

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
        params = " ".join(f"{k}={v}" for k, v in result.params.items())
        self.rule(f"{result.symbol}  {result.strategy}" + (f"  {params}" if params else ""))
        self.write(f"{ui.GUTTER}{FAINT}{result.dates[0]} → {result.dates[-1]}"
                   f"   {result.slippage_bps:g}bps slippage, ${result.commission:g}/trade,"
                   f" ${result.cash:,.0f} start{RESET}", "")

        beat = mine["total_return"] > theirs["total_return"]
        self.stats([
            ("strategy", (GREEN if beat else INK) + ui.percent(mine["total_return"] * 100) + RESET
             if not beat else ui.percent(mine["total_return"] * 100)),
            ("buy & hold", ui.percent(theirs["total_return"] * 100)),
            ("ends at", f"{INK}{ui.money(mine['final'], '$')}{RESET}"),
            ("sharpe", f"{INK}{mine['sharpe']:.2f}{RESET} {FAINT}vs {theirs['sharpe']:.2f}{RESET}"),
            ("max fall", f"{RED}{mine['max_drawdown'] * 100:.0f}%{RESET}"),
            ("trades", f"{INK}{mine['trades']}{RESET}"),
            ("won", f"{INK}{mine['win_rate'] * 100:.0f}%{RESET}" if mine["win_rate"] else "—"),
            ("invested", f"{INK}{mine['exposure'] * 100:.0f}%{RESET}"),
        ])
        for note in notes:
            self.para(note, colour=AMBER, marker="⚠")

        self.write("")
        self.write(*ui.chart([(result.benchmark, "·", FAINT), (result.equity, "█", CLAY)],
                             _width(), self._chart_height(),
                             result.dates[0], result.dates[-1]))
        self.write(ui.legend([("█", CLAY, "strategy"), ("·", FAINT, "buy & hold")]))

        if costs:
            self.write("")
            self.stats([(f"{level:g}bps", ui.percent(value * 100, 0))
                        for level, value in sorted(costs.items())])
        self.write("")
        for line in explain.plain(result):
            self.para(line)
        self.write("")
        self.para("`explain` unpacks it · `ask <question>` interrogates it · `risk` goes deeper "
                  "· `trades` lists them", colour=FAINT)

    def _chart_height(self) -> int:
        """Charts shrink in a short window instead of pushing everything
        else off the screen."""
        lines = shutil.get_terminal_size((100, 30)).lines
        return max(5, min(12, (lines - 14) // 2))

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

    def _needs_model(self) -> bool:
        if model() is not None:
            return False
        self.para("That one needs Jugnu's model, which lives in your main account. Run this "
                  "terminal there (⌘⇧T in Jugnu) -- everything else here works either way.",
                  colour=AMBER)
        return True

    def cmd_build(self, args: list[str]) -> None:
        if self._needs_model():
            return
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
        if self._needs_model():
            return
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
        if result is not None and self._needs_model():
            return
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
        if self._needs_model():
            return
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

        if self.session.where == "jugnu":
            self._remote({"action": "portfolio", "sleeves": spec, "start": start},
                         f"portfolio {len(sleeves)} sleeves", self._show_portfolio)
            return

        def work(entry):
            self._show_portfolio(portfolio.run(sleeves, start=start or None))
        self.job(f"portfolio {len(sleeves)} sleeves", work)

    def _show_portfolio(self, result: dict) -> None:
        self.session.last_portfolio = result
        mine, theirs = result["metrics"], result["benchmark_metrics"]
        self.rule(f"portfolio  {len(result['sleeves'])} sleeves  "
                  f"rebalanced {result['rebalance']}")
        self.stats([
            ("together", ui.percent(mine["total_return"] * 100)),
            ("equal-weight hold", ui.percent(theirs["total_return"] * 100)),
            ("sharpe", f"{INK}{mine['sharpe']:.2f}{RESET}"),
            ("max fall", f"{RED}{mine['max_drawdown'] * 100:.0f}%{RESET}"),
        ])
        self.table(["sleeve", "weight", "return", "sharpe", "max fall", "trades"],
                   [[f"{INK}{s['label']}{RESET}", f"{s['weight'] * 100:.0f}%",
                     ui.percent(s["return"] * 100, 0), f"{s['sharpe']:.2f}",
                     f"{RED}{s['max_drawdown'] * 100:.0f}%{RESET}", str(s["trades"])]
                    for s in result["sleeves"]],
                   aligns="<>>>>>", trim=[5, 4])
        self.write("")
        self.write(*ui.chart([(result["benchmark"], "·", FAINT), (result["equity"], "█", CLAY)],
                             _width(), self._chart_height(),
                             result["dates"][0], result["dates"][-1]))
        self.write(ui.legend([("█", CLAY, "portfolio"), ("·", FAINT, "equal-weight hold")]), "")
        self._correlation_table(result["correlation"])
        for note in result["notes"]:
            self.para(note)

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
        if self.session.where == "jugnu":
            self._remote({"action": "risk", "symbol": symbol, "strategy": strategy,
                          "start": start, "params": params},
                         f"risk {strategy} {symbol}",
                         lambda reply: self._show_risk(reply["symbol"], reply["strategy"],
                                                       reply["deep"], reply["verdicts"]))
            return

        def work(entry):
            bars = load(symbol, start=start or None)
            result = analysis.backtest(bars, strategy, params, symbol=symbol)
            self.session.last_result = result
            self._show_risk(symbol, strategy, risk.full(result, bars),
                            risk.verdicts(risk.full(result, bars), result))
        self.job(f"risk {strategy} {symbol}", work)

    def _show_risk(self, symbol: str, strategy: str, deep: dict, verdicts: list[str]) -> None:
        trades = deep.get("trades", {})
        water = deep["underwater"]
        self.rule(f"{symbol}  {strategy}  risk")
        self.stats([
            ("calmar", f"{INK}{deep['calmar']:.2f}{RESET}"),
            ("ulcer", f"{INK}{deep['ulcer']:.1f}{RESET}"),
            ("beta", f"{INK}{deep['beta']:.2f}{RESET}"),
            ("alpha/yr", ui.percent(deep["alpha"] * 100)),
            ("worst day", f"{RED}{deep['var95'] * 100:.1f}%{RESET}"),
            ("worst tail", f"{RED}{deep['cvar95'] * 100:.1f}%{RESET}"),
            ("under water", f"{INK}{water['share_under_water'] * 100:.0f}%{RESET}"),
            ("longest", f"{INK}{water['longest_days']}d{RESET}"),
        ])
        if water["still_under_water"]:
            self.para(f"Still under water: {water['current_drawdown'] * 100:.0f}% below its best.",
                      colour=AMBER, marker="⚠")
        if trades.get("round_trips"):
            self.stats([
                ("round trips", f"{INK}{trades['round_trips']}{RESET}"),
                ("won", f"{INK}{trades['win_rate'] * 100:.0f}%{RESET}"),
                ("payoff", f"{INK}{trades['payoff']:.2f}{RESET}"),
                ("profit factor", f"{INK}{trades['profit_factor']:.2f}{RESET}"),
                ("avg win", f"{GREEN}{ui.money(trades['average_win'], '$')}{RESET}"),
                ("avg loss", f"{RED}{ui.money(trades['average_loss'], '$')}{RESET}"),
                ("per trade", f"{INK}{ui.money(trades['expectancy'], '$')}{RESET}"),
                ("median hold", f"{INK}{trades['median_hold_days']:.0f}d{RESET}"),
            ])
        self._yearly_table(deep.get("yearly", []))
        for note in verdicts:
            self.para(note, colour=AMBER, marker="⚠")

    def _yearly_table(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.write("", f"{ui.GUTTER}{FAINT}year by year{RESET}")
        scale = max((abs(row["strategy"]) for row in rows), default=1) or 1
        bar_width = max(8, min(30, _width() - 40))
        for row in rows:
            self.write(f"{ui.GUTTER}{FAINT}{row['year']}{RESET} "
                       + ui.pad(ui.percent(row["strategy"] * 100, 0), 8, ">") + " "
                       + ui.bar(row["strategy"], scale, bar_width)
                       + f" {FAINT}hold {row['benchmark'] * 100:+.0f}%{RESET}")

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
            self.rule(f"{symbol}  {len(names)} strategies")
            rows = [(name, analysis.backtest(bars, name, symbol=symbol)) for name in names]
            hold = rows[0][1].benchmark_metrics
            table = []
            for name, result in sorted(rows, key=lambda r: -r[1].metrics["total_return"]):
                mine = result.metrics
                table.append([
                    f"{INK}{name}{RESET}",
                    ui.percent(mine["total_return"] * 100, 0),
                    f"{mine['sharpe']:.2f}",
                    f"{RED}{mine['max_drawdown'] * 100:.0f}%{RESET}",
                    str(mine["trades"]),
                    f"{mine['exposure'] * 100:.0f}%",
                ])
            table.append([f"{FAINT}buy & hold{RESET}",
                          ui.percent(hold["total_return"] * 100, 0),
                          f"{hold['sharpe']:.2f}",
                          f"{RED}{hold['max_drawdown'] * 100:.0f}%{RESET}", "1", "100%"])
            self.table(["strategy", "return", "sharpe", "max fall", "trades", "invested"],
                       table, aligns="<>>>>>", trim=[5, 4])
            winners = [n for n, r in rows if r.metrics["total_return"] > hold["total_return"]]
            self.para(f"{len(winners)} of {len(rows)} beat buying and holding"
                      + (f": {', '.join(winners)}." if winners else " -- none of them."))
        self.job(f"compare {len(names)} on {symbol}", work)

    def cmd_screen(self, args: list[str]) -> None:
        filters = " ".join(args)
        symbols = sorted(set(quotes.watchlist() + [s for s in self.markets
                                                   if not s.endswith("-USD")]))
        if self.session.where == "jugnu":
            self._remote({"action": "screen", "symbols": symbols, "filters": filters},
                         "screening",
                         lambda reply: self._show_screen(reply["rows"], filters, len(symbols)))
            return

        def work(entry):
            rows = screen.scan(sorted(set(symbols)), filters=filters)
            self._show_screen(rows, filters, len(symbols))
        self.job("screening", work)

    def _show_screen(self, rows: list[dict], filters: str, total: int) -> None:
        if True:
            self.rule(f"screen  {len(rows)} of {total}"
                      + (f"  {filters}" if filters else ""))
            table = []
            for row in rows:
                if "error" in row:
                    table.append([f"{INK}{row['symbol']}{RESET}", f"{FAINT}{row['error']}{RESET}",
                                  "", "", "", "", ""])
                    continue
                rsi_colour = GREEN if row["rsi"] <= 35 else (RED if row["rsi"] >= 65 else INK)
                table.append([
                    f"{INK}{row['symbol']}{RESET}",
                    ui.money(row["price"]),
                    ui.percent(row["change"]),
                    f"{rsi_colour}{row['rsi']:.0f}{RESET}",
                    (AMBER if row["vol"] >= 2 else FAINT) + f"{row['vol']:.1f}×" + RESET,
                    f"{FAINT}{row['near_high'] * 100:.0f}%{RESET}",
                    ui.percent(row["mom20"], 0),
                    f"{FAINT}{', '.join(row.get('flags', []))}{RESET}",
                ])
            self.table(["symbol", "price", "day", "rsi", "vol", "52w", "20d", ""],
                       table, aligns="<>>>>>><", trim=[5, 6, 4])
            if not filters:
                self.para("filter it: `screen rsi<35` · `screen vol>2` · "
                          "`screen near_high>0.97 trend=1`", colour=FAINT)

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
                self.write(f"{ui.GUTTER}{DIM}heavy commands now run in this session{RESET}")
                rest = args[1:]
                if rest:
                    self.run_command(" ".join(rest))
                return
        self.write(f"  {DIM}running {'in the Jugnu account' if self.session.where == 'jugnu' else 'here'}"
                   f" · `on jugnu` or `on me`{RESET}")

    def _remote(self, payload: dict, label: str, render_fn) -> None:
        """Runs one action in the Jugnu account and renders the JSON it
        sends back through the same panel the local path uses."""
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

    def cmd_tour(self, args: list[str]) -> None:
        """Runs one real backtest and explains every number in it."""
        symbol = (args[0] if args else "AAPL").upper()

        def work(entry):
            self.rule("what this thing is")
            self.para("A simulator. It replays a strategy over real prices, pays real-looking "
                      "costs, and tells you what would have happened -- next to what doing "
                      "nothing would have done. Nothing here touches money.")
            self.write("")
            self.para(f"Running `bt sma {symbol} 2015` now: buy when the 20-day average crosses "
                      f"above the 100-day, sell when it crosses back.", colour=INK)

            bars = load(symbol, start="2015-01-01")
            result = analysis.backtest(bars, "sma", symbol=symbol)
            costs = analysis.cost_sensitivity(bars, "sma", symbol=symbol)
            notes = metrics.warnings(result.metrics, result.benchmark_metrics, cost_curve=costs,
                                     bars=len(bars), trades=result.metrics["trades"])
            self.session.last_result = result
            self._show_result(result, notes, costs)

            self.rule("how to read that")
            for label, text in [
                ("strategy vs buy & hold",
                 "The first pair of numbers. If the strategy is lower, the work lost to doing "
                 "nothing -- which is the usual answer, and the reason this is the headline."),
                ("max fall",
                 "The worst peak-to-trough drop along the way. It's the number that decides "
                 "whether you could actually have held on."),
                ("sharpe",
                 "Return per unit of wobble. Under 0.5 you're being paid badly for the stress."),
                ("the slippage row",
                 "The same run at higher trading costs. A strategy that only works at zero cost "
                 "doesn't work."),
                ("the warnings",
                 "Printed above the chart on purpose. Most tools hide these."),
            ]:
                self.write(f"{ui.GUTTER}{INK}{label}{RESET}")
                self.write(*ui.note(text, _width(), colour=DIM, indent=ui.GUTTER + "  "))
            self.rule("try these next")
            self.write(*self._command_list([
                ("explain", "the same result, in plain words"),
                ("risk sma " + symbol, "drawdowns, the year-by-year table, trade stats"),
                ("sweep sma " + symbol, "every setting, to see if this one was luck"),
                ("wf sma " + symbol, "tune on old data, trade the next stretch blind"),
                ("vs sma rsi breakout " + symbol, "three strategies side by side"),
                ("paper start sma " + symbol, "run it forward on live prices, on paper"),
                ("help", "everything else"),
            ], _width()))
        self.job(f"tour {symbol}", work)

    def cmd_clear(self, args: list[str]) -> None:
        with self.lock:
            self.session.lines.clear()
        self.dirty = True

    def cmd_quit(self, args: list[str]) -> None:
        self.running = False


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def model() -> object | None:
    """Jugnu's model client, when this account has it.

    The Jugnu account only receives the `trading` package -- llm_client and
    the rest of the backend live in your home folder, which that account
    can't read. So a terminal running there does every bit of analysis and
    simply has no model: `ask`, `why`, `explain` and `build` say so instead
    of crashing on the import, which is what they used to do.
    """
    try:
        import llm_client
        return llm_client
    except ImportError:
        return None


def _width() -> int:
    """The usable width: the real one, minus the last cell (see draw)."""
    return max(30, shutil.get_terminal_size((100, 30)).columns - 1)


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
    "tour": Terminal.cmd_tour, "start": Terminal.cmd_tour, "learn": Terminal.cmd_tour,
    "clear": Terminal.cmd_clear,
    "quit": Terminal.cmd_quit, "exit": Terminal.cmd_quit,
}


def _pick_model_mode() -> None:
    """Run from a plain shell, the terminal has no API key in its
    environment. If Claude Code is installed and logged in, use the plan --
    it costs nothing per call and needs no configuration."""
    llm_client = model()
    if llm_client is None or os.environ.get("JUGNU_MODEL_MODE"):
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
