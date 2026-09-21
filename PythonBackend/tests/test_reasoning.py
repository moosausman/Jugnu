"""Phase 3/4: schemas, planner validation, parallel executor, verification,
retries, replanning, questions, cancellation, persistence, scheduler, router."""

import _env  # noqa: F401

import threading
import time
import unittest

import executor
import permissions
import planner
import router
import run_context
import scheduler
import session
import tasks_store
import verifier
from agent_core import AgentResult
from pydantic import ValidationError
from schemas import PlanSpec, Verdict
from tests.fake_llm import FakeLLM, is_planner, is_replanner, is_verifier, after_tool
from tool_runtime import ToolOutcome


def _plan(tasks, **kw):
    return planner.Plan(goal=kw.pop("goal", "g"), tasks=tasks, objective=kw.pop("objective", "obj"), **kw)


class SchemaTests(unittest.TestCase):
    def test_cycle_rejected(self):
        with self.assertRaises(ValidationError):
            PlanSpec(objective="x", tasks=[
                {"id": 1, "agent": "files", "description": "a", "depends_on": [2]},
                {"id": 2, "agent": "files", "description": "b", "depends_on": [1]}])

    def test_unknown_dependency_and_duplicate_ids(self):
        with self.assertRaises(ValidationError):
            PlanSpec(objective="x", tasks=[{"id": 1, "agent": "files", "description": "a", "depends_on": [9]}])
        with self.assertRaises(ValidationError):
            PlanSpec(objective="x", tasks=[{"id": 1, "agent": "a", "description": "a"},
                                           {"id": 1, "agent": "b", "description": "b"}])

    def test_scheduled_needs_schedule(self):
        with self.assertRaises(ValidationError):
            PlanSpec(objective="x", request_type="scheduled", tasks=[])
        spec = PlanSpec(objective="x", request_type="scheduled",
                        schedule={"in_minutes": 5, "instruction": "check the build"})
        self.assertEqual(spec.schedule.in_minutes, 5)

    def test_coercion(self):
        spec = PlanSpec(objective="x", assumptions="one", tasks=[
            {"id": 1, "agent": "files", "description": "a", "depends_on": None},
            {"id": 2, "agent": "files", "description": "b", "depends_on": "1"}])
        self.assertEqual(spec.assumptions, ["one"])
        self.assertEqual(spec.tasks[1].depends_on, [1])


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLM().install()

    def tearDown(self):
        self.llm.uninstall()

    def test_invalid_agent_gets_corrected(self):
        responses = iter([
            {"objective": "x", "tasks": [{"id": 1, "agent": "wizard", "description": "do it"}]},
            {"objective": "x", "tasks": [{"id": 1, "agent": "files", "description": "do it"}]},
        ])
        self.llm.when(is_planner, lambda r: next(responses))
        plan = planner.make_plan("organize my downloads")
        self.assertEqual(plan.tasks[0].agent, "files")
        correction = self.llm.calls[-1]["messages"][-1]["content"]
        self.assertIn("unknown agent", correction)

    def test_fallback_to_answer(self):
        self.llm.when(is_planner, lambda r: "not json at all")
        plan = planner.make_plan("hello there")
        self.assertEqual([t.agent for t in plan.tasks], ["answer"])

    def test_structured_fields_survive(self):
        self.llm.when(is_planner, lambda r: {
            "objective": "Prepare for CSC236", "complexity": "complex", "assumptions": ["test is next week"],
            "success_criteria": ["plan exists"],
            "tasks": [{"id": 1, "agent": "files", "title": "Find", "description": "find notes", "risk": "read",
                       "completion_criteria": "list of files", "model_tier": "fast"},
                      {"id": 2, "agent": "writer", "description": "write plan", "depends_on": [1], "risk": "low"}]})
        plan = planner.make_plan("help me prepare for my CSC236 test")
        self.assertEqual(plan.complexity, "complex")
        self.assertEqual(plan.assumptions, ["test is next week"])
        self.assertEqual(plan.tasks[0].model_tier, "fast")
        self.assertEqual(plan.tasks[1].model_tier, "deep", "writer defaults to deep tier")
        roundtrip = planner.Plan.from_dict(plan.to_dict())
        self.assertEqual(roundtrip.tasks[1].depends_on, [1])


