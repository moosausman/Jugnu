"""
executor.py -- runs a Plan to a verified result.

    ready tasks (dependencies done) run in parallel  ->  each task: agent
    tool loop  ->  verify (postconditions + criteria)  ->  retry with
    feedback / replan / ask the user / accept partial  ->  checkpoint  ->
    objective check  ->  final synthesis

Scheduling rules:
  * A task starts once every task it depends on is done.
  * Independent tasks run concurrently (up to MAX_PARALLEL). Tasks whose
    agent drives the shared screen (Agent.exclusive) take a lock, so two of
    them never fight over keyboard focus.
  * A failed task gets up to task.max_attempts tries; each retry carries the
    verifier's reason and what was missing.
  * When a task can't be completed, the rest of the plan is revised
    (planner.replan) up to MAX_REPLANS times -- the cause is fixed rather
    than the same approach repeated. Tasks depending on a failed task are
    skipped, unless the replan provides another route.
  * A task that needs the user's input stops new work; the question is
    returned as the answer (the run's state is checkpointed, so the reply
    continues it -- see session.py).
  * Every task has a wall-clock limit (TASK_TIMEOUT); cancellation and pause
    come from the run context and are honoured between steps.

Model tiers: each task runs on its tier's model -- fast/standard/deep map to
the FAST/PLANNER/DEEP roles in llm_client.
"""

from __future__ import annotations

import contextvars
import os
import re
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Optional

import audit
import run_context
import skills
import verifier
from agent_core import AgentResult, run_tool_loop
from agents import AGENTS
from memory import agent_system_prompt, facts_as_string, persona_reply
from planner import Plan, Task, replan

MAX_PARALLEL = int(os.environ.get("JUGNU_MAX_PARALLEL_TASKS", "4"))
MAX_REPLANS = 1
TASK_TIMEOUT = float(os.environ.get("JUGNU_TASK_TIMEOUT", str(20 * 60)))
# Tool-call budgets per task. Every step re-sends the whole conversation, so
# a long loop is the single biggest cost; well-chosen tools finish in a few.
STEPS_BY_TIER = {"fast": 6, "standard": 12, "deep": 20}
ROLE_BY_TIER = {"fast": "FAST", "standard": "PLANNER", "deep": "DEEP"}

# Agents that can produce a useful (clearly caveated) result from their own
# knowledge when an upstream step failed.
_PARTIAL_OK_AGENTS = {"writer", "answer"}

# One screen, one keyboard: shared by every run (foreground and background),
# so two screen-driving tasks never interleave anywhere in the process.
_DESKTOP_LOCK = threading.Lock()

# Kept for callers that still import it (e.g. older tests/tools).
MAX_TASK_ITERATIONS = STEPS_BY_TIER["standard"]


def log(message: str) -> None:
    print(f"[executor] {message}", file=sys.stderr, flush=True)


def _max_steps(tier: str) -> int:
    override = os.environ.get("JUGNU_MAX_TASK_STEPS")
    if override and override.isdigit():
        return int(override)
    return STEPS_BY_TIER.get(tier, STEPS_BY_TIER["standard"])


# ---------------------------------------------------------------------------
# One task
# ---------------------------------------------------------------------------

def _task_context(task: Task, shared_memory: dict, recent_context: str, plan: Optional[Plan],
                  feedback: str = "") -> str:
    parts = []
    if recent_context:
        parts.append(f"Context:\n{recent_context}")
    if plan is not None and len(plan.tasks) > 1:
        parts.append(
            f"Overall objective (for orientation -- other tasks in the plan handle the other parts, "
            f"so do ONLY your task): {plan.objective}")
    if plan is not None and plan.assumptions:
        parts.append("Working assumptions: " + "; ".join(plan.assumptions))
    parts.append(f"Your task: {task.description}")
    if task.completion_criteria:
        parts.append(f"Done when: {task.completion_criteria}")
    if task.verification:
        parts.append(f"It will be checked by: {task.verification}")
    if task.depends_on:
        parts.append("Results from earlier steps in this plan:")
        for dependency in task.depends_on:
            parts.append(f"- Task {dependency}: {shared_memory.get(dependency, '(no result)')}")
    if feedback:
        parts.append(f"Your previous attempt did not pass verification: {feedback}\n"
                     "Fix exactly that. Don't repeat what failed.")
    if getattr(task, "note", "").startswith("working from incomplete inputs"):
        parts.append("NOTE: an earlier step you depend on failed (" + task.note[len("working from incomplete inputs: "):] +
                     "). Complete your task as well as you can from the partial results and your own knowledge, "
                     "and state clearly in your output what could not be verified or is based on general knowledge.")
    return "\n\n".join(parts)


