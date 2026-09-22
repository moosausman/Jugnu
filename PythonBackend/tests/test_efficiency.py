"""Efficiency: requests that need no model, requests that skip the planner,
and agent loops that stop instead of burning calls on work that can't succeed."""

import _env  # noqa: F401  (isolates data dir; must be first)

import unittest

import agent_core
import fastpath
import router
import run_context
import search
import session
import tasks_store
import tool_runtime
from fake_llm import FakeLLM, is_planner
from tools import tools as tools_module


class FastPathParseTests(unittest.TestCase):
    def parsed(self, text):
        intent = fastpath.parse(text)
        return (intent.tool, intent.args) if intent else None

    def test_music_requests(self):
        self.assertEqual(self.parsed("play levitating by dua lipa"),
                         ("play_song", {"song": "levitating", "artist": "dua lipa"}))
        self.assertEqual(self.parsed("Play Stand By Me please"), ("play_song", {"song": "Stand By Me"}))
        self.assertEqual(self.parsed("play some drake"), ("play_artist", {"artist": "drake"}))
        self.assertEqual(self.parsed("play the after hours album by the weeknd"),
                         ("play_album", {"album": "after hours", "artist": "the weeknd"}))
        self.assertEqual(self.parsed("play the lofi beats playlist"), ("play_playlist", {"playlist": "lofi beats"}))
        self.assertEqual(self.parsed("pause"), ("spotify_control", {"action": "pause"}))
        self.assertEqual(self.parsed("stop the music"), ("spotify_control", {"action": "pause"}))
        self.assertEqual(self.parsed("next song"), ("spotify_control", {"action": "next"}))
        self.assertEqual(self.parsed("volume 40"), ("spotify_control", {"volume": 40}))
        self.assertEqual(self.parsed("what's playing?"), ("spotify_now_playing", {}))

    def test_leaves_anything_unclear_to_the_model(self):
        for text in ("play that song again", "play me something relaxing", "play my playlist",
                     "play the video on youtube", "open my downloads folder", "aura", "play a song for studying"):
            self.assertIsNone(fastpath.parse(text), text)

    def test_open_only_real_apps(self):
        fastpath._app_index = {"safari": "Safari", "visual studio code": "Visual Studio Code"}
        try:
            self.assertEqual(self.parsed("open safari"), ("open_app", {"app_name": "Safari"}))
            self.assertEqual(self.parsed("launch Visual Studio Code"), ("open_app", {"app_name": "Visual Studio Code"}))
            self.assertIsNone(fastpath.parse("open the pod bay doors"))
        finally:
            fastpath._app_index = None


class FastPathRunTests(unittest.TestCase):
    def setUp(self):
        self.ctx = run_context.RunContext()
        self.token = run_context.activate(self.ctx)
        self.llm = FakeLLM().install()
        self._orig = dict(tools_module.FUNCTIONS)

    def tearDown(self):
        self.llm.uninstall()
        run_context.deactivate(self.token)
        tools_module.FUNCTIONS.clear()
        tools_module.FUNCTIONS.update(self._orig)

    def test_play_runs_one_tool_and_no_model(self):
        calls = []

        def play_song(song: str, artist: str = ""):
            calls.append((song, artist))
            return {"ok": True, "resolved": {"name": "Levitating", "artist": "Dua Lipa"}}
        tools_module.FUNCTIONS["play_song"] = play_song
        conversation = [{"role": "system", "content": "s"}]
        answer = session.run_turn(conversation, "play levitating by dua lipa")
        self.assertEqual(answer, "Playing Levitating by Dua Lipa.")
        self.assertEqual(calls, [("levitating", "dua lipa")])
        self.assertEqual(self.llm.calls, [], "the fast path must not call the model")
        self.assertEqual(self.ctx.usage["calls"], 0)
        self.assertEqual(conversation[-1]["content"], answer)

    def test_permission_error_is_explained_not_retried(self):
        calls = []

        def play_song(song: str, artist: str = ""):
            calls.append(song)
            return {"ok": False, "error": "Not authorized to send Apple events to Spotify."}
        tools_module.FUNCTIONS["play_song"] = play_song
        answer = fastpath.try_handle("play blinding lights")
        self.assertEqual(len(calls), 1)
        self.assertIn("Automation", answer)
        self.assertIn("Spotify", answer)
        self.assertEqual(self.llm.calls, [])


