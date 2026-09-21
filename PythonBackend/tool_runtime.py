"""
tool_runtime.py -- the one path every tool call takes.

    validate args  ->  assess risk  ->  allow / ask / deny  ->  run (timeout,
    cancellable)  ->  classify outcome  ->  redact  ->  wrap untrusted
    content  ->  audit  ->  notify hooks (learning, verification)

agent_core.run_tool_loop used to call the tool function directly. Now it
calls execute(), which returns the text the model sees plus a structured
ToolOutcome the rest of the system (executor, learning, UI) can reason
about without re-parsing strings.

Error taxonomy (ErrorType) and the recovery each one suggests:
    invalid_arguments   fix the arguments and retry
    unknown_tool        use a tool that exists
    not_found           correct the name/path, or search for it first
    conflict            it already exists / would overwrite -- change params
    os_permission       a macOS permission is missing -- tell the user how to grant it
    policy_denied       switched off in Jugnu's Permissions -- don't retry
    user_denied         the user said no -- adapt, don't ask again for the same thing
    timeout             retry once, or use a lighter tool
    unavailable         MacBridge / network / session down -- retry later or stop
    rate_limited        wait before retrying
    repeated_failure    blocked: same failing call too many times -- change approach
    tool_failure        anything else -- try a different tool or approach
"""

from __future__ import annotations

import contextvars
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

import approvals
import audit
import injection
import policy
import run_context
from redact import redact

MAX_RESULT_CHARS = 4000   # every step re-sends earlier results; keep them lean
DEFAULT_TIMEOUT = 180.0
MAX_IDENTICAL_FAILURES = 2      # same tool + same args failing this often -> blocked
MAX_TOOL_FAILURES_PER_RUN = 6   # same tool failing this often in one run -> blocked

# Per-tool wall-clock ceilings. Shell tools carry their own timeout argument.
_TIMEOUTS = {
    "search_web": 150.0, "screen_capture": 40.0, "ui_snapshot": 30.0, "whatsapp_read": 30.0,
    "mail_list": 75.0, "mail_search": 75.0, "notion_sync_workspace": 120.0,
}

_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool")


class ErrorType:
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    OS_PERMISSION = "os_permission"
    POLICY_DENIED = "policy_denied"
    USER_DENIED = "user_denied"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    REPEATED_FAILURE = "repeated_failure"
    TOOL_FAILURE = "tool_failure"


RECOVERY = {
    ErrorType.INVALID_ARGUMENTS: "Fix the arguments to match the tool's parameters and try again.",
    ErrorType.UNKNOWN_TOOL: "Use one of the tools you actually have.",
    ErrorType.NOT_FOUND: "Check the exact name/path first (search or list), then retry with the correct one.",
    ErrorType.CONFLICT: "The target already exists or would be overwritten -- pick a different name or confirm overwrite is intended.",
    ErrorType.OS_PERMISSION: "A macOS permission is missing. Don't retry -- tell the user exactly which permission to grant (System Settings → Privacy & Security).",
    ErrorType.POLICY_DENIED: "This capability is switched off in Jugnu's Permissions. Don't retry; use another way or report it.",
    ErrorType.USER_DENIED: "The user declined this action. Don't request it again; adapt the plan or report what's left undone.",
    ErrorType.TIMEOUT: "Retry once, or use a lighter/narrower tool. Check state first -- the action may have completed anyway.",
    ErrorType.UNAVAILABLE: "The service isn't reachable right now. Retry once later, or report it.",
    ErrorType.RATE_LIMITED: "Wait before retrying, or use a different source.",
    ErrorType.REPEATED_FAILURE: "This exact approach keeps failing. Change strategy -- a different tool, different arguments, or report the blocker.",
    ErrorType.TOOL_FAILURE: "Try a different tool or approach, or report the error if nothing else fits.",
}

# Whether retrying the *same* call can ever help.
RETRYABLE = {ErrorType.TIMEOUT, ErrorType.UNAVAILABLE, ErrorType.RATE_LIMITED}