def _agent_prompt(agent_name: str) -> str:
    prompt = agent_system_prompt(agent_name, facts_as_string())
    try:
        import memory_store
        unreliable = memory_store.unreliable_tools_block()
        if unreliable:
            prompt += "\n\n" + unreliable
    except Exception:
        pass
    return prompt


def run_task(task, shared_memory, recent_context="", on_status=None, on_tool_call=None,
             on_tool_result=None, plan: Optional[Plan] = None, feedback: str = "",
             return_result: bool = False):
    """Runs one attempt of one task. Returns the agent's report (a str), or
    the full AgentResult when return_result=True (the executor needs the
    tool outcomes for verification)."""
    user_message = _task_context(task, shared_memory, recent_context, plan, feedback)

    if task.agent == "answer":
        if on_status:
            on_status("thinking")
        # Deep-tier answers (explanations, teaching) get the strongest model.
        content = persona_reply(user_message, role="DEEP" if getattr(task, "model_tier", "") == "deep" else "EASY")
        result = AgentResult(content=content, messages=[])
        return result if return_result else content

    if task.agent == "daily_review":
        if on_status:
            on_status("thinking")
        import daily_review  # local: avoids the executor <-> daily_review import cycle
        content = daily_review.build_report()
        result = AgentResult(content=content, messages=[])
        return result if return_result else content

    agent = AGENTS[task.agent]

    # A known-good route for this kind of task, if one has been learned.
    known = skills.recall(task.description, task.agent)
    if known:
        user_message = f"{user_message}\n\n{skills.as_prompt_block(known)}"

    messages = [
        {"role": "system", "content": _agent_prompt(task.agent)},
        {"role": "user", "content": user_message},
    ]
    tier = getattr(task, "model_tier", "") or agent.default_tier
    result = run_tool_loop(
        messages=messages,
        schemas=agent.schemas,
        functions=agent.functions,
        role=ROLE_BY_TIER.get(tier, "PLANNER"),
        max_iterations=_max_steps(tier),
        on_status=on_status,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )

    succeeded = bool(result.steps) and not result.exhausted and not result.last_failed
    if succeeded:
        skills.record(task.description, task.agent, result.steps, succeeded=True)
    elif known:
        skills.feedback(known["id"], worked=False)

    return result if return_result else result.content


# ---------------------------------------------------------------------------
# Plan execution
# ---------------------------------------------------------------------------

class _Callbacks:
    def __init__(self, on_task_start, on_status, on_tool_call, on_tool_result, on_checkpoint):
        self.on_task_start = on_task_start
        self.on_status = on_status
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_checkpoint = on_checkpoint

    def bind(self, task):
        return dict(
            on_status=(lambda label, t=task: self.on_status(t, label)) if self.on_status else None,
            on_tool_call=(lambda n, a, t=task: self.on_tool_call(t, n, a)) if self.on_tool_call else None,
            on_tool_result=(lambda n, a, r, t=task: self.on_tool_result(t, n, a, r)) if self.on_tool_result else None,
        )


def _emit_task(ctx, task: Task, **extra) -> None:
    ctx.send({"type": "task_update", "task_id": task.id, "status": task.status,
              "attempts": task.attempts, "note": task.note[:300], "task": task.to_dict(), **extra})


def _topological_order(tasks):
    """Dependency order (used for synthesis and by older callers)."""
    by_id = {task.id: task for task in tasks}
    done, ordered = set(), []

    def visit(task, stack):
        if task.id in done:
            return
        if task.id in stack:
            raise ValueError(f"circular dependency involving task {task.id}")
        for dependency in task.depends_on:
            if dependency in by_id:
                visit(by_id[dependency], stack | {task.id})
        ordered.append(task)
        done.add(task.id)

    for task in tasks:
        visit(task, frozenset())
    return ordered