class ExecutorTests(unittest.TestCase):
    """Drives PlanRunner with a stubbed run_task/verifier to test scheduling."""

    def setUp(self):
        self._run_task, self._check, self._replan = executor.run_task, verifier.check_task, executor.replan
        self.log, self.lock = [], threading.Lock()
        self.ctx = run_context.RunContext()
        self.token = run_context.activate(self.ctx)

    def tearDown(self):
        executor.run_task, verifier.check_task, executor.replan = self._run_task, self._check, self._replan
        run_context.deactivate(self.token)

    def _stub(self, behaviour):
        def fake_run_task(task, shared, recent_context="", plan=None, feedback="", return_result=False, **_):
            with self.lock:
                self.log.append(("start", task.id, time.time(), feedback))
            content, delay = behaviour(task, shared, feedback)
            time.sleep(delay)
            with self.lock:
                self.log.append(("end", task.id, time.time(), ""))
            r = AgentResult(content=content, messages=[])
            return r if return_result else content
        executor.run_task = fake_run_task

    def _verdict_from_content(self):
        verifier.check_task = lambda task, result, complexity="standard": (
            Verdict(passed=True, reason="ok") if not result.content.startswith("FAIL")
            else Verdict(passed=False, reason=result.content, recovery=result.content.split(":")[1]))

    def test_parallel_independent_and_ordered_dependent(self):
        self._stub(lambda t, s, f: (f"result {t.id}", 0.4))
        self._verdict_from_content()
        plan = _plan([planner.Task(1, "files", "a"), planner.Task(2, "research", "b"),
                      planner.Task(3, "writer", "c", depends_on=[1, 2])])
        start = time.time()
        executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None, None)).run()
        elapsed = time.time() - start
        self.assertTrue(all(t.status == "done" for t in plan.tasks))
        self.assertLess(elapsed, 1.15, "tasks 1 and 2 should overlap")
        starts = {e[1]: e[2] for e in self.log if e[0] == "start"}
        ends = {e[1]: e[2] for e in self.log if e[0] == "end"}
        self.assertGreaterEqual(starts[3], max(ends[1], ends[2]))

    def test_exclusive_agents_never_overlap(self):
        self._stub(lambda t, s, f: ("ok", 0.3))
        self._verdict_from_content()
        plan = _plan([planner.Task(1, "automation", "a"), planner.Task(2, "whatsapp", "b"),
                      planner.Task(3, "files", "c")])
        executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None, None)).run()
        spans = {}
        for kind, tid, ts, _ in self.log:
            spans.setdefault(tid, {})[kind] = ts
        a, b = spans[1], spans[2]
        self.assertTrue(a["end"] <= b["start"] or b["end"] <= a["start"], "screen-driving tasks overlapped")

    def test_retry_with_feedback_then_pass(self):
        attempts = {}

        def behaviour(t, s, feedback):
            attempts[t.id] = attempts.get(t.id, 0) + 1
            return ("FAIL:retry" if attempts[t.id] == 1 else "fixed", 0)
        self._stub(behaviour)
        self._verdict_from_content()
        plan = _plan([planner.Task(1, "files", "write the file", max_attempts=2)])
        executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None, None)).run()
        self.assertEqual(plan.tasks[0].status, "done")
        self.assertEqual(plan.tasks[0].attempts, 2)
        second_feedback = [e[3] for e in self.log if e[0] == "start"][1]
        self.assertIn("FAIL", second_feedback)

    def test_retry_limit_and_dependents_skipped(self):
        self._stub(lambda t, s, f: ("FAIL:retry", 0) if t.id == 1 else ("ok", 0))
        self._verdict_from_content()
        executor.replan = lambda plan, why, recent_context="": None
        plan = _plan([planner.Task(1, "files", "a", max_attempts=2), planner.Task(2, "files", "b", depends_on=[1]),
                      planner.Task(3, "research", "c")])
        executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None, None)).run()
        by_id = {t.id: t for t in plan.tasks}
        self.assertEqual(by_id[1].status, "failed")
        self.assertEqual(by_id[1].attempts, 2)
        self.assertEqual(by_id[2].status, "skipped")
        self.assertEqual(by_id[3].status, "done")

    def test_replan_adds_alternative_route(self):
        from schemas import ReplanSpec
        self._stub(lambda t, s, f: ("FAIL:replan", 0) if t.id == 1 else (f"alt {t.id}", 0))
        self._verdict_from_content()
        executor.replan = lambda plan, why, recent_context="": ReplanSpec(
            reason="use another folder", tasks=[{"id": 3, "agent": "files", "description": "try ~/Desktop"}])
        plan = _plan([planner.Task(1, "files", "a"), planner.Task(2, "writer", "b", depends_on=[1])])
        executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None, None)).run()
        by_id = {t.id: t for t in plan.tasks}
        self.assertEqual(by_id[1].status, "failed")
        self.assertEqual(by_id[2].status, "skipped")
        self.assertEqual(by_id[3].status, "done")
        self.assertEqual(plan.replans, 1)

    def test_replan_limit(self):
        from schemas import ReplanSpec
        counter = {"n": 10}

        def always_new(plan, why, recent_context=""):
            counter["n"] += 1
            return ReplanSpec(reason="again", tasks=[{"id": counter["n"], "agent": "files", "description": "x"}])
        self._stub(lambda t, s, f: ("FAIL:replan", 0))
        self._verdict_from_content()
        executor.replan = always_new
        plan = _plan([planner.Task(1, "files", "a")])
        executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None, None)).run()
        self.assertEqual(plan.replans, executor.MAX_REPLANS)
        self.assertEqual(len(plan.tasks), 1 + executor.MAX_REPLANS)

    def test_question_stops_scheduling(self):
        self._stub(lambda t, s, f: ("need info", 0))
        verifier.check_task = lambda task, result, complexity="standard": Verdict(
            passed=False, recovery="ask_user", question="Which course?") if task.id == 1 else Verdict(passed=True)
        plan = _plan([planner.Task(1, "files", "a"), planner.Task(2, "writer", "b", depends_on=[1])])
        answer = executor.execute_plan(plan)
        self.assertEqual(answer, "Which course?")
        self.assertEqual(plan.pending_question, "Which course?")
        self.assertEqual({t.id: t.status for t in plan.tasks}, {1: "blocked", 2: "pending"})

    def test_cancel_mid_plan(self):
        self._stub(lambda t, s, f: ("ok", 0.3))
        self._verdict_from_content()
        plan = _plan([planner.Task(1, "files", "a"), planner.Task(2, "writer", "b", depends_on=[1])])
        threading.Timer(0.1, self.ctx.cancel).start()
        with self.assertRaises(run_context.RunCancelled):
            executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None, None)).run()
        self.assertIn(plan.tasks[1].status, ("cancelled", "pending"))

    def test_checkpoint_called_per_task(self):
        self._stub(lambda t, s, f: ("ok", 0))
        self._verdict_from_content()
        seen = []
        plan = _plan([planner.Task(1, "files", "a"), planner.Task(2, "writer", "b", depends_on=[1])])
        executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None,
                                                          lambda p, s: seen.append(dict(s)))).run()
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen[-1], {1: "ok", 2: "ok"})


    def test_writer_continues_after_failed_research(self):
        self._stub(lambda t, s, f: ("FAIL:accept_partial", 0) if t.id == 1 else ("guide written", 0))
        self._verdict_from_content()
        executor.replan = lambda plan, why, recent_context="": None
        plan = _plan([planner.Task(1, "research", "search the web"),
                      planner.Task(2, "writer", "write the guide", depends_on=[1]),
                      planner.Task(3, "files", "move the guide", depends_on=[1])])
        executor.PlanRunner(plan, "", executor._Callbacks(None, None, None, None, None)).run()
        by_id = {t.id: t for t in plan.tasks}
        self.assertEqual(by_id[1].status, "failed")
        self.assertEqual(by_id[2].status, "done", "writer proceeds with partial inputs")
        self.assertTrue(by_id[2].note == "" or "working from" in by_id[2].note or by_id[2].status == "done")
        self.assertEqual(by_id[3].status, "skipped", "tool-driven steps still skip")


