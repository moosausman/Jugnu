"""
The terminal face:  python -m trading <command>

    term                      the full-screen terminal (start here)
    backtest sma --symbol AAPL --from 2015-01-01
    replay   rsi --symbol BTC-USD --speed 40
    sweep    sma --symbol AAPL
    forward  sma --symbol AAPL --folds 5
    costs    sma --symbol AAPL
    paper    start|step|status|stop
    runs     [id]
    strategies

Everything prints plain text -- no curses, no plotting library -- so it
works over SSH and pastes into a message intact.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from . import analysis, metrics, paper, render, store, strategies
from .data import DataError, load


def _params(pairs: list[str] | None) -> dict:
    out: dict = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"--set wants name=value, got {item!r}")
        name, value = item.split("=", 1)
        try:
            out[name] = int(value)
        except ValueError:
            try:
                out[name] = float(value)
            except ValueError:
                out[name] = value
    return out


def _bars(args):
    return load(args.symbol, args.interval, start=getattr(args, "start", None),
                end=getattr(args, "end", None))


def _run(args):
    bars = _bars(args)
    result = analysis.backtest(bars, args.strategy, _params(args.set), symbol=args.symbol.upper(),
                               interval=args.interval, cash=args.cash, commission=args.commission,
                               slippage_bps=args.slippage)
    costs = analysis.cost_sensitivity(bars, args.strategy, _params(args.set), symbol=args.symbol.upper(),
                                      interval=args.interval, cash=args.cash, commission=args.commission)
    notes = metrics.warnings(result.metrics, result.benchmark_metrics, cost_curve=costs,
                             bars=len(bars), trades=result.metrics["trades"])
    print()
    print(render.summary(result, notes=notes))
    print()
    print("  Return at other slippage levels:")
    print(render.cost_table(costs))
    carlo = analysis.monte_carlo(result)
    if carlo.get("enough_trades"):
        print(f"\n  Reshuffling these {carlo['round_trips']} trades 1000 times: "
              f"{metrics.format_percent(carlo['p05'])} … {metrics.format_percent(carlo['p50'])} … "
              f"{metrics.format_percent(carlo['p95'])}  "
              f"({carlo['losing_share']:.0%} of orderings lose money)")
    run_id = store.save_run(result, notes)
    print(f"\n  saved as {run_id}\n")


def _replay(args):
    """Step through the run bar by bar, redrawing in place."""
    bars = _bars(args)
    result = analysis.backtest(bars, args.strategy, _params(args.set), symbol=args.symbol.upper(),
                               interval=args.interval, cash=args.cash, commission=args.commission,
                               slippage_bps=args.slippage)
    trades_by_date: dict[str, list] = {}
    for trade in result.trades:
        trades_by_date.setdefault(trade.date, []).append(trade)
    delay = 1.0 / max(1, args.speed)
    blotter: list[str] = []
    try:
        for i in range(2, len(result.equity) + 1):
            window = result.equity[:i]
            date = result.dates[i - 1]
            for trade in trades_by_date.get(date[:10], []):
                blotter.append(f"  {trade.date}  {trade.side:<4} {trade.shares:>10.4f} @ {trade.price:>10,.2f}"
                               + (f"   P&L {trade.pnl:>+9,.2f}" if trade.pnl is not None else ""))
            held = result.benchmark[:i]
            print("\x1b[2J\x1b[H", end="")
            print(f"  {result.symbol} · {result.strategy}   {result.dates[0]} → {date}"
                  f"   [{i}/{len(result.equity)}]\n")
            print(f"  equity  ${window[-1]:>12,.0f}   buy & hold  ${held[-1]:>12,.0f}"
                  f"   {metrics.format_percent(window[-1] / window[0] - 1)}")
            print("\n  " + render.sparkline(window, width=70))
            print("\n  trades")
            for line in blotter[-8:]:
                print(line)
            sys.stdout.flush()
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\n  stopped.")
        return
    print()
    print(render.summary(result, notes=metrics.warnings(result.metrics, result.benchmark_metrics,
                                                        bars=len(bars),
                                                        trades=result.metrics["trades"])))
    print()


def _sweep(args):
    bars = _bars(args)
    spec = strategies.get(args.strategy)
    ranges = dict(spec.ranges)
    for item in args.range or []:
        name, span = item.split("=", 1)
        low, high, step = (float(x) for x in span.split(":"))
        ranges[name] = (int(low), int(high), int(step)) if step.is_integer() else (low, high, step)
    result = analysis.sweep(bars, args.strategy, ranges, symbol=args.symbol.upper(),
                            interval=args.interval, cash=args.cash, commission=args.commission,
                            slippage_bps=args.slippage, limit=args.limit)
    print(f"\n  {args.symbol.upper()} · {args.strategy} · {result['runs']} combinations\n")
    print(render.heatmap(result))
    best = result["best"]
    print(f"\n  best      {' '.join(f'{k}={v}' for k, v in best['params'].items())}"
          f"   return {metrics.format_percent(best['return'])}  sharpe {best['sharpe']:.2f}")
    print(f"  median    {metrics.format_percent(result['median_return'])}"
          f"   benchmark {metrics.format_percent(result['benchmark_return'])}")
    print(f"  {result['robustness']:.0%} of all combinations beat buy & hold.")
    if result["robustness"] == 0:
        print("  ⚠ Nothing in this grid beat buy & hold. The best one here is still the wrong trade.")
    elif result["robustness"] < 0.25:
        print("  ⚠ The winner stands almost alone -- that's the shape of a fluke, not an edge.")
    print()


def _forward(args):
    bars = _bars(args)
    result = analysis.walk_forward(bars, args.strategy, symbol=args.symbol.upper(), interval=args.interval,
                                   folds=args.folds, cash=args.cash, commission=args.commission,
                                   slippage_bps=args.slippage)
    print(f"\n  {args.symbol.upper()} · {args.strategy} · tuned on each stretch, traded on the next\n")
    for fold in result["folds"]:
        params = " ".join(f"{k}={v}" for k, v in fold["params"].items())
        print(f"  {fold['from']} → {fold['to']}  {params:<28}"
              f"in {metrics.format_percent(fold['in_sample_return']):>8}"
              f"   out {metrics.format_percent(fold['out_of_sample_return']):>8}"
              f"   hold {metrics.format_percent(fold['benchmark_return']):>8}")
    print(f"\n  out of sample total {metrics.format_percent(result['out_of_sample_return'])}"
          f"   buy & hold {metrics.format_percent(result['benchmark_return'])}")
    print("  " + ("Beat buy & hold out of sample." if result["beat_benchmark"]
                  else "⚠ Did not beat buy & hold out of sample."))
    if not result["consistent"]:
        print("  ⚠ At least one out-of-sample stretch lost money.")
    print()


def _costs(args):
    bars = _bars(args)
    curve = analysis.cost_sensitivity(bars, args.strategy, _params(args.set), symbol=args.symbol.upper(),
                                      interval=args.interval, cash=args.cash, commission=args.commission,
                                      levels=(0.0, 2.0, 5.0, 10.0, 20.0, 50.0))
    print(f"\n  {args.symbol.upper()} · {args.strategy} · return by slippage\n")
    print(render.cost_table(curve))
    print()


def _paper(args):
    if args.action == "start":
        result = paper.start(args.symbol, args.strategy, _params(args.set), cash=args.cash,
                             interval=args.interval)
        print(f"\n  paper account {result['id']} · {result['symbol']} · {result['strategy']}"
              f" · ${result['equity']:,.2f}\n")
        return
    if args.action == "stop":
        print("  stopped." if paper.stop(args.id) else "  no such account.")
        return
    if args.action == "step":
        rows = [paper.step(args.id)] if args.id else paper.step_all()
    else:
        rows = [paper.status(args.id)] if args.id else paper.all_status()
    if not rows:
        print("\n  No paper accounts. Start one:  python -m trading paper start --symbol AAPL --strategy sma\n")
        return
    print()
    for row in rows:
        if "error" in row:
            print(f"  {row['symbol']:<10} error: {row['error']}")
            continue
        holding = f"{row['position']:.4f} @ ${row['price']:,.2f}" if row["in_market"] else "in cash"
        print(f"  {row['id']}  {row['symbol']:<10} {row['strategy']:<10} "
              f"${row['equity']:>10,.2f}  {metrics.format_percent(row['return']):>8}"
              f"   hold {metrics.format_percent(row['buy_and_hold_return']):>8}   {holding}")
    print()


def _runs(args):
    if args.id:
        run = store.get_run(args.id)
        if run is None:
            print("  no such run.")
            return
        print(json.dumps({k: run[k] for k in ("id", "symbol", "strategy", "params", "start", "end",
                                              "metrics", "benchmark", "notes")}, indent=2))
        return
    rows = store.list_runs(args.limit)
    if not rows:
        print("\n  No saved runs yet.\n")
        return
    print()
    for row in rows:
        when = dt.datetime.fromtimestamp(row["created"]).strftime("%b %d %H:%M")
        params = " ".join(f"{k}={v}" for k, v in row["params"].items())
        print(f"  {row['id']}  {when}  {row['symbol']:<9} {row['strategy']:<9} {params:<24}"
              f"{metrics.format_percent(row['return']):>9}  hold {metrics.format_percent(row['benchmark_return']):>9}")
    print()


def _strategies(args):
    print()
    for spec in strategies.describe():
        defaults = " ".join(f"{k}={v}" for k, v in spec["defaults"].items())
        print(f"  {spec['name']:<12} {spec['description']}")
        if defaults:
            print(f"  {'':<12} defaults: {defaults}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m trading",
                                     description="Paper-trading simulator. No broker, no real money.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub, strategy=True):
        if strategy:
            sub.add_argument("strategy", help="sma, rsi, breakout, volatility, hold")
        sub.add_argument("--symbol", default="AAPL", help="AAPL, RY.TO, BTC-USD…")
        sub.add_argument("--interval", default="1d", choices=["1d", "1h", "1wk"])
        sub.add_argument("--from", dest="start", default=None, help="YYYY-MM-DD")
        sub.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD")
        sub.add_argument("--cash", type=float, default=10_000.0)
        sub.add_argument("--commission", type=float, default=1.0, help="per trade, in dollars")
        sub.add_argument("--slippage", type=float, default=5.0, help="basis points")
        sub.add_argument("--set", action="append", help="parameter, e.g. --set fast=20")
        return sub

    common(subparsers.add_parser("backtest", help="run it once and report")).set_defaults(fn=_run)
    replay = common(subparsers.add_parser("replay", help="watch it trade, bar by bar"))
    replay.add_argument("--speed", type=int, default=25, help="bars per second")
    replay.set_defaults(fn=_replay)
    sweep = common(subparsers.add_parser("sweep", help="every parameter combination, as a heatmap"))
    sweep.add_argument("--range", action="append", help="name=low:high:step")
    sweep.add_argument("--limit", type=int, default=400)
    sweep.set_defaults(fn=_sweep)
    forward = common(subparsers.add_parser("forward", help="walk-forward: tune, then trade blind"))
    forward.add_argument("--folds", type=int, default=4)
    forward.set_defaults(fn=_forward)
    common(subparsers.add_parser("costs", help="return at each slippage level")).set_defaults(fn=_costs)

    paper_parser = subparsers.add_parser("paper", help="run forward on live prices, imaginary money")
    paper_parser.add_argument("action", choices=["start", "step", "status", "stop"])
    paper_parser.add_argument("--id", default=None)
    paper_parser.add_argument("--symbol", default="AAPL")
    paper_parser.add_argument("--strategy", default="sma")
    paper_parser.add_argument("--interval", default="1d", choices=["1d", "1h", "1wk"])
    paper_parser.add_argument("--cash", type=float, default=10_000.0)
    paper_parser.add_argument("--set", action="append")
    paper_parser.set_defaults(fn=_paper)

    runs = subparsers.add_parser("runs", help="saved backtests")
    runs.add_argument("id", nargs="?", default=None)
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(fn=_runs)

    subparsers.add_parser("strategies", help="what's available").set_defaults(fn=_strategies)
    term = subparsers.add_parser("term", help="the full-screen Jugnu terminal")
    term.add_argument("commands", nargs="*", help="commands to run on start, e.g. 'bt sma AAPL 2015'")
    term.set_defaults(fn=_term)
    subparsers.add_parser("install-openbb",
                          help="add OpenBB (fundamentals, options) in its own environment"
                          ).set_defaults(fn=_install_openbb)
    api = subparsers.add_parser("api", help="JSON in, JSON out (used by remote runs)")
    api.add_argument("payload", nargs="?", default="", help="a JSON object; omit to read stdin")
    api.set_defaults(fn=_api)
    return parser


def _api(args):
    """The machine face. Prints exactly one line of JSON."""
    from . import appapi
    raw = args.payload or sys.stdin.read()
    try:
        request = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad JSON: {e}"}))
        return
    print(json.dumps(appapi.handle(request), default=str))


def _install_openbb(args):
    from . import sources
    print("\n  Installing OpenBB into its own environment beside Jugnu's data.")
    print("  A few hundred MB, one-off. Prices keep coming from Yahoo and Binance either way.\n")
    ok, detail = sources.install(on_status=lambda text: print(f"  {text}", flush=True))
    print(f"\n  {'installed ' + detail if ok else 'failed: ' + detail}\n")


def _term(args):
    from .terminal import main as terminal_main
    raise SystemExit(terminal_main(getattr(args, "commands", None)))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (DataError, KeyError, ValueError) as e:
        print(f"\n  {e}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