class DirectRoutingTests(unittest.TestCase):
    def test_single_intent_requests_skip_the_planner(self):
        self.assertEqual(router.direct_agent("what's on my calendar today"), "calendar")
        self.assertEqual(router.direct_agent("msg buddy boy on whatsapp saying hi"), "whatsapp")
        self.assertEqual(router.direct_agent("what's the weather tomorrow"), "lookup")
        self.assertEqual(router.direct_agent("who won the world cup"), "research")
        self.assertEqual(router.direct_agent("explain recursion"), "answer")
        self.assertEqual(router.direct_agent("play me something relaxing"), "system")

    def test_multi_step_scheduled_or_long_form_requests_are_planned(self):
        for text in ("open safari and go to youtube", "remind me in 20 minutes to stretch",
                     "research the best laptops and write a report", "every morning send me the news",
                     "aura", "what's the latest on the election today"):
            self.assertNotIn(router.direct_agent(text), ("calendar", "whatsapp", "files", "answer"), text)
        self.assertIsNone(router.direct_agent("open safari and go to youtube"))
        self.assertIsNone(router.direct_agent("remind me in 20 minutes to stretch"))

    def test_direct_turn_makes_no_planner_call(self):
        llm = FakeLLM().install()
        ctx = run_context.RunContext()
        token = run_context.activate(ctx)
        try:
            llm.when(lambda r: True, lambda r: "Recursion is a function calling itself.")
            answer = session.run_turn([{"role": "system", "content": "s"}], "explain recursion")
        finally:
            llm.uninstall()
            run_context.deactivate(token)
        self.assertIn("calling itself", answer)
        self.assertFalse(any(is_planner({"system": c["system"]}) for c in llm.calls))
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(tasks_store.get(ctx.run_id)["status"], "done")


