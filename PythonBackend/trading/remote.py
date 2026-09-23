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
        # A launcher the Jugnu account can run: its own Python, its own
        # data directory, inside a folder it's allowed to read.
        launcher = SHARED / "jugnu"
        launcher.write_text("#!/bin/bash\n"
                            f"cd {SHARED} || exit 1\n"
                            f"export JUGNU_DATA_DIR={SHARED}/data\n"
                            "exec /usr/bin/python3 -m trading \"${@:-term}\"\n")
        os.chmod(launcher, 0o755)
        stamp.write_text(str(time.time()))
    return SHARED


def launcher_path() -> str:
    install()
    return str(SHARED / "jugnu")


def grid(count: int, screen: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    """Window rectangles for `count` terminals, tiled over the screen.
    Wider than tall, because a chart pane wants width."""
    import math
    x0, y0, x1, y1 = screen
    columns = min(count, max(1, math.ceil(math.sqrt(count))))
    rows = max(1, math.ceil(count / columns))
    width = (x1 - x0) // columns
    height = (y1 - y0) // rows
    out = []
    for i in range(count):
        column, row = i % columns, i // columns
        left = x0 + column * width
        top = y0 + row * height
        out.append((left, top, left + width, top + height))
    return out


def screen_bounds(port: int | None = None) -> tuple[int, int, int, int]:
    try:
        reply = post_json(f"http://127.0.0.1:{port or AGENT_PORT}/screen/displays", {"args": {}}, timeout=10)
        display = (reply.get("displays") or [{}])[0].get("bounds") or {}
        return (int(display.get("x", 0)), int(display.get("y", 0)) + 25,
                int(display.get("x", 0)) + int(display.get("width", 1512)),
                int(display.get("y", 0)) + int(display.get("height", 982)) - 80)
    except Exception:
        return (0, 25, 1512, 900)


def open_terminals(count: int = 1, commands: list[str] | None = None, tile: bool = True) -> int:
    """Opens the trading terminal in the Jugnu account -- several at once,
    tiled, each running its own command. Watch them on Jugnu's Desk."""
    script = launcher_path()
    count = max(1, min(8, count))
    commands = commands or []
    places = grid(count, screen_bounds()) if tile and count > 1 else []
    opened = 0
    for index in range(count):
        command = commands[index] if index < len(commands) else ""
        arguments = f" {_shell_quote(command)}" if command else ""
        lines = ['tell application "Terminal"', "  activate",
                 f'  do script "clear && \'{script}\' term{arguments}"', "  delay 0.35"]
        if places:
            left, top, right, bottom = places[index]
            lines.append(f"  try\n    set bounds of front window to "
                         f"{{{left}, {top}, {right}, {bottom}}}\n  end try")
        else:
            lines.append("  try\n    set number of columns of front window to 108\n"
                         "    set number of rows of front window to 34\n  end try")
        lines.append("end tell")
        source = "\n".join(lines)
        try:
            reply = post_json(f"http://127.0.0.1:{AGENT_PORT}/shell/run",
                              {"args": {"command": f"osascript -e {_quote(source)}", "timeout": 40}},
                              timeout=60)
        except HttpError as e:
            raise RemoteError(f"The Jugnu account isn't answering ({e}).") from e
        if reply.get("ok") is False:
            raise RemoteError(str(reply.get("error") or "couldn't open a window there"))
        opened += 1
        time.sleep(0.45)          # Terminal drops windows asked for too fast
    return opened


def _shell_quote(text: str) -> str:
    """A command going through AppleScript into a shell: quote it for the
    shell, then leave AppleScript's own quoting to _quote()."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


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
