import contextvars
import os
import time

import requests
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Sessions: which macOS login Jugnu acts in
# ---------------------------------------------------------------------------
# macOS ties GUI automation to a login session. Apple Events, the
# accessibility tree, the keyboard and mouse, and the screen all belong to
# ONE session, and a process in a different session simply cannot reach
# them -- there is no permission that grants it. So acting as a separate
# user means a separate MacBridge running inside that user's session, and
# choosing between them means choosing a port.
#
#   primary  :8765   your own login. Your apps, your windows, your files.
#   agent    :8766   the dedicated agent account's login, provisioned with
#                    second-self's setup/provision.sh, which bootstraps
#                    services into that session via `launchctl bootstrap
#                    gui/<uid>`.
#
# Which one is the default comes from JUGNU_DEFAULT_SESSION (the Settings
# toggle "Work in the Jugnu account"). Either way, each request can name
# the other one ("do this on the Jugnu account" / "on my account") -- the
# planner picks the session per turn, and set_target_session switches
# mid-task.
SESSIONS = {
    "primary": os.environ.get("MACBRIDGE_URL", "http://localhost:8765"),
    "agent": os.environ.get("MACBRIDGE_AGENT_URL", "http://localhost:8766"),
}

# Falls back to "primary" so that, until the agent account is provisioned,
# everything behaves exactly as it did before.
DEFAULT_SESSION = os.environ.get("JUGNU_DEFAULT_SESSION", "primary")

# Per context, not per process: a background workflow working in the Jugnu
# account and a foreground request on the user's own desktop can run at the
# same time, and each must keep hitting its own MacBridge. Threads started
# by the executor copy their parent's context, so tasks inherit the run's
# session.
_active: contextvars.ContextVar = contextvars.ContextVar("jugnu_macbridge_session", default=None)


def active_session() -> str:
    return _active.get() or DEFAULT_SESSION


def set_active_session(name: str) -> str:
    """Point subsequent calls (in this run/task) at a different session."""
    if name not in SESSIONS:
        raise ValueError(f"unknown session {name!r}; expected one of {sorted(SESSIONS)}")
    _active.set(name)
    return name


def reset_session() -> str:
    """Back to DEFAULT_SESSION. Called at the start of every run, so a
    one-off "do this on my account" doesn't stick for later requests."""
    _active.set(DEFAULT_SESSION)
    return DEFAULT_SESSION


def session_url(name: str | None = None) -> str:
    return SESSIONS[name or active_session()]


def session_available(name: str) -> bool:
    """Whether a MacBridge is actually answering in that session.

    Checked rather than assumed: the agent session only exists once the
    account has been provisioned AND logged in via Fast User Switching, and
    a request silently landing in the wrong session would be worse than a
    clear failure.
    """
    now = time.monotonic()
    cached = _availability_cache.get(name)
    if cached and now - cached[0] < _AVAILABILITY_TTL:
        return cached[1]
    try:
        ok = requests.get(SESSIONS[name] + "/health", timeout=2).ok
    except Exception:
        ok = False
    _availability_cache[name] = (now, ok)
    return ok


# Checked when building every prompt and every turn's context, so a short
# cache keeps that to one /health round trip per session every few seconds.
_AVAILABILITY_TTL = 10.0
_availability_cache: dict = {}


# Kept as a module attribute for anything that still reads it directly.
BASE = SESSIONS["primary"]

# MacBridge's Server.swift supports bearer auth, but only when MACBRIDGE_TOKEN
# is set in ITS environment -- unset, it accepts any request from any local
# process running as this user, which is its documented default trust model.
# This client previously sent no Authorization header at all, so even setting
# the variable server-side would have locked Jugnu out with a 401 rather than
# securing anything. Both halves read the same variable now: export it once,
# in the environment that launches both, and auth switches on end to end.
# Leave it unset and behaviour is exactly as before.
_TOKEN = os.environ.get("MACBRIDGE_TOKEN", "")


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}


class MacBridgeTimeout(TimeoutError):
    """Python gave up waiting -- this is NOT confirmation that the action
    didn't happen. requests.timeout only bounds how long *this process*
    waits for a response; it doesn't cancel whatever the MacBridge server
    is doing. If the server is mid-AppleScript-call when the timeout fires,
    that call keeps running (and may still complete, or already have) --
    Python just stops listening for the answer.

    The single most common cause of a call hanging the full timeout with
    no error is a silent macOS Automation/Accessibility permission prompt
    for the MacBridge app (common after a rebuild, since a new code
    signature can silently revoke a previously-granted permission) --
    check System Settings > Privacy & Security > Automation/Accessibility.

    Callers that can sanity-check afterwards (e.g. shell_read_front_window
    after a shell_stage timeout) should do that instead of assuming
    failure -- but be aware the window they read may still be showing
    stale content if the staged command never actually landed.
    """


def call(
    endpoint: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    timeout: Optional[float] = 20,
    session: Optional[str] = None,
    **kwargs: Any,
) -> dict:
    """POST to the MacBridge server.

    tools.py calls this two different ways: with a plain dict as the second
    positional argument (`call("/calendar/create", args)`) and with keyword
    arguments (`call("/files/read", path=path)`). This accepts both — and
    both at once, if a caller ever does that — by merging whichever it gets
    into a single "args" object.

    timeout: seconds to wait for a response before giving up, per call.
        Kept keyword-only (rather than folded into **kwargs) so it's never
        mistaken for a payload field to send to the server. Endpoints that
        legitimately take longer than a quick lookup (app activation,
        System Events automation) should pass a larger value explicitly
        rather than relying on the 20s default meant for fast calls.

        None means wait indefinitely, matching requests' own semantics --
        for an endpoint like /shell/run, where the caller supplies its own
        server-side watchdog value in the request body (Shell.execute's
        `timeout` argument, enforced Mac-side), the client timeout here
        has to be at least that long too, or Python gives up and reports a
        spurious failure for a command that was actually going to finish,
        or be killed on schedule, right on the server's own timeline. If
        that server-side value means "wait indefinitely" (<=0), the client
        needs to match with None here, not some arbitrary finite number
        pretending to be indefinite.
    """
    payload: Dict[str, Any] = dict(args) if args else {}
    payload.update(kwargs)

    try:
        r = requests.post(
            session_url(session) + endpoint,
            json={"args": payload},
            headers=_headers(),
            timeout=timeout,
        )
    except requests.exceptions.Timeout as e:
        raise MacBridgeTimeout(
            f"{endpoint} did not respond within {timeout}s in the "
            f"{session or active_session()!r} session ({session_url(session)}). "
            f"This means Python stopped waiting, not that the action was "
            f"cancelled or failed on the Mac side -- see MacBridgeTimeout's "
            f"docstring. If this is the agent session, check that the account "
            f"is provisioned and logged in via Fast User Switching."
        ) from e

    r.raise_for_status()
    return r.json()
