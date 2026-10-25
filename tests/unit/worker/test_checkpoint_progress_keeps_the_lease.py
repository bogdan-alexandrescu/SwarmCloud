"""A checkpoint still making progress keeps its lease past the bound (#1808).

task_8cff36540fd44bf7839f lost three attempts in a row on 2026-10-21: each
periodic checkpoint of a growing tree (238,819 files, 291 MB, about 7.5 min)
ran past `heartbeat_meanwhile_max_seconds` (360 s, three lease timeouts), the
thread under it stopped beating, and the reconciler reclaimed a LIVE agent.
The owner's fix (2026-10-21, lane CHECKPOINT-PROGRESS), one property per test:

1. past the bound, `_heartbeat_meanwhile` keeps beating for as long as the
   checkpoint writer's progress counter (entries walked, bytes written)
   advances;
2. no advance for one lease timeout is a wedge: it stops, as it did at the
   bound before;
3. a HARD ceiling (`heartbeat_meanwhile_ceiling_seconds`) stops it whatever
   the progress;
4. a fenced attempt stops at once, extension or not (invariant 5);
5. `_checkpoint` hands the thread the manager's counter, and the extension is
   logged once with the bound, the time used and the progress seen;
6. the counter advances with the files and bytes an archive writes;
7. the regenerable test and lint caches (`.pytest_cache`, `.mypy_cache`,
   `.ruff_cache`, and `__pycache__` through `BUILD_DIRS`) are left out of a
   checkpoint wherever they sit, and the agent's work beside them is kept.

Every timing below is on a 1 s beat with a 3 s bound, so one lease timeout --
the bound's third (`config.HEARTBEAT_MEANWHILE_BOUND_LEASE_TIMEOUTS`) -- is
1 s. Tests 1-4 run the beat thread on a virtual clock (`_Clock`, through the
worker's `heartbeat_clock` and `heartbeat_wait`), so a beat lands at the same
instant on every run and no band around it is needed (#2233). Test 5 drives
the whole worker loop on the real clock; its window leaves about half a beat
of slack each way.
"""

from __future__ import annotations

import dataclasses
import io
import json
import tarfile
import threading
import time
from typing import Callable

import pytest

from agent_worker import config as config_mod
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import TOOL_CACHES, CheckpointManager
from agent_worker.config import WorkerConfig
from agent_worker.errors import ConfigError
from agent_worker.logs import build_logger

from worker_seeds import TENANT, seed_attempt


def _count_beats(worker) -> list[float]:
    """The real-clock times of the beats, for test 5's whole-loop run."""
    beats: list[float] = []
    real = worker._heartbeat

    def counted():
        beats.append(time.monotonic())
        return real()

    worker._heartbeat = counted  # type: ignore[method-assign]
    return beats


class _Clock:
    """A virtual monotonic clock the beat thread reads and waits on (#2233).

    Tests 1-4 ran on real sleeps with half a beat of slack, and on a loaded
    runner one beat landed at 7.01 s against a 7 s band (run 40089538773)
    with nothing wrong in `_heartbeat_meanwhile`. Here the thread's clock is
    `now`, and its wait moves `now` forward by the time it asked to wait,
    running any action scheduled inside that span on the way (and 1 ms for a
    wait of nothing, as a real clock would). The block ends
    at `ends_at`: the wait that reaches it returns as the real `stop` would.
    Every beat lands at the same instant on every run, so the assertions
    read the property itself, not a band around it.
    """

    def __init__(self, ends_at: float) -> None:
        self.now = 0.0
        self.ends_at = ends_at
        self.spun = False
        self._waits = 0
        self._scheduled: list[tuple[float, Callable[[], object]]] = []

    def at(self, when: float, action: Callable[[], object]) -> None:
        self._scheduled.append((when, action))
        self._scheduled.sort(key=lambda entry: entry[0])

    def monotonic(self) -> float:
        return self.now

    def wait(self, event: threading.Event, seconds: float) -> bool:
        self._waits += 1
        if self._waits > 100_000:
            # A thread that never asks for time would hang a real worker's
            # CPU; end it, and `_hold` fails the test on it.
            self.spun = True
            return True
        # A real clock moves while a thread loops, even through a wait of
        # nothing; 1 ms per such wait keeps a busy loop (the mutations below
        # make several) going forward in time rather than standing still.
        target = self.now + max(seconds, 0.001)
        while self._scheduled and self._scheduled[0][0] <= min(target, self.ends_at):
            when, action = self._scheduled.pop(0)
            self.now = when
            action()
        if target >= self.ends_at:
            self.now = self.ends_at
            return True
        self.now = target
        return event.is_set()


