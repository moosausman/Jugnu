"""
approvals.py -- asking the user before consequential actions.

When policy.decide() says "confirm", the tool runtime calls request(),
which:
  1. records a pending approval (in memory, and in the audit log),
  2. tells the UI -- {"type": "approval_request", ...} -- so it can show a
     card with the exact action, why it needs a yes, and its risk,
  3. blocks that one tool call (not the whole backend) until the user
     answers, the run is cancelled, or the approval times out.

Answers come back through bridge.py's {"type": "approval_response"}:
    decision   "approve"       this one call
               "approve_task"  this call and the same kind of action for the
                               rest of this task (never for HIGH risk)
               "deny"          refuse; the agent is told and must adapt
    note       optional text passed to the agent ("use the other folder")

Without a UI (the CLI REPL), the question is asked on the terminal instead.
With neither (a headless test), requests are denied -- never silently
approved.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import audit
from policy import Assessment, Risk
from run_context import RunCancelled, RunContext

APPROVAL_TIMEOUT_SECONDS = 30 * 60


@dataclass
class PendingApproval:
    approval_id: str
    run_id: str
    task_id: object
    tool: str
    assessment: Assessment
    why: str
    created: float = field(default_factory=time.time)
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = ""
    note: str = ""

    def to_event(self) -> dict:
        return {
            "type": "approval_request",
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "tool": self.tool,
            "summary": self.assessment.summary,
            "risk": self.assessment.risk.label,
            "capability": self.assessment.capability,
            "reversible": self.assessment.reversible,
            "reasons": list(self.assessment.reasons),
            "why": self.why,
            "allow_task_scope": self.assessment.risk < Risk.HIGH,
            "created": self.created,
        }


_pending: dict[str, PendingApproval] = {}
_lock = threading.Lock()
_emitter: Optional[Callable[[dict], None]] = None
_interactive_console = False


def set_emitter(emit: Callable[[dict], None]) -> None:
    """bridge.py registers its emit() here at startup."""
    global _emitter
    _emitter = emit


def use_console(enabled: bool = True) -> None:
    """main.py (the CLI) asks on the terminal instead of through a UI."""
    global _interactive_console
    _interactive_console = enabled


def pending() -> list[dict]:
    with _lock:
        return [p.to_event() for p in _pending.values()]


def respond(approval_id: str, decision: str, note: str = "") -> bool:
    """Delivers the user's answer. Returns False for an unknown/expired id."""
    if decision not in ("approve", "approve_task", "deny"):
        decision = "deny"
    with _lock:
        item = _pending.get(approval_id)
    if item is None:
        return False
    if decision == "approve_task" and item.assessment.risk >= Risk.HIGH:
        decision = "approve"  # HIGH risk is always confirmed one call at a time
    item.decision, item.note = decision, (note or "")[:1000]
    item.event.set()
    return True


def cancel_for_run(run_id: str) -> None:
    """Wakes every approval waiting on a cancelled run."""
    with _lock:
        items = [p for p in _pending.values() if p.run_id == run_id]
    for item in items:
        item.decision = item.decision or "deny"
        item.event.set()


def _send(ctx: RunContext, payload: dict) -> None:
    """Through the run's own emitter when it has one, otherwise the global
    one -- a background run without an emitter must still be able to ask."""
    if ctx.emit is not None:
        ctx.send(payload)
    elif _emitter is not None:
        try:
            _emitter({"run_id": ctx.run_id, **payload})
        except Exception:
            pass


def _console_ask(item: PendingApproval) -> tuple[str, str]:
    a = item.assessment
    print(f"\n⚠️  Jugnu wants to: {a.summary}", flush=True)
    print(f"   risk: {a.risk.label}  ({'; '.join(a.reasons) or item.why})", flush=True)
    try:
        answer = input("   Allow? [y]es / [t]ask / [n]o: ").strip().lower()
    except EOFError:
        return "deny", ""
    if answer.startswith("t") and a.risk < Risk.HIGH:
        return "approve_task", ""
    if answer.startswith("y"):
        return "approve", ""
    return "deny", answer if answer not in ("n", "no", "") else ""


def request(ctx: RunContext, tool: str, assessment: Assessment, why: str,
            timeout: float = APPROVAL_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Blocks until the user decides. Returns (decision, note) where
    decision is "approve", "approve_task", or "deny". Raises RunCancelled
    if the run is cancelled while waiting."""
    item = PendingApproval(
        approval_id=uuid.uuid4().hex[:12], run_id=ctx.run_id, task_id=ctx.current_task,
        tool=tool, assessment=assessment, why=why,
    )
    audit.log("approval_request", run_id=ctx.run_id, task_id=ctx.current_task, tool=tool,
              risk=assessment.risk.label, status="pending", summary=assessment.summary,
              detail={"why": why, "reasons": assessment.reasons, "approval_id": item.approval_id})

    if _interactive_console and _emitter is None and sys.stdin.isatty():
        decision, note = _console_ask(item)
    elif _emitter is None:
        decision, note = "deny", "No one is available to approve this right now."
    else:
        with _lock:
            _pending[item.approval_id] = item
        _send(ctx, item.to_event())
        try:
            _send(ctx, {"type": "task_status", "task_id": ctx.current_task, "status": "waiting_approval"})
            deadline = time.time() + timeout
            while not item.event.wait(0.5):
                if ctx.cancelled:
                    raise RunCancelled()
                if time.time() > deadline:
                    item.decision, item.note = "deny", "The approval request timed out."
                    break
            if ctx.cancelled:
                raise RunCancelled()
            decision, note = item.decision or "deny", item.note
        finally:
            with _lock:
                _pending.pop(item.approval_id, None)
            if _emitter is not None:
                try:
                    _emitter({"type": "approval_resolved", "approval_id": item.approval_id,
                              "run_id": ctx.run_id, "decision": item.decision or "deny"})
                except Exception:
                    pass
            _send(ctx, {"type": "task_status", "task_id": ctx.current_task, "status": "running"})

    audit.log("approval_decision", run_id=ctx.run_id, task_id=ctx.current_task, tool=tool,
              risk=assessment.risk.label, status=decision, summary=assessment.summary,
              detail={"note": note, "approval_id": item.approval_id})
    return decision, note
