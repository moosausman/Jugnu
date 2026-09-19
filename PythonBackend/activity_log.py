"""
activity_log.py -- continuous, lightweight activity logging.

awareness.py answers "what's going on right now" for a single turn.
proactive.py asks "is this specific change worth mentioning". This module
is the third piece: it just watches, quietly, all day, and writes down how
long each app stayed frontmost -- the raw material daily_review.py turns
into an end-of-day recap.

Deliberately NOT a screen recorder or OCR pipeline: no screenshots, no
image capture, nothing sent to a model on every poll. It logs app name +
window title + duration -- the same level of detail Screen Time /
RescueTime already show you, at a fraction of the storage and zero extra
model cost. If you want a richer, read-what-was-actually-on-screen version
of this later, that would layer periodic screenshots + local OCR (or a
vision-model call) on top of this same watcher -- a bigger, separate
undertaking, not something to bolt on silently here.

Off by default the same way everything else here is -- see
JUGNU_TRACK_ACTIVITY in bridge.py.
"""

import atexit
import threading
import time

import awareness as aware
import semantic_memory as smem

DEFAULT_POLL_SECONDS = 30.0

# A long stretch in one app (a whole afternoon in Xcode) would otherwise
# only get written once the user finally switches away -- meaning a
# same-day report requested mid-session would see nothing for it yet.
# Checkpointing splits it into rows every this-many seconds instead.
CHECKPOINT_SECONDS = 900  # 15 min


class ActivityLogger:
    """Polls awareness.capture() on a timer and logs one timeline entry
    per completed stretch of frontmost-app time -- not one entry per poll,
    so a whole day of watching costs dozens of rows, not thousands."""

    def __init__(self, poll_seconds: float = DEFAULT_POLL_SECONDS):
        self._poll_seconds = poll_seconds
        self._current_app: str | None = None
        self._current_title: str = ""
        self._started_at: float | None = None

    def start(self) -> None:
        atexit.register(self._flush_current)
        threading.Thread(target=self._loop, name="activity-logger", daemon=True).start()

    def _loop(self) -> None:
        while True:
            time.sleep(self._poll_seconds)
            try:
                self._tick()
            except Exception as e:
                aware.log(f"activity logger error: {e}")

    def _tick(self) -> None:
        snapshot = aware.capture()
        app = snapshot.frontmost_app

        if app == self._current_app:
            if self._started_at and time.time() - self._started_at >= CHECKPOINT_SECONDS:
                self._flush_current()
                self._current_app = app
                self._current_title = snapshot.window_title
                self._started_at = snapshot.when
            return

        self._flush_current()
        self._current_app = app
        self._current_title = snapshot.window_title
        self._started_at = snapshot.when

    def _flush_current(self) -> None:
        if self._current_app and self._started_at:
            duration = time.time() - self._started_at
            smem.record_activity(self._current_app, self._current_title, duration)
        self._current_app = None
        self._started_at = None