@dataclass
class ToolOutcome:
    tool: str
    args: dict
    ok: bool
    text: str                       # what the model sees
    raw: Any = None                 # the tool's own return value
    error_type: str | None = None
    error: str | None = None
    risk: str = "read"
    decision: str = "allow"         # allow | confirmed | denied | blocked
    duration: float = 0.0
    injection_score: int = 0
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool, "ok": self.ok, "error_type": self.error_type,
            "error": self.error, "risk": self.risk, "decision": self.decision,
            "duration": round(self.duration, 3), "injection_score": self.injection_score,
        }


# Subscribers (learning, verification) -- called after every executed call.
_hooks: list[Callable[[ToolOutcome, "run_context.RunContext"], None]] = []


def add_hook(fn: Callable[[ToolOutcome, "run_context.RunContext"], None]) -> None:
    if fn not in _hooks:
        _hooks.append(fn)


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

def _coerce(value, expected: str):
    """Best-effort coercion of a model-supplied value to the schema type.
    Returns (value, ok)."""
    if expected == "string":
        if isinstance(value, str):
            return value, True
        if isinstance(value, (int, float, bool)):
            return str(value), True
        return value, False
    if expected == "integer":
        if isinstance(value, bool):
            return value, False
        if isinstance(value, int):
            return value, True
        if isinstance(value, float) and value.is_integer():
            return int(value), True
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value.strip()), True
        return value, False
    if expected == "number":
        if isinstance(value, bool):
            return value, False
        if isinstance(value, (int, float)):
            return value, True
        if isinstance(value, str):
            try:
                return float(value.strip()), True
            except ValueError:
                return value, False
        return value, False
    if expected == "boolean":
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "1", "0"):
            return value.strip().lower() in ("true", "yes", "1"), True
        if isinstance(value, int):
            return bool(value), True
        return value, False
    if expected == "array":
        if isinstance(value, list):
            return value, True
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed, True
            except json.JSONDecodeError:
                pass
        return value, False
    if expected == "object":
        if isinstance(value, dict):
            return value, True
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed, True
            except json.JSONDecodeError:
                pass
        return value, False
    return value, True


def validate_args(schema: dict | None, args) -> tuple[dict, list[str]]:
    """Validates and coerces args against a tool's JSON schema. Returns
    (clean_args, problems). Unknown parameters are problems, not silently
    passed through -- they used to surface as a Python TypeError."""
    if not isinstance(args, dict):
        return {}, [f"arguments must be a JSON object, got {type(args).__name__}"]
    if not schema:
        return dict(args), []
    params = schema.get("function", {}).get("parameters", {})
    props: dict = params.get("properties", {})
    required = params.get("required", [])
    clean, problems = {}, []
    for key, value in args.items():
        if key not in props:
            problems.append(f"unknown parameter '{key}' (valid: {', '.join(props) or 'none'})")
            continue
        if value is None and key not in required:
            continue
        expected = props[key].get("type", "string")
        coerced, ok = _coerce(value, expected)
        if not ok:
            problems.append(f"'{key}' must be {expected}, got {type(value).__name__}")
            continue
        clean[key] = coerced
    for key in required:
        if key not in clean:
            if key in args and args[key] is not None:
                continue  # already reported as a type problem
            problems.append(f"missing required parameter '{key}'")
    return clean, problems


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = [
    (ErrorType.OS_PERMISSION, re.compile(
        r"not authori[sz]ed|not permitted|permission (?:is )?denied|access (?:is )?denied|"
        r"privacy & security|accessibility (?:access|permission)|screen recording|errAEEventNotPermitted|-1743|"
        r"operation not permitted|TCC|check [a-z ]{0,20}permission|grant [a-z ]{0,30}access", re.I)),
    (ErrorType.NOT_FOUND, re.compile(
        r"not found|no such file|doesn't exist|does not exist|couldn't find|could not find|"
        r"unable to find|no matches|unknown (?:app|application)|can't get", re.I)),
    (ErrorType.CONFLICT, re.compile(r"already exists|file exists|would overwrite|overwrite is false", re.I)),
    (ErrorType.TIMEOUT, re.compile(r"timed? ?out|did not respond within|deadline exceeded", re.I)),
    (ErrorType.RATE_LIMITED, re.compile(r"rate.?limit|too many requests|\b429\b|quota", re.I)),
    (ErrorType.UNAVAILABLE, re.compile(
        r"connection (?:refused|reset|error)|failed to establish|max retries exceeded|"
        r"isn't responding|not running|unreachable|name or service not known|nodename nor servname|\b50[234]\b", re.I)),
    (ErrorType.INVALID_ARGUMENTS, re.compile(r"is required|must be|invalid|expected .* got|missing", re.I)),
]


