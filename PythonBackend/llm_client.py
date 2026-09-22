"""
llm_client.py -- the only module in Jugnu that is allowed to import `ollama`
or talk to a hosted model API (claude_code.py is its helper for running on
the user's Claude plan). Every other file (agent_core, executor,
planner, search, semantic_memory) calls llm_client.chat() / llm_client.embed()
and never knows or cares whether that request just ran on this machine or
went out over the network.
Why centralize this:
  1. Provider switch: one env var flips the entire app between a local
     Ollama model and a hosted API -- no per-file changes.
  2. RAM/latency: keep_alive, timeouts, and retries are enforced in one
     place instead of copy-pasted (or forgotten) at every call site.
  3. Tool-calling translation only has to be written once.
------------------------------------------------------------------------
SWITCHING PROVIDERS
------------------------------------------------------------------------
Nothing here needs a code change -- it's all environment variables.
  JUGNU_PROVIDER=local|api          global default. "local" if unset.
  JUGNU_<ROLE>_PROVIDER=local|api   optional per-role override.
                                     ROLE is one of: PLANNER, AGENT, SEARCH, EMBED
                                     e.g. JUGNU_SEARCH_PROVIDER=api lets you run
                                     search/rerank on a hosted model while the
                                     planner and task agents stay local.
  ROLE MEANS "WHO DECIDES", NOT "WHICH FILE": PLANNER is the one role that
  ever decides anything -- which tasks to break a goal into (planner.py)
  AND, for every task that needs tools, which tool to call and with what
  arguments (agent_core.py's run_tool_loop, driven from executor.py). AGENT
  is deliberately the *smaller* model: it only ever renders already-decided
  results into words (the no-tool "answer" task, and the final multi-task
  synthesis) -- it never picks a tool. This is what lets you point
  JUGNU_PLANNER_MODEL at something big/smart and JUGNU_MODEL at something
  small/cheap and have that split actually matter.
  Local-mode model choice, per role (no code changes needed, just env vars):
    JUGNU_PLANNER_MODEL   default: qwen3.6:27b
    JUGNU_MODEL           default: qwen3.5:9b   (AGENT, and SEARCH's fallback)
    JUGNU_SEARCH_MODEL    optional override for SEARCH specifically
    JUGNU_EMBED_MODEL     default: nomic-embed-text
  See _local_model_for() below -- this is the one place these are read.
  JUGNU_OLLAMA_RETRIES  default: 1 -- how many times to retry a local
                         ollama.chat()/ollama.embed() call that raised,
                         before giving up. Matches JUGNU_API_RETRIES'
                         default so both providers fail the same way.
API mode ("api") talks to any OpenAI-compatible /chat/completions and
/embeddings endpoint. Point JUGNU_API_BASE_URL at whichever provider you
want -- this is genuinely provider-agnostic, e.g.:
  OpenAI:      https://api.openai.com/v1
  Gemini:      https://generativelanguage.googleapis.com/v1beta/openai
  OpenRouter:  https://openrouter.ai/api/v1
  Groq:        https://api.groq.com/openai/v1
  Together:    https://api.together.xyz/v1
Also set:
  JUGNU_API_KEY=...                 (or a provider-native var -- see
                                      _KEY_ENV_CANDIDATES below; whichever
                                      is set first wins)
  JUGNU_API_CHAT_MODEL=gemini-2.5-flash        (or gpt-4.1-mini, etc.)
  JUGNU_API_EMBED_MODEL=text-embedding-004     (only needed if you also
                                                 flip EMBED to "api" --
                                                 recommended to leave
                                                 embeddings on "local",
                                                 see note below)
  JUGNU_<ROLE>_API_MODEL=...         per-role model override, same ROLE
                                      names as above.
NOTE ON EMBEDDINGS: search.py and semantic_memory.py embed constantly
(every search, every logged message/tool-call) and are latency-sensitive.
Unless you have a specific reason to use a hosted embedding model, leave
JUGNU_EMBED_PROVIDER unset (or "local") and only switch chat roles to
"api" -- nomic-embed-text locally is fast, free, and this whole app was
tuned around that assumption.
Tool schemas (tools.py's make_schema()) are already OpenAI function-
calling shape, and Ollama's tool format matches it, so tool definitions
need zero changes to work with either provider.
------------------------------------------------------------------------
"""
from __future__ import annotations
import json
import os
import sys
import time
import ollama
import requests

