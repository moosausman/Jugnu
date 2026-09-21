"""Phase 6: reflection, correction learning, skill library."""

import _env  # noqa: F401

import time
import unittest

import audit
import learning
import memory_store as ms
import planner
import skill_library as sl
from schemas import Reflection, SkillCandidate
from tests.fake_llm import FakeLLM


def _done_plan():
    plan = planner.Plan(goal="Help me prepare for my CSC236 test", objective="Prepare for the CSC236 test", tasks=[
        planner.Task(1, "files", "Find CSC236 materials in ~/Documents", risk="read"),
        planner.Task(2, "calendar", "Find free study blocks this week", risk="read"),
        planner.Task(3, "writer", "Write a study plan to ~/Documents/CSC236/plan.md", depends_on=[1, 2], risk="low"),
    ])
    for t in plan.tasks:
        t.status, t.attempts, t.result = "done", 1, f"result of {t.id}"
    return plan


class SkillLibraryTests(unittest.TestCase):
    def setUp(self):
        for s in sl.list_all():
            sl.delete(s["id"])

    def _propose(self, name="Prepare for a test"):
        cand = SkillCandidate(name=name, purpose="Turn a test date into a study plan with notes and reminders",
                              trigger_phrases=["prepare for my test", "study for"], inputs=["course code"],
                              expected_output="study plan file", verification="plan file exists")
        return sl.propose(cand, _done_plan(), run_id="r1", tools=["file_search", "calendar_list", "file_write"])

    def test_propose_is_suggested_and_not_matched_until_accepted(self):
        skill = self._propose()
        self.assertEqual(skill["status"], "suggested")
        self.assertEqual(len(skill["steps"]), 3)
        self.assertEqual(skill["risk"], "low")
        self.assertEqual(sl.match("help me prepare for my test in CSC263"), [])
        sl.update(skill["id"], status="active")
        hits = sl.match("help me prepare for my test in CSC263")
        self.assertEqual(hits[0]["id"], skill["id"])
        block, ids = sl.planner_block("help me prepare for my test in CSC263")
        self.assertIn("Prepare for a test", block)
        self.assertEqual(ids, [skill["id"]])

    def test_versions_history_and_revert(self):
        skill = self._propose()
        v2 = sl.update(skill["id"], purpose="Better purpose")
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["history"][-1]["purpose"], skill["purpose"])
        reverted = sl.revert(skill["id"], 1)
        self.assertEqual(reverted["purpose"], skill["purpose"])
        self.assertEqual(reverted["version"], 3)
        status_only = sl.update(skill["id"], status="disabled")
        self.assertEqual(status_only["version"], 3, "status changes don't create versions")

    def test_active_skill_never_overwritten_by_reflection(self):
        skill = self._propose()
        sl.update(skill["id"], status="active", purpose="User-edited purpose")
        self.assertIsNone(self._propose())
        self.assertEqual(sl.get(skill["id"])["purpose"], "User-edited purpose")

    def test_stats_and_review_flag(self):
        skill = self._propose()
        sl.update(skill["id"], status="active")
        for ok in (False, False, True):
            sl.record_use(skill["id"], ok)
        s = sl.get(skill["id"])
        self.assertEqual((s["successes"], s["failures"]), (1, 2))
        self.assertTrue(s["needs_review"])
        self.assertEqual(sl.match("prepare for my test"), [], "skills needing review aren't offered")

    def test_single_step_and_secret_skills_rejected(self):
        plan = planner.Plan("x", [planner.Task(1, "files", "one step")], objective="x")
        plan.tasks[0].status = "done"
        cand = SkillCandidate(name="One step thing", purpose="just one step")
        self.assertIsNone(sl.propose(cand, plan))
        cand = SkillCandidate(name="Leaky skill", purpose="uses key AIzaSyA1234567890abcdefghijklmnopqrstuv")
        self.assertIsNone(sl.propose(cand, _done_plan()))


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        ms.clear()
        for s in sl.list_all():
            sl.delete(s["id"])

    def test_apply_reflection_stores_and_guards(self):
        plan = _done_plan()
        reflection = Reflection(
            objective="Prepare for CSC236", succeeded=True, outcome_summary="Plan written, 3 study blocks found",
            lessons=["CSC236 materials are under ~/Documents/UofT/CSC236"],
            memories=[
                {"content": "CSC236 notes live in ~/Documents/UofT/CSC236", "kind": "semantic", "source": "observed",
                 "confidence": 0.9},
                {"content": "The user loves studying at 5am", "kind": "user", "source": "user_stated",
                 "key": "pref.study_time", "confidence": 0.9},
            ],
            skill={"name": "Prepare for a test", "purpose": "Study plan from course material",
                   "trigger_phrases": ["prepare for my test"]},
            planning_advice="Search ~/Documents/UofT first for course material",
            code_change_suggestion="file_search should support content search")
        stored = learning.apply_reflection("run-refl", plan, reflection, user_texts=[plan.goal])
        self.assertEqual(stored["memories"], 2)
        self.assertTrue(stored["skill"])
        self.assertTrue(stored["proposal"])
        user_mem = ms.list_memories(kind="user")[0]
        self.assertEqual(user_mem["source"], "inferred", "unsaid 'user_stated' claims are downgraded")
        self.assertLessEqual(user_mem["confidence"], ms.INFERRED_MAX_CONFIDENCE)
        self.assertTrue(ms.list_memories(kind="strategy"))
        self.assertTrue(ms.list_memories(kind="episodic"))
        self.assertEqual(sl.get(stored["skill"])["status"], "suggested")
        proposals = audit.recent(kinds=["code_change_proposal"])
        self.assertTrue(proposals)
        self.assertEqual(proposals[0]["status"], "proposed")

    def test_reflect_with_model_output(self):
        llm = FakeLLM().install()
        try:
            llm.when(lambda r: "REFLECTION step" in r["system"], lambda r: {
                "objective": "Prepare for CSC236", "succeeded": True, "outcome_summary": "done",
                "lessons": ["Check the course syllabus PDF for test coverage first"], "memories": []})
            result = learning.reflect("run-x", _done_plan(), "Your plan is ready.")
        finally:
            llm.uninstall()
        self.assertIsNotNone(result)
        self.assertIn("syllabus", ms.list_memories(kind="strategy")[0]["content"])

    def test_trivial_runs_not_reflected(self):
        plan = planner.Plan("battery?", [planner.Task(1, "system", "check battery", risk="read")],
                            objective="battery", complexity="simple")
        plan.tasks[0].status, plan.tasks[0].attempts = "done", 1
        self.assertFalse(learning._worth_reflecting(plan, "80%"))
        # A simple request isn't reflected on even after a retry: its errors
        # are already recorded by the per-call hooks, without a model call.
        plan.tasks[0].attempts = 2
        self.assertFalse(learning._worth_reflecting(plan, "80%"))
        # Multi-step work that hit trouble is.
        two = planner.Plan("prep", [planner.Task(1, "files", "a"), planner.Task(2, "writer", "b", depends_on=[1])],
                           objective="prep", complexity="standard")
        for t in two.tasks:
            t.status, t.attempts = "done", 1
        two.tasks[1].status = "failed"
        self.assertTrue(learning._worth_reflecting(two, "partly"))


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        ms.clear()

    def test_detection(self):
        for text in ("No, my midterm is on the 17th", "that's wrong", "Actually, I meant CSC263", "I said Friday"):
            self.assertTrue(learning.looks_like_correction(text), text)
        for text in ("now make it shorter", "what's the weather"):
            self.assertFalse(learning.looks_like_correction(text), text)

    def test_correction_becomes_user_memory_and_lesson(self):
        llm = FakeLLM().install()
        try:
            llm.when(lambda r: "just corrected Jugnu" in r["system"], lambda r: {
                "corrected_facts": [{"content": "My midterm is on the 17th", "key": "csc236.midterm_date"}],
                "lesson": "Confirm exam dates from the user's own message before scheduling"})
            learning._record_correction("Jugnu: your midterm is on the 15th", "No, my midterm is on the 17th")
        finally:
            llm.uninstall()
        user = ms.list_memories(kind="user")
        self.assertEqual(user[0]["source"], "user_stated")
        self.assertIn("17th", user[0]["content"])
        self.assertTrue(ms.list_memories(kind="strategy"))


if __name__ == "__main__":
    unittest.main()