def classify_error(message: str) -> str:
    for error_type, pattern in _ERROR_PATTERNS:
        if pattern.search(message or ""):
            return error_type
    return ErrorType.TOOL_FAILURE


def _result_failure(raw) -> str | None:
    """The failure message inside a tool's own return value, or None if it
    represents success. Mirrors the {"ok": false, "error": ...} convention
    every MacBridge route and most Python tools follow, plus the plain-text
    failures a few older tools return."""
    if isinstance(raw, dict):
        if raw.get("ok") is False or raw.get("success") is False:
            return str(raw.get("error") or raw.get("message") or raw.get("stderr") or "failed")
        if "exitCode" in raw and isinstance(raw.get("exitCode"), int) and raw["exitCode"] != 0:
            detail = (raw.get("stderr") or raw.get("stdout") or "").strip()
            return f"command exited with code {raw['exitCode']}" + (f": {detail[:500]}" if detail else "")
        return None
    if isinstance(raw, str):
        head = raw.strip()[:200].lower()
        if head.startswith(("tool error", "error:", "app not found", "could not ", "unknown tool",
                            "search_failed", "geocoding error", "weather lookup error", "no location found",
                            "not saved:", "wikipedia error", "notion error")):
            return raw.strip()[:500]
    return None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _stringify(raw) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, indent=2, default=str)
    except (TypeError, ValueError):
        return str(raw)


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    omitted = len(text) - MAX_RESULT_CHARS
    return (text[:MAX_RESULT_CHARS] + f"\n\n... [truncated -- {omitted} more characters omitted. "
            "Use a narrower or paginated tool (file_read_range, file_head/tail, a more specific "
            "command) if you need more.]")


def _timeout_for(tool: str, args: dict) -> float:
    if tool in ("shell_run", "shell_run_script"):
        try:
            t = float(args.get("timeout", 60) or 60)
        except (TypeError, ValueError):
            t = 60.0
        return (1800.0 if t <= 0 else min(t, 1800.0)) + 30.0
    return _TIMEOUTS.get(tool, DEFAULT_TIMEOUT)


def _failure_key(tool: str, args: dict) -> str:
    return tool + ":" + json.dumps(args, sort_keys=True, default=str)[:2000]


class _RunFailureTracker:
    """Per-run failure counts for repeated-error detection."""

    def __init__(self):
        self.by_call: dict[str, int] = {}
        self.by_tool: dict[str, int] = {}

    def blocked(self, tool: str, args: dict) -> str | None:
        if self.by_call.get(_failure_key(tool, args), 0) >= MAX_IDENTICAL_FAILURES:
            return f"{tool} already failed {MAX_IDENTICAL_FAILURES} times with these exact arguments in this task."
        if self.by_tool.get(tool, 0) >= MAX_TOOL_FAILURES_PER_RUN:
            return f"{tool} has failed {MAX_TOOL_FAILURES_PER_RUN} times in this task."
        return None

    def record(self, tool: str, args: dict, ok: bool, error_type: str | None) -> None:
        if ok or error_type in (ErrorType.USER_DENIED, ErrorType.POLICY_DENIED):
            return
        key = _failure_key(tool, args)
        self.by_call[key] = self.by_call.get(key, 0) + 1
        self.by_tool[tool] = self.by_tool.get(tool, 0) + 1


_trackers: dict[str, _RunFailureTracker] = {}
_trackers_lock = threading.Lock()


def _tracker(run_id: str) -> _RunFailureTracker:
    with _trackers_lock:
        if run_id not in _trackers:
            if len(_trackers) > 200:
                _trackers.clear()
            _trackers[run_id] = _RunFailureTracker()
        return _trackers[run_id]


def _known_fix(tool: str, error_type: str, error: str) -> str | None:
    """A fix learned from an earlier occurrence of this same failure."""
    try:
        import memory_store
        return memory_store.known_fix(tool, error_type, error)
    except Exception:
        return None


