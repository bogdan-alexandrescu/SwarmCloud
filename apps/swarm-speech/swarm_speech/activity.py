"""The server's half of the machine's watchdog (docs/design/voice-it-own-speech.md §4.5).

The server records how many authenticated sockets it holds in one small file
on the host's disk (the launcher mounts STATE_DIR into the container):
`{"open": <n>, "at": <unix seconds>}`, rewritten on every open and close and
every REFRESH_SECONDS while any is open. The host's watchdog (watchdog.py)
powers the machine off when no socket has been open for its limit. A server
that dies stops refreshing the file, so a crash with sockets open still ends
in a power-off once the file is stale: the machine never runs with no session.

Written to a temporary file and renamed, so the watchdog never reads half.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILENAME = "activity.json"
# Under the watchdog's FRESH_SECONDS (90), so a live socket is never read as stale.
REFRESH_SECONDS = 30


class Activity:
    def __init__(self, state_dir: Path, clock=time.time) -> None:
        self._path = state_dir / FILENAME
        self._clock = clock
        self.open = 0

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"open": self.open, "at": self._clock()}), encoding="utf-8")
        os.replace(tmp, self._path)

    def opened(self) -> None:
        self.open += 1
        self._write()

    def closed(self) -> None:
        self.open = max(0, self.open - 1)
        self._write()

    def refresh(self) -> None:
        if self.open:
            self._write()
