"""
permissions.py -- what Jugnu is allowed to touch, and how freely.

One JSON file (permissions.json in Jugnu's data folder), edited from the
Settings UI through bridge.py's "set_permissions" request and read by
policy.py on every tool call, so a change applies to the very next action
without restarting anything.

    autonomy_mode   manual | balanced | autonomous  (see policy.decide)
    capabilities    per-capability on/off switches (see CAPABILITIES)
    read_roots      folders Jugnu may read without asking
    write_roots     folders Jugnu may create/modify in without asking
    project_roots   folders where dev commands (build, test, git) are routine
    protected_paths never touched without explicit approval, in any mode
    blocked_apps    apps desktop control may not drive without approval

Paths use "~" and are expanded against the home directory of whichever
macOS session is active: the Jugnu account's own home is always fully
readable/writable by Jugnu (it is its own workspace), in addition to the
configured roots.
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path

from memory import APP_SUPPORT_DIR

PERMISSIONS_FILE = APP_SUPPORT_DIR / "permissions.json"

AUTONOMY_MODES = ("manual", "balanced", "autonomous")

# capability -> (label, description). Keep in sync with policy.TOOL_POLICIES.
CAPABILITIES: dict[str, tuple[str, str]] = {
    "web": ("Web research", "Search the web and read public pages."),
    "files_read": ("Read files", "Read and search files inside allowed folders."),
    "files_write": ("Change files", "Create, edit, move and trash files inside allowed folders."),
    "shell": ("Terminal commands", "Run commands. Destructive or installing commands always ask."),
    "dev": ("Developer tools", "Git, builds and tests inside project folders."),
    "apps": ("Open and close apps", "Launch, quit and switch between applications."),
    "desktop": ("Mouse, keyboard and windows", "Click, type, scroll and arrange windows."),
    "screen": ("Screen reading", "Screenshots, OCR and accessibility snapshots."),
    "clipboard": ("Clipboard", "Read and set the clipboard."),
    "notifications": ("Notifications and speech", "Show notifications, timers and speak aloud."),
    "calendar": ("Calendar", "Read and create calendar events."),
    "reminders": ("Reminders", "Read, create and complete reminders."),
    "contacts": ("Contacts", "Look people up in Contacts."),
    "notes": ("Notes", "Read and write Apple Notes."),
    "mail_read": ("Read mail", "List and search your inbox."),
    "mail_send": ("Send mail", "Send email -- always asks before sending."),
    "messaging": ("Messaging", "WhatsApp chats; sending always asks."),
    "browser": ("Browser", "Open and read Safari tabs."),
    "media": ("Music and media", "Spotify and system media controls."),
    "system": ("System controls", "Volume, battery, sleep, lock, system status."),
    "notion": ("Notion", "Read and write your Notion workspace."),
    "scripting": ("AppleScript and Shortcuts", "Run AppleScript and Shortcuts."),
    "location": ("Location and weather", "Geocoding, weather and reference lookups."),
    "memory": ("Memory", "Remember facts and learn from outcomes."),
    "scheduling": ("Scheduled tasks", "Run tasks you schedule, later or on repeat."),
    "temp_email": ("Disposable email", "Create throwaway inboxes for signups."),
}

DEFAULTS: dict = {
    "version": 1,
    "autonomy_mode": "balanced",
    "capabilities": {name: True for name in CAPABILITIES},
    "read_roots": ["~"],
    "write_roots": ["~/Desktop", "~/Documents", "~/Downloads", "~/Jugnu", "/Users/Shared"],
    "project_roots": ["~/Desktop", "~/Documents", "~/Developer", "~/Projects", "~/code"],
    "protected_paths": [
        "~/.ssh", "~/.gnupg", "~/.aws", "~/.config/gh", "~/.netrc", "~/.docker/config.json",
        "~/Library/Keychains", "~/Library/Cookies", "~/Library/Application Support/com.apple.TCC",
        "/System", "/Library", "/usr", "/bin", "/sbin", "/private/etc", "/etc",
    ],
    "blocked_apps": ["Keychain Access", "Passwords", "System Settings", "System Preferences"],
}

_lock = threading.RLock()
_cache: dict | None = None
_cache_mtime: float | None = None


def _merge_defaults(data: dict) -> dict:
    merged = deepcopy(DEFAULTS)
    for key, value in (data or {}).items():
        if key == "capabilities" and isinstance(value, dict):
            merged["capabilities"].update({k: bool(v) for k, v in value.items() if k in CAPABILITIES})
        elif key in merged:
            merged[key] = value
    if merged["autonomy_mode"] not in AUTONOMY_MODES:
        merged["autonomy_mode"] = DEFAULTS["autonomy_mode"]
    for key in ("read_roots", "write_roots", "project_roots", "protected_paths", "blocked_apps"):
        if not isinstance(merged[key], list):
            merged[key] = deepcopy(DEFAULTS[key])
        merged[key] = [str(v) for v in merged[key] if str(v).strip()]
    return merged


def load() -> dict:
    """Current permissions, defaults filled in. Cached on file mtime."""
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = PERMISSIONS_FILE.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if _cache is not None and mtime == _cache_mtime:
            return deepcopy(_cache)
        data = {}
        if mtime is not None:
            try:
                data = json.loads(PERMISSIONS_FILE.read_text() or "{}")
            except (json.JSONDecodeError, OSError):
                data = {}
        _cache, _cache_mtime = _merge_defaults(data), mtime
        return deepcopy(_cache)


def save(data: dict) -> dict:
    """Validates, persists and returns the full permissions object."""
    global _cache, _cache_mtime
    with _lock:
        merged = _merge_defaults(data)
        tmp = PERMISSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=2))
        os.replace(tmp, PERMISSIONS_FILE)
        _cache, _cache_mtime = merged, PERMISSIONS_FILE.stat().st_mtime
        return deepcopy(merged)


def update(patch: dict) -> dict:
    """Applies a partial change (e.g. {"autonomy_mode": "autonomous"} or
    {"capabilities": {"shell": False}}) and returns the result."""
    current = load()
    for key, value in (patch or {}).items():
        if key == "capabilities" and isinstance(value, dict):
            current["capabilities"].update(value)
        else:
            current[key] = value
    return save(current)


def autonomy_mode() -> str:
    return load()["autonomy_mode"]


def capability_enabled(name: str) -> bool:
    return bool(load()["capabilities"].get(name, True))


def describe() -> dict:
    """Permissions plus capability labels, for the UI."""
    data = load()
    data["capability_info"] = {k: {"label": v[0], "description": v[1]} for k, v in CAPABILITIES.items()}
    data["modes"] = list(AUTONOMY_MODES)
    return data


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def session_home(session: str | None = None) -> str:
    """Home directory of the macOS login a tool call will act in."""
    try:
        from tools.macbridge import active_session
        session = session or active_session()
    except Exception:
        session = session or "primary"
    if session == "agent":
        return f"/Users/{os.environ.get('JUGNU_AGENT_USER', 'jugnu')}"
    return str(Path.home())


def expand(path: str, session: str | None = None) -> str:
    """Normalizes a path the way MacBridge will see it: ~ against the
    session's home, relative paths against that home, .. collapsed."""
    if not path:
        return ""
    home = session_home(session)
    p = str(path).strip()
    if p == "~" or p.startswith("~/"):
        p = home + p[1:]
    elif not p.startswith("/"):
        p = os.path.join(home, p)
    return os.path.normpath(p)


