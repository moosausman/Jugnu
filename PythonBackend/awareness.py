"""
awareness.py -- situational awareness for Jugnu: what's on the user's screen
and what they're doing right now, independent of anything they've typed.

memory.py knows what the user has *told* Jugnu (facts) and *said*
(conversation). semantic_memory.py knows what the user has *done over
time* (habits, past searches). This module is the third leg: what's true
*right now* -- which app is frontmost, what its window is called, what
else is open, how long the machine has sat idle. Nobody has to ask for
any of this; it's ambient, the same way a person glancing at your screen
would just notice you're in Xcode, not Slack.

Two ways this gets used:

  1. Passive, every turn -- session.py's build_context() calls
     as_prompt_block() and folds it in alongside recent conversation and
     longitudinal memory, so the planner and every task agent already
     know what's going on without a tool call or a "who's asking" agent.
     This is the CONTEXT half of situational awareness.

  2. Active, on a timer -- bridge.py's background watcher calls capture()
     every JUGNU_PROACTIVE_POLL_SECONDS and, via significant_change() /
     describe_change(), notices when the frontmost app changes. That's
     what lets Jugnu consider saying something unprompted (see
     proactive.py) -- the INITIATIVE half.

Implementation is macOS System Events (AppleScript) + ioreg, run through
subprocess with short timeouts -- consistent with how the rest of Jugnu's
tool layer already drives the Mac. Everything here is best-effort and
silent-degrading by design (same philosophy as semantic_memory.py):
a permission not yet granted, or a query that fails, means an empty or
partial snapshot, never a broken turn.

FIRST-RUN NOTE: reading the frontmost app/window through System Events
requires the same Accessibility/Automation permission grant tools.py's
shell_stage and applescript-based tools already need. If it's not granted
yet, queries fail quietly and awareness stays blank until the user grants
Jugnu access in System Settings -> Privacy & Security -> Accessibility
(and Automation, for controlling System Events specifically). This module
warns once via log() rather than on every single poll.
"""

import datetime
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import os

_IS_MAC = sys.platform == "darwin"
_OSASCRIPT_TIMEOUT = 2.0  # seconds -- never let a stalled query hang a turn

AWARENESS_ENABLED = os.environ.get("JUGNU_ENABLE_AWARENESS", "1") != "0"

# How long an idle stretch has to be before it's worth mentioning in the
# prompt block at all -- a 20-second pause while the user reads something
# isn't "idle", it's just reading.
IDLE_MENTION_THRESHOLD_SECONDS = 180

_CACHE_TTL = 1.5  # seconds -- collapses back-to-back capture() calls within
# the same turn into one round of subprocess calls, without going stale
# across turns or watcher polls (both are seconds to minutes apart).


def log(message: str) -> None:
    print(f"[awareness] {message}", file=sys.stderr, flush=True)


@dataclass
class Snapshot:
    when: float
    local_time: str
    frontmost_app: str | None
    window_title: str
    running_apps: list = field(default_factory=list)
    idle_seconds: float | None = None


# --- gathering ---------------------------------------------------------

_warned_permission = False


def _osascript(script: str) -> str | None:
    if not _IS_MAC:
        return None
    global _warned_permission
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT,
        )
        if result.returncode != 0:
            if not _warned_permission:
                log(
                    "System Events query failed -- likely no Accessibility/"
                    "Automation permission granted yet. Grant Jugnu access "
                    "in System Settings -> Privacy & Security. Awareness "
                    "will stay blank until then (this warning only prints "
                    "once)."
                )
                _warned_permission = True
            return None
        return result.stdout.strip()
    except Exception as e:
        log(f"osascript call failed: {e}")
        return None


def _frontmost() -> tuple[str, str]:
    """Returns (app_name, window_title) in a single round trip -- one
    subprocess spawn and one System Events query instead of two, since
    every call here is on the critical path of a turn or a poll tick."""
    script = """
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set appName to name of frontApp
        try
            set winTitle to name of front window of frontApp
        on error
            set winTitle to ""
        end try
    end tell
    return appName & linefeed & winTitle
    """
    raw = _osascript(script)
    if not raw:
        return "", ""
    lines = raw.split("\n", 1)
    app = lines[0].strip()
    title = lines[1].strip() if len(lines) > 1 else ""
    return app, title


