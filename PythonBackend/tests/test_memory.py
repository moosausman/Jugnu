"""Phase 2: structured memory, contradictions, decay, error/tool memory, migration."""

import _env  # noqa: F401

import json
import time
import unittest
from contextlib import closing

import learning
import memory
import memory_store as ms
import run_context
import tool_runtime


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        ms.clear()

    def test_create_and_recall_by_relevance(self):
        ms.remember("CSC236 lecture notes are in ~/Documents/UofT/CSC236", kind="semantic",
                    source="observed", confidence=0.9, importance=0.7)
        ms.remember("The user prefers studying in the evening", kind="user", key="pref.study_time",
                    source="user_stated", confidence=0.95)
        ms.remember("Spotify playback needs the desktop app running", kind="semantic", source="tool_result")
        hits = ms.recall("where are my CSC236 notes", kinds=("semantic",), k=2)
        self.assertTrue(hits)
        self.assertIn("CSC236", hits[0]["content"])
        self.assertEqual(hits[0]["use_count"], 0)  # returned before the touch; stored count now 1
        self.assertEqual(ms.get(hits[0]["id"])["use_count"], 1)

    def test_same_content_reinforces(self):
        a = ms.remember("Likes jazz", kind="user", key="pref.music", source="inferred", confidence=0.4)
        b = ms.remember("likes jazz", kind="user", key="pref.music", source="inferred", confidence=0.4)
        self.assertEqual(b["action"], "reinforced")
        self.assertEqual(a["memory"]["id"], b["memory"]["id"])
        self.assertLessEqual(ms.get(a["memory"]["id"])["confidence"], ms.INFERRED_MAX_CONFIDENCE)
        c = ms.remember("likes jazz", kind="user", key="pref.music", source="user_stated", confidence=0.9)
        self.assertEqual(c["action"], "reinforced")
        self.assertEqual(ms.get(a["memory"]["id"])["source"], "user_stated")

    def test_contradiction_newer_confirmed_wins_and_history_kept(self):
        old = ms.remember("Lives in Toronto", kind="user", key="user.city", source="user_stated", confidence=0.9)
        new = ms.remember("Lives in Mississauga", kind="user", key="user.city", source="user_stated", confidence=0.9)
        self.assertEqual(new["action"], "superseded")
        self.assertEqual(ms.get(old["memory"]["id"])["status"], "superseded")
        active = ms.list_memories(kind="user")
        self.assertEqual([m["content"] for m in active], ["Lives in Mississauga"])
        detail = ms.get(new["memory"]["id"])
        self.assertEqual(detail["history"][0]["content"], "Lives in Toronto")

    def test_guess_never_overrides_stated_fact(self):
        ms.remember("Name is Moosa", kind="user", key="user.name", source="user_stated", confidence=1.0)
        r = ms.remember("Name is Musa", kind="user", key="user.name", source="inferred", confidence=0.9)
        self.assertEqual(r["action"], "kept_existing")
        active = ms.list_memories(kind="user")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["content"], "Name is Moosa")
        self.assertTrue(active[0]["data"]["disputed_by"])

    def test_inferred_confidence_capped_and_labelled(self):
        r = ms.remember("Probably a night owl", kind="user", key="pref.sleep", source="inferred", confidence=0.99)
        self.assertLessEqual(r["memory"]["confidence"], ms.INFERRED_MAX_CONFIDENCE)
        self.assertIn("unconfirmed", ms.user_profile_block())

    def test_secrets_refused(self):
        for text in ("My API key is AIzaSyA1234567890abcdefghijklmnopqrstuv", "password=hunter2hunter2"):
            with self.assertRaises(ms.MemoryRejected):
                ms.remember(text, kind="user")
        self.assertEqual(ms.list_memories(), [])

    def test_user_edit_and_delete_restores_previous(self):
        first = ms.remember("Editor is VS Code", kind="user", key="pref.editor", source="user_stated")
        second = ms.remember("Editor is Cursor", kind="user", key="pref.editor", source="user_stated")
        edited = ms.update(second["memory"]["id"], content="Editor is Cursor (for Python)")
        self.assertEqual(edited["source"], "user_confirmed")
        self.assertEqual(edited["confidence"], 1.0)
        self.assertTrue(ms.delete(second["memory"]["id"]))
        self.assertEqual(ms.get(first["memory"]["id"])["status"], "active")

    def test_scope_affects_ranking(self):
        ms.remember("Run tests with pytest -q", kind="strategy", scope="project:/Users/x/app", source="observed")
        ms.remember("Run tests with npm test", kind="strategy", scope="project:/Users/x/web", source="observed")
        hits = ms.recall("how to run tests", kinds=("strategy",), scope="project:/Users/x/web", k=2)
        self.assertIn("npm", hits[0]["content"])

    def test_decay_and_prune(self):
        r = ms.remember("Old trivia", kind="episodic", importance=0.1, confidence=0.3)
        long_ago = time.time() - 400 * 86400
        with closing(ms._connect()) as conn, conn:
            conn.execute("UPDATE memories SET updated_at = ?, last_used_at = ?, created_at = ? WHERE id = ?",
                         (long_ago, long_ago, long_ago, r["memory"]["id"]))
        keep = ms.remember("Stable fact", kind="user", importance=0.1, confidence=0.3, source="user_stated")
        ms.maintain()
        self.assertIsNone(ms.get(r["memory"]["id"]))
        self.assertIsNotNone(ms.get(keep["memory"]["id"]), "user facts never decay")

    def test_counts_and_clear_kind(self):
        ms.remember("a", kind="semantic")
        ms.remember("b", kind="error")
        self.assertEqual(ms.counts()["semantic"]["active"], 1)
        ms.clear("semantic")
        self.assertEqual(ms.counts()["semantic"]["active"], 0)
        self.assertEqual(ms.counts()["error"]["active"], 1)


