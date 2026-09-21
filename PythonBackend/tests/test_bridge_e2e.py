"""End-to-end: the real bridge.py process, speaking the Swift app's JSON
protocol, driven by a local fake OpenAI-compatible model server. Nothing
reaches the real Mac (MacBridge points at a closed port) or a real model.
"""

import _env  # noqa: F401

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeModel(BaseHTTPRequestHandler):
    """Scripted /chat/completions: answers by which prompt it's looking at."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body["messages"]
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        last = msgs[-1]
        user_text = "\n".join(m.get("content") or "" for m in msgs if m["role"] == "user")
        reply = self.server.script(system, user_text, last, bool(body.get("tools")))
        if isinstance(reply, float):          # a delay, for cancellation tests
            time.sleep(reply)
            reply = "slow answer"
        message = {"role": "assistant", "content": None}
        if isinstance(reply, list):
            message["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                                      "function": {"name": n, "arguments": json.dumps(a)}}
                                     for i, (n, a) in enumerate(reply)]
        else:
            message["content"] = reply if isinstance(reply, str) else json.dumps(reply)
        data = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def script(system, user_text, last, has_tools):
    if "Jugnu's PLANNER" in system:
        # Only the current request line -- earlier turns are in the context too.
        import re
        m = re.search(r"User's request: (.*)", user_text)
        user_text = m.group(1) if m else user_text
        if "remember" in user_text.lower() and "favourite editor" in user_text.lower():
            return {"objective": "Remember the user's editor", "request_type": "quick_action",
                    "tasks": [{"id": 1, "agent": "memory", "description": "Save that the user's favourite editor is Helix",
                               "risk": "low"}]}
        if "email" in user_text.lower():
            return {"objective": "Email Bob", "request_type": "quick_action",
                    "tasks": [{"id": 1, "agent": "mail", "description": "Send Bob an email saying hi", "risk": "high"}]}
        if "slow" in user_text.lower():
            return {"objective": "Slow thing", "tasks": [{"id": 1, "agent": "research", "description": "look it up"}]}
        return {"objective": "chat", "tasks": [{"id": 1, "agent": "answer", "description": "reply"}]}
    if "VERIFIER" in system:
        return {"passed": True, "confidence": 0.9, "reason": "tool evidence confirms it"}
    if "REFLECTION" in system:
        return {"objective": "x", "succeeded": True, "outcome_summary": "fine", "memories": []}
    if has_tools:
        if last["role"] == "tool":
            return "Done."
        m = __import__("re").search(r"Your task: (.*)", user_text)
        user_text = m.group(1) if m else user_text
        if "favourite editor" in user_text:
            return [("remember", {"content": "The user's favourite editor is Helix", "kind": "user",
                                  "key": "pref.editor", "source": "user_stated", "confidence": 0.95})]
        if "Send Bob" in user_text:
            return [("mail_send", {"to": "bob@example.com", "subject": "Hi", "body": "Hi Bob"})]
        if "look it up" in user_text:
            return 3.0
    return "Hello there."


class BridgeEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
        cls.server.script = script
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.data = tempfile.mkdtemp(prefix="jugnu-e2e-")
        env = dict(os.environ, JUGNU_DATA_DIR=cls.data, JUGNU_PROVIDER="api", JUGNU_EMBED_PROVIDER="local",
                   JUGNU_API_BASE_URL=f"http://127.0.0.1:{cls.server.server_address[1]}",
                   JUGNU_API_CHAT_MODEL="fake", JUGNU_API_KEY="fake-key-1234567890",
                   JUGNU_ENABLE_AWARENESS="0", JUGNU_ENABLE_EMBEDDINGS="0",
                   MACBRIDGE_URL="http://127.0.0.1:9", MACBRIDGE_AGENT_URL="http://127.0.0.1:9",
                   PYTHONUNBUFFERED="1")
        cls.proc = subprocess.Popen([sys.executable, "bridge.py"], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=open(os.path.join(cls.data, "stderr.log"), "w"), text=True)
        cls.events = queue.Queue()
        threading.Thread(target=lambda: [cls.events.put(json.loads(l)) for l in cls.proc.stdout], daemon=True).start()
        cls.wait_for(lambda e: e["type"] == "ready", 30)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.send({"type": "quit"})
            cls.proc.wait(timeout=10)
        except Exception:
            cls.proc.kill()
        cls.server.shutdown()

    @classmethod
    def send(cls, obj):
        cls.proc.stdin.write(json.dumps(obj) + "\n")
        cls.proc.stdin.flush()

    @classmethod
    def wait_for(cls, predicate, timeout=60, seen=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                e = cls.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if seen is not None:
                seen.append(e)
            if predicate(e):
                return e
        raise AssertionError("timed out waiting for event")

    def test_1_plan_execute_verify_and_remember(self):
        seen = []
        self.send({"type": "send", "id": "r1", "text": "Remember that my favourite editor is Helix"})
        answer = self.wait_for(lambda e: e["type"] == "assistant" and e.get("id") == "r1", seen=seen)
        kinds = [e["type"] for e in seen]
        for expected in ("run_started", "plan", "task_update", "tool_start", "tool_end", "verification"):
            self.assertIn(expected, kinds)
        self.assertEqual(answer["content"], "Done.")
        tool_end = next(e for e in seen if e["type"] == "tool_end")
        self.assertTrue(tool_end["ok"])
        self.send({"type": "memory_list", "kind": "user"})
        memories = self.wait_for(lambda e: e["type"] == "memories")
        self.assertTrue(any("Helix" in m["content"] for m in memories["items"]))

    def test_2_high_risk_waits_for_approval_and_respects_denial(self):
        seen = []
        self.send({"type": "send", "id": "r2", "text": "email Bob to say hi"})
        request = self.wait_for(lambda e: e["type"] == "approval_request", seen=seen)
        self.assertEqual(request["tool"], "mail_send")
        self.assertEqual(request["risk"], "high")
        self.assertFalse(request["allow_task_scope"])
        self.send({"type": "approval_response", "approval_id": request["approval_id"], "decision": "deny",
                   "note": "not now"})
        self.wait_for(lambda e: e["type"] == "assistant" and e.get("id") == "r2", seen=seen)
        denied = next(e for e in seen if e["type"] == "tool_end" and e["tool"] == "mail_send")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error_type"], "user_denied")

    def test_3_cancel_mid_run(self):
        seen = []
        self.send({"type": "send", "id": "r3", "text": "do the slow thing"})
        started = self.wait_for(lambda e: e["type"] == "run_started" and e.get("id") == "r3", seen=seen)
        time.sleep(0.5)
        self.send({"type": "cancel", "run_id": started["run_id"]})
        answer = self.wait_for(lambda e: e["type"] == "assistant" and e.get("id") == "r3", timeout=30, seen=seen)
        self.assertTrue(answer.get("cancelled"))

    def test_4_audit_and_permissions_requests(self):
        self.send({"type": "get_audit", "limit": 50})
        audit = self.wait_for(lambda e: e["type"] == "audit")
        kinds = {e["kind"] for e in audit["events"]}
        self.assertTrue({"tool_call", "approval_request", "approval_decision"} <= kinds)
        self.assertFalse(any("fake-key-1234567890" in json.dumps(e) for e in audit["events"]),
                         "the API key must never reach the audit log")
        self.send({"type": "set_permissions", "patch": {"autonomy_mode": "manual"}})
        perms = self.wait_for(lambda e: e["type"] == "permissions" and e.get("saved"))
        self.assertEqual(perms["permissions"]["autonomy_mode"], "manual")


if __name__ == "__main__":
    unittest.main()
