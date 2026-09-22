"""
claude_code.py -- runs Jugnu's model calls on the user's Claude plan, through
the Claude Code CLI in headless mode (`claude -p`). Only llm_client uses it.

Each call is one short `claude -p` run with Claude Code's own tools, MCP
servers, settings and session history switched off: Claude answers as a
plain model, and Jugnu keeps doing the acting (policy, approvals, MacBridge).

Tool calling: `claude -p` can't be handed arbitrary function schemas, so the
tools go into the system prompt and Claude answers with one JSON object,
{"content": ..., "tool_calls": [...]}. That comes back to the caller in the
same shape the API and Ollama give, so agent_core never knows the difference.

Setup, once: install Claude Code and log in (run `claude` in Terminal, then
/login with your Claude account). The CLI is found on PATH or in the usual
install places.

  JUGNU_CLAUDE_PATH        optional: the `claude` binary to use
  JUGNU_CLAUDE_MODEL       default: sonnet  (planning and tool work)
  JUGNU_CLAUDE_EASY_MODEL  default: haiku   (EASY/FAST: chat, checks)
  JUGNU_CLAUDE_TIMEOUT     default: 180 seconds per call
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_TIMEOUT = float(os.environ.get("JUGNU_CLAUDE_TIMEOUT", "180"))

# Where `claude` usually lands: the native installer, the old local
# installer, Homebrew, npm/bun globals, and nvm.
_CANDIDATES = [
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.bun/bin/claude",
    "~/.nvm/versions/node/*/bin/claude",
]

# Flags Jugnu uses when this version of Claude Code has them (checked once
# against `claude --help`). Without them a call still works, just with more
# of Claude Code's own setup loaded.
_OPTIONAL_FLAGS = ("--tools", "--strict-mcp-config", "--setting-sources", "--no-session-persistence",
                   "--max-turns", "--system-prompt")

# Taken out of the child's environment: an API key would bill the API
# instead of the plan, and the CLAUDECODE markers make it think it's nested
# inside another Claude Code session.
_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

_EASY_ROLES = ("EASY", "FAST")


class ClaudeCodeError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[claude_code] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Finding the CLI
# ---------------------------------------------------------------------------

def find_cli() -> str | None:
    explicit = os.environ.get("JUGNU_CLAUDE_PATH")
    if explicit:
        path = os.path.expanduser(explicit)
        return path if os.access(path, os.X_OK) else None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    for pattern in _CANDIDATES:
        for path in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if os.access(path, os.X_OK):
                return path
    return None


def _env(cli: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    # The app launches the backend with a bare PATH; an npm-installed CLI
    # also needs `node`, which usually sits next to it or in Homebrew.
    extra = [os.path.dirname(cli), "/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "/usr/bin:/bin")])
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"  # no update checks or telemetry per call
    return env


def _workdir() -> str:
    """An empty folder to run in, so no project's CLAUDE.md or .mcp.json
    gets pulled into Jugnu's calls."""
    base = Path(os.environ.get("JUGNU_DATA_DIR") or Path.home() / "Library" / "Application Support" / "Jugnu")
    path = base / "claude-code"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


_features_lock = threading.Lock()
_features: dict[str, set] = {}
_versions: dict[str, str] = {}


def _supported_flags(cli: str) -> set:
    with _features_lock:
        if cli not in _features:
            try:
                out = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=20,
                                     env=_env(cli), cwd=_workdir())
                text = out.stdout + out.stderr
            except Exception as e:
                log(f"couldn't read `claude --help` ({e}); using only the basic flags")
                text = ""
            _features[cli] = {flag for flag in _OPTIONAL_FLAGS if flag in text}
        return _features[cli]


def status() -> dict:
    """Whether Claude Code is installed, and which version -- for the UI."""
    cli = find_cli()
    if not cli:
        return {"installed": False}
    if cli not in _versions:
        try:
            out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=20,
                                 env=_env(cli), cwd=_workdir())
            _versions[cli] = (out.stdout.strip().split() or [""])[0]
        except Exception:
            _versions[cli] = ""
    return {"installed": True, "path": cli, "version": _versions[cli]}