def _failed(tool, args, error_type, error, risk="read", decision="allow", raw=None, duration=0.0) -> ToolOutcome:
    error = redact(str(error))[:1500]
    payload = {"ok": False, "error_type": error_type, "error": error,
               "recovery": RECOVERY.get(error_type, RECOVERY[ErrorType.TOOL_FAILURE])}
    if error_type not in (ErrorType.USER_DENIED, ErrorType.POLICY_DENIED, ErrorType.UNKNOWN_TOOL):
        fix = _known_fix(tool, error_type, error)
        if fix:
            payload["known_fix_from_past_experience"] = fix
    return ToolOutcome(tool=tool, args=args, ok=False, text=json.dumps(payload, indent=2), raw=raw,
                       error_type=error_type, error=error, risk=risk, decision=decision, duration=duration)


def _in_agent_account() -> bool:
    """Whether this call runs in the Jugnu account (the agent session)."""
    try:
        from tools import macbridge
        return macbridge.active_session() == "agent"
    except Exception:
        return False


def execute(tool: str, args, functions: dict, schemas_by_name: dict | None = None,
            ctx: "run_context.RunContext | None" = None) -> ToolOutcome:
    """Runs one tool call under the full policy. Never raises, except
    RunCancelled when the run is cancelled."""
    ctx = ctx or run_context.current()
    ctx.checkpoint()
    run_context.check_task_stop()
    raw_args = args if isinstance(args, dict) else {}

    if tool not in functions:
        outcome = _failed(tool, raw_args, ErrorType.UNKNOWN_TOOL,
                          f"No tool named '{tool}' is available to this agent.")
        _finish(outcome, ctx)
        return outcome

    schema = (schemas_by_name or {}).get(tool)
    clean, problems = validate_args(schema, args)
    if problems:
        outcome = _failed(tool, raw_args, ErrorType.INVALID_ARGUMENTS, "; ".join(problems))
        _finish(outcome, ctx)
        return outcome

    tracker = _tracker(ctx.run_id)
    blocked = tracker.blocked(tool, clean)
    if blocked:
        outcome = _failed(tool, clean, ErrorType.REPEATED_FAILURE, blocked, decision="blocked")
        _finish(outcome, ctx)
        return outcome

    assessment = policy.assess(tool, clean)
    task_key = (tool, assessment.capability)
    decision = policy.decide(
        assessment, user_initiated=ctx.user_initiated, tainted=ctx.tainted,
        pre_approved=task_key in ctx.task_approvals, sandboxed=_in_agent_account(),
    )
    risk = assessment.risk.label
    decision_label = "allow"

    if decision.action == "deny":
        audit.log("policy_block", run_id=ctx.run_id, task_id=ctx.current_task, tool=tool, risk=risk,
                  status="denied", summary=assessment.summary, detail=decision.to_dict())
        outcome = _failed(tool, clean, ErrorType.POLICY_DENIED, decision.why, risk=risk, decision="denied")
        _finish(outcome, ctx)
        return outcome

    if decision.action == "confirm":
        answer, note = approvals.request(ctx, tool, assessment, decision.why)
        if answer == "deny":
            msg = "The user declined this action." + (f" Their note: {note}" if note else "")
            outcome = _failed(tool, clean, ErrorType.USER_DENIED, msg, risk=risk, decision="denied")
            _finish(outcome, ctx)
            return outcome
        if answer == "approve_task":
            ctx.task_approvals.add(task_key)
        decision_label = "confirmed"
        ctx.checkpoint()

    ctx.send({"type": "tool_start", "task_id": ctx.current_task, "tool": tool,
              "summary": assessment.summary, "risk": risk})
    audit.log("tool_call", run_id=ctx.run_id, task_id=ctx.current_task, tool=tool, risk=risk,
              status=decision_label, summary=assessment.summary, detail={"args": clean})

    started = time.time()
    fn = functions[tool]

    if tool in _INLINE_TOOLS:
        # These change or read state of the *calling* task's context (which
        # macOS session it targets), so they must run in that context, not
        # in a copy on a worker thread.
        try:
            raw = fn(**clean)
        except Exception as e:
            outcome = _failed(tool, clean, classify_error(str(e)), str(e), risk=risk, decision=decision_label)
            _finish(outcome, ctx)
            return outcome
        return _complete(tool, clean, raw, risk, decision_label, started, ctx, tracker)

    # Every ContextVar (the run, the task id, the target macOS session)
    # travels with the call onto the worker thread.
    future = _pool.submit(contextvars.copy_context().run, fn, **clean)
    limit = _timeout_for(tool, clean)
    deadline = started + limit
    raw = None
    try:
        while True:
            try:
                raw = future.result(timeout=0.5)
                break
            except FutureTimeout:
                if ctx.cancelled:
                    raise run_context.RunCancelled()
                if time.time() > deadline:
                    outcome = _failed(tool, clean, ErrorType.TIMEOUT,
                                      f"{tool} did not finish within {int(limit)}s. It may still complete "
                                      "in the background -- check state before repeating it.",
                                      risk=risk, decision=decision_label, duration=time.time() - started)
                    tracker.record(tool, clean, False, ErrorType.TIMEOUT)
                    _finish(outcome, ctx)
                    return outcome
    except run_context.RunCancelled:
        raise
    except TypeError as e:
        outcome = _failed(tool, clean, ErrorType.INVALID_ARGUMENTS, str(e), risk=risk,
                          decision=decision_label, duration=time.time() - started)
        tracker.record(tool, clean, False, outcome.error_type)
        _finish(outcome, ctx)
        return outcome
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        outcome = _failed(tool, clean, classify_error(message), message, risk=risk,
                          decision=decision_label, duration=time.time() - started)
        tracker.record(tool, clean, False, outcome.error_type)
        _finish(outcome, ctx)
        return outcome

    return _complete(tool, clean, raw, risk, decision_label, started, ctx, tracker)


