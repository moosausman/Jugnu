"""
fastpath.py -- requests that need no model at all.

    "play levitating by dua lipa"   "play some drake"   "play the after hours album"
    "pause"   "next song"   "volume 40"   "what's playing"   "open safari"

Recognised with patterns, run as ONE tool call through tool_runtime (the
same policy gate, audit log and activity events as any other call) and
answered with a fixed sentence: no planner, no agent loop, no model call,
about a second end to end.

Anything the patterns aren't sure about returns None and takes the normal
route, so a miss costs nothing but a regex.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import agent_core
import run_context
import tool_runtime
from tools import macbridge
from tools.tools import FUNCTIONS, SCHEMAS

_SCHEMAS_BY_NAME = {s["function"]["name"]: s for s in SCHEMAS}


@dataclass
class Intent:
    tool: str
    args: dict = field(default_factory=dict)
    describe: Callable[[dict], str] = lambda raw: "Done."
    session: Optional[str] = None     # None = the run's default session
    # A second call, run only if the first one worked. "Play X at max
    # volume" is two actions and one sentence; without this the volume
    # half was silently swallowed into the artist's name.
    then: Optional["Intent"] = None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_POLITE = re.compile(r"^(?:hey |ok |okay )?(?:jugnu[, ]+)?(?:please |can you |could you |would you |pls )?", re.I)
_TAIL = re.compile(r"(?:[\s,]+(?:please|pls|for me|jugnu|thanks|thank you))+$|[\s.!?]+$", re.I)

# Things "play" is said about that aren't a Spotify search.
_NOT_SPOTIFY = re.compile(r"\b(video|youtube|netflix|movie|film|episode|podcast|game|apple music|music app|"
                          r"in safari|on safari|playground)\b", re.I)
# Needs the conversation (or judgement) to know what's meant: "that song
# again", "something relaxing", "my playlist", "a song for studying".
_VAGUE = re.compile(r"^(?:it|that|this|them|those|these|the same|again|my)\b|\bagain$|"
                    r"\b(?:something|anything|songs?|music|tunes|vibes?|mood|playlists?)\b(?!.* by )", re.I)

_MUSIC_WORDS = r"(?:the )?(?:music|song|track|spotify|playback)"
_NOT_ARTISTS = {"me", "you", "us", "him", "her", "them", "it", "my side", "your side", "the radio", "the sea",
                "the river", "the window", "the way", "myself", "yourself", "now", "day"}


def _clean(text: str) -> str:
    text = " ".join((text or "").split())
    text = _POLITE.sub("", text)
    return _TAIL.sub("", text).strip()


# "on spotify" anywhere, not just at the end -- and whatever short word
# precedes it, because "pn spotify" is one slipped key and the artist is
# not called "hania amir pn spotify".
_ON_SPOTIFY = re.compile(r"[\s,]+(?:\S{1,4}\s+)?spotify\b", re.I)

_LOUDNESS = {"max": 100, "maximum": 100, "full": 100, "highest": 100, "loudest": 100,
             "high": 85, "loud": 85, "half": 50, "low": 25, "lowest": 10,
             "min": 10, "minimum": 10, "quiet": 20, "quietly": 20}

# A loudness clause at the end of a play request: "at max volume",
# "on full volume", "at volume 40", "at 40% volume", "at full blast".
_VOLUME_CLAUSE = re.compile(
    r"[\s,]+(?:at|on|with|in)\s+(?:the\s+)?(?:"
    r"(?P<word>max|maximum|full|highest|loudest|high|loud|half|low|lowest|min|minimum|quiet|quietly)"
    r"\s+volume"
    r"|volume\s+(?P<after>\d{1,3})\s*(?:%|percent)?"
    r"|(?P<before>\d{1,3})\s*(?:%|percent)?\s+volume"
    r")$"
    r"|[\s,]+(?:at\s+)?(?:full\s+blast|max(?:imum)?)$", re.I)


def _strip_spotify(obj: str) -> str:
    return _ON_SPOTIFY.sub("", obj).strip().strip(",")


def _split_volume(obj: str) -> tuple[str, Optional[int]]:
    """The request without its loudness clause, and the level it asked for."""
    match = _VOLUME_CLAUSE.search(obj)
    if not match:
        return obj, None
    word = match.groupdict().get("word")
    digits = match.groupdict().get("after") or match.groupdict().get("before")
    if digits is not None:
        level = int(digits)
        if not 0 <= level <= 100:
            return obj, None          # "at volume 900" isn't a volume
    else:
        level = _LOUDNESS.get((word or "max").lower(), 100)
    return obj[:match.start()].strip().strip(","), level


def _session_hint(text: str) -> Optional[str]:
    import router
    return router.session_hint(text)


def parse(text: str) -> Optional[Intent]:
    """The one tool call `text` asks for, or None if it isn't obviously one."""
    t = _clean(text)
    low = t.lower()
    if not t or len(t) > 120:
        return None

    # -- playback controls ----------------------------------------------------
    if re.fullmatch(rf"(?:pause|stop)(?: {_MUSIC_WORDS})?", low) or low in ("pause it", "pause spotify"):
        return Intent("spotify_control", {"action": "pause"}, lambda r: "Paused.", "primary")
    if re.fullmatch(rf"(?:resume|unpause|continue)(?: {_MUSIC_WORDS})?|play|play music|resume playing", low):
        return Intent("spotify_control", {"action": "play"}, _describe_state("Playing"), "primary")
    if re.fullmatch(r"(?:next|skip)(?: (?:the )?(?:song|track|this|it|this song))?|(?:play )?(?:the )?next (?:song|track)",
                    low):
        return Intent("spotify_control", {"action": "next"}, _describe_state("Now playing"), "primary")
    if re.fullmatch(r"(?:previous|prev|go back)(?: (?:song|track))?|(?:play )?(?:the )?(?:previous|last) (?:song|track)", low):
        return Intent("spotify_control", {"action": "previous"}, _describe_state("Now playing"), "primary")
    volume = re.fullmatch(r"(?:set |turn )?(?:the )?(?:spotify |music )?volume(?: to| at)? (\d{1,3})(?: ?%| percent)?", low)
    if volume and 0 <= int(volume.group(1)) <= 100:
        level = int(volume.group(1))
        return Intent("spotify_control", {"volume": level}, lambda r, v=level: f"Spotify volume set to {v}%.", "primary")
    if re.fullmatch(r"what(?:'s| is) (?:playing|this song|the song|on)(?: now| right now)?|what song is (?:this|playing)"
                    r"|what(?:'s| is) playing on spotify", low):
        return Intent("spotify_now_playing", {}, _describe_now_playing, "primary")

    # -- play <something> -------------------------------------------------------
    m = re.fullmatch(r"play (.+)", t, flags=re.I)
    if m:
        # Strip the loudness clause before parsing, or it ends up inside
        # the artist and the search finds nothing.
        obj, level = _split_volume(_strip_spotify(m.group(1)))
        intent = _parse_play(obj) if obj else None
        if intent is not None and level is not None:
            intent.then = Intent("spotify_control", {"volume": level},
                                 lambda r, v=level: f"Volume at {v}%.", "primary")
        return intent
    # -- open <app> -----------------------------------------------------------------
    m = re.fullmatch(r"(?:open|launch|start) (?:up )?(?:the )?(.+?)(?: app)?", t, flags=re.I)
    if m:
        app = installed_app(m.group(1))
        if app:
            return Intent("open_app", {"app_name": app}, lambda r, a=app: f"Opened {a}.")
    return None