class VerifierTests(unittest.TestCase):
    def make_outcome(self, **kw):
        base = dict(tool="file_write", args={"path": "/tmp/x"}, ok=True, text="{}")
        base.update(kw)
        return ToolOutcome(**base)

    def test_denied_action_accepts_partial(self):
        r = AgentResult("did most", [], outcomes=[self.make_outcome(tool="mail_send", ok=False, error_type="user_denied")])
        v = verifier.check_task(planner.Task(1, "mail", "send it", risk="high"), r)
        self.assertFalse(v.passed)
        self.assertEqual(v.recovery, "accept_partial")

    def test_claimed_change_without_tools_fails(self):
        v = verifier.check_task(planner.Task(1, "files", "move files", risk="medium"), AgentResult("Done!", []))
        self.assertFalse(v.passed)
        self.assertEqual(v.recovery, "retry")

    def test_postcondition_failure(self):
        import tools.tools as t
        original = t.FUNCTIONS["file_exists"]
        t.FUNCTIONS["file_exists"] = lambda path: {"ok": True, "exists": False}
        try:
            r = AgentResult("wrote it", [], outcomes=[self.make_outcome()])
            v = verifier.check_task(planner.Task(1, "files", "write", risk="low"), r)
        finally:
            t.FUNCTIONS["file_exists"] = original
        self.assertFalse(v.passed)
        self.assertIn("missing", v.reason)

    def test_postcondition_success_passes_without_model(self):
        import tools.tools as t
        original = t.FUNCTIONS["file_exists"]
        t.FUNCTIONS["file_exists"] = lambda path: {"ok": True, "exists": True}
        try:
            r = AgentResult("wrote it", [], outcomes=[self.make_outcome()])
            v = verifier.check_task(planner.Task(1, "files", "write", risk="low"), r)
        finally:
            t.FUNCTIONS["file_exists"] = original
        self.assertTrue(v.passed)