_INLINE_TOOLS = {"set_target_session", "current_session"}


def _complete(tool, clean, raw, risk, decision_label, started, ctx, tracker) -> ToolOutcome:
    duration = time.time() - started
    failure = _result_failure(raw)
    text = _truncate(redact(_stringify(raw)))
    if failure:
        error_type = classify_error(failure)
        guidance = {"error_type": error_type, "recovery": RECOVERY.get(error_type)}
        fix = _known_fix(tool, error_type, failure)
        if fix:
            guidance["known_fix_from_past_experience"] = fix
        text = text + "\n\n" + json.dumps(guidance, indent=2)
    wrapped, score, hits = injection.wrap(tool, text)
    if score >= injection.WARN_THRESHOLD and not ctx.tainted:
        ctx.tainted = True
        audit.log("injection_warning", run_id=ctx.run_id, task_id=ctx.current_task, tool=tool,
                  summary=f"Possible prompt injection in {tool} output", detail={"hits": hits, "score": score})
        ctx.send({"type": "security_warning", "tool": tool,
                  "message": "Some content Jugnu read looked like it was trying to give it instructions. "
                             "It's being ignored, and riskier actions in this task will ask first."})

    outcome = ToolOutcome(
        tool=tool, args=clean, ok=failure is None, text=wrapped, raw=raw,
        error_type=classify_error(failure) if failure else None, error=redact(failure) if failure else None,
        risk=risk, decision=decision_label, duration=duration, injection_score=score,
    )
    tracker.record(tool, clean, outcome.ok, outcome.error_type)
    _finish(outcome, ctx)
    return outcome


def _finish(outcome: ToolOutcome, ctx) -> None:
    audit.log("tool_result", run_id=ctx.run_id, task_id=ctx.current_task, tool=outcome.tool,
              risk=outcome.risk, status="ok" if outcome.ok else (outcome.error_type or "failed"),
              summary=(outcome.error or outcome.text[:300]),
              detail={**outcome.to_dict(), "preview": outcome.text[:1500]})
    ctx.send({"type": "tool_end", "task_id": ctx.current_task, "tool": outcome.tool,
              "ok": outcome.ok, "error_type": outcome.error_type,
              "duration": round(outcome.duration, 2),
              "preview": redact((outcome.error or _preview(outcome.raw)))[:240]})
    for hook in list(_hooks):
        try:
            hook(outcome, ctx)
        except Exception:
            pass


def _preview(raw) -> str:
    text = _stringify(raw) if raw is not None else ""
    return " ".join(text.split())[:240]
