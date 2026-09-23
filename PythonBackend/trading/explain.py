"""
explain.py -- saying what the numbers mean, in words.

Two levels, deliberately:

  plain()    written from the run's own data, instantly, with no model
             call. Always shown under a result, because a number you
             can't interpret is worse than no number.
  deep()     asks Jugnu's model, using the real figures as its material.
             Only when the user types `explain` or `ask`.

The tone rule: no jargon without its meaning attached, and never a
reassuring sentence about a losing strategy.
"""
from __future__ import annotations

from .engine import Result

# What the words mean, for the `explain <term>` command. Written for
# someone who has never read a finance textbook.
GLOSSARY = {
    "sharpe": "Return per unit of wobble. Above 1 is good, under 0.5 means you're being paid "
              "badly for the stress. It's the return divided by how much the value jumps around.",
    "sortino": "Like Sharpe, but it only counts downward wobble. Being up a lot in one day isn't "
               "a risk you need compensating for.",
    "drawdown": "The worst peak-to-trough fall. -36% means that at some point your $10,000 was "
                "worth $6,400 and you had to sit there watching it.",
    "cagr": "The smooth yearly rate that would get you from the start value to the end value. "
            "Real years are lumpier than this.",
    "slippage": "The gap between the price you saw and the price you got, in basis points "
                "(5bps = 0.05%). It's small per trade and enormous over hundreds of them.",
    "exposure": "How much of the time your money was actually in the market rather than in cash.",
    "win rate": "The share of completed round trips that made money. A high win rate with a low "
                "return means the losses are bigger than the wins.",
    "benchmark": "Buying the same thing on day one and doing nothing. If a strategy can't beat "
                 "that, it's an expensive way to be worse.",
    "walk forward": "Pick your settings on one stretch of history, then trade the next stretch "
                    "blind. It's the difference between a plan and a memory.",
    "robustness": "The share of nearby settings that also worked. If only one combination made "
                  "money, you found a coincidence, not an edge.",
    "monte carlo": "Shuffling the order of your own trades thousands of times. Same trades, "
                   "different sequence -- it shows how much of your result was luck of timing.",
    "basis point": "One hundredth of a percent. 20bps is 0.2%.",
}


def _years(result: Result) -> float:
    """Calendar years the run covers. Crypto trades 365 days a year and
    equities about 252, so this has to ask rather than assume -- it was
    calling a 9-year BTC run "13 years"."""
    from .data import periods_per_year
    return len(result.equity) / periods_per_year(result.symbol, result.interval)


def _period_words(result: Result) -> str:
    years = _years(result)
    if result.interval != "1d":
        return f"{len(result.equity)} bars"
    return f"about {years:.0f} years" if years >= 1.5 else f"{len(result.equity)} trading days"


def plain(result: Result) -> list[str]:
    """Three or four sentences that say what actually happened. No model."""
    mine, theirs = result.metrics, result.benchmark_metrics
    lines: list[str] = []
    gap = mine["total_return"] - theirs["total_return"]
    verb = "beat" if gap > 0 else "lost to"
    lines.append(
        f"Over {_period_words(result)}, {result.strategy} turned ${result.cash:,.0f} into "
        f"${mine['final']:,.0f}. It {verb} buying and holding {result.symbol}, which would have "
        f"ended at ${theirs['final']:,.0f}.")

    if mine["exposure"] < 0.8:
        lines.append(
            f"It was only in the market {mine['exposure'] * 100:.0f}% of the time -- the rest in cash. "
            f"That's why it missed part of the rise, and also why its worst fall "
            f"({mine['max_drawdown'] * 100:.0f}%) was milder than holding "
            f"({theirs['max_drawdown'] * 100:.0f}%).")
    else:
        lines.append(f"It was invested almost the whole time, so it lived through nearly the same "
                     f"falls as holding: {mine['max_drawdown'] * 100:.0f}% at worst.")

    if mine["trades"]:
        per_year = mine["trades"] / max(_years(result), 0.1)
        win = f", {mine['win_rate'] * 100:.0f}% of them profitable" if mine["win_rate"] is not None else ""
        lines.append(f"It traded {mine['trades']} times ({per_year:.0f} a year{win}), and every one "
                     f"paid ${result.commission:g} plus {result.slippage_bps:g}bps of slippage.")

    if mine["sharpe"] < theirs["sharpe"]:
        lines.append(f"Risk-adjusted, it was worse too: Sharpe {mine['sharpe']:.2f} against "
                     f"{theirs['sharpe']:.2f}. You took on the timing risk and were paid less for it.")
    else:
        lines.append(f"Risk-adjusted it did better: Sharpe {mine['sharpe']:.2f} against "
                     f"{theirs['sharpe']:.2f} -- a smoother ride for the return it got.")
    return lines


