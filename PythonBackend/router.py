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
        ], role="EASY", retries=1)
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


# ---------------------------------------------------------------------------
# Direct routing: single-intent requests skip the planner
# ---------------------------------------------------------------------------
# The planner is one full model call (a 2.6k-token prompt plus context) that
# for most requests just answers "one task, agent X". When a request is
# plainly about one thing, pick the agent here and run it directly.
# Anything that looks multi-step, scheduled, long-form or ambiguous returns
# None and still gets a proper plan.

_MULTI_STEP = re.compile(
    r"\b(?:and then|then|after that|afterwards|followed by|as well as|and also|once (?:that'?s|it'?s) done|first\b.*\bthen)\b"
    r"|;|\band (?:open|send|create|play|email|message|text|search|find|make|write|schedule|remind|add|delete|move|"
    r"save|book|download|install|reply|call|check|tell)\b", re.I)
_NEEDS_PLANNER = re.compile(
    r"\b(?:every|each (?:day|week|morning|night)|daily|weekly|remind me|in \d+ (?:min|minute|hour|second)s?|at \d|"
    r"tomorrow at|later|schedule (?:a|it|this)|research|report|compare|study guide|essay|plan (?:for|my|a)|"
    r"build|code|debug|fix|refactor|website|app for|project|analy[sz]e|summari[sz]e (?:my|the) (?:day|week)|"
    r"recap|daily review|in the background|jugnu account|my account)\b", re.I)

_DOMAINS = [
    ("whatsapp", re.compile(r"\bwhats ?app\b", re.I)),
    ("mail", re.compile(r"\b(?:e-?mails?|inbox|gmail|mailbox)\b", re.I)),
    ("calendar", re.compile(r"\b(?:calendar|meetings?|events?|appointments?|reminders?|to-?do|"
                            r"what'?s on my (?:day|schedule)|am i free|my schedule)\b", re.I)),
    ("notion", re.compile(r"\bnotion\b", re.I)),
    ("notes", re.compile(r"\b(?:apple notes|notes app|my notes|a note)\b", re.I)),
    ("contacts", re.compile(r"\b(?:phone number|contact info|contacts?|number for)\b", re.I)),
    ("lookup", re.compile(r"\b(?:weather|forecast|temperature|raining|rain today|snow)\b", re.I)),
    ("research", re.compile(r"\b(?:search(?: the web| for)?|google|look up|latest|news|who won|score|"
                            r"price of|how much (?:is|does)|stock|release date)\b", re.I)),
    ("system", re.compile(r"^play\b|\b(?:spotify|songs?|music|playlist|album|volume|mute|battery|lock (?:the |my )?screen|"
                          r"sleep|clipboard|quit|close (?:the )?\w+ app|open \w+)\b", re.I)),
    ("files", re.compile(r"\b(?:files?|folders?|downloads|desktop|documents|pdf|finder|rename|unzip|zip)\b", re.I)),
    ("browser", re.compile(r"\b(?:safari|tabs?|website|webpage|url)\b", re.I)),
    ("memory", re.compile(r"^(?:remember|forget|note that|don'?t forget)\b|\bwhat do you (?:know|remember)\b", re.I)),
    ("trading", re.compile(r"\bback ?test(?:ed|ing|s)?\b|\bpaper trad\w+|\btrading terminals?\b|"
                           r"\btrading strateg(?:y|ies)\b|\bsharpe\b|\bdrawdown\b|\bslippage\b|"
                           r"\bbuy and hold\b|\bwalk[- ]forward\b|\bmax drawdown\b|"
                           r"\b(?:sims?|simulations?) (?:on|for|of|in)\b|"
                           r"\brun \d+ (?:sims?|simulations?)\b", re.I)),
]

# Questions about the user's own things -- their files, their accounts,
# what they have -- cannot be answered from the model's own knowledge, so
# they never take the no-tools "answer" route however they're phrased.
_ABOUT_THEIR_STUFF = re.compile(r"\b(?:do i have|have i got|my own|available to me|i have|"
                                r"mine)\b|\bmy \w+", re.I)

# Plain questions a model answers from its own knowledge: no tools, one call.
_ANSWERABLE = re.compile(r"^(?:what|why|how|who|explain|define|describe|tell me (?:a|about)|write (?:me )?a "
                         r"(?:poem|joke|haiku|limerick)|give me (?:an? )?(?:example|idea|tip)s?|is it|should i|"
                         r"can you explain|what'?s the difference)\b", re.I)
_TIME_SENSITIVE = re.compile(r"\b(?:today|tonight|now|latest|current|currently|recent|news|this (?:week|month|year)|"
                             r"yesterday|last (?:night|week)|20[2-9]\d|score|price|stock|weather|open now)\b", re.I)


def direct_agent(text: str) -> str | None:
    """The one agent a simple request needs, or None when it should be planned."""
    t = " ".join((text or "").split())
    if not t or len(t.split()) > 30 or _MULTI_STEP.search(t) or _NEEDS_PLANNER.search(t):
        return None
    matched = [name for name, pattern in _DOMAINS if pattern.search(t)]
    if len(matched) == 1:
        return matched[0]
    # "open a trading terminal" reads as both system ("open X") and trading.
    # It's one call to one tool, which the system agent owns -- planning it
    # would cost a second model call to reach the same place.
    if set(matched) == {"system", "trading"} and _TERMINAL.search(t):
        return "system"
    if (not matched and _ANSWERABLE.search(t) and not _TIME_SENSITIVE.search(t)
            and not _ABOUT_THEIR_STUFF.search(t)):
        return "answer"
    return None


_TERMINAL = re.compile(r"\b(?:trading )?terminals?\b", re.I)


def attachment_agent(instruction: str) -> str | None:
    """For an instruction about attached text ("summarize this", "fix the
    grammar", "reply to this"): a plain answer -- no tools, one model call
    (local in Hybrid mode) -- unless it clearly needs a plan."""
    t = " ".join((instruction or "").split())
    if _MULTI_STEP.search(t) or _SCHEDULED.search(t):
        return None
    return "answer"


# Timing words that make a request a schedule rather than an answer.
_SCHEDULED = re.compile(r"\b(?:every|each (?:day|week|morning|night)|daily|weekly|remind me|in \d+ "
                        r"(?:min|minute|hour|second)s?|at \d|tomorrow at|later|in the background)\b", re.I)