def _roots(key: str, session: str | None) -> list[str]:
    return [expand(r, session) for r in load()[key]]


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def is_protected(path: str, session: str | None = None) -> bool:
    p = expand(path, session)
    return any(_within(p, root) for root in _roots("protected_paths", session))


def _in_agent_home(p: str, session: str | None) -> bool:
    agent_home = session_home("agent")
    return _within(p, agent_home) and (session or _current_session()) == "agent"


def _current_session() -> str:
    try:
        from tools.macbridge import active_session
        return active_session()
    except Exception:
        return "primary"


def can_read(path: str, session: str | None = None) -> bool:
    p = expand(path, session)
    if is_protected(p, session):
        return False
    if _in_agent_home(p, session):
        return True
    return any(_within(p, root) for root in _roots("read_roots", session))


def can_write(path: str, session: str | None = None) -> bool:
    p = expand(path, session)
    if is_protected(p, session):
        return False
    if _in_agent_home(p, session):
        return True
    return any(_within(p, root) for root in _roots("write_roots", session))


def in_project(path: str, session: str | None = None) -> bool:
    p = expand(path, session)
    if _in_agent_home(p, session):
        return True
    return any(_within(p, root) for root in _roots("project_roots", session))


def app_blocked(app_name: str) -> bool:
    name = (app_name or "").strip().lower()
    return bool(name) and any(name == b.lower() for b in load()["blocked_apps"])
