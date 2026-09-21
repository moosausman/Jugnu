"""
redact.py -- keeps secrets out of prompts, logs, memories and the UI.

Two layers:
  1. Exact values: every secret Jugnu itself was configured with (the API
     key, Spotify/Notion secrets, the MacBridge token) is known at startup
     from the environment, so any occurrence of those exact strings is
     replaced wherever it shows up -- a tool that cats a .env file, an
     error message echoing a request header, a memory the model tried to
     save.
  2. Shapes: well-known credential formats (Google/OpenAI/GitHub/Slack/AWS
     keys, bearer tokens, JWTs, PEM private keys, password=... pairs) that
     Jugnu was never told about but may still encounter in files or pages.

redact() is cheap enough to run on every tool result: a handful of
precompiled regexes over at most a few KB (agent_core truncates results
before this matters).
"""

from __future__ import annotations

import os
import re

_SECRET_ENV_VARS = (
    "JUGNU_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "ANTHROPIC_API_KEY",
    "SPOTIFY_CLIENT_SECRET", "NOTION_API_KEY", "NOTION_TOKEN", "MACBRIDGE_TOKEN",
    "GITHUB_TOKEN", "GH_TOKEN",
)

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[REDACTED GOOGLE KEY]"),
    (re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}"), "[REDACTED API KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED SLACK TOKEN]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"\bsecret_[A-Za-z0-9]{30,}"), "[REDACTED TOKEN]"),
    (re.compile(r"\bntn_[A-Za-z0-9]{30,}"), "[REDACTED TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[REDACTED JWT]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:basic|bearer)?\s*[A-Za-z0-9._\-+/=]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|client[_-]?secret|password|passwd|pwd)"
                r"[\"']?\s*[:=]\s*[\"']?)([^\s\"'&,;]{6,})"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(https?://[^\s:/@]+:)([^\s@/]{3,})(@)"), r"\1[REDACTED]\3"),
]

_known_values: list[str] = []


def refresh_known_secrets() -> None:
    """Re-read secret values from the environment. Called at import; call
    again if the environment changes in-process (tests do)."""
    global _known_values
    values = []
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if len(value) >= 8:
            values.append(value)
    # Longest first so a secret that contains another is fully replaced.
    _known_values = sorted(set(values), key=len, reverse=True)


refresh_known_secrets()


def redact(text):
    """Returns `text` with secrets replaced. Non-strings pass through."""
    if not isinstance(text, str) or not text:
        return text
    for value in _known_values:
        if value in text:
            text = text.replace(value, "[REDACTED SECRET]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(obj):
    """Recursively redacts strings inside dicts/lists (for structured logs)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def contains_secret(text: str) -> bool:
    """Whether `text` holds anything redact() would remove -- used to refuse
    storing it in memory at all rather than storing a mangled version."""
    return isinstance(text, str) and redact(text) != text
