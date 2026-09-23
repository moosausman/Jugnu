# Jugnu architecture

```
SwiftUI app ── chat · approval cards · Activity panel (⌘J) · Settings
   │  JSON lines over stdio, full-duplex (bridge.py)
bridge.py ── TurnWorker (one foreground turn at a time) + management requests
   ├─ session.py      route → fast path | direct agent | plan → dispatch → persist
   ├─ fastpath.py     "play X", "pause", "volume 40", "open Safari": one tool call, no model
   ├─ router.py       small talk, pending answers, session/background hints, and
   │                  direct_agent(): single-intent requests skip the planner
   ├─ planner.py      goal → validated PlanSpec (schemas.py); replan() after failures
   │                  (multi-step, scheduled, long-form or ambiguous requests only)
   ├─ executor.py     parallel DAG: exclusive screen lock, retries w/ feedback, replanning,
   │                  partial continuation, checkpoints, objective check, honest synthesis
   ├─ verifier.py     postconditions (file exists, event listed, app running); model
   │                  verdicts only for complex plans
   ├─ agent_core.py   one agent's tool loop (ask_user pauses the plan); stops at once on a
   │                  missing macOS permission, a switched-off capability or a denial, and
   │                  after 3 failed calls in a row
   ├─ search.py       search_web: MacBridge /web/search (DuckDuckGo in WebKit) → top pages →
   │                  best-matching passages. No model calls.
   ├─ tool_runtime.py validate → policy → approve → run (timeout/cancel) → classify → redact → audit
   │    ├─ policy.py       risk per tool + shell/AppleScript classifiers + decide()
   │    ├─ permissions.py  autonomy mode, capability switches, folder scopes (permissions.json)
   │    ├─ approvals.py    approval cards; blocks one call, not the backend
   │    ├─ injection.py    untrusted-content wrapping + injection detection (taints the run)
   │    └─ redact.py       secrets out of prompts, logs, memory, UI
   ├─ memory_store.py kinds: user / episodic / semantic / error / strategy; confidence, source,
   │                  scope, contradictions, history, decay; tool reliability stats
   ├─ learning.py     per-call hooks (tool stats, error→fix memory), post-run reflection,
   │                  correction learning, code-change *proposals* (never applied)
   ├─ skill_library.py learned workflows: suggested → active (user-approved), versions, stats
   ├─ tasks_store.py  durable runs + checkpoints; resume after restart; waiting-on-user runs
   ├─ scheduler.py    one-off / recurring tasks and timers (catch-up, stale skip)
   ├─ workflows.py    background runs, resume, scheduler callbacks
   └─ audit.py        everything planned/called/approved/failed/remembered
MacBridge (Swift, per macOS session: :8765 you, :8766 Jugnu account)
   Files · Shell · AppleScript · Calendar · Reminders · Contacts · Mail · Safari (+page text)
   Keyboard · Mouse · Accessibility · Screen · Windows · OCR (Vision) · Documents (PDFKit)
   Permission status · Spotify (+ keyless search) · WhatsApp (+ one-call /whatsapp/message)
   Web search (off-screen WebKit)

## Model mode (API / Hybrid / Local / Claude)
Switched live from the ··· menu or Settings › Models (`set_model_mode`, no
restart). Hybrid runs the EASY role -- chit-chat, plain answers, summaries of
selected text, and background checks (verifying, routing, reflecting,
extracting) -- on Ollama for free, and everything that calls tools on the
API. If Ollama is down, Hybrid quietly uses the API for that call. Local runs
every chat role on Ollama. Local calls are counted separately (`local_calls`).

Claude runs every chat role on the user's Claude plan through the Claude Code
CLI (`claude_code.py`): one `claude -p --output-format json` run per call,
with Claude Code's own tools, MCP servers, settings and session history off,
in an empty working folder, and `ANTHROPIC_API_KEY` removed from its
environment so it bills the plan. Tools go in the system prompt and Claude
answers `{"content", "tool_calls"}` JSON, which comes back in the same shape
as the API's, so Jugnu still does all the acting (policy, approvals,
MacBridge). Sonnet for planning and tools, Haiku for EASY/FAST (Settings ›
Models). It never falls back to the API. Needs the standalone CLI, logged in
once with `claude`; the copy inside the Claude desktop app isn't used.
Personal use on your own Mac only: a Jugnu shipped to other people must use
API keys.

## Jugnu's desk (⌘⇧D)
A live view of the macOS session Jugnu works in -- normally the Jugnu
account, which has a full desktop of its own even while it's switched out.
MacBridge keeps one `SCStream` running and hands out its latest JPEG
(`/screen/stream/start|frame|stop`); the app long-polls it, so a still
screen costs nothing and the capture stops itself once the window closes.
No model is involved: pixels only, no API or plan usage.

What a screen-sharing window can't do, and this does: it captions the work
from the run's own events (objective, step, current tool), keeps about two
minutes of frames so you can scrub back to the moment something went wrong
with each tool call marked on the line, and hands control over politely --
taking the pointer pauses the run (`pause`/`resume`) instead of fighting it,
and giving it back resumes. Clicks and keys in the window are replayed
through `/mouse/*` and `/keyboard/*` in that session.

Note: MacBridge's "pointer parked in the top-left corner" escape gesture now
applies only to a session someone is sitting at (`kCGSSessionOnConsoleKey`).
In a switched-out session the pointer rests there forever, which blocked
every move the Jugnu account tried to make.

## Trading -- a paper simulator (⌘⇧T opens the terminal)
`trading/` is a self-contained backtester: price history from Yahoo's chart
endpoint and Binance's klines (no keys, no extra packages, cached in
`trading.db`), an event-driven engine, five strategies, and the analysis that
keeps a backtest honest. Three faces, one engine: the terminal below,
Jugnu's tools (`tools/trading_tools.py`) so you can ask in chat, and
`python -m trading` for scripting. There is no trading window: a SwiftUI
view over the same engine was a slower way to reach the same numbers, so
⌘⇧T opens the terminal in Terminal.app instead.

Rules the engine keeps, because they're where homemade backtesters cheat:
a strategy sees bars only up to the one it decides on (`engine.View` raises
if it reaches further), fills happen at the NEXT bar's open with slippage and
commission deducted, and trades fire only when the target weight moves
(a rebalance band), not on every price drift. Every result is reported
against buy & hold over the same period with the same costs, and
`metrics.warnings()` says out loud when it underperforms, when costs kill it,
or when there are too few trades to mean anything. `analysis` adds cost
sensitivity, parameter sweeps with a robustness figure, walk-forward, and a
Monte Carlo trade shuffle.

Paper accounts (`paper.py`) run the same rules forward on live prices with
imaginary money, stepping idempotently per bar, and starting one schedules a
daily check that reports back in chat. It is a simulator: no brokerage, no
API keys, no orders. The tools are read-risk (`paper_trade` is low) under a
`trading` capability, and Jugnu reports what a simulation did rather than
advising what to buy.

## The Jugnu terminal
`PythonBackend/jugnu` (or `python -m trading term`) is the main way in:
a full screen with live markets on top, work in the middle and a command
line at the bottom. Raw ANSI, no curses and no dependencies, so it starts
instantly and works over SSH. Quotes come from Yahoo and Binance in a
thread pool, cached a few seconds; network and model work runs off the
input loop so typing never blocks.

Commands: `q` `ch` `wl` `mkt` `screen` `corr` `alert` (prices and scanning),
`bt` `vs` `pf` `risk` `sweep` `wf` `costs` `replay` `paper` `runs` `trades`
`y` `strats` `show` (research, all local arithmetic), `jobs` `job` `on`
(what's running, and where), `fa` `opt` `source` (OpenBB), and four that
spend a model call and say so: `build` `ask` `why` `explain`.

**It fits the window.** `ui.py` holds the terminal's typography: every
panel is built from `rule`, `stats`, `table`, `note`, `chart`, `bar`, and
each takes the window width and respects it. Tables shed their least
important columns rather than wrapping, prose wraps rather than truncating,
charts size to the pane, and the market panel collapses to a one-line
ticker below 96 columns or 28 rows. A test asserts no panel ever draws a
line wider than the window, because the first version hardcoded column
positions and turned into confetti at 80 columns.

**Heavy work defaults to the Jugnu account.** When that session is up, the
terminal points there on start: sweeps don't compete with the session
you're typing in, and you never switch user. `on me` keeps work local;
`on jugnu` sends it back. A job's `@jugnu` label means it actually ran
there -- `bt`, `sweep`, `wf`, `risk`, `pf` and `screen` each have a remote
path through `appapi`, and both paths render through the same panel
function so the two can't drift.

**Several at once.** Every heavy command becomes a numbered job
(`jobs.py`): the prompt returns immediately, four run in parallel, a strip
under the market panel shows what's in flight, and `job N` re-prints one.
Output is attributed per job through a thread-local, so concurrent runs
don't interleave.

**On the other account.** `on jugnu` sends the heavy commands to the second
macOS login (`remote.py`). Two macOS facts shape it: your home folder is
drwx------, so that account can't read the repo, and it has only the system
Python. So the package is copied to /Users/Shared/jugnu-trading and speaks
HTTP through `_http.py` (requests when present, urllib otherwise) --
the whole package imports nothing outside the standard library. Commands
travel over MacBridge's shell endpoint on :8766 and come back as JSON from
`appapi.py` (`python -m trading api '{...}'`).

**Quant depth.** `risk.py`: Calmar, Ulcer, VaR/CVaR from the real
distribution, beta and alpha against the benchmark, rolling Sharpe, time
under water, per-year returns, and trade statistics (payoff, profit factor,
expectancy, hold times, whether one trade carried the result).
`portfolio.py`: several sleeves (strategy × symbol × weight) aligned on a
union calendar with forward fill, rebalanced on a schedule, against an
equal-weight buy-and-hold of the same symbols, with the correlation matrix
that shows whether "diversification" is one bet held twice. `screen.py`
scans a universe for RSI extremes, volume spikes, 52-week position, trend
and momentum, filterable (`screen rsi<35 vol>2`). `alerts.py` watches
price or indicator conditions in the background and fires a macOS
notification through MacBridge, once, then disarms.

**OpenBB, optionally.** `sources.py` adds fundamentals, options and analyst
data when OpenBB is installed. It lives in a sidecar virtualenv beside
Jugnu's data, never in the app bundle: it's ~250MB and AGPLv3, so linking
it into something distributable would make that AGPL too. Prices always
come from Yahoo and Binance directly. `jugnu install-openbb` sets it up
(and runs `openbb.build()`, without which a fresh install's first call dies
on its own stale generated modules).

Two things make it Jugnu's rather than another ticker:

**Everything explains itself for free.** `explain.py` writes plain English
from the run's own numbers with no model call -- what the strategy did,
when it was in cash, what the costs took -- because a number you can't
interpret is worse than no number. `explain <term>` defines the jargon.
The model-backed layer (`ask`, `explain` with a result) reasons over the
same figures and is told never to flatter a losing strategy.

**`build <description>` writes a real strategy.** `authoring.py` asks the
model for a function, then refuses to trust it: the source is parsed and
walked (no imports, no attribute outside the context object, no dunders,
no unbounded loops, only a whitelist of calls) and then executed with a
stripped `__builtins__`. A rejection is fed back once so it can fix itself.
What survives is saved in `trading/custom/` as an ordinary file you can
read and edit, registered everywhere the registry is used, and immediately
backtested with the usual warnings. `ctx.entry_price` exists so stops and
targets are expressible.

`why SYM` reads Yahoo's news endpoint (a general web search returns
encyclopaedia pages when you want this morning), falls back to the open web
when there's nothing fresh, and is told to say when the sources don't
explain the move rather than inventing a reason.

## Shortcuts
⌥Space (global, configurable in Settings › General): opens Jugnu from any app
with the text selected there attached, if any (MacBridge `/ui/selection`:
Accessibility, or Cmd-C with the clipboard restored for apps Accessibility
can't read); pressed with Jugnu in front, hides it. Registered with Carbon,
so no extra permission; Settings says so if another app holds the shortcut.
Hold ⌥ while Jugnu is in front to talk; letting go sends. A local event
monitor, so Option in other apps is never affected.

## Cost per request
Every model call re-sends the agent's instructions and tool schemas, so the
number of calls is the cost. Roughly:
* "play X", "pause", "open Safari" -- no model call (fastpath.py)
* one-domain requests ("what's on my calendar", "who won…") -- the agent's own
  calls only, usually 2 (no planner, no verifier)
* questions answerable without tools -- 1 call
* multi-step work -- planner + agents + one synthesis; verification and
  reflection only when the plan is complex
Each turn reports its model calls and tokens (the `assistant` event's
`usage`, the audit log's `turn_end`, and the Activity panel).
```

## Rules that don't bend
* HIGH risk (send, publish, permanent delete, install, purchase, security settings,
  `git push`) is confirmed in every autonomy mode, one call at a time.
* Protected paths and anything outside allowed folders are confirmed.
* Work the user didn't start (proactive/background with no user request) asks before
  anything beyond reading.
* After content that looks like prompt injection, the run's medium-risk actions ask.
* Secrets are never stored in memory and are redacted everywhere else.
* Learning changes memories, strategies and skills only. Code changes are proposals.

## Tests
`cd PythonBackend && PythonEnv/bin/python -m unittest discover -s tests`
(129 tests; `test_bridge_e2e.py` runs the real bridge against a fake model server,
`test_efficiency.py` covers the fast path, direct routing and the loop's stop rules.)

## Adding a tool
1. Write the function (Google-style docstring with `Args:`) in `tools/`.
2. Add it to `_tool_functions` in `tools/tools.py`.
3. Give it a `ToolPolicy` in `policy.py` (tests fail if you don't).
4. Add it to an agent in `agents.py` (the registry check fails if no agent holds it).
5. If it returns outside content, leave it out of `injection.TRUSTED_OUTPUT_TOOLS`.
