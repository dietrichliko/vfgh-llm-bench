"""Activity marker for the idle stop of `bench all`.

The web process touches a file on every request (except health checks) and while its
background tasks run; the supervisor reads the file's modification time.
"""

import time
from pathlib import Path

THROTTLE_SECONDS = 30.0


class ActivityMarker:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._last = 0.0

    def touch(self) -> None:
        now = time.monotonic()
        if now - self._last < THROTTLE_SECONDS:
            return
        self._last = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def last(self) -> float | None:
        """Wall-clock time of the last activity, or None if there was none."""
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None