class PlanRunner:
    def __init__(self, plan: Plan, recent_context: str, callbacks: _Callbacks, shared_memory: dict | None = None):
        self.plan = plan
        self.recent_context = recent_context
        self.cb = callbacks
        self.shared = shared_memory if shared_memory is not None else {}
        self.ctx = run_context.current()
        self.exclusive_lock = _DESKTOP_LOCK
        self.question: Optional[str] = None
        self.stop_scheduling = False
        self._state_lock = threading.Lock()

    # -- state -----------------------------------------------------------------
    def _by_id(self) -> dict:
        return {t.id: t for t in self.plan.tasks}

    def _checkpoint(self) -> None:
        if self.cb.on_checkpoint:
            try:
                self.cb.on_checkpoint(self.plan, self.shared)
            except Exception as e:
                log(f"checkpoint failed: {e}")

    def _progress(self) -> None:
        total = len(self.plan.tasks)
        done = sum(1 for t in self.plan.tasks if t.status == "done")
        running = [t.title for t in self.plan.tasks if t.status in ("running", "verifying")]
        if total > 1:
            line = f"{done}/{total} done"
            if running:
                line += " · " + ", ".join(running[:2])
            self.ctx.progress(line, done=done, total=total)

    def _ready(self) -> list:
        by_id = self._by_id()
        ready = []
        for t in self.plan.tasks:
            if t.status != "pending":
                continue
            deps = [by_id.get(d) for d in t.depends_on]
            # "Continue with a partial result": a writing/answering step can
            # still produce something useful from its own knowledge when an
            # input step failed (e.g. web research was rate-limited) -- better
            # than skipping it and delivering nothing. It's told the input is
            # incomplete and must say so.
            finished = all(d is not None and d.status in ("done", "failed") for d in deps)
            failed_inputs = [d for d in deps if d is not None and d.status == "failed"]
            if finished and failed_inputs and t.agent in _PARTIAL_OK_AGENTS and not self.stop_scheduling:
                if not t.note.startswith("working from"):
                    t.note = ("working from incomplete inputs: " +
                              ", ".join(f"{d.title} ({d.note or 'failed'})"[:160] for d in failed_inputs))
                ready.append(t)
                continue
            if any(d is None or d.status in ("failed", "skipped", "cancelled") for d in deps):
                blocking = [str(d.id) for d in deps if d is not None and d.status != "done"]
                t.status = "skipped"
                t.note = f"skipped: depends on task(s) {', '.join(blocking) or '?'} which didn't complete"
                _emit_task(self.ctx, t)
                continue
            if all(d.status == "done" for d in deps):
                ready.append(t)
        return ready

    # -- one task with retries ----------------------------------------------------
    def _run_one(self, task: Task) -> None:
        stop = threading.Event()
        run_context.set_task_stop(stop)
        self.ctx.current_task = task.id
        timer = threading.Timer(TASK_TIMEOUT, stop.set)
        timer.daemon = True
        timer.start()
        agent = AGENTS.get(task.agent)
        lock = self.exclusive_lock if (agent is not None and agent.exclusive) else None
        try:
            if lock:
                while not lock.acquire(timeout=0.5):
                    self.ctx.checkpoint()
            try:
                self._attempts(task)
            finally:
                if lock:
                    lock.release()
        except run_context.TaskStopped:
            task.status = "failed"
            task.note = f"timed out after {int(TASK_TIMEOUT // 60)} minutes"
            _emit_task(self.ctx, task)
        finally:
            timer.cancel()
            run_context.set_task_stop(None)

    def _attempts(self, task: Task) -> None:
        feedback = ""
        while task.attempts < task.max_attempts:
            self.ctx.checkpoint()
            task.attempts += 1
            task.status = "running"
            _emit_task(self.ctx, task)
            if self.cb.on_task_start:
                self.cb.on_task_start(task)
            self._progress()
            result: AgentResult = run_task(task, self.shared, self.recent_context, plan=self.plan,
                                           feedback=feedback, return_result=True, **self.cb.bind(task))
            task.result = result.content or ""
            task.status = "verifying"
            _emit_task(self.ctx, task)
            verdict = verifier.check_task(task, result, complexity=self.plan.complexity)
            audit.log("verification", run_id=self.ctx.run_id, task_id=task.id, tool=task.agent,
                      status="passed" if verdict.passed else "failed", summary=verdict.reason,
                      detail=verdict.model_dump())
            self.ctx.send({"type": "verification", "task_id": task.id, "passed": verdict.passed,
                           "reason": verdict.reason, "missing": verdict.missing})
            if verdict.passed:
                task.status = "done"
                task.note = verdict.reason if verdict.reason and verdict.reason != "Completed without errors." else ""
                with self._state_lock:
                    self.shared[task.id] = task.result
                _emit_task(self.ctx, task)
                return
            task.note = verdict.reason
            task.recovery = verdict.recovery
            if verdict.recovery == "ask_user":
                with self._state_lock:
                    self.question = verdict.question or result.needs_input or verdict.reason
                    self.stop_scheduling = True
                task.status = "blocked"
                _emit_task(self.ctx, task)
                return
            if verdict.recovery in ("accept_partial", "stop", "replan"):
                break
            feedback = verdict.reason + (f" Missing: {'; '.join(verdict.missing)}" if verdict.missing else "")
            if task.attempts < task.max_attempts:
                self.ctx.progress(f"Retrying \"{task.title}\": {verdict.reason[:120]}")
        task.status = "failed"
        with self._state_lock:
            # Partial results still flow to dependents that get replanned.
            self.shared[task.id] = f"(incomplete) {task.result}"
        _emit_task(self.ctx, task)

    # -- replanning -----------------------------------------------------------------
    def _maybe_replan(self, failed: Task) -> bool:
        if failed.optional or self.plan.replans >= MAX_REPLANS or self.stop_scheduling:
            return False
        # A simple request: if it failed, say why instead of paying for a
        # new plan and a second attempt at the same thing.
        if self.plan.complexity == "simple":
            return False
        # Only when the verifier judged that a different route could work --
        # not for a missing permission, a declined action or a hard stop.
        if getattr(failed, "recovery", "replan") != "replan":
            return False
        if any(k in (failed.note or "").lower() for k in ("declined", "permission", "switched off",
                                                           "not authorized", "didn't go ahead")):
            return False
        self.ctx.progress(f"Rethinking the plan: {failed.title} didn't work")
        try:
            spec = replan(self.plan, why=f"Task {failed.id} ({failed.title}) failed: {failed.note}",
                          recent_context=self.recent_context)
            error = ""
        except Exception as e:  # replan() logs and returns None; this is belt and braces
            spec, error = None, str(e)
        self.plan.replans += 1
        if spec is None or spec.abandon or not spec.tasks:
            audit.log("recovery", run_id=self.ctx.run_id, task_id=failed.id, status="abandoned",
                      summary=(spec.reason if spec else f"replan failed {error}".strip()))
            return False
        if spec.assumptions:
            self.plan.assumptions = list(dict.fromkeys(self.plan.assumptions + spec.assumptions))
        new_tasks = [Task.from_spec(t) for t in spec.tasks]
        # Tasks that were waiting on the failed one are superseded by the new route.
        for t in self.plan.tasks:
            if t.status == "pending" and failed.id in t.depends_on:
                t.status = "skipped"
                t.note = "replaced by a revised plan"
                _emit_task(self.ctx, t)
        self.plan.tasks.extend(new_tasks)
        audit.log("recovery", run_id=self.ctx.run_id, task_id=failed.id, status="replanned",
                  summary=spec.reason, detail={"new_tasks": [t.to_dict() for t in new_tasks]})
        self.ctx.send({"type": "replan", "reason": spec.reason, "tasks": [t.to_dict() for t in new_tasks],
                       "assumptions": self.plan.assumptions})
        return True

    # -- main loop ------------------------------------------------------------------
    def run(self) -> None:
        pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix=f"task-{self.ctx.run_id}")
        running: dict = {}
        try:
            while True:
                self.ctx.checkpoint()
                if not self.stop_scheduling:
                    for task in self._ready():
                        if len(running) >= MAX_PARALLEL:
                            break
                        task.status = "queued"
                        running[pool.submit(contextvars.copy_context().run, self._run_one, task)] = task
                if not running:
                    break
                finished, _ = wait(list(running), timeout=0.5, return_when=FIRST_COMPLETED)
                for future in finished:
                    task = running.pop(future)
                    try:
                        future.result()
                    except run_context.RunCancelled:
                        raise
                    except Exception as e:
                        task.status = "failed"
                        task.note = f"crashed: {e}"
                        log(f"task {task.id} crashed: {e}")
                        _emit_task(self.ctx, task)
                    if task.status == "failed":
                        self._maybe_replan(task)
                    self._checkpoint()
                    self._progress()
        except run_context.RunCancelled:
            for t in self.plan.tasks:
                if t.status in ("pending", "queued", "running", "verifying"):
                    t.status = "cancelled"
            self._checkpoint()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        # Anything never started (e.g. scheduling stopped for a question).
        for t in self.plan.tasks:
            if t.status in ("pending", "queued"):
                t.status = "skipped" if not self.stop_scheduling else "pending"
        self._checkpoint()