class FactsCompatibilityTests(unittest.TestCase):
    def setUp(self):
        ms.clear()

    def test_migration_from_facts_file(self):
        memory._facts_migrated = False
        memory.FACTS_FILE.write_text(json.dumps({"name": "Moosa", "university": "UTM"}))
        profile = memory.facts_as_string()
        self.assertIn("Moosa", profile)
        self.assertIn("UTM", profile)
        self.assertFalse(memory.FACTS_FILE.exists())
        memory.save_fact("university", "University of Toronto Mississauga")
        self.assertIn("University of Toronto Mississauga", memory.facts_as_string())
        self.assertNotIn("university: UTM", memory.facts_as_string())
        memory.clear_facts()
        self.assertEqual(memory.facts_as_string(), "")


class ErrorAndToolMemoryTests(unittest.TestCase):
    def setUp(self):
        ms.clear()
        learning.install_hooks()

    def test_failure_then_success_learns_fix(self):
        attempts = []

        def open_notes(path: str):
            attempts.append(path)
            if "Notes" not in path:
                return {"ok": False, "error": f"No such file: {path}"}
            return {"ok": True}

        schema = {"function": {"name": "file_read", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
        funcs = {"file_read": open_notes}
        ctx = run_context.RunContext()
        bad = tool_runtime.execute("file_read", {"path": "~/Documents/notes.txt"}, funcs, {"file_read": schema}, ctx=ctx)
        self.assertFalse(bad.ok)
        good = tool_runtime.execute("file_read", {"path": "~/Documents/Notes/notes.txt"}, funcs,
                                    {"file_read": schema}, ctx=ctx)
        self.assertTrue(good.ok)
        errors = ms.list_memories(kind="error")
        self.assertEqual(len(errors), 1)
        self.assertIn("fix", errors[0]["data"])
        self.assertIn("Notes", errors[0]["data"]["fix"])

        # Next time the same failure happens, the fix is shown with the error.
        again = tool_runtime.execute("file_read", {"path": "~/Documents/notes.txt"}, funcs, {"file_read": schema},
                                     ctx=run_context.RunContext())
        self.assertIn("known_fix_from_past_experience", again.text)

        stats = {t["tool"]: t for t in ms.tool_stats()}
        self.assertEqual(stats["file_read"]["successes"], 1)
        self.assertEqual(stats["file_read"]["failures"], 2)

    def test_unreliable_tools_block(self):
        for _ in range(5):
            ms.record_tool_use("flaky_tool", False, 0.1, "timeout")
        ms.record_tool_use("flaky_tool", True, 0.1)
        self.assertIn("flaky_tool", ms.unreliable_tools_block())


if __name__ == "__main__":
    unittest.main()
