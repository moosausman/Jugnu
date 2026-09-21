# Jugnu architecture

```
SwiftUI app ── chat · approval cards · Activity panel (⌘J) · Settings
   │  JSON lines over stdio, full-duplex (bridge.py)
bridge.py ── TurnWorker (one foreground turn at a time) + management requests
   ├─ session.py      route → context → plan → dispatch (now / scheduled / background) → persist
   ├─ router.py       small talk, answers to pending questions, session/background hints
   ├─ planner.py      goal → validated PlanSpec (schemas.py); replan() after failures
   ├─ executor.py     parallel DAG: exclusive screen lock, retries w/ feedback, replanning,
   │                  partial continuation, checkpoints, objective check, honest synthesis
   ├─ verifier.py     postconditions (file exists, event listed, app running) + model verdicts
   ├─ agent_core.py   one agent's tool loop (ask_user pauses the plan)
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
   Permission status · Spotify · WhatsApp
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
(100 tests; `test_bridge_e2e.py` runs the real bridge against a fake model server.)

## Adding a tool
1. Write the function (Google-style docstring with `Args:`) in `tools/`.
2. Add it to `_tool_functions` in `tools/tools.py`.
3. Give it a `ToolPolicy` in `policy.py` (tests fail if you don't).
4. Add it to an agent in `agents.py` (the registry check fails if no agent holds it).
5. If it returns outside content, leave it out of `injection.TRUSTED_OUTPUT_TOOLS`.