def _parse_play(obj: str) -> Optional[Intent]:
    obj = re.sub(r"^me ", "", obj, flags=re.I)   # "play me X"
    if not obj or _NOT_SPOTIFY.search(obj) or len(obj) > 90:
        return None
    # Explicit playlist/album/artist phrasings are checked before vagueness,
    # so "play the lofi beats playlist" isn't mistaken for a vague request.
    explicit = re.search(r"\b(?:album|playlist)\b|^(?:some|songs by|music by|something by|anything by|more) ",
                         obj, flags=re.I)
    if not explicit and _VAGUE.search(obj):
        return None

    m = re.fullmatch(r"(?:the )?album (.+?)(?: by (.+))?|(?:the )?(.+?) album(?: by (.+))?", obj, flags=re.I)
    if m:
        album, artist = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        args = {"album": album.strip()}
        if artist:
            args["artist"] = artist.strip()
        return Intent("play_album", args, _describe_play("album"), "primary")

    m = re.fullmatch(r"(?:the |my )?playlist (.+)|(?:the |a |some )?(.+?) playlist", obj, flags=re.I)
    if m:
        name = (m.group(1) or m.group(2)).strip()
        if name.lower() in ("my", "a", "the", "some", "that", "this") or obj.lower().startswith("my "):
            return None   # one of the user's own playlists: needs its link, or a question
        return Intent("play_playlist", {"playlist": name}, _describe_play("playlist"), "primary")

    m = re.fullmatch(r"(?:some|songs by|music by|something by|anything by|more) (.+)", obj, flags=re.I)
    if m:
        return Intent("play_artist", {"artist": m.group(1).strip()}, _describe_play("artist"), "primary")

    m = re.fullmatch(r"(?:the song )?(.+?) by (.+)", obj, flags=re.I)
    # "Stand By Me", "Killed by the Radio" -- a " by " that's part of the title.
    if m and m.group(2).strip().lower() not in _NOT_ARTISTS and len(m.group(2).strip()) > 1:
        return Intent("play_song", {"song": m.group(1).strip(), "artist": m.group(2).strip()},
                      _describe_play("track"), "primary")

    song = re.sub(r"^the song ", "", obj, flags=re.I).strip()
    return Intent("play_song", {"song": song}, _describe_play("track"), "primary")


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------

_APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
             os.path.expanduser("~/Applications"), "/Applications/Utilities"]
_app_index: dict | None = None


def installed_app(name: str) -> Optional[str]:
    """The installed app's proper name for `name` ("safari" -> "Safari"),
    or None when no installed app has that exact name."""
    global _app_index
    if _app_index is None:
        _app_index = {}
        for folder in _APP_DIRS:
            try:
                for entry in os.listdir(folder):
                    if entry.endswith(".app"):
                        _app_index.setdefault(entry[:-4].lower(), entry[:-4])
            except OSError:
                pass
    return _app_index.get(" ".join(name.lower().split()))


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

def _describe_play(kind: str):
    def describe(raw: dict) -> str:
        resolved = (raw or {}).get("resolved") or {}
        name = resolved.get("name") or (raw or {}).get("track") or "it"
        artist = resolved.get("artist") or (raw or {}).get("artist") or ""
        if kind == "artist":
            return f"Playing {name}."
        if kind == "playlist":
            return f"Playing the {name} playlist."
        if kind == "album":
            return f"Playing {name}" + (f" by {artist}." if artist else ".")
        return f"Playing {name}" + (f" by {artist}." if artist else ".")
    return describe


def _describe_state(lead: str):
    def describe(raw: dict) -> str:
        track, artist = (raw or {}).get("track"), (raw or {}).get("artist")
        if track:
            return f"{lead} {track}" + (f" by {artist}." if artist else ".")
        return "Done."
    return describe


def _describe_now_playing(raw: dict) -> str:
    raw = raw or {}
    if not raw.get("running", True):
        return "Spotify isn't open."
    if raw.get("track") and raw.get("playing", raw.get("playerState") == "playing"):
        return f"Playing {raw['track']}" + (f" by {raw['artist']}." if raw.get("artist") else ".")
    if raw.get("track"):
        return f"Paused on {raw['track']}" + (f" by {raw['artist']}." if raw.get("artist") else ".")
    return "Nothing is playing right now."


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------

def try_handle(text: str, session: Optional[str] = None) -> Optional[str]:
    """Runs `text` without a model if it's one of the patterns above.
    Returns the reply, or None to let the normal route take it.
    `session` is the account the user picked for this request, if any
    (music always plays in the user's own account regardless)."""
    intent = parse(text)
    if intent is None or intent.tool not in FUNCTIONS:
        return None
    ctx = run_context.current()
    macbridge.reset_session()
    hinted = session or _session_hint(text)
    if intent.session:
        macbridge.set_active_session(intent.session)
    elif hinted:
        macbridge.set_active_session(hinted)
    elif not macbridge.session_available(macbridge.active_session()):
        return None  # let the normal route explain which account isn't running

    ctx.send({"type": "status", "label": intent.tool})
    outcome = tool_runtime.execute(intent.tool, intent.args, FUNCTIONS, _SCHEMAS_BY_NAME, ctx=ctx)
    if outcome.ok:
        raw = outcome.raw if isinstance(outcome.raw, dict) else {}
        reply = intent.describe(raw)
        if intent.then is not None:
            after = tool_runtime.execute(intent.then.tool, intent.then.args, FUNCTIONS,
                                         _SCHEMAS_BY_NAME, ctx=ctx)
            if after.ok:
                reply += " " + intent.then.describe(after.raw if isinstance(after.raw, dict) else {})
            else:
                reply += " I couldn't set the volume, though."
        return reply
    if outcome.error_type in (tool_runtime.ErrorType.OS_PERMISSION, tool_runtime.ErrorType.POLICY_DENIED,
                              tool_runtime.ErrorType.USER_DENIED):
        return agent_core._stop_message(outcome)
    error = (outcome.error or "it didn't work").strip().rstrip(".")
    if "isn't running" in error.lower() and intent.tool in ("spotify_control", "spotify_now_playing"):
        if intent.tool == "spotify_now_playing":
            return "Spotify isn't open, so nothing's playing."
        return ("Spotify isn't open. Tell me what to play and I'll start it."
                if intent.args.get("action") == "play" else "Spotify isn't open.")
    if intent.tool.startswith("play_") and error.lower().startswith("couldn't find"):
        return error + ". Try the exact title, or add the artist."
    return f"That didn't work: {error}."
