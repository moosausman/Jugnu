"""A scripted stand-in for llm_client.chat, so planner/executor/session code
runs for real in tests without a model or network.

Rules are (predicate, responder) pairs checked in order; the first whose
predicate matches the request answers it. Responders return either a
string (assistant text) or a list of (tool_name, args) tool calls.
"""

import json
import threading

import llm_client


class FakeLLM:
    def __init__(self):
        self.rules = []
        self.calls = []
        self._lock = threading.Lock()
        self._orig = None

    def when(self, predicate, responder):
        self.rules.append((predicate, responder))
        return self

    def install(self):
        self._orig = llm_client.chat
        llm_client.chat = self.chat
        return self

    def uninstall(self):
        if self._orig is not None:
            llm_client.chat = self._orig

    def chat(self, messages, tools=None, think=False, role="AGENT", keep_alive=None, model=None):
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        last = messages[-1]
        with self._lock:
            self.calls.append({"role": role, "system": system, "last": last, "tools": tools, "messages": messages})
        req = {"role": role, "system": system, "messages": messages, "tools": tools, "last": last}
        for predicate, responder in self.rules:
            if predicate(req):
                out = responder(req)
                break
        else:
            out = "ok"
        if isinstance(out, list):
            calls = [llm_client.ToolCall(name=n, arguments=a, id=f"c{i}") for i, (n, a) in enumerate(out)]
            return llm_client.ChatResponse(llm_client.Message("assistant", "", calls))
        if isinstance(out, dict):
            out = json.dumps(out)
        return llm_client.ChatResponse(llm_client.Message("assistant", out))


def is_planner(req):
    return "Jugnu's PLANNER" in req["system"] and "revising a plan" not in req["system"]


def is_replanner(req):
    return "revising a plan" in req["system"]


def is_verifier(req):
    return "Jugnu's VERIFIER" in req["system"]


def after_tool(req):
    return req["last"].get("role") == "tool"