class AgentLoopStopTests(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLM().install()
        self.ctx = run_context.RunContext()
        self.token = run_context.activate(self.ctx)

    def tearDown(self):
        self.llm.uninstall()
        run_context.deactivate(self.token)

    def _schema(self, name):
        return {"type": "function", "function": {"name": name, "description": "x",
                                                 "parameters": {"type": "object", "properties": {}}}}

    def test_permission_error_stops_the_loop(self):
        tries = []

        def spotify_control():
            tries.append(1)
            return {"ok": False, "error": "Not authorized to send Apple events to Spotify."}
        self.llm.when(lambda r: True, lambda r: [("spotify_control", {})])
        result = agent_core.run_tool_loop([{"role": "system", "content": "s"}, {"role": "user", "content": "pause"}],
                                          [self._schema("spotify_control")], {"spotify_control": spotify_control},
                                          max_iterations=10)
        self.assertEqual(len(tries), 1, "a missing permission must not be retried")
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("Privacy & Security", result.content)

    def test_three_failures_in_a_row_stop_the_loop(self):
        tries = []

        def flaky(**kwargs):
            tries.append(1)
            return {"ok": False, "error": f"something broke {len(tries)}"}
        n = {"i": 0}

        def respond(r):
            n["i"] += 1
            return [("flaky", {"attempt": n["i"]})]
        self.llm.when(lambda r: True, respond)
        import policy
        policy.register("flaky", policy.Risk.LOW, "notifications")
        schema = {"type": "function", "function": {"name": "flaky", "description": "x", "parameters": {
            "type": "object", "properties": {"attempt": {"type": "integer"}}}}}
        result = agent_core.run_tool_loop([{"role": "system", "content": "s"}, {"role": "user", "content": "go"}],
                                          [schema], {"flaky": flaky}, max_iterations=10)
        self.assertEqual(len(tries), agent_core.MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(len(self.llm.calls), agent_core.MAX_CONSECUTIVE_FAILURES)
        self.assertIn("couldn't", result.content)


class SearchPassageTests(unittest.TestCase):
    def test_picks_the_passage_that_answers(self):
        text = ("Welcome to our site. We cover sports.\n"
                "Spain won the 2026 FIFA World Cup final, beating Argentina 1-0 after extra time.\n"
                "Subscribe to our newsletter for more.")
        best = search.best_passages(text, "who won the 2026 world cup final", count=1)
        self.assertEqual(len(best), 1)
        self.assertIn("Spain won", best[0])

    def test_dates(self):
        self.assertEqual([d.isoformat() for d in search.extract_dates("Published July 19, 2026 and 2026-07-20")],
                         ["2026-07-20", "2026-07-19"])


class UsageTests(unittest.TestCase):
    def test_model_calls_are_counted_per_run(self):
        import llm_client
        ctx = run_context.RunContext()
        token = run_context.activate(ctx)
        try:
            llm_client._record_usage(1200, 40)
            llm_client._record_usage(800, 10)
            llm_client._record_usage(500, 20, local=True)
        finally:
            run_context.deactivate(token)
        self.assertEqual(ctx.usage, {"calls": 2, "prompt_tokens": 2000, "completion_tokens": 50, "local_calls": 1})


class ModelModeTests(unittest.TestCase):
    def setUp(self):
        import llm_client
        self.llm_client = llm_client
        self._mode = llm_client._mode

    def tearDown(self):
        self.llm_client._mode = self._mode

    def test_modes_route_roles(self):
        c = self.llm_client
        c.set_mode("api")
        self.assertEqual({c._provider_for(r) for r in ("PLANNER", "AGENT", "FAST", "DEEP", "EASY")}, {"api"})
        c.set_mode("local")
        self.assertEqual({c._provider_for(r) for r in ("PLANNER", "AGENT", "FAST", "DEEP", "EASY")}, {"local"})
        c.set_mode("hybrid")
        self.assertEqual(c._provider_for("EASY"), "local")
        self.assertEqual(c._provider_for("PLANNER"), "api")
        self.assertEqual(c._provider_for("FAST"), "api")
        with self.assertRaises(ValueError):
            c.set_mode("cloud")

    def test_hybrid_falls_back_to_the_api_when_ollama_is_down(self):
        import os
        c = self.llm_client
        c.set_mode("hybrid")
        orig_chat, orig_api = c.ollama.chat, c._api_chat
        env = {k: os.environ.get(k) for k in ("JUGNU_API_BASE_URL", "JUGNU_API_KEY")}
        os.environ.update({"JUGNU_API_BASE_URL": "http://127.0.0.1:9", "JUGNU_API_KEY": "k"})
        used = []

        def down(**kwargs):
            raise ConnectionError("ollama not running")
        c.ollama.chat = down
        c._api_chat = lambda messages, tools=None, role="AGENT": used.append(role) or c.ChatResponse(
            c.Message("assistant", "from the api"))
        try:
            reply = c.chat([{"role": "user", "content": "hi"}], role="EASY")
        finally:
            c.ollama.chat, c._api_chat = orig_chat, orig_api
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(reply.message.content, "from the api")
        self.assertEqual(used, ["EASY"])


class AttachmentTests(unittest.TestCase):
    def test_instructions_about_selected_text_are_answered_directly(self):
        for text in ("Summarize this.", "fix the grammar", "Draft a reply to this."):
            self.assertEqual(router.direct_agent(text) or router.attachment_agent(text), "answer", text)
        self.assertEqual(router.direct_agent("Turn this into reminders."), "calendar")
        self.assertIsNone(router.attachment_agent("remind me about this tomorrow at 9"))

    def test_selected_text_reaches_the_task_and_the_history(self):
        llm = FakeLLM().install()
        ctx = run_context.RunContext()
        token = run_context.activate(ctx)
        seen = []
        try:
            llm.when(lambda r: True, lambda r: seen.append(r["last"]["content"]) or "A short summary.")
            conversation = [{"role": "system", "content": "s"}]
            answer = session.run_turn(conversation, "Summarize this.",
                                      attachment={"text": "Quarterly revenue rose 12 percent.", "app": "Safari"})
        finally:
            llm.uninstall()
            run_context.deactivate(token)
        self.assertEqual(answer, "A short summary.")
        self.assertEqual(len(llm.calls), 1, "one plain answer, no planner")
        self.assertIn("Quarterly revenue rose 12 percent.", seen[0])
        self.assertIn("from Safari", conversation[1]["content"])


if __name__ == "__main__":
    unittest.main()