def plain_sweep(sweep_result: dict) -> list[str]:
    robustness = sweep_result["robustness"]
    lines = [f"{sweep_result['runs']} settings tried. The best made "
             f"{sweep_result['best']['return'] * 100:+.1f}%, the middle one "
             f"{sweep_result['median_return'] * 100:+.1f}%, and holding made "
             f"{sweep_result['benchmark_return'] * 100:+.1f}%."]
    if robustness == 0:
        lines.append("Not one combination beat holding. There's nothing here to tune -- the idea "
                     "itself doesn't work on this symbol.")
    elif robustness < 0.25:
        lines.append(f"Only {robustness * 100:.0f}% beat holding. When a single setting wins and its "
                     "neighbours lose, you've found a quirk of this exact history, and it won't repeat.")
    else:
        lines.append(f"{robustness * 100:.0f}% of settings beat holding, so the effect is broad rather "
                     "than one lucky number. That's the shape a real edge makes -- though a rising "
                     "market flatters everything.")
    return lines


def plain_paper(account: dict) -> list[str]:
    where = ("holding" if account.get("in_market") else "in cash")
    return [f"{account['symbol']} · {account['strategy']}: ${account['equity']:,.2f}, "
            f"{account['return'] * 100:+.1f}% since it opened, against "
            f"{account['buy_and_hold_return'] * 100:+.1f}% for holding. Currently {where}."]


def deep(result: Result, question: str = "") -> str:
    """Ask the model, with the real numbers as its material. Used by
    `explain` and `ask` -- the only place in the terminal that costs a call."""
    import llm_client

    mine, theirs = result.metrics, result.benchmark_metrics
    trades = result.trades[-25:]
    facts = "\n".join([
        f"symbol {result.symbol}, strategy {result.strategy} {result.params}",
        f"period {result.dates[0]} to {result.dates[-1]} ({len(result.equity)} bars, {result.interval})",
        f"costs: {result.slippage_bps}bps slippage, ${result.commission} commission, ${result.cash} start",
        f"strategy: return {mine['total_return']:.3f}, cagr {mine['cagr']:.3f}, "
        f"maxdd {mine['max_drawdown']:.3f}, sharpe {mine['sharpe']:.2f}, trades {mine['trades']}, "
        f"win rate {mine['win_rate']}, exposure {mine['exposure']:.2f}",
        f"buy & hold: return {theirs['total_return']:.3f}, cagr {theirs['cagr']:.3f}, "
        f"maxdd {theirs['max_drawdown']:.3f}, sharpe {theirs['sharpe']:.2f}",
        "recent trades: " + "; ".join(f"{t.date} {t.side} @{t.price:.2f}"
                                      + (f" pnl {t.pnl:+.0f}" if t.pnl is not None else "")
                                      for t in trades),
        "equity by year: " + _yearly(result),
    ])
    instruction = question.strip() or (
        "Explain what this strategy actually did and why it ended where it did. Say which periods "
        "made or lost the money, and what would break it.")
    messages = [
        {"role": "system", "content":
            "You are Jugnu's market analyst, writing into a terminal. Be concrete and brief: at most "
            "six short lines, no headings, no bullet symbols, no markdown. Use the numbers given. "
            "Never predict prices, never recommend buying or selling anything, and never reassure "
            "the user about a strategy that lost to buy-and-hold. If the data doesn't answer the "
            "question, say so."},
        {"role": "user", "content": f"{facts}\n\n{instruction}"},
    ]
    response = llm_client.chat(messages, role="EASY")
    return (response["message"].content or "").strip()


def _yearly(result: Result) -> str:
    by_year: dict[str, float] = {}
    for date, value in zip(result.dates, result.equity):
        by_year[date[:4]] = value
    years = sorted(by_year)
    out = []
    previous = result.cash
    for year in years:
        out.append(f"{year} {(by_year[year] / previous - 1) * 100:+.0f}%")
        previous = by_year[year]
    return ", ".join(out)


def term(word: str) -> str | None:
    key = word.strip().lower()
    for name, meaning in GLOSSARY.items():
        if key == name or key in name.split():
            return f"{name}: {meaning}"
    return None