class PersistenceTests(unittest.TestCase):
    def test_checkpoint_interrupt_resume(self):
        plan = _plan([planner.Task(1, "files", "a"), planner.Task(2, "writer", "b", depends_on=[1])])
        plan.tasks[0].status, plan.tasks[0].result = "done", "found 3 files"
        plan.tasks[1].status, plan.tasks[1].attempts = "running", 1
        tasks_store.create("run-persist", "goal", background=True)
        tasks_store.checkpoint("run-persist", plan, {1: "found 3 files"}, status="running")
        interrupted = [r["run_id"] for r in tasks_store.mark_interrupted()]
        self.assertIn("run-persist", interrupted)
        run = tasks_store.get("run-persist")
        self.assertEqual(run["status"], "interrupted")
        resumed, shared = tasks_store.prepare_resume(run)
        self.assertEqual(resumed.tasks[0].status, "done")
        self.assertEqual(resumed.tasks[1].status, "pending")
        self.assertEqual(shared[1], "found 3 files")

    def test_waiting_question_lookup(self):
        tasks_store.create("run-q", "goal")
        tasks_store.wait_for_user("run-q", "Which folder?")
        self.assertEqual(tasks_store.pending_question()["run_id"], "run-q")
        tasks_store.finish("run-q", "done")
        self.assertIsNone(tasks_store.pending_question())


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        for s in scheduler.list_schedules():
            scheduler.cancel(s["id"])

    def test_parse_times(self):
        now = time.mktime(time.strptime("2026-09-20 10:00", "%Y-%m-%d %H:%M"))
        self.assertAlmostEqual(scheduler.parse_when(in_minutes=30, now=now), now + 1800)
        nine = scheduler.parse_when("09:00", now=now)
        self.assertGreater(nine, now)  # already past today -> tomorrow
        self.assertEqual(time.strftime("%d %H:%M", time.localtime(nine)), "21 09:00")
        self.assertEqual(time.strftime("%H:%M", time.localtime(scheduler.parse_when("2026-09-21T08:30"))), "08:30")
        with self.assertRaises(ValueError):
            scheduler.parse_when("sometime soon")

    def test_recurring_fires_and_advances(self):
        item = scheduler.add("daily brief", in_minutes=0, every_minutes=1440)
        fired = []
        loop = scheduler.SchedulerLoop(on_task=lambda it: fired.append(it["id"]) or "run-1", on_notify=lambda it: None)
        loop.tick(now=time.time() + 1)
        self.assertEqual(fired, [item["id"]])
        again = scheduler.get(item["id"])
        self.assertTrue(again["enabled"])
        self.assertGreater(again["next_run"], time.time() + 1400 * 60)

    def test_one_off_timer_disables_and_stale_skipped(self):
        t = scheduler.add("stretch", in_minutes=0, kind="notify")
        notified = []
        loop = scheduler.SchedulerLoop(on_task=lambda it: "x", on_notify=lambda it: notified.append(it["id"]))
        loop.tick(now=time.time() + 1)
        self.assertEqual(notified, [t["id"]])
        self.assertFalse(scheduler.get(t["id"])["enabled"])
        stale = scheduler.add("old", in_minutes=0, kind="notify")
        loop.tick(now=time.time() + (scheduler.CATCH_UP_HOURS + 1) * 3600)
        self.assertEqual(notified, [t["id"]], "stale one-off must not fire")
        self.assertFalse(scheduler.get(stale["id"])["enabled"])