def running_apps() -> list:
    """Names of visible (Dock-eligible) running apps -- background-only
    helpers and menu-bar-only agents are excluded, same set a user would
    see cycling through Cmd+Tab."""
    raw = _osascript(
        'tell application "System Events" to get name of every process '
        'whose background only is false'
    )
    if not raw:
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse event, via ioreg's
    HIDIdleTime (nanoseconds since last input). None if unavailable."""
    if not _IS_MAC:
        return None
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT,
        ).stdout
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                ns = int(line.rsplit("=", 1)[-1].strip())
                return round(ns / 1_000_000_000, 1)
    except Exception as e:
        log(f"idle time read failed: {e}")
    return None


_cache: tuple | None = None  # (captured_at, Snapshot)


def capture(force: bool = False) -> Snapshot:
    """Takes a fresh snapshot of what's currently going on, parallelizing
    the underlying subprocess calls (same ThreadPoolExecutor pattern
    search.py uses for concurrent scraping) so the total added latency is
    close to the slowest single query rather than their sum. Cached briefly
    (see _CACHE_TTL) so multiple callers within the same instant -- or the
    same turn -- don't each pay for their own round of subprocess calls."""
    global _cache
    now = time.time()
    if not force and _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]

    with ThreadPoolExecutor(max_workers=3) as pool:
        frontmost_future = pool.submit(_frontmost)
        apps_future = pool.submit(running_apps)
        idle_future = pool.submit(idle_seconds)

        app, window_title = frontmost_future.result()
        apps = apps_future.result()
        idle = idle_future.result()

    snapshot = Snapshot(
        when=now,
        local_time=datetime.datetime.now().strftime("%A, %B %d, %Y, %I:%M %p"),
        frontmost_app=app or None,
        window_title=window_title,
        running_apps=apps,
        idle_seconds=idle,
    )
    _cache = (now, snapshot)
    return snapshot


# --- formatting ---------------------------------------------------------

def as_prompt_block(snapshot: Snapshot | None = None) -> str:
    """Formatted for direct injection into the planner/agent context, same
    pattern as facts_as_string() and get_habit_summary(). Returns "" if
    awareness is disabled (JUGNU_ENABLE_AWARENESS=0) or nothing useful was
    gathered (e.g. permission not granted yet)."""
    if not AWARENESS_ENABLED:
        return ""

    snap = snapshot or capture()
    lines = [f"Local time: {snap.local_time}."]

    if snap.frontmost_app:
        line = f"Active app: {snap.frontmost_app}"
        if snap.window_title:
            line += f' ("{snap.window_title}")'
        lines.append(line)

    others = [a for a in snap.running_apps if a != snap.frontmost_app]
    if others:
        shown, rest = others[:15], others[15:]
        suffix = f", and {len(rest)} more" if rest else ""
        lines.append(f"Also open: {', '.join(shown)}{suffix}")

    if snap.idle_seconds is not None and snap.idle_seconds >= IDLE_MENTION_THRESHOLD_SECONDS:
        lines.append(f"User has been idle for about {int(snap.idle_seconds // 60)} min.")

    if len(lines) == 1:
        # Only the timestamp came through -- nothing else was gatherable
        # (no permission yet, or a non-Mac dev environment). A bare
        # timestamp isn't worth injecting into every prompt.
        return ""

    return "What's happening right now:\n" + "\n".join(f"- {l}" for l in lines)


# --- change detection (for the proactive watcher) -----------------------

def significant_change(prev: "Snapshot | None", curr: "Snapshot") -> bool:
    """What counts as 'worth a second look' for bridge.py's proactive
    watcher. Deliberately narrow -- a frontmost-app switch, full stop --
    so a flickering window title (a page finishing its load, a document
    autosaving) doesn't trigger a model call on every poll."""
    if prev is None or not curr.frontmost_app:
        return False
    return prev.frontmost_app != curr.frontmost_app


def describe_change(prev: "Snapshot | None", curr: "Snapshot") -> str:
    """One plain sentence describing the change, or "" if there isn't one
    -- handed to proactive.consider() as concrete grounding rather than
    making that module re-derive it from two raw snapshots."""
    if not significant_change(prev, curr):
        return ""
    return f"The user just switched from {prev.frontmost_app} to {curr.frontmost_app}."


# --- the Jugnu account's desktop -----------------------------------------
# Everything above reads the login this process runs in -- the user's own
# screen. When work happens in the dedicated agent account (see
# tools/macbridge.py), that tells the model nothing about the desktop it is
# actually driving, so this asks that session's MacBridge directly.

def agent_desktop_block() -> str:
    """What's frontmost and open in the agent account's session, or "" when
    that session isn't running. Best-effort, like everything here."""
    if not AWARENESS_ENABLED:
        return ""
    try:
        from tools.macbridge import call, session_available
        if not session_available("agent"):
            return ""
        front = call("/keyboard/frontmost", timeout=3, session="agent")
        apps = call("/ui/apps", timeout=3, session="agent")
    except Exception as e:
        log(f"agent desktop read failed: {e}")
        return ""
    lines = []
    if front.get("ok") and front.get("name"):
        lines.append(f"Active app: {front['name']}")
    names = [a.get("name") for a in apps.get("apps", []) if a.get("name")]
    others = [n for n in names if n != front.get("name")]
    if others:
        lines.append(f"Also open: {', '.join(others[:15])}")
    if not lines:
        lines.append("Nothing open yet.")
    return ("The Jugnu account's desktop (separate login -- not what the user "
            "is looking at unless they switch to it):\n"
            + "\n".join(f"- {l}" for l in lines))
