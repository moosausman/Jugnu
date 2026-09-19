import os

import requests
from typing import Any, Dict, Optional

BASE = os.environ.get("MACBRIDGE_URL", "http://localhost:8765")

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
            BASE + endpoint,
            json={"args": payload},
            headers=_headers(),
            timeout=timeout,
        )
    except requests.exceptions.Timeout as e:
        raise MacBridgeTimeout(
            f"{endpoint} did not respond within {timeout}s. This means "
            f"Python stopped waiting, not that the action was cancelled or "
            f"failed on the Mac side -- see MacBridgeTimeout's docstring."
        ) from e

    r.raise_for_status()
    return r.json()