def _advancing_until(clock: _Clock, until: float) -> Callable[[], tuple[int, int]]:
    """A checkpoint counter that moves every 0.1 s of the clock until `until`."""

    def reading() -> tuple[int, int]:
        tenths = int(min(clock.now, until) * 10)
        return (tenths, tenths * 4096)

    return reading


def _hold(worker, clock: _Clock, progress=None, what="checkpoint (test)") -> list[float]:
    """Run `_heartbeat_meanwhile` on `clock` until it ends; the beats' times."""
    beats: list[float] = []
    real = worker._heartbeat

    def counted():
        beats.append(round(clock.now, 3))
        return real()

    worker._heartbeat = counted  # type: ignore[method-assign]
    worker.heartbeat_clock = clock.monotonic
    worker.heartbeat_wait = clock.wait
    before = set(threading.enumerate())
    with worker._heartbeat_meanwhile(what, progress=progress):
        for thread in set(threading.enumerate()) - before:
            if thread.name == "heartbeat-meanwhile":
                # Real seconds, only as a backstop: on the virtual clock the
                # thread finishes in milliseconds.
                thread.join(timeout=30)
                assert not thread.is_alive(), "the beat thread did not end on the virtual clock"
    assert not clock.spun, "the beat thread spun without waiting"
    return beats


def _gaps(beats: list[float]) -> list[float]:
    return [round(later - earlier, 2) for earlier, later in zip(beats, beats[1:])]


def _worker(db, worker_factory, **kwargs):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    defaults = dict(
        heartbeat_interval_seconds=1, checkpoint_interval_seconds=60, control_poll_seconds=60,
        heartbeat_meanwhile_max_seconds=3, heartbeat_meanwhile_ceiling_seconds=30,
    )
    defaults.update(kwargs)
    worker, _, _ = worker_factory(**defaults)
    return worker


# ---------------------------------------------------------------------------
# 1-4. the thread under a long operation, on a virtual clock
# ---------------------------------------------------------------------------


def test_a_checkpoint_that_keeps_advancing_past_the_bound_keeps_beating(db, worker_factory):
    """The #1808 case: still archiving at the bound. Before, the thread
    stopped at 3 s and nothing beat after it."""
    worker = _worker(db, worker_factory)
    clock = _Clock(ends_at=7)
    beats = _hold(worker, clock, progress=_advancing_until(clock, 7))
    past_bound = [b for b in beats if b > 3]
    # MUTATION: ignore `progress` past the bound (stop there as before) and
    # no beat lands after 3 s.
    assert len(past_bound) >= 3, (
        f"{len(past_bound)} beats past the 3 s bound while the checkpoint advanced: {past_bound}"
    )
    # Every beat within one interval of the one before: the lease never went
    # a lease timeout unextended while the checkpoint advanced.
    assert max(_gaps(beats)) <= 1, beats


def test_a_checkpoint_that_stops_advancing_stops_beating_one_lease_timeout_later(
    db, worker_factory
):
    """Advancing until 5 s, then silent until 10 s: a wedge from 5 s. The
    thread beats past the bound while it advances and stops one lease timeout
    (1 s) after the last advance it saw."""
    worker = _worker(db, worker_factory)
    clock = _Clock(ends_at=10)
    beats = _hold(worker, clock, progress=_advancing_until(clock, 5))
    extended = [b for b in beats if 3 < b <= 5]
    after_wedge = [b for b in beats if b > 5 + 1]
    # MUTATION: ignore `progress` past the bound and `extended` is empty --
    # the control that shows the extension was live before the wedge.
    assert extended, f"no beat past the bound while the checkpoint still advanced: {beats}"
    assert max(_gaps(beats)) <= 1, beats
    # MUTATION: keep beating past the bound until the ceiling whether or not
    # the counter moves, and beats land at 6, 7, 8 and 9 s.
    assert not after_wedge, (
        f"beats more than a lease timeout after the checkpoint stopped advancing: {after_wedge}"
    )


def test_the_hard_ceiling_stops_the_beating_even_while_the_checkpoint_advances(
    db, worker_factory
):
    """Ceiling 5 s, advancing for the whole 9 s: no beat at or after the ceiling."""
    worker = _worker(db, worker_factory, heartbeat_meanwhile_ceiling_seconds=5)
    clock = _Clock(ends_at=9)
    beats = _hold(worker, clock, progress=_advancing_until(clock, 9))
    extended = [b for b in beats if 3 < b < 5]
    past_ceiling = [b for b in beats if b >= 5]
    # MUTATION: ignore `progress` and `extended` is empty (the control).
    assert extended, f"no beat between the bound and the ceiling while the checkpoint advanced: {beats}"
    # MUTATION: drop the ceiling, and beats land at 5, 6, 7 and 8 s.
    assert not past_ceiling, f"beats at or past the 5 s ceiling: {past_ceiling}"


