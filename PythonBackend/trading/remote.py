"""
remote.py -- running simulations in the Jugnu account.

The second macOS account is a whole machine sitting idle. Long sweeps
belong there: they don't compete with your editor, they can't be killed by
closing a window, and they keep running while you're logged out.

Getting the code there takes two tricks, both forced by macOS:

  your home folder is drwx------, so the Jugnu account cannot read the
  repo. The package is copied to /Users/Shared/jugnu-trading, which both
  accounts can reach; and

  that account has only the system Python, with no third-party packages.
  Which is why this package speaks HTTP through _http.py and imports
  nothing beyond the standard library.

Commands travel over MacBridge's shell endpoint on the agent session's
port -- the same channel Jugnu already uses to act in that account -- and
come back as JSON.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from ._http import HttpError, post_json

SHARED = Path("/Users/Shared/jugnu-trading")
AGENT_PORT = int(os.environ.get("JUGNU_AGENT_PORT", "8766"))
PACKAGE = Path(__file__).parent
_install_lock = threading.Lock()


class RemoteError(RuntimeError):
    pass


def available() -> bool:
    try:
        post_json(f"http://127.0.0.1:{AGENT_PORT}/health", {}, timeout=3)
        return True
    except HttpError:
        try:
            from ._http import get_json
            get_json(f"http://127.0.0.1:{AGENT_PORT}/health", timeout=3)
            return True
        except HttpError:
            return False


def install(force: bool = False) -> Path:
    """Copies the package somewhere both accounts can read, and makes a data
    directory the other account can write. Returns the install path.

    Locked and atomic: two jobs going remote at once used to delete the tree
    while the other account was importing from it, which failed with an
    ImportError that looked like a bug in the simulation. The new copy is
    built beside the old one and swapped in one move."""
    target = SHARED / "trading"
    stamp = SHARED / ".synced"
    with _install_lock:
        newest = max((p.stat().st_mtime for p in PACKAGE.glob("*.py")), default=0)
        if not force and stamp.exists() and stamp.stat().st_mtime >= newest and target.exists():
            return SHARED

        SHARED.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".sync-", dir=SHARED))
        fresh = staging / "trading"
        shutil.copytree(PACKAGE, fresh,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "custom"))
        # Custom strategies go too: sweeping one is the main reason to use
        # the other account.
        custom = PACKAGE / "custom"
        if custom.exists():
            shutil.copytree(custom, fresh / "custom", ignore=shutil.ignore_patterns("__pycache__"))
        for path in fresh.rglob("*"):
            try:
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
            except PermissionError:
                pass
        os.chmod(fresh, 0o755)
        previous = SHARED / ".trading-old"
        shutil.rmtree(previous, ignore_errors=True)
        if target.exists():
            os.rename(target, previous)
        os.rename(fresh, target)
        shutil.rmtree(previous, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        (SHARED / "data").mkdir(exist_ok=True)
        for path in [SHARED, SHARED / "data"]:
            try:
                os.chmod(path, 0o777 if path.name == "data" else 0o755)
            except PermissionError:
                pass
        stamp.write_text(str(time.time()))
    return SHARED


def run(payload: dict, *, timeout: float = 900.0, python: str = "/usr/bin/python3") -> dict:
    """Runs one `trading api` call in the Jugnu account and returns its JSON."""
    install()
    quoted = json.dumps(payload).replace("'", "'\\''")
    command = (f"cd {SHARED} && JUGNU_DATA_DIR={SHARED}/data "
               f"{python} -m trading api '{quoted}'")
    try:
        reply = post_json(f"http://127.0.0.1:{AGENT_PORT}/shell/run",
                          {"args": {"command": command, "timeout": int(timeout)}},
                          timeout=timeout + 30)
    except HttpError as e:
        raise RemoteError(f"The Jugnu account isn't answering ({e}). Log into it once and "
                          f"leave it running in the background.") from e
    if reply.get("ok") is False:
        raise RemoteError(str(reply.get("error") or "the shell call failed"))
    stdout = (reply.get("stdout") or "").strip()
    if reply.get("exitCode") not in (0, None):
        detail = (reply.get("stderr") or stdout or "").strip().splitlines()
        raise RemoteError(f"Remote run failed: {detail[-1] if detail else 'unknown error'}")
    try:
        return json.loads(stdout.splitlines()[-1]) if stdout else {}
    except (json.JSONDecodeError, IndexError) as e:
        raise RemoteError(f"Remote run returned something unreadable: {stdout[:200]}") from e


def where_am_i() -> str:
    """Which account this process is in -- so the terminal can say so, and
    so a remote job never tries to go remote again."""
    return "jugnu" if Path.home().name == "jugnu" else "primary"
