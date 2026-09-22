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

## Model mode (API / Hybrid / Local)
Switched live from the ··· menu or Settings › Models (`set_model_mode`, no
restart). Hybrid runs the EASY role -- chit-chat, plain answers, summaries of
selected text, and background checks (verifying, routing, reflecting,
extracting) -- on Ollama for free, and everything that calls tools on the
API. If Ollama is down, Hybrid quietly uses the API for that call. Local runs
every chat role on Ollama. Local calls are counted separately (`local_calls`).

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
