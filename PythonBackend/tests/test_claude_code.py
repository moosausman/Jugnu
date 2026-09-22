"""Claude mode: Jugnu's model calls run through the Claude Code CLI on the
user's Claude plan. A stand-in `claude` script plays the CLI here, so no real
call is made."""

import _env  # noqa: F401  (isolates data dir; must be first)

import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest

import claude_code
import llm_client
import run_context

# A fake `claude`: answers --help/--version, and for -p logs its argv, stdin
# and environment, then prints whatever reply file the test queued up.
_FAKE = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    d = os.environ["FAKE_CLAUDE_DIR"]
    if "--help" in sys.argv:
        print("Options: -p --output-format --model --system-prompt --tools --strict-mcp-config "
              "--setting-sources --no-session-persistence --max-turns")
        sys.exit(0)
    if "--version" in sys.argv:
        print("2.1.300 (Claude Code)")
        sys.exit(0)
    prompt = sys.stdin.read()
    calls = json.load(open(os.path.join(d, "calls.json"))) if os.path.exists(os.path.join(d, "calls.json")) else []
    calls.append({{"argv": sys.argv[1:], "stdin": prompt,
                   "api_key": os.environ.get("ANTHROPIC_API_KEY"), "cwd": os.getcwd()}})
    json.dump(calls, open(os.path.join(d, "calls.json"), "w"))
    replies = sorted(f for f in os.listdir(d) if f.startswith("reply"))
    reply = json.load(open(os.path.join(d, replies[0])))
    os.remove(os.path.join(d, replies[0]))
    print(json.dumps(reply))
    sys.exit(reply.pop("_exit", 0))