def check() -> dict:
    """One tiny real call, to confirm Claude Code is logged in."""
    result = status()
    if not result["installed"]:
        return {**result, "ok": False, "error": _NOT_INSTALLED}
    try:
        reply, _ = _run([{"role": "user", "content": "Reply with just: OK"}], None, "EASY")
        return {**result, "ok": True, "reply": reply[:40]}
    except ClaudeCodeError as e:
        return {**result, "ok": False, "error": str(e)}


def model_for(role: str) -> str:
    if role.upper() in _EASY_ROLES:
        return os.environ.get("JUGNU_CLAUDE_EASY_MODEL") or "haiku"
    return os.environ.get("JUGNU_CLAUDE_MODEL") or "sonnet"


# ---------------------------------------------------------------------------
# Prompt: Jugnu's message list -> system prompt + one transcript
# ---------------------------------------------------------------------------

_TOOL_PROTOCOL = """\
# Tools
You can call the tools below. They run on the user's Mac, and each result \
comes back to you in a <tool_result> block.

<tools>
{tools}
</tools>

# How to answer
Answer with exactly one JSON object and nothing else -- no prose, no code fence:
{{"content": "<your reply to the user, or empty>", "tool_calls": [{{"name": "<tool>", "arguments": {{...}}}}]}}
- To act, put the calls in tool_calls. Several calls are fine when they don't depend on each other.
- When you're done, or no tool is needed, leave tool_calls empty and put your reply in content.
- Never describe a tool call in content instead of making it, and never invent a tool result."""


def _tool_lines(tools: list) -> str:
    lines = []
    for tool in tools or []:
        fn = tool.get("function", tool)
        lines.append(json.dumps({"name": fn.get("name"), "description": fn.get("description", ""),
                                 "parameters": fn.get("parameters", {})}, separators=(",", ":")))
    return "\n".join(lines)


def _text(content) -> str:
    if content is None:
        return ""
    return content if isinstance(content, str) else json.dumps(content)


def build_prompt(messages: list, tools: list | None) -> tuple[str, str]:
    """(system prompt, prompt). A single user message goes as it is; a longer
    conversation becomes a tagged transcript ending on the assistant's turn."""
    system = "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system").strip()
    if tools:
        system = (system + "\n\n" if system else "") + _TOOL_PROTOCOL.format(tools=_tool_lines(tools))
    turns = [m for m in messages if m.get("role") != "system"]

    if len(turns) == 1 and turns[0].get("role") == "user" and not tools:
        return system, _text(turns[0].get("content"))

    parts = []
    for m in turns:
        role = m.get("role")
        if role == "user":
            parts.append(f"<user>\n{_text(m.get('content'))}\n</user>")
        elif role == "assistant":
            calls = [{"name": (tc.get("function") or {}).get("name"),
                      "arguments": (tc.get("function") or {}).get("arguments") or {}}
                     for tc in m.get("tool_calls") or []]
            body = json.dumps({"content": _text(m.get("content")), "tool_calls": calls}) if calls or tools \
                else _text(m.get("content"))
            parts.append(f"<assistant>\n{body}\n</assistant>")
        elif role == "tool":
            parts.append(f"<tool_result name=\"{m.get('name') or ''}\">\n{_text(m.get('content'))}\n</tool_result>")
    ending = ("Continue as the assistant: answer with the JSON object for your next turn." if tools
              else "Continue as the assistant: write only your next reply.")
    return system, "\n\n".join(parts) + "\n\n" + ending


# ---------------------------------------------------------------------------
# Reply: Claude's JSON -> content + tool calls
# ---------------------------------------------------------------------------