def test_a_fenced_attempt_stops_beating_at_once_even_while_the_checkpoint_advances(
    db, worker_factory
):
    """Past the bound and advancing, the task's generation moves on at 5.5 s:
    the thread asks the control plane before every beat and stops on the
    first ask after the fence. A stale generation never keeps a lease
    (invariant 5)."""
    worker = _worker(db, worker_factory)
    clock = _Clock(ends_at=10)
    fenced_at = 5.5
    clock.at(fenced_at, lambda: db.doc("tasks/task_1").update({"current_generation": 2}))
    beats = _hold(worker, clock, progress=_advancing_until(clock, 10))
    before_fence = [b for b in beats if 3 < b < fenced_at]
    after_fence = [b for b in beats if b > fenced_at]
    # MUTATION: ignore `progress` and `before_fence` is empty (the control).
    assert before_fence, f"no beat past the bound before the fence: {beats}"
    # MUTATION: skip the poll on an extended beat (the progress vouches for
    # it) and beats land at 6, 7, 8 and 9 s.
    assert not after_fence, f"beats after the attempt was fenced: {after_fence}"


def test_without_a_progress_counter_the_bound_is_as_it_was(db, worker_factory):
    """Every other long operation (a clone, a carrier push) passes no
    counter: it stops at the bound, exactly as before #1808."""
    worker = _worker(db, worker_factory)
    clock = _Clock(ends_at=6)
    beats = _hold(worker, clock, what="clone")
    past_bound = [b for b in beats if b > 3]
    # The control: it did beat, up to the bound.
    assert beats, "no beat at all before the bound"
    # MUTATION: extend without a counter (treat None as advancing) and beats
    # land at 3, 4 and 5 s.
    assert not past_bound, f"beats past the bound with no progress counter: {past_bound}"


# ---------------------------------------------------------------------------
# 5. the checkpoint's own call site, and its one log event
# ---------------------------------------------------------------------------


def _records(log_stream) -> list[dict]:
    out = []
    for line in log_stream.getvalue().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def test_the_checkpoint_hands_its_progress_to_the_heartbeat_and_logs_the_extension(
    db, worker_factory, log_stream
):
    """`_checkpoint` passes the manager's counter: a stand-in create that
    advances it for 6 s keeps its lease past the 3 s bound, and the worker
    logs ONE event naming the bound, the time used and the progress seen."""
    worker = _worker(db, worker_factory)
    beats = _count_beats(worker)
    real_create = worker.checkpoints.create
    window: list[float] = []

    def slow_create(ws, *, label="periodic"):
        if window:
            return real_create(ws, label=label)
        start = time.monotonic()
        end = start + 6
        while time.monotonic() < end:
            worker.checkpoints.progress.files += 1
            worker.checkpoints.progress.bytes += 4096
            time.sleep(0.1)
        window.append(start)
        return real_create(ws, label=label)

    worker.checkpoints.create = slow_create  # type: ignore[method-assign]
    worker.run()
    assert window, "no checkpoint was written"
    start = window[0]
    past_bound = [round(b - start, 2) for b in beats if start + 3.5 < b < start + 6]
    # MUTATION: call `_heartbeat_meanwhile` in `_checkpoint` without
    # `progress=` and nothing beats past the bound.
    assert past_bound, "the checkpoint's own heartbeat stopped at the bound while it advanced"
    events = [r for r in _records(log_stream) if r.get("event") == "heartbeat_extended"]
    # MUTATION: drop the log line (none), or log it on every extended beat
    # (several).
    assert len(events) == 1, events
    event = events[0]
    assert event["bound_seconds"] == 3
    assert event["ceiling_seconds"] == 30
    assert event["during"].startswith("checkpoint (")
    # MUTATION: log the bound as the time used, or the reading at the start
    # as the progress, and these fail.
    assert event["seconds"] >= 5.5, event
    # (The real create after the stand-in's 6 s adds its own entries and
    # bytes inside the same block, so these are floors.)
    assert event["files"] >= 40 and event["bytes"] >= 40 * 4096, event
    assert event["ended"] == "finished", event


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _from_env(monkeypatch, **env: str) -> WorkerConfig:
    for name in ("HEARTBEAT_INTERVAL_SECONDS", "HEARTBEAT_GRACE_SECONDS", "LEASE_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    base = {
        "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
        "TENANT_ID": "eng", "GENERATION": "1", "RUNNER_PROFILE": "mock",
        "PROJECT_ID": "swarm-test",
    }
    base.update(env)
    for name, value in base.items():
        monkeypatch.setenv(name, value)
    return WorkerConfig.from_env()