import claude_code
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


_API_TIMEOUT = float(os.environ.get("JUGNU_API_TIMEOUT", "120"))
_API_RETRIES = int(os.environ.get("JUGNU_API_RETRIES", "2"))
# Local Ollama calls had zero retry logic -- any transient hiccup (daemon
# still starting, a model swap mid-request, a brief connection reset) took
# down the whole turn immediately, while API mode already retried the same
# class of failure via _post_with_retries below. This closes that gap: one
# blind retry by default, same linear backoff shape as the API path, so
# both providers degrade the same way instead of local mode being the
# fragile one.
_OLLAMA_RETRIES = int(os.environ.get("JUGNU_OLLAMA_RETRIES", "1"))

def log(message: str) -> None:
    print(f"[llm_client] {message}", file=sys.stderr, flush=True)

def _with_retries(fn, what: str):
    last_exc = None
    for attempt in range(_OLLAMA_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < _OLLAMA_RETRIES:
                log(f"{what} failed ({exc}) -- retrying ({attempt + 1}/{_OLLAMA_RETRIES})")
                time.sleep(0.5 * (attempt + 1))
    raise last_exc
# RAM efficiency: keep the local model resident between calls instead of
# Ollama unloading and reloading multi-GB weights every time the planner,
# an agent, and then search all call it seconds apart. Set to "-1" to keep
# loaded indefinitely, or e.g. "10m" to bound idle RAM use.
_OLLAMA_KEEP_ALIVE = os.environ.get("JUGNU_OLLAMA_KEEP_ALIVE", "30m")
# First of these that's set wins -- lets you use a provider-native var
# (OPENAI_API_KEY etc.) without an extra JUGNU_API_KEY if you'd rather not
# duplicate it.
_KEY_ENV_CANDIDATES = [
    "JUGNU_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
]
_ROLES = ("PLANNER", "AGENT", "SEARCH", "EMBED", "FAST", "DEEP", "EASY")

# Model tiers. FAST is for cheap judgement calls (routing, verification,
# memory extraction); DEEP for hard planning, coding and analysis. Each
# falls back to an existing role, so nothing changes until a tier model is
# configured:  FAST -> AGENT,  DEEP -> PLANNER.
#
# EASY is text-only work that never picks a tool: chit-chat, explanations
# and summaries, and background judgement calls (verifying, routing,
# reflecting, extracting). It's what Hybrid mode runs locally for free.
_ROLE_FALLBACK = {"FAST": "AGENT", "DEEP": "PLANNER", "EASY": "FAST"}

# ---------------------------------------------------------------------------
# Model mode -- the user's switch, changeable at runtime (no restart):
#   api     every chat role on the hosted API
#   hybrid  EASY work on the local Ollama model, everything else on the API
#   local   every chat role on Ollama
#   claude  every chat role on the user's Claude plan, through the Claude
#           Code CLI (claude_code.py) -- no API credits
# Unset: the older JUGNU_PROVIDER / JUGNU_<ROLE>_PROVIDER variables decide.
# Embeddings never follow the mode: they stay wherever JUGNU_EMBED_PROVIDER says.
# ---------------------------------------------------------------------------
MODES = ("api", "hybrid", "local", "claude")
_mode = os.environ.get("JUGNU_MODEL_MODE", "").lower()


def model_mode() -> str:
    if _mode in MODES:
        return _mode
    return "local" if os.environ.get("JUGNU_PROVIDER", "local").lower() == "local" else "api"


def set_mode(mode: str) -> str:
    """Switches where chat roles run, effective from the next call."""
    global _mode
    mode = (mode or "").lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    _mode = mode
    log(f"model mode -> {mode}")
    return mode


def local_status() -> dict:
    """Whether Ollama is answering, and which models it has -- for the UI."""
    try:
        r = requests.get(os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/tags", timeout=2)
        r.raise_for_status()
        return {"running": True, "models": sorted(m["name"] for m in r.json().get("models", []))}
    except Exception:
        return {"running": False, "models": []}


def _provider_for(role: str) -> str:
    role = role.upper()
    assert role in _ROLES, f"unknown role {role!r}, expected one of {_ROLES}"
    if role != "EMBED" and _mode in MODES:
        if _mode == "hybrid":
            return "local" if role == "EASY" else "api"
        return _mode
    explicit = os.environ.get(f"JUGNU_{role}_PROVIDER")
    if not explicit and role in _ROLE_FALLBACK:
        explicit = os.environ.get(f"JUGNU_{_ROLE_FALLBACK[role]}_PROVIDER")
    return (explicit or os.environ.get("JUGNU_PROVIDER", "local")).lower()

# ---------------------------------------------------------------------------
# Local-mode model resolution
# ---------------------------------------------------------------------------
# API mode already resolves "which model does this role use" in one place
# (_api_model_for below). Local mode used to be resolved separately -- and
# inconsistently -- in every caller: executor.py, planner.py, and bridge.py
# each kept their own `os.environ.get("JUGNU_MODEL", ...)` constant, and
# search.py kept a *third*, hardcoded copy ("qwen3.5:9b" with no env
# override at all) in its own Config dataclass. A global model change
# ("I want to try a different model") meant editing multiple files and
# still missing search.py.
#
# This is the single source of truth now: every caller passes a role and,
# optionally, an explicit model to override it -- nothing else needs to
# know an env var name.
#
#   JUGNU_PLANNER_MODEL   default: qwen3.6:27b   (PLANNER role)
#   JUGNU_MODEL           default: qwen3.5:9b    (AGENT role, and SEARCH's
#                                                  fallback if JUGNU_SEARCH_MODEL
#                                                  isn't set)
#   JUGNU_SEARCH_MODEL    optional override for SEARCH specifically
#   JUGNU_EMBED_MODEL     default: nomic-embed-text (EMBED role)
def _local_model_for(role: str) -> str:
    role = role.upper()
    if role == "EASY":
        return os.environ.get("JUGNU_EASY_MODEL") or _local_model_for("AGENT")
    if role == "FAST":
        return os.environ.get("JUGNU_FAST_MODEL") or _local_model_for("AGENT")
    if role == "DEEP":
        return os.environ.get("JUGNU_DEEP_MODEL") or _local_model_for("PLANNER")
    if role == "PLANNER":
        return os.environ.get("JUGNU_PLANNER_MODEL", "qwen3.6:27b")
    if role == "SEARCH":
        return os.environ.get("JUGNU_SEARCH_MODEL") or os.environ.get("JUGNU_MODEL", "qwen3.5:9b")
    if role == "EMBED":
        return default_embed_model()
    # AGENT, and anything else -- the default chat model.
    return os.environ.get("JUGNU_MODEL", "qwen3.5:9b")

def default_embed_model() -> str:
    """The embedding model local mode uses. Exposed so any caller that wants
    to *display* the model name (e.g. a log hint to `ollama pull ...`)
    reads the same value embed() actually uses, instead of keeping its own
    copy that can drift out of sync."""
    return os.environ.get("JUGNU_EMBED_MODEL", "nomic-embed-text")

def resolved_model_name(role: str) -> str:
    """What model is actually in play for `role` right now, in whichever
    provider is active -- for display/logging only. chat()/embed() resolve
    this internally and never need it passed back in."""
    role = role.upper()
    provider = _provider_for(role)
    if provider == "api":
        return _api_embed_model_for(role) if role == "EMBED" else _api_model_for(role)
    if provider == "claude":
        return f"claude {claude_code.model_for(role)}"
    return _local_model_for(role)

def _api_key() -> str:
    for name in _KEY_ENV_CANDIDATES:
        val = os.environ.get(name)
        if val:
            return val
    raise RuntimeError(
        "API mode is on but no API key is set. Set JUGNU_API_KEY, or a "
        "provider-native var (OPENAI_API_KEY, GEMINI_API_KEY, ...)."
    )

def _api_base_url() -> str:
    url = os.environ.get("JUGNU_API_BASE_URL")
    if not url:
        raise RuntimeError(
            "API mode is on but JUGNU_API_BASE_URL isn't set, e.g. "
            "https://api.openai.com/v1 or "
            "https://generativelanguage.googleapis.com/v1beta/openai"
        )
    return url.rstrip("/")

def _api_model_for(role: str) -> str:
    role = role.upper()
    model = os.environ.get(f"JUGNU_{role}_API_MODEL")
    if not model and role in _ROLE_FALLBACK:
        model = os.environ.get(f"JUGNU_{_ROLE_FALLBACK[role]}_API_MODEL")
    model = model or os.environ.get("JUGNU_API_CHAT_MODEL")
    if not model:
        raise RuntimeError(
            f"No API chat model configured for role {role}. Set "
            f"JUGNU_API_CHAT_MODEL (or JUGNU_{role.upper()}_API_MODEL)."
        )
    return model

def _api_embed_model_for(role: str) -> str:
    model = os.environ.get(f"JUGNU_{role.upper()}_API_MODEL") or os.environ.get("JUGNU_API_EMBED_MODEL")
    if not model:
        raise RuntimeError(
            f"No API embedding model configured. Set JUGNU_API_EMBED_MODEL "
            f"(or JUGNU_{role.upper()}_API_MODEL)."
        )
    return model

# ---------------------------------------------------------------------------
# Response wrapper -- mimics ollama's ChatResponse/Message/ToolCall shape so
# every existing call site (agent_core.py's message.model_dump(),
# tool_call.function.name, response["message"].content, etc.) keeps working
# completely unchanged regardless of which provider actually answered.
# ---------------------------------------------------------------------------
class _ToolCallFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments or {}

class ToolCall:
    def __init__(self, name, arguments, id=None, extra_content=None):
        self.id = id
        self.function = _ToolCallFunction(name, arguments)
        self.extra_content = extra_content  # Gemini's thought_signature, if present
    def model_dump(self) -> dict:
        d = {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }
        if self.extra_content:
            d["extra_content"] = self.extra_content
        return d

class Message:
    def __init__(self, role, content=None, tool_calls=None):
        self.role = role
        self.content = content or ""
        self.tool_calls = tool_calls or []
    def __getitem__(self, key):
        return getattr(self, key)
    def model_dump(self) -> dict:
        d = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.model_dump() for tc in self.tool_calls]
        return d

class ChatResponse:
    def __init__(self, message: Message):
        self.message = message
    def __getitem__(self, key):
        return getattr(self, key)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def chat(messages, tools=None, think=False, role="AGENT", keep_alive=None, model=None):
    """Drop-in replacement for ollama.chat(). Callers pick a `role`, not a
    model -- the model is resolved from the role (see _local_model_for /
    _api_model_for above) unless `model` is given explicitly to override it."""
    provider = _provider_for(role)
    if provider == "local":
        resolved_model = model or _local_model_for(role)
        kwargs = {"model": resolved_model, "messages": messages, "think": think}
        if tools:
            kwargs["tools"] = tools
        kwargs["keep_alive"] = keep_alive if keep_alive is not None else _OLLAMA_KEEP_ALIVE
        try:
            response = _with_retries(lambda: ollama.chat(**kwargs), f"ollama.chat ({resolved_model})")
        except Exception as e:
            # Hybrid promises easy work never fails because of the local
            # side: Ollama not running, or the model not pulled.
            if model_mode() == "hybrid" and _api_configured():
                log(f"local model unavailable ({e}); using the API for this call")
                return _api_chat(messages, tools=tools, role=role)
            raise RuntimeError(
                f"The local model ({resolved_model}) isn't available: {e}. Start Ollama and run "
                f"`ollama pull {resolved_model}`, or switch Jugnu to API mode.") from e
        _record_usage(getattr(response, "prompt_eval_count", 0), getattr(response, "eval_count", 0), local=True)
        return response
    if provider == "api":
        return _api_chat(messages, tools=tools, role=role)
    if provider == "claude":
        return _claude_chat(messages, tools=tools, role=role)
    raise ValueError(f"unknown provider {provider!r} (expected 'local', 'api' or 'claude')")

def embed(input, role="EMBED", model=None):  # noqa: A002 (matches ollama.embed's param name)
    """Drop-in replacement for ollama.embed(). Returns {"embeddings": [...]}
    either way. `model` is an optional override -- normally the role's
    default embedding model (see default_embed_model()) is used."""
    provider = _provider_for(role)
    if provider == "local":
        resolved_model = model or default_embed_model()
        return _with_retries(
            lambda: ollama.embed(model=resolved_model, input=input, keep_alive=_OLLAMA_KEEP_ALIVE),
            f"ollama.embed ({resolved_model})",
        )
    if provider == "api":
        return _api_embed(input, role=role)
    raise ValueError(f"unknown provider {provider!r} (expected 'local' or 'api')")

# ---------------------------------------------------------------------------
# API mode internals
# ---------------------------------------------------------------------------
def _post_with_retries(url, headers, payload):
    last_exc = None
    for attempt in range(_API_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=_API_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            # Surface Google's actual error message instead of the generic
            # "400 Client Error: Bad Request" that hides it. The URL is not
            # included -- some providers put the key in it.
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            status = resp.status_code
            last_exc = RuntimeError(f"model API error {status}: {str(body)[:800]}")
            # 4xx other than rate limiting won't get better by retrying.
            if status not in (408, 429) and status < 500:
                break
            if attempt < _API_RETRIES:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * (attempt + 1)
                time.sleep(min(delay, 20.0))
        except Exception as exc:
            last_exc = exc
            if attempt < _API_RETRIES:
                time.sleep(0.5 * (attempt + 1))
    raise last_exc

def _as_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content)

def _to_wire_messages(messages: list) -> list:
    """Translate our internal message list (Ollama-shaped, dict tool-call
    arguments, no tool_call_id) into strict OpenAI wire format (string
    tool-call arguments, tool_call_id required on every tool-result
    message). tool_call_ids are regenerated deterministically from message
    position every call -- cheap, and stays consistent across turns since
    the message list is append-only."""
    wire = []
    pending_ids: list[str] = []
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            tcs, ids = [], []
            for i, tc in enumerate(msg["tool_calls"]):
                fn = tc.get("function", {}) or {}
                args = fn.get("arguments") or {}
                if not isinstance(args, str):
                    args = json.dumps(args)
                tc_id = tc.get("id") or f"call_{idx}_{i}"
                ids.append(tc_id)
                wire_tc = {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": fn.get("name"), "arguments": args},
                }
                if tc.get("extra_content"):
                    wire_tc["extra_content"] = tc["extra_content"]
                tcs.append(wire_tc)
            wire.append({"role": "assistant", "content": msg.get("content") or None, "tool_calls": tcs})
            pending_ids = ids
        elif role == "tool":
            tc_id = pending_ids.pop(0) if pending_ids else f"call_unmatched_{idx}"
            wire.append({"role": "tool", "tool_call_id": tc_id, "content": _as_text(msg.get("content"))})
        else:
            wire.append({"role": role, "content": _as_text(msg.get("content"))})
    return wire

def _message_from_wire(choice: dict) -> Message:
    tool_calls = []
    for tc in choice.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(ToolCall(
            name=fn.get("name"),
            arguments=args,
            id=tc.get("id"),
            extra_content=tc.get("extra_content"),
        ))
    return Message(role=choice.get("role", "assistant"), content=choice.get("content"), tool_calls=tool_calls)
def _api_chat(messages, tools, role) -> ChatResponse:
    payload = {
        "model": _api_model_for(role),
        "messages": _to_wire_messages(messages),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    data = _post_with_retries(
        f"{_api_base_url()}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        payload=payload,
    )
    usage = data.get("usage") or {}
    _record_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    choice = data["choices"][0]["message"]
    return ChatResponse(_message_from_wire(choice))


def _claude_chat(messages, tools, role) -> ChatResponse:
    """One call on the user's Claude plan. No fallback to the API on
    failure: quietly spending API credits is what this mode exists to avoid."""
    try:
        content, calls, usage = claude_code.chat(messages, tools, role)
    except claude_code.ClaudeCodeError as e:
        raise RuntimeError(str(e)) from e
    prompt = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    _record_usage(prompt, usage.get("output_tokens", 0))
    tool_calls = [ToolCall(name=name, arguments=args, id=f"call_cc_{i}") for i, (name, args) in enumerate(calls)]
    return ChatResponse(Message(role="assistant", content=content, tool_calls=tool_calls))


def _record_usage(prompt_tokens, completion_tokens, local: bool = False) -> None:
    """Credits one model call (and its tokens) to the run that made it.
    Local calls are counted separately: they cost nothing."""
    try:
        import run_context
        ctx = run_context.get()
        if ctx is not None:
            ctx.add_usage(prompt_tokens or 0, completion_tokens or 0, local=local)
    except Exception:
        pass


def _api_configured() -> bool:
    return bool(os.environ.get("JUGNU_API_BASE_URL")) and any(os.environ.get(n) for n in _KEY_ENV_CANDIDATES)

def _api_embed(input, role) -> dict:  # noqa: A002
    if isinstance(input, str):
        input = [input]
    data = _post_with_retries(
        f"{_api_base_url()}/embeddings",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        payload={"model": _api_embed_model_for(role), "input": input},
    )
    ordered = sorted(data["data"], key=lambda d: d["index"])
    return {"embeddings": [d["embedding"] for d in ordered]}