def parse_reply(text: str, tools: list | None) -> tuple[str, list]:
    """(content, [(name, arguments)]). Without tools the text is the reply.
    With tools, the first JSON object that looks like an answer wins; if
    there's none, the whole text is taken as a plain reply."""
    text = (text or "").strip()
    if not tools:
        return text, []
    answer = _find_answer(text)
    if answer is None:
        return text, []
    calls = []
    for call in answer.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = call.get("name") or (call.get("function") or {}).get("name")
        args = call.get("arguments", (call.get("function") or {}).get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name:
            calls.append((str(name), args if isinstance(args, dict) else {}))
    return _text(answer.get("content")).strip(), calls


def _find_answer(text: str) -> dict | None:
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text, start)
            if isinstance(value, dict) and ("tool_calls" in value or "content" in value):
                return value
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

_NOT_INSTALLED = ("Claude Code isn't installed. Install it (curl -fsSL https://claude.ai/install.sh | bash), "
                  "run `claude` in Terminal once and log in, or switch Jugnu to another mode.")


def _friendly(message: str) -> str:
    low = message.lower()
    if any(s in low for s in ("/login", "log in", "logged in", "login", "authenticat", "invalid api key",
                              "oauth", "credential")):
        return ("Claude Code isn't logged in. Open Terminal, run `claude`, and log in with your Claude "
                "account (/login). Then try again.")
    if any(s in low for s in ("usage limit", "limit reached", "rate limit", "limit will reset", "out of extra usage")):
        return ("Your Claude plan's usage limit is used up for now. It resets on its own; until then, switch "
                "Jugnu to API or Local in the ··· menu.")
    return f"Claude Code failed: {message.strip()[:400]}"


def _command(cli: str, system: str, role: str) -> list:
    flags = _supported_flags(cli)
    cmd = [cli, "-p", "--output-format", "json", "--model", model_for(role)]
    if system and "--system-prompt" in flags:
        cmd += ["--system-prompt", system]
    if "--tools" in flags:
        cmd += ["--tools", ""]                    # Claude Code's own tools off: Jugnu does the acting
    if "--strict-mcp-config" in flags:
        cmd += ["--strict-mcp-config"]            # ...and none of the user's MCP servers
    if "--setting-sources" in flags:
        cmd += ["--setting-sources", ""]          # no hooks or settings from ~/.claude
    if "--no-session-persistence" in flags:
        cmd += ["--no-session-persistence"]       # don't save every call as a session
    if "--max-turns" in flags:
        cmd += ["--max-turns", "1"]
    return cmd


def _run(messages: list, tools: list | None, role: str) -> tuple[str, dict]:
    """Runs one call; returns (Claude's text, usage)."""
    cli = find_cli()
    if not cli:
        raise ClaudeCodeError(_NOT_INSTALLED)
    system, prompt = build_prompt(messages, tools)
    cmd = _command(cli, system, role)
    if system and "--system-prompt" not in cmd:
        prompt = f"{system}\n\n---\n\n{prompt}"
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=_TIMEOUT,
                              env=_env(cli), cwd=_workdir())
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError(f"Claude Code didn't answer within {int(_TIMEOUT)} seconds.")
    except OSError as e:
        raise ClaudeCodeError(f"Couldn't start Claude Code ({cli}): {e}")

    data = None
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    except (json.JSONDecodeError, IndexError):
        data = None
    if not isinstance(data, dict):
        raise ClaudeCodeError(_friendly(proc.stderr or proc.stdout or f"exit code {proc.returncode}"))
    if data.get("is_error") or proc.returncode != 0 or data.get("subtype", "success") != "success":
        raise ClaudeCodeError(_friendly(str(data.get("result") or data.get("error") or proc.stderr
                                            or data.get("subtype") or "unknown error")))
    return str(data.get("result") or ""), data.get("usage") or {}


def chat(messages: list, tools: list | None, role: str) -> tuple[str, list, dict]:
    """(content, [(tool name, arguments)], usage) for one model call."""
    text, usage = _run(messages, tools, role)
    content, calls = parse_reply(text, tools)
    return content, calls, usage