def execute_plan(plan, recent_context="", on_task_start=None, on_status=None, on_tool_call=None,
                 on_tool_result=None, on_checkpoint: Optional[Callable] = None,
                 shared_memory: Optional[dict] = None) -> str:
    """Executes a plan and returns one verified, user-facing answer."""
    if plan.needs_clarification:
        return plan.clarify

    runner = PlanRunner(plan, recent_context,
                        _Callbacks(on_task_start, on_status, on_tool_call, on_tool_result, on_checkpoint),
                        shared_memory=shared_memory)
    runner.run()

    if runner.question:
        plan.pending_question = runner.question
        return runner.question

    tasks = _topological_order(plan.tasks)
    # A single conversational/quick task: its own result is the answer.
    if len(tasks) == 1 and tasks[0].status == "done":
        return tasks[0].result or "Done."
    # A simple request that failed: the agent's own explanation (or the
    # stop message) already says why -- no extra model call to reword it.
    if len(tasks) == 1 and plan.complexity == "simple" and (tasks[0].result or tasks[0].note):
        return tasks[0].result or f"I couldn't do that: {tasks[0].note}"

    objective = None
    if plan.complexity == "complex" and len(tasks) > 1:
        objective = verifier.check_objective(plan, tasks)
        audit.log("verification", run_id=runner.ctx.run_id, status="objective_" + ("passed" if objective.passed else "gaps"),
                  summary=objective.reason, detail=objective.model_dump())
    return synthesize(plan, tasks, objective)