def test_the_ceiling_is_four_bounds_and_never_under_the_bound(monkeypatch):
    cfg = _from_env(monkeypatch)
    assert cfg.heartbeat_meanwhile_max_seconds == 360
    # MUTATION: derive the ceiling from anything but four bounds and this
    # reads another number.
    assert cfg.heartbeat_meanwhile_ceiling_seconds == 4 * 360
    assert config_mod.HEARTBEAT_MEANWHILE_BOUND_LEASE_TIMEOUTS == 3
    # MUTATION: drop the validation and a ceiling under the bound is accepted.
    with pytest.raises(ConfigError, match="ceiling"):
        dataclasses.replace(cfg, heartbeat_meanwhile_ceiling_seconds=300)


# ---------------------------------------------------------------------------
# 6. the counter
# ---------------------------------------------------------------------------


def _manager(store, log_stream):
    logger = build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="claude-code", stream=log_stream,
    )
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=logger,
    )


def _write(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_the_progress_counter_advances_with_the_entries_and_bytes_an_archive_writes(
    store, tmp_path, log_stream
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    for index in range(30):
        _write(ws.work / f"src/m{index}.py", "x = 1\n" * 200)
    manager = _manager(store, log_stream)
    first = manager.progress.reading()
    assert first == (0, 0)
    manager.create(ws)
    second = manager.progress.reading()
    # MUTATION: count nothing in the walk, or nothing written by the archive
    # file, and one of these stays at zero.
    assert second[0] >= 30, second
    assert second[1] > 0, second
    # Monotonic across checkpoints: a second (incremental, nearly empty) one
    # only adds. MUTATION: reset the counter per checkpoint and this fails.
    _write(ws.work / "src/new.py")
    manager.create(ws)
    third = manager.progress.reading()
    assert third[0] > second[0] and third[1] > second[1], (second, third)


# ---------------------------------------------------------------------------
# 7. the regenerable test and lint caches are left out
# ---------------------------------------------------------------------------


def _names(store, record):
    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return archive.getnames()


@pytest.mark.parametrize("cache", [".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__"])
def test_each_test_and_lint_cache_is_left_out_and_the_work_beside_it_is_kept(
    store, tmp_path, log_stream, cache
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    work, checkout = ws.work, ws.checkout()
    # Where the tools write them: the checkout's root, a package below it,
    # and HOME itself.
    _write(checkout / cache / "v/cache/lastfailed", "{}\n")
    _write(checkout / "apps/pkg" / cache / "entry", "cached\n")
    _write(work / cache / "entry", "cached\n")
    # The agent's work beside each one.
    _write(checkout / "tests/test_app.py", "def test_x():\n    pass\n")
    _write(checkout / "apps/pkg/module.py", "x = 1\n")
    _write(work / "notes.md", "half done\n")
    # A file whose name only starts like the cache is not the cache.
    _write(checkout / f"{cache}.md", "a note about the cache\n")

    record = _manager(store, log_stream).create(ws)
    names = _names(store, record)
    for left_out in (f"repo/{cache}", f"repo/apps/pkg/{cache}", cache):
        leaked = [n for n in names if n == left_out or n.startswith(left_out + "/")]
        # MUTATION: drop this entry from `TOOL_CACHES` (or `__pycache__` from
        # `BUILD_DIRS`) and its paths are archived.
        assert not leaked, f"{left_out} was archived: {leaked}"
    for kept in ("repo/tests/test_app.py", "repo/apps/pkg/module.py", "notes.md",
                 f"repo/{cache}.md"):
        # MUTATION: match by prefix instead of by whole name and the `.md`
        # beside it is lost.
        assert kept in names, f"{kept} is the agent's work and was left out: {names}"


def test_the_added_caches_match_anywhere_and_nothing_unsafe_was_added():
    """The three added entries are `**/` (anywhere in `work/`), and none of
    the directories the owner said never to add on a guess is in the tuple."""
    for cache in (".pytest_cache", ".mypy_cache", ".ruff_cache"):
        # MUTATION: add it HOME-relative and the checkout's copy is archived
        # (the parametrized test above goes red too).
        assert f"**/{cache}" in TOOL_CACHES, TOOL_CACHES
    names = {entry.rsplit("/", 1)[-1] for entry in TOOL_CACHES}
    # MUTATION: add `.venv`, `dist`, `build` or a database's data directory.
    assert not names & {".venv", "venv", "dist", "build", "pgdata", "data", "postgres"}, names
