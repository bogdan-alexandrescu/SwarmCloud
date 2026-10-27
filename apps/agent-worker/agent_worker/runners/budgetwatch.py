"""The runner's half of the per-agent budget (BUDGET-CAP, part of #2459; `agent_worker.budget`).

Reads the CLI's stream as it is written -- fed by the stdout capture's tap,
as `cliagent.AccountStreamWatcher` is, so a stream past the capture's cap is
still read whole -- and does two things:

  * writes what usage it has seen to the CHANNEL file the worker named
    (`budget.CHANNEL_ENV`): `{usage_by_model, tokens, turns, stopped_for}`,
    replaced atomically, after every poll that saw something new;
  * reads the SIGNAL file (`budget.SIGNAL_ENV`) the worker writes, and
    answers `poll()` with why the CLI must stop now: `stop` is honoured at
    the next TURN BOUNDARY (`budget.UsageMeter.feed`), so the agent loses no
    turn it took; `kill` at once, the one mid-turn stop (a turn past 120%).

THE ROLLOUT, TOO (codex; BGD's pick "Also read the rollout", 2026-10-27).
codex's stream reports usage once per start, when its one turn ends, so a
start is also measured from the files `tap(usage_files)` lists -- codex's
rollouts -- read from the size each had when the start began: what an
earlier attempt or start wrote there is not this start's. Only lines naming
`token_count` are parsed (`UsageMeter.feed_rollout`); a link or anything
but a regular file is not read, since the agent owns its HOME.

ONE WATCHER PER RUNNER, across every start of the CLI the runner makes (the
finish pass, the repair turns): a resumed start's stream is the same
session's, and its usage is added, never counted again from zero. It never
raises on what the stream holds; a line that is not JSON is not an event.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from ..budget import (
    ACTION_KILL,
    ACTION_STOP,
    CHANNEL_ENV,
    SIGNAL_ENV,
    STOPPED_AT_BOUNDARY,
    STOPPED_MID_TURN,
    UsageMeter,
    read_json,
)


class BudgetStreamWatcher:
    """The usage of one runner's CLI, reported to the worker; the worker's stop, honoured."""

    def __init__(self, channel: Path, signal: Path) -> None:
        self._channel = channel
        self._signal = signal
        self.meter = UsageMeter()
        self._partial = b""
        self._fed: list[bytes] = []
        self._lock = threading.Lock()
        self.stopped_for: str | None = None
        self._dirty = False
        self._usage_files: Callable[[], Iterable[Path]] | None = None
        #: Per usage file: the offset read to, and the half line after it.
        self._offsets: dict[Path, int] = {}
        self._file_partial: dict[Path, bytes] = {}

    def tap(self, usage_files: Callable[[], Iterable[Path]] | None = None) -> Callable[[bytes], None]:
        """The callable one start's stdout capture feeds. A new start begins
        with no half line from the last. `usage_files` lists the files whose
        later lines carry this start's running usage (codex's rollouts)."""
        with self._lock:
            self._fed = []
        self._partial = b""
        self.meter.start()
        self._usage_files = usage_files
        self._offsets = {}
        self._file_partial = {}
        for path in self._listed():
            size = _regular_size(path)
            if size is not None:
                self._offsets[path] = size
        return self._feed

    def _listed(self) -> list[Path]:
        if self._usage_files is None:
            return []
        try:
            return list(self._usage_files())
        except OSError:
            return []

    def _rollout_records(self) -> list[dict[str, Any]]:
        """The `token_count` lines appended to the usage files since the last read."""
        records: list[dict[str, Any]] = []
        for path in self._listed():
            size = _regular_size(path)
            if size is None:
                continue
            offset = self._offsets.get(path, 0)
            if size < offset:
                # Replaced or cut short: not the file this start was reading.
                offset = 0
                self._file_partial.pop(path, None)
            if size == offset:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(size - offset)
            except OSError:
                continue
            self._offsets[path] = offset + len(chunk)
            lines = (self._file_partial.get(path, b"") + chunk).split(b"\n")
            self._file_partial[path] = lines.pop()
            for raw in lines:
                if b'"token_count"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    def _feed(self, chunk: bytes) -> None:
        with self._lock:
            self._fed.append(chunk)

    def _events(self) -> list[dict[str, Any]]:
        with self._lock:
            chunk = b"".join(self._fed)
            self._fed = []
        if not chunk:
            return []
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        events: list[dict[str, Any]] = []
        for raw in lines:
            raw = raw.strip()
            if not raw.startswith(b"{"):
                continue
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _asked(self) -> str | None:
        action = read_json(self._signal).get("action")
        return action if action in (ACTION_STOP, ACTION_KILL) else None

    def poll(self, *, honour: bool = True) -> str | None:
        """Read what is new; the action the CLI must stop for now, or None.

        `honour` False reads and reports without stopping anything: the read
        after the CLI has exited, which must not call an exit the CLI made on
        its own a stop for the budget."""
        stop: str | None = None
        for event in self._events():
            boundary = self.meter.feed(event)
            self._dirty = True
            if honour and boundary and self.stopped_for is None and self._asked() is not None:
                self.stopped_for = STOPPED_AT_BOUNDARY
                stop = ACTION_STOP
                # Nothing after this boundary is read: the CLI stops here.
                break
        # After the stream: a rollout line read once this start's
        # `turn.completed` is in is that turn's, which the stream counted.
        for record in self._rollout_records():
            before = self.meter.tokens()
            self.meter.feed_rollout(record)
            if self.meter.tokens() != before:
                self._dirty = True
        if honour and stop is None and self.stopped_for is None and self._asked() == ACTION_KILL:
            self.stopped_for = STOPPED_MID_TURN
            stop = ACTION_KILL
        if self._dirty or stop is not None:
            self.write()
        return stop

    def write(self) -> None:
        """The channel, replaced atomically. Advisory: a channel that cannot be
        written is a run the worker sees no usage of, which it runs unstopped."""
        self._dirty = False
        body = json.dumps({
            "usage_by_model": self.meter.usage_by_model(),
            "tokens": self.meter.tokens(),
            "turns": self.meter.turns,
            "stopped_for": self.stopped_for,
        })
        tmp = self._channel.with_name(f".{self._channel.name}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
            os.replace(tmp, self._channel)
        except OSError:
            pass


def _regular_size(path: Path) -> int | None:
    """The size of a regular file not reached through a link, or None."""
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def budget_watcher() -> BudgetStreamWatcher | None:
    """The watcher, when the worker named a budget channel; None otherwise,
    and then the runner runs exactly as it did before #2459."""
    channel = os.environ.get(CHANNEL_ENV, "").strip()
    signal = os.environ.get(SIGNAL_ENV, "").strip()
    if not channel or not signal:
        return None
    return BudgetStreamWatcher(Path(channel), Path(signal))