# ---------------------------------------------------------------------------
# Final synthesis + quality control
# ---------------------------------------------------------------------------

_SYNTHESIS_RULES = """Write the reply to the user from the results above.
- Lead with the outcome in one or two sentences.
- Then, only as much as is useful: what was done and what changed (exact
  file paths, event names/times, links), and anything that still needs the
  user's attention (steps that failed or were declined, permissions needed,
  gaps the verifier found). Keep it scannable -- short lines or bullets.
- If assumptions were made, name them in one short line so the user can
  correct them.
- For research, cite sources as links next to the claims they support.
- Say ONLY what the results support. Never claim something succeeded if its
  result says it failed or was incomplete.
- Don't mention tasks, agents, plans, or verification -- just answer."""


def synthesize(plan: Plan, tasks: list, objective=None) -> str:
    done = [t for t in tasks if t.status == "done"]
    not_done = [t for t in tasks if t.status != "done"]
    lines = [f"User's request: {plan.goal}", f"Objective: {plan.objective}"]
    if plan.assumptions:
        lines.append("Assumptions made: " + "; ".join(plan.assumptions))
    # What did NOT happen goes first and is stated as fact, so the reply
    # can't paper over it. A skipped "write the report" task means no report
    # exists -- the model used to announce it anyway.
    if not_done:
        lines.append("\nNOT DONE -- these did not happen; nothing they would have produced exists:")
        for t in not_done:
            lines.append(f"- {t.title} [{t.status.upper()}]: {t.note or 'no result'}")
    if objective is not None and not objective.passed and objective.missing:
        lines.append("Still missing according to the final check: " + "; ".join(objective.missing))
    lines.append("\nCompleted work:")
    for task in done:
        lines.append(f"- {task.title}\n  Result: {(task.result or '').strip()[:3000]}")
    for task in not_done:
        if task.result:
            lines.append(f"- (partial, from an incomplete step) {task.title}\n  Result: {task.result.strip()[:1500]}")
    lines.append("\n" + _SYNTHESIS_RULES)
    role = "DEEP" if plan.complexity == "complex" else "AGENT"
    answer = persona_reply("\n".join(lines), role=role)
    # A second model pass reviewing the reply: for complex work, and whenever
    # something didn't happen -- the one case where a reply most often
    # overclaims. A fully successful ordinary run skips it.
    if answer and (plan.complexity == "complex" or not_done):
        answer = _quality_control(answer, "\n".join(lines[:-1]), role) or answer
    answer = _check_claimed_files(answer, "\n".join(lines[:-1]), role)
    return answer or "I finished, but couldn't put together a summary -- the details are in the activity log."