""")


class ClaudeModeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fake-claude-")
        path = os.path.join(self.dir, "claude")
        with open(path, "w") as f:
            f.write(_FAKE.format(python=sys.executable))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.saved = {k: os.environ.get(k) for k in ("JUGNU_CLAUDE_PATH", "FAKE_CLAUDE_DIR", "ANTHROPIC_API_KEY")}
        os.environ.update(JUGNU_CLAUDE_PATH=path, FAKE_CLAUDE_DIR=self.dir, ANTHROPIC_API_KEY="sk-should-not-leak")
        claude_code._features.clear()
        claude_code._versions.clear()
        self.old_mode = llm_client._mode
        llm_client.set_mode("claude")
        self.queued = 0

    def tearDown(self):
        llm_client._mode = self.old_mode
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def reply(self, result, **extra):
        self.queued += 1
        body = {"type": "result", "subtype": "success", "is_error": False, "result": result,
                "usage": {"input_tokens": 12, "cache_read_input_tokens": 100, "output_tokens": 7}, **extra}
        with open(os.path.join(self.dir, f"reply{self.queued:03d}.json"), "w") as f:
            json.dump(body, f)

    def calls(self):
        with open(os.path.join(self.dir, "calls.json")) as f:
            return json.load(f)

    TOOLS = [{"type": "function", "function": {
        "name": "play_song", "description": "Play a song on Spotify.",
        "parameters": {"type": "object", "properties": {"song": {"type": "string"}}}}}]

    def test_plain_reply(self):
        self.reply("Hello there.")
        response = llm_client.chat([{"role": "system", "content": "You are Jugnu."},
                                    {"role": "user", "content": "hi"}], role="EASY")
        self.assertEqual(response["message"].content, "Hello there.")
        self.assertEqual(response["message"].tool_calls, [])
        call = self.calls()[0]
        self.assertEqual(call["stdin"], "hi")
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "You are Jugnu.")
        self.assertEqual(argv[argv.index("--tools") + 1], "")          # Claude Code's own tools off
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIsNone(call["api_key"])                              # the plan, never the API
        self.assertTrue(call["cwd"].endswith("claude-code"))

    def test_tool_call_round_trip(self):
        self.reply('{"content": "", "tool_calls": [{"name": "play_song", "arguments": {"song": "Levitating"}}]}')
        messages = [{"role": "system", "content": "You are Jugnu."}, {"role": "user", "content": "play levitating"}]
        response = llm_client.chat(messages, tools=self.TOOLS, role="PLANNER")
        message = response["message"]
        self.assertEqual(len(message.tool_calls), 1)
        self.assertEqual(message.tool_calls[0].function.name, "play_song")
        self.assertEqual(message.tool_calls[0].function.arguments, {"song": "Levitating"})
        first = self.calls()[0]
        self.assertEqual(first["argv"][first["argv"].index("--model") + 1], "sonnet")
        system = first["argv"][first["argv"].index("--system-prompt") + 1]
        self.assertIn('"name":"play_song"', system)
        self.assertIn('"tool_calls"', system)

        # Next turn: the call and its result are in the transcript.
        messages += [message.model_dump(), {"role": "tool", "name": "play_song", "content": "Playing Levitating."}]
        self.reply("Sure -- here it is:\n```json\n{\"content\": \"Playing Levitating.\", \"tool_calls\": []}\n```")
        final = llm_client.chat(messages, tools=self.TOOLS, role="PLANNER")["message"]
        self.assertEqual(final.content, "Playing Levitating.")
        self.assertEqual(final.tool_calls, [])
        transcript = self.calls()[1]["stdin"]
        self.assertIn("<user>\nplay levitating\n</user>", transcript)
        self.assertIn('"name": "play_song"', transcript)
        self.assertIn('<tool_result name="play_song">\nPlaying Levitating.\n</tool_result>', transcript)

    def test_usage_is_counted(self):
        self.reply("ok")
        ctx = run_context.RunContext(run_id="t", emit=lambda payload: None)
        token = run_context.activate(ctx)
        try:
            llm_client.chat([{"role": "user", "content": "hi"}], role="EASY")
        finally:
            run_context.deactivate(token)
        self.assertEqual(ctx.usage["calls"], 1)
        self.assertEqual(ctx.usage["prompt_tokens"], 112)
        self.assertEqual(ctx.usage["completion_tokens"], 7)

    def test_not_logged_in_is_explained_and_never_falls_back_to_the_api(self):
        self.reply("Invalid API key · Please run /login", is_error=True, _exit=1)
        with self.assertRaises(RuntimeError) as caught:
            llm_client.chat([{"role": "user", "content": "hi"}], role="AGENT")
        self.assertIn("isn't logged in", str(caught.exception))

    def test_usage_limit_is_explained(self):
        self.reply("Claude AI usage limit reached|1760000000", is_error=True, _exit=1)
        with self.assertRaises(RuntimeError) as caught:
            llm_client.chat([{"role": "user", "content": "hi"}], role="AGENT")
        self.assertIn("usage limit", str(caught.exception))

    def test_missing_cli(self):
        os.environ["JUGNU_CLAUDE_PATH"] = os.path.join(self.dir, "nope")
        self.assertEqual(claude_code.status(), {"installed": False})
        with self.assertRaises(RuntimeError) as caught:
            llm_client.chat([{"role": "user", "content": "hi"}], role="AGENT")
        self.assertIn("isn't installed", str(caught.exception))

    def test_status_and_check(self):
        self.assertEqual(claude_code.status()["version"], "2.1.300")
        self.reply("OK")
        self.assertTrue(claude_code.check()["ok"])

    def test_mode_is_reported(self):
        self.assertEqual(llm_client.model_mode(), "claude")
        self.assertEqual(llm_client.resolved_model_name("PLANNER"), "claude sonnet")
        self.assertEqual(llm_client.resolved_model_name("EASY"), "claude haiku")


class ReplyParsingTests(unittest.TestCase):
    TOOLS = ClaudeModeTests.TOOLS

    def test_forms_claude_might_answer_in(self):
        parse = claude_code.parse_reply
        self.assertEqual(parse('{"content": "Done.", "tool_calls": []}', self.TOOLS), ("Done.", []))
        self.assertEqual(parse('Here you go: {"tool_calls": [{"name": "play_song", "arguments": "{\\"song\\": \\"x\\"}"}]}',
                               self.TOOLS), ("", [("play_song", {"song": "x"})]))
        self.assertEqual(parse('{"tool_calls": [{"function": {"name": "play_song", "arguments": {}}}]}', self.TOOLS),
                         ("", [("play_song", {})]))
        self.assertEqual(parse("I can't do that.", self.TOOLS), ("I can't do that.", []))
        self.assertEqual(parse('{"a": 1}', None), ('{"a": 1}', []))   # no tools: text untouched

    def test_single_message_goes_as_is(self):
        system, prompt = claude_code.build_prompt([{"role": "system", "content": "S"},
                                                   {"role": "user", "content": "Q"}], None)
        self.assertEqual((system, prompt), ("S", "Q"))


if __name__ == "__main__":
    unittest.main()