class RouterTests(unittest.TestCase):
    def test_quick_routes(self):
        for text in ("thanks!", "hi jugnu", "ok", "Good morning"):
            self.assertEqual(router.quick_route(text), "conversation", text)
        self.assertEqual(router.quick_route("stop"), "control")
        self.assertIsNone(router.quick_route("thanks, now email the notes to my TA"))
        self.assertTrue(router.is_answer_to("Which course?", "CSC236"))


class SessionTests(unittest.TestCase):
    """run_turn end to end with a scripted model: plan -> tool -> verify -> answer."""

    def setUp(self):
        permissions.save({"autonomy_mode": "balanced"})
        self.llm = FakeLLM().install()
        self.ctx = run_context.RunContext()
        self.token = run_context.activate(self.ctx)
        import tools.tools as t
        self.tools = t
        self._orig = dict(t.FUNCTIONS)

    def tearDown(self):
        self.llm.uninstall()
        run_context.deactivate(self.token)
        self.tools.FUNCTIONS.clear()
        self.tools.FUNCTIONS.update(self._orig)

    def test_full_turn_executes_verifies_and_persists(self):
        written = {}
        import agents
        agents.AGENTS["files"].tool_names  # noqa: B018 -- registry loaded
        self.tools.FUNCTIONS["file_write"] = lambda path, content, overwrite=False, createIntermediateDirectories=True: (
            written.__setitem__(path, content) or {"ok": True, "path": path})
        self.tools.FUNCTIONS["file_exists"] = lambda path: {"ok": True, "exists": path in written}
        self.llm.when(is_planner, lambda r: {"objective": "Write a note", "request_type": "quick_action",
                                             "tasks": [{"id": 1, "agent": "files", "description": "write note",
                                                        "risk": "low"}]})
        self.llm.when(lambda r: r["tools"] and not after_tool(r),
                      lambda r: [("file_write", {"path": "~/Documents/note.md", "content": "hi"})])
        self.llm.when(lambda r: r["tools"] and after_tool(r), lambda r: "Wrote ~/Documents/note.md")
        conversation = [{"role": "system", "content": "s"}]
        answer = session.run_turn(conversation, "write a note saying hi")
        self.assertEqual(answer, "Wrote ~/Documents/note.md")
        self.assertEqual(written, {"~/Documents/note.md": "hi"})
        run = tasks_store.get(self.ctx.run_id)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["plan"]["tasks"][0]["status"], "done")
        self.assertEqual(conversation[-1]["content"], answer)

    def test_question_then_answer_resumes_same_run(self):
        self.llm.when(is_planner, lambda r: {"clarify": "Which professor should I email?", "objective": "email"})
        conversation = [{"role": "system", "content": "s"}]
        q = session.run_turn(conversation, "email my professor about the extension")
        self.assertIn("Which professor", q)
        waiting = tasks_store.pending_question()
        self.assertIsNotNone(waiting)
        # Answer: planner now returns a real plan including the answer.
        self.llm.rules.clear()
        self.llm.when(is_planner, lambda r: {"objective": "email", "tasks": [
            {"id": 1, "agent": "answer", "description": "draft it"}]})
        self.llm.when(lambda r: True, lambda r: "Draft for Prof. Smith ready.")
        ctx2 = run_context.RunContext()
        token2 = run_context.activate(ctx2)
        try:
            answer = session.run_turn(conversation, "Prof Smith")
        finally:
            run_context.deactivate(token2)
        self.assertIn("Prof. Smith", answer)
        planner_msgs = [c for c in self.llm.calls if is_planner({"system": c["system"]})]
        self.assertIn("Prof Smith", planner_msgs[-1]["last"]["content"])
        self.assertIsNone(tasks_store.pending_question())

    def test_small_talk_skips_planner(self):
        self.llm.when(lambda r: True, lambda r: "Anytime.")
        answer = session.run_turn([{"role": "system", "content": "s"}], "thanks!")
        self.assertEqual(answer, "Anytime.")
        self.assertFalse(any(is_planner({"system": c["system"]}) for c in self.llm.calls))


