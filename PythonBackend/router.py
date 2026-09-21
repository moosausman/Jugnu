"""
router.py -- deciding, cheaply, what kind of thing a message is before the
full planner runs.

    conversation   "thanks", "hi", "lol ok"      -> direct reply, no planning
    answer         a reply to a question a paused run asked -> resume that run
    control        "stop", "cancel that"          -> handled by the caller
    (everything else goes to the planner, which classifies further into
     quick_action / agentic / scheduled / background)

The fast paths are regex-only (no model call). The only model call here is
is_answer_to(), and only when a run is actually waiting on the user.
"""

from __future__ import annotations

import re

_SMALL_TALK = re.compile(
    r"^\s*(?:hi|hey|hello|yo|sup|hiya|good (?:morning|afternoon|evening|night)|thanks?(?: you)?(?: so much)?|"
    r"thx|ty|cheers|cool|nice|great|awesome|ok(?:ay)?|k|kk|lol|lmao|haha+|got it|sounds good|perfect|"
    r"bye|see ya|good ?bye|night|gn|how are you\??|what'?s up\??|wassup)"
    r"[\s!.?,:)]*(?:jugnu)?[\s!.?,:)]*$", re.I)

_CONTROL = re.compile(r"^\s*(?:stop|cancel|abort|nevermind|never mind|forget it)(?: (?:it|that|this))?[\s!.]*$", re.I)


def quick_route(text: str) -> str | None:
    """'conversation' or 'control' for messages that need no planning, else None."""
    if _CONTROL.match(text or ""):
        return "control"
    if _SMALL_TALK.match(text or ""):
        return "conversation"
    return None


def is_answer_to(question: str, message: str) -> bool:
    """Whether `message` answers a pending question (vs. being a new,
    unrelated request). Cheap heuristics first, then one FAST-model call."""
    msg = (message or "").strip()
    if not msg or not question:
        return False
    if quick_route(msg) == "conversation" and not re.match(r"^\s*(?:yes|yeah|yep|no|nope|ok)", msg, re.I):
        return False
    if len(msg.split()) <= 4 and not re.search(r"\b(?:open|play|search|find|create|send|delete|what|how|why)\b", msg, re.I):
        return True  # a short reply right after a question is almost always the answer
    from planner import request_json
    try:
        data = request_json([
            {"role": "system", "content": (
                "Jugnu asked the user a question while doing a task. Decide if the user's new message "
                "answers that question (even partially) or is a different, new request. "
                'Respond with exactly one JSON object: {"answers": true|false}')},
            {"role": "user", "content": f"Jugnu's question: {question}\nUser's message: {msg}"},
        ], role="FAST", retries=1)
        return bool(data.get("answers"))
    except Exception:
        return False


_AGENT_SESSION = re.compile(
    r"\b(?:on|in|using|from|with)\s+(?:the\s+)?(?:jugnu(?:'s)?\s+(?:account|user|desktop|mac|session|login)|"
    r"your\s+(?:own\s+)?(?:account|desktop|user|mac|login|session)|agent\s+(?:account|session|desktop))\b", re.I)
_PRIMARY_SESSION = re.compile(r"\b(?:on|in)\s+my\s+(?:own\s+)?(?:account|desktop|mac|login|user)\b", re.I)
_BACKGROUND = re.compile(
    r"\b(?:in\s+the\s+background(?!\s+(?:of|image|color|colour|picture|photo))|background\s+(?:task|job)|while\s+i(?:'m| am)\s+(?:away|busy|working)|"
    r"let\s+me\s+know\s+when\s+(?:it'?s\s+)?done|no\s+(?:need|rush)\s+to\s+wait)\b", re.I)


def session_hint(text: str) -> str | None:
    """'agent' / 'primary' when the request names a desktop explicitly --
    applied even if the planner model forgets to set it."""
    if _AGENT_SESSION.search(text or ""):
        return "agent"
    if _PRIMARY_SESSION.search(text or ""):
        return "primary"
    return None


def wants_background(text: str) -> bool:
    return bool(_BACKGROUND.search(text or ""))