_PATH_RE = re.compile(r"(?<![\w/])((?:~|/Users/[^/\s`'\"]+)/[^\s`'\"<>|*]+\.[A-Za-z0-9]{1,8})")
_CLAIM_RE = re.compile(r"\b(created|saved|wrote|written|generated|exported|stored|made|placed|put|added)\b", re.I)


def _check_claimed_files(answer: str, evidence: str, role: str) -> str:
    """Deterministic backstop for the one false claim that matters most:
    telling the user a file exists when it doesn't. Every path the answer
    mentions next to a creation verb is checked in the run's session; if any
    is missing, the reply is rewritten with that fact, and if the rewrite
    still claims it, an explicit correction is appended."""
    if not answer:
        return answer
    claimed = []
    for match in _PATH_RE.finditer(answer):
        path = match.group(1).rstrip(".,;:)")
        window = answer[max(0, match.start() - 160): match.end() + 80]
        if _CLAIM_RE.search(window):
            claimed.append(path)
    missing = []
    for path in dict.fromkeys(claimed):
        exists = verifier._exists(path)
        if exists is False:
            missing.append(path)
    if not missing:
        return answer
    audit.log("verification", status="claimed_file_missing", summary="; ".join(missing))
    fixed = persona_reply(
        f"{evidence[:12000]}\n\nFACT CHECK: these files do NOT exist -- they were never created: "
        f"{', '.join(missing)}.\nDraft reply (which wrongly says they were):\n{answer}\n\n"
        f"Rewrite the reply so it's accurate about what does and doesn't exist.\n{_SYNTHESIS_RULES}", role=role)
    if fixed and not any(p in fixed and _CLAIM_RE.search(fixed) and "not" not in fixed.lower() for p in missing):
        return fixed
    return (fixed or answer) + "\n\nNote: " + ", ".join(missing) + (" was" if len(missing) == 1 else " were") + \
        " not actually created."


_QC_PROMPT = """Check a draft reply against the work results it's based on.
Flag only real problems: claims not supported by the results, anything in the
NOT DONE list described as done (e.g. a file "created" whose step was
skipped), missing mention of something that failed or still needs the user,
or missing source links for researched facts.
Respond with exactly one JSON object: {"ok": true|false, "problems": ["..."]}"""


def _quality_control(answer: str, evidence: str, role: str) -> str | None:
    from planner import request_json
    try:
        review = request_json([
            {"role": "system", "content": _QC_PROMPT},
            {"role": "user", "content": f"Results:\n{evidence[:12000]}\n\nDraft reply:\n{answer}"},
        ], role="EASY", retries=1)
    except Exception:
        return None
    problems = [str(p) for p in (review.get("problems") or [])][:6]
    if review.get("ok", True) or not problems:
        return None
    return persona_reply(
        f"{evidence[:12000]}\n\nA draft reply had these problems: {'; '.join(problems)}.\n"
        f"Draft:\n{answer}\n\nRewrite the reply fixing exactly those problems.\n{_SYNTHESIS_RULES}", role=role)