if __name__ == "__main__":
    unittest.main()


class HonestSynthesisTests(unittest.TestCase):
    """The final answer must not claim work that didn't happen."""

    def setUp(self):
        self.llm = FakeLLM().install()

    def tearDown(self):
        self.llm.uninstall()

    def test_skipped_step_is_listed_as_not_done_and_qc_runs(self):
        plan = _plan([planner.Task(1, "files", "find files"), planner.Task(2, "writer", "write report", depends_on=[1])],
                     goal="find files and write report")
        plan.tasks[0].status, plan.tasks[0].result = "done", "a.dmg 1GB"
        plan.tasks[1].status, plan.tasks[1].note = "skipped", "depends on 1"
        prompts = []

        def responder(req):
            prompts.append(req["last"]["content"])
            if "Check a draft reply" in req["system"]:
                return {"ok": False, "problems": ["claims the report was created but that step was skipped"]}
            if "had these problems" in req["last"]["content"]:
                return "I found the largest file (a.dmg, 1GB) but didn't write the report."
            return "Report created at ~/Documents/report.md."
        self.llm.when(lambda r: True, responder)
        answer = executor.synthesize(plan, plan.tasks)
        self.assertIn("NOT DONE", prompts[0])
        self.assertIn("didn't write the report", answer)


class RecoveryAndHonestyTests(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLM().install()

    def tearDown(self):
        self.llm.uninstall()

    def test_replan_ids_are_renumbered(self):
        plan = _plan([planner.Task(1, "research", "search"), planner.Task(2, "writer", "write", depends_on=[1])])
        plan.tasks[0].status, plan.tasks[0].result = "failed", "no results"
        plan.tasks[1].status = "skipped"
        self.llm.when(is_replanner, lambda r: {"reason": "write from knowledge", "tasks": [
            {"id": 1, "agent": "answer", "description": "outline topics"},
            {"id": 2, "agent": "writer", "description": "write guide", "depends_on": [1]}]})
        spec = planner.replan(plan, "search failed")
        self.assertEqual([t.id for t in spec.tasks], [3, 4])
        self.assertEqual(spec.tasks[1].depends_on, [3])

    def test_claimed_missing_file_is_corrected(self):
        import tools.tools as t
        original = t.FUNCTIONS["file_exists"]
        t.FUNCTIONS["file_exists"] = lambda path: {"ok": True, "exists": False}
        self.llm.when(lambda r: True, lambda r: "Guide saved to ~/Documents/CSC236/guide.md.")
        try:
            out = executor._check_claimed_files("I created ~/Documents/CSC236/guide.md for you.", "evidence", "AGENT")
        finally:
            t.FUNCTIONS["file_exists"] = original
        self.assertIn("not actually created", out)

    def test_router_hints(self):
        self.assertEqual(router.session_hint("On the Jugnu account, research X"), "agent")
        self.assertEqual(router.session_hint("do it in your account please"), "agent")
        self.assertEqual(router.session_hint("open it on my account"), "primary")
        self.assertIsNone(router.session_hint("research X"))
        self.assertTrue(router.wants_background("do this in the background"))
        self.assertFalse(router.wants_background("what's in the background of this photo"))
