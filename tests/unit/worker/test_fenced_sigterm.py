"""A superseded worker leaves the task and the lease alone on its way out.

WHAT WAS WRONG. Found by the browser-eviction lane (PR #41), read from the code.
Invariant 5 was enforced at two points: before the agent starts
(`validate_generation`) and on each control poll while it runs. It was not
enforced on the paths that END an attempt, and a worker can reach those
between two polls:

  * SIGTERM. The reconciler deleting a Job and the platform reclaiming an
    instance both arrive this way. `_handle_interruption` checkpointed and
    parked the task SCHEDULED_RETRY without checking the fence.
      - Fenced, not yet re-leased: the stale worker parked the task over the
        fence. It cleared `current_lease_id` and released its own lease, which
        the reconciler was holding until the Job was gone. Nothing in the
        platform promotes a SCHEDULED_RETRY park, so the task stayed there.
      - Re-leased, new attempt RUNNING: RUNNING -> PARKED is legal, so the
        stale worker parked the new attempt's task.
      - Re-leased, new attempt still LEASED: LEASED -> PARKED is illegal. The
        InvalidTransition reached `run()`'s crash handler, and its
        `_safe_finish` wrote FAILED over the new attempt.
  * a crash. `_safe_finish` writes FAILED over whatever the task is now.
  * a runner that exits before the next poll. `_finalise` wrote the stale
    attempt's SUCCEEDED over the fenced task.

Each of those writes read the task and then wrote it blind, so a check before
the write would still lose to a fence that lands between the two. The tests
below require the check to be inside the transaction that writes.

  * a park that is announced before it is made. `_park_for_quota` and
    `_park_no_account` emitted QUOTA_EXHAUSTED into the task's stream, THEN
    parked. A fence that landed during the park's uploads was met by the park,
    which refused, but the announcement was already in a stream that now
    belongs to a newer generation. The tests at the bottom require the
    announcement to commit with the park or not at all.

WHAT IS PINNED. After the fence lands, the stale worker writes only its own
attempt document. This is measured on `FakeFirestore.writes`, the list of every
write made, rather than on chosen fields. A field-by-field check is how a
`latest_checkpoint` overwrite, or an event under someone else's attempt, gets
past a test. The one exception is the fence that the periodic checkpoint meets
mid-run. That is the RUNNING path, and it emits the same `generation_fenced`
event the control poll always has. Its test allows events and nothing else.

HOW THE SIGTERM IS DELIVERED. `_interrupted` is set directly, which is all the
worker's SIGTERM handler does. A real signal to the test process would kill it
wherever the handler is not installed.

HOW THE RACE IS HELD STILL. The heartbeat, the periodic checkpoint and the
control poll are set to 60s (`QUIET`). Between the fence and the interruption,
the supervision loop therefore writes nothing and polls nothing. The worker
cannot find the fence on the mid-run path, which was always correct, and has to
meet it on the way out, which is the path under test. The tests that need one of
those three running hold it behind a lock while the world changes.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

import pytest
from fakes import (
    FakeCollectionRef,
    FakeDocumentRef,
    FakeFirestore,
    FakeSnapshot,
    FakeTransaction,
)

from agent_worker.accountlease import NoAccount
from agent_worker.errors import ExitCode
from agent_worker.runners.mock import PROGRESS_DIR
from swarm_common.admission import release_lease_in_transaction
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from conftest import build_worker, seed_attempt

#: No heartbeat, no periodic checkpoint and no control poll while the runner
#: works, so nothing the worker does between the fence and the SIGTERM can
#: either write or discover the fence. See the module docstring.
QUIET = dict(
    heartbeat_interval_seconds=60,
    checkpoint_interval_seconds=60,
    control_poll_seconds=60,
    timeout_seconds=60,
)

#: A mock runner that works for about twenty seconds in half-second steps. That
#: is long enough that it is still running whenever the test acts on it,
#: including on a slow CI host.
LONG_RUN = {"prompt": "long", "steps": 40, "sleep_seconds": 20.0}

#: A mock runner that exits on its own about two seconds in.
SHORT_RUN = {"prompt": "short", "steps": 4, "sleep_seconds": 2.0}

#: A mock runner that reports a thirty-minute rate limit and exits at once.
#: Thirty minutes is far past `max_in_worker_retry_delay_seconds` (45), so the
#: worker parks rather than retrying in place.
QUOTA_RUN = {
    "prompt": "burn quota",
    "steps": 1,
    "sleep_seconds": 0.05,
    "quota_exhausted": True,
    "provider": "anthropic",
    "retry_after_seconds": 1800,
}


# ---------------------------------------------------------------------------
# the world the stale worker finds
# ---------------------------------------------------------------------------


def fence(db: FakeFirestore) -> None:
    """The write `ControlStore.invalidate_generation` makes: the generation, bumped.

    The browser-eviction repair does exactly this and nothing else in its first
    pass. It leaves the lease held and the state alone until a later pass has
    seen the Job go.
    """
    task = db.doc("tasks/task_1")
    task["current_generation"] = int(task["current_generation"]) + 1
    task["updated_at"] = utcnow()


def re_lease(db: FakeFirestore, *, state: TaskState) -> None:
    """The reconciler reclaims generation 1, and the scheduler admits generation 2.

    The fence comes first, then the release through the frozen admission module,
    so the pools are decremented for real. Then admission: a new lease at the new
    generation, its units counted on every pool, and the task pointing at it in
    `state`. LEASED is a new attempt not yet started. RUNNING is one that has
    already advanced.
    """
    fence(db)
    release_lease_in_transaction(
        FakeTransaction(db), db=db, lease_id="lease_1", reason="reconciler:test"
    )
    old = db.doc("leases/lease_1")
    now = utcnow()
    task = db.doc("tasks/task_1")
    db.seed(
        "leases/lease_2",
        {
            "lease_id": "lease_2",
            "task_id": "task_1",
            "attempt_id": "att_2",
            "tenant_id": old["tenant_id"],
            "generation": task["current_generation"],
            "pools": list(old["pools"]),
            "units": old["units"],
            "state": state.value,
            "created_at": now,
            "dispatch_deadline": now + timedelta(seconds=300),
            "expires_at": now + timedelta(seconds=120),
            "heartbeat_at": None,
            "released_at": None,
        },
    )
    for name in old["pools"]:
        pool = db.doc(f"pools/{name}")
        pool["active"] = int(pool["active"]) + int(old["units"])
    task.update(
        state=state.value,
        current_lease_id="lease_2",
        attempt_count=int(task.get("attempt_count", 1)) + 1,
        updated_at=now,
    )


@dataclass
class World:
    """Every document except the attempts, as they stood the moment the fence landed."""

    docs: dict[str, dict[str, Any]]
    writes_before: int


def freeze(db: FakeFirestore) -> World:
    return World(
        docs={
            path: copy.deepcopy(doc)
            for path, doc in list(db.documents.items())
            if not path.startswith("attempts/")
        },
        writes_before=len(db.writes),
    )


def assert_left_alone(
    db: FakeFirestore, world: World, *, events_allowed: bool = False
) -> None:
    """Since `world` was frozen, the worker wrote its own attempt document and nothing else.

    `events_allowed` is for the mid-run fence only, which records
    `generation_fenced` in the task's event stream the way the control poll
    always has. Even there, the task document, the leases and the pools must
    not change.
    """
    task_now = db.doc("tasks/task_1")
    task_then = world.docs["tasks/task_1"]
    assert task_now.get("state") == task_then.get("state"), (
        f"the stale worker wrote {task_now.get('state')} over a task at "
        f"{task_then.get('state')}, generation {task_then.get('current_generation')}, "
        f"lease {task_then.get('current_lease_id')}"
    )
    changed = sorted(p for p, doc in world.docs.items() if db.documents.get(p) != doc)
    assert changed == [], f"documents the stale worker changed after it was fenced: {changed}"

    def is_own(path: str) -> bool:
        if path == "attempts/att_1":
            return True
        return events_allowed and path.startswith("tasks/task_1/events/")

    written = [path for _, path, _ in db.writes[world.writes_before:]]
    foreign = sorted({path for path in written if not is_own(path)})
    assert foreign == [], f"the stale worker wrote {foreign} after it was fenced"
    created = sorted(p for p in db.documents if p not in world.docs and not is_own(p))
    assert created == [], f"documents the stale worker created after it was fenced: {created}"


def assert_the_attempt_says_it_stood_down(db: FakeFirestore) -> None:
    """The one document that IS the stale worker's records why it left."""
    attempt = db.doc("attempts/att_1")
    assert attempt.get("exit_code") == ExitCode.GENERATION_FENCED, attempt
    assert "generation" in str(attempt.get("error") or ""), attempt
    assert attempt.get("completed_at") is not None, attempt


# ---------------------------------------------------------------------------
# driving the worker
# ---------------------------------------------------------------------------


def runner_is_working(worker: Any) -> bool:
    """The runner is alive and has finished a step, so its SIGTERM handler is in."""
    ws = worker.ws
    child = worker._child
    return (
        ws is not None
        and child is not None
        and child.poll() is None
        and (ws.work / PROGRESS_DIR / "step-0001.txt").exists()
    )


class Trigger:
    """Runs `action` once, from another thread, as soon as the runner is working."""

    def __init__(self, worker: Any, action: Callable[[], None]) -> None:
        self.fired = threading.Event()
        self.error: BaseException | None = None

        def watch() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if runner_is_working(worker):
                    try:
                        action()
                    except BaseException as exc:  # surfaced by `join`
                        self.error = exc
                    self.fired.set()
                    return
                time.sleep(0.02)

        self._thread = threading.Thread(target=watch, daemon=True)
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=60)
        if self.error is not None:
            raise self.error
        assert self.fired.is_set(), "the runner never started working; nothing was tested"


def sigterm(worker: Any) -> None:
    """All the worker's SIGTERM handler does."""
    worker._interrupted = True


# ---------------------------------------------------------------------------
# SIGTERM
# ---------------------------------------------------------------------------


def test_a_fenced_worker_sigtermed_leaves_the_task_and_the_lease_untouched(
    db, worker_factory, store
):
    """The browser-eviction shape: fenced first, the Job deleted after.

    The reconciler has bumped the generation and is holding the lease until it
    has seen the Job go. The stale worker must stand down. It must not
    checkpoint over the task's pointer, park the task, clear its lease pointer,
    release the lease or emit into the task's stream.
    """
    seed_attempt(db, task_input=LONG_RUN, pool_active=3)
    worker, _, _ = worker_factory(**QUIET)
    frozen: dict[str, World] = {}

    def fence_then_sigterm() -> None:
        fence(db)
        frozen["world"] = freeze(db)
        sigterm(worker)

    trigger = Trigger(worker, fence_then_sigterm)
    exit_code = worker.run()
    trigger.join()

    world = frozen["world"]
    assert_left_alone(db, world)
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("pools/global")["active"] == 3
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_the_attempt_says_it_stood_down(db)
    # No checkpoint of stale work was uploaded for a later attempt to find.
    assert [k for k in store.list_keys("tenants/") if "/checkpoints/" in k] == []


def test_an_unfenced_worker_sigtermed_still_checkpoints_parks_and_gives_the_slot_back(
    db, worker_factory
):
    """What must NOT change. The fence check is a gate on the way out, not a
    new way out: a worker that still owns its task parks exactly as before.

    This cannot be red first. It passes on the old code by design, and it is
    what fails if the fix over-reaches.
    """
    seed_attempt(db, task_input=LONG_RUN, pool_active=5)
    worker, _, _ = worker_factory(**QUIET)
    units = int(db.doc("leases/lease_1")["units"])
    pools = list(db.doc("leases/lease_1")["pools"])

    trigger = Trigger(worker, lambda: sigterm(worker))
    exit_code = worker.run()
    trigger.join()

    assert exit_code == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.SCHEDULED_RETRY.value
    assert task["current_lease_id"] is None
    assert task["latest_checkpoint"], "the interrupted attempt must leave a checkpoint behind"
    assert db.doc("leases/lease_1")["released_at"] is not None
    for name in pools:
        assert db.doc(f"pools/{name}")["active"] == 5 - units, name
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked and parked[-1]["detail"].get("cause") == "worker_interrupted", parked
    assert EventType.LEASE_RELEASED.value in db.event_types("task_1")


@pytest.mark.parametrize(
    "new_attempt_state",
    [TaskState.LEASED, TaskState.RUNNING],
    ids=["new-attempt-leased", "new-attempt-running"],
)
def test_a_stale_worker_sigtermed_never_overwrites_the_task_a_newer_attempt_holds(
    db, worker_factory, new_attempt_state
):
    """The scheduler has already admitted generation 2 when generation 1 is stopped.

    LEASED: the old code's park was an illegal LEASED -> PARKED, and the crash
    handler that caught it wrote FAILED over the new attempt. RUNNING: the park
    was legal, and the new attempt's task was parked out from under it.
    """
    seed_attempt(db, task_input=LONG_RUN, pool_active=4)
    worker, _, _ = worker_factory(**QUIET)
    frozen: dict[str, World] = {}

    def re_lease_then_sigterm() -> None:
        re_lease(db, state=new_attempt_state)
        frozen["world"] = freeze(db)
        sigterm(worker)

    trigger = Trigger(worker, re_lease_then_sigterm)
    exit_code = worker.run()
    trigger.join()

    world = frozen["world"]
    assert_left_alone(db, world)
    assert db.doc("tasks/task_1")["current_lease_id"] == "lease_2"
    assert db.doc("leases/lease_2")["released_at"] is None
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_the_attempt_says_it_stood_down(db)


# ---------------------------------------------------------------------------
# the other ways out: a crash, and a runner that finishes on its own
# ---------------------------------------------------------------------------


def test_a_stale_worker_that_crashes_never_writes_failed_over_a_newer_attempt(
    db, worker_factory
):
    """`_safe_finish` is the crash handler, and it wrote FAILED over anything.

    The crash is the heartbeat write raising, which is how a Firestore outage
    reaches the supervision loop. Heartbeats run every second here, and the
    whole heartbeat (lease write and event) sits behind a lock, so none can land
    between the re-lease and the freeze.
    """
    seed_attempt(db, task_input=LONG_RUN, pool_active=4)
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=1,
        checkpoint_interval_seconds=60,
        control_poll_seconds=60,
        timeout_seconds=60,
    )
    gate = threading.Lock()
    crashed = threading.Event()
    real_heartbeat = worker._heartbeat

    def heartbeat() -> None:
        with gate:
            if crashed.is_set():
                raise RuntimeError("the heartbeat write failed")
            real_heartbeat()

    worker._heartbeat = heartbeat  # type: ignore[method-assign]
    frozen: dict[str, World] = {}

    def re_lease_then_crash() -> None:
        with gate:
            re_lease(db, state=TaskState.LEASED)
            frozen["world"] = freeze(db)
            crashed.set()

    trigger = Trigger(worker, re_lease_then_crash)
    exit_code = worker.run()
    trigger.join()

    assert_left_alone(db, frozen["world"])
    assert db.doc("tasks/task_1")["state"] == TaskState.LEASED.value
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_the_attempt_says_it_stood_down(db)


def test_a_fenced_worker_whose_runner_finishes_before_the_next_poll_does_not_write_its_result(
    db, worker_factory
):
    """`_finalise`: the runner exits on its own a second after the fence lands.

    No poll runs in that second, so the old code checkpointed over the task's
    pointer and wrote SUCCEEDED over a task a newer generation owns.
    """
    seed_attempt(db, task_input=SHORT_RUN, pool_active=3)
    worker, _, _ = worker_factory(**QUIET)
    frozen: dict[str, World] = {}

    def fence_now() -> None:
        fence(db)
        frozen["world"] = freeze(db)

    trigger = Trigger(worker, fence_now)
    exit_code = worker.run()
    trigger.join()

    assert_left_alone(db, frozen["world"])
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_1")["released_at"] is None
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_the_attempt_says_it_stood_down(db)


def test_a_fence_the_periodic_checkpoint_meets_stops_the_runner_like_the_poll_would(
    db, worker_factory
):
    """The periodic checkpoint writes the task's `latest_checkpoint`, so it is
    fenced like every other task write. A worker that meets the fence there
    has found it mid-run. It stops the runner and records `generation_fenced`
    with phase `running`, exactly as when the control poll finds it.

    The old code pointed the task at the stale checkpoint, let the runner work
    on for its full twenty seconds, then wrote SUCCEEDED.
    """
    seed_attempt(db, task_input=LONG_RUN, pool_active=3)
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=60,
        checkpoint_interval_seconds=1,
        control_poll_seconds=60,
        timeout_seconds=60,
    )
    gate = threading.Lock()
    real_checkpoint = worker._checkpoint

    def checkpoint(label: str) -> Any:
        with gate:
            return real_checkpoint(label)

    worker._checkpoint = checkpoint  # type: ignore[method-assign]
    frozen: dict[str, World] = {}

    def fence_between_checkpoints() -> None:
        with gate:
            fence(db)
            frozen["world"] = freeze(db)

    started = time.monotonic()
    trigger = Trigger(worker, fence_between_checkpoints)
    exit_code = worker.run()
    trigger.join()
    elapsed = time.monotonic() - started

    assert_left_alone(db, frozen["world"], events_allowed=True)
    assert exit_code == ExitCode.GENERATION_FENCED
    fenced = [e for e in db.events("task_1") if e["type"] == EventType.GENERATION_FENCED.value]
    assert fenced and fenced[-1]["detail"]["phase"] == "running", fenced
    assert elapsed < 15, f"the runner worked on for {elapsed:.1f}s after its task was fenced"


# ---------------------------------------------------------------------------
# the check and the write are ONE transaction
# ---------------------------------------------------------------------------


class _Aborted(Exception):
    """A commit refused because a document the transaction read has changed."""


class _PointInTimeRef(FakeDocumentRef):
    """A read returns the document AS IT WAS, then lets a concurrent writer in.

    `FakeDocumentRef.get` hands back a snapshot that aliases the live dict, so a
    write made after the read would show up in it. That would hide exactly the
    interleaving this file exists to test.
    """

    def get(self, *_args: Any, **_kwargs: Any) -> FakeSnapshot:
        data = self._db.documents.get(self.path)
        snap = FakeSnapshot(self.path, copy.deepcopy(data) if data is not None else None)
        self._db.after_read(self.path)
        return snap


class _PointInTimeCollection(FakeCollectionRef):
    def document(self, doc_id: str | None = None) -> FakeDocumentRef:
        return _PointInTimeRef(self._db, super().document(doc_id).path)


class ContendedFirestore(FakeFirestore):
    """`FakeFirestore` plus a one-shot hook that runs right after a document is read."""

    def __init__(self) -> None:
        super().__init__()
        self._after_read: dict[str, Callable[[], None]] = {}

    def collection(self, name: str) -> FakeCollectionRef:
        return _PointInTimeCollection(self, name)

    def interleave(self, path: str, writer: Callable[[], None]) -> None:
        """Run `writer` once, right after the next read of `path` returns."""
        self._after_read[path] = writer

    def after_read(self, path: str) -> None:
        writer = self._after_read.pop(path, None)
        if writer is not None:
            writer()


class _ContendedTransaction:
    """Reads go through; writes wait for a commit that checks nothing it read moved."""

    def __init__(self, db: ContendedFirestore) -> None:
        self._db = db
        self._read: dict[str, Any] = {}
        self._writes: list[Callable[[], None]] = []

    def get(self, ref: Any, **_kwargs: Any) -> Any:
        snap = ref.get()
        self._read.setdefault(ref.path, snap.to_dict())
        return snap

    def set(self, ref: Any, data: dict[str, Any], merge: bool = False) -> None:
        self._writes.append(lambda: ref.set(data, merge=merge))

    def update(self, ref: Any, data: dict[str, Any]) -> None:
        self._writes.append(lambda: ref.update(data))

    def commit(self) -> None:
        for path, seen in self._read.items():
            if self._db.documents.get(path) != seen:
                raise _Aborted(f"{path} changed after this transaction read it")
        for write in self._writes:
            write()


class ContendedTransactionRunner:
    """Firestore's transaction contract, single-threaded.

    A commit whose read set changed is aborted and the body is run again against
    fresh reads, as `firestore.transactional` does. Production Firestore locks
    rather than aborting, and both give the one guarantee asserted here: the
    branch that commits was chosen from a state nobody changed in between. The
    same model as tests/unit/control_plane/test_request_cancel_is_transactional.py,
    built on the worker's fakes.
    """

    def __init__(self, db: ContendedFirestore, max_attempts: int = 5) -> None:
        self._db = db
        self._max_attempts = max_attempts
        self.aborted: list[str] = []

    def run(self, fn: Callable[[Any], Any]) -> Any:
        for _ in range(self._max_attempts):
            txn = _ContendedTransaction(self._db)
            result = fn(txn)
            try:
                txn.commit()
            except _Aborted as exc:
                self.aborted.append(str(exc))
                continue
            return result
        raise RuntimeError("the transaction was contended on every attempt")


def test_a_fence_that_lands_between_the_parks_read_and_its_write_is_still_honoured(
    store, tmp_path, log_stream
):
    """The case a pre-check cannot close: the worker is NOT fenced when it
    decides to park, and IS fenced by the time the park is written.

    The fence is made by a concurrent writer that runs right after the park's
    own read of the task. A read-then-blind-write writes PARKED over it. A check
    made before the park and not repeated also writes PARKED over it. Only a
    check made inside the park's transaction sees its commit refused, re-reads,
    and stands down.
    """
    db = ContendedFirestore()
    seed_attempt(db, task_input=LONG_RUN, pool_active=3)
    worker, _, _ = build_worker(
        db, store, tmp_path, log_stream, txn_runner=ContendedTransactionRunner(db), **QUIET
    )
    frozen: dict[str, World] = {}

    def fence_now() -> None:
        fence(db)
        frozen["world"] = freeze(db)

    real_park = worker.control.park

    def park(**kwargs: Any) -> None:
        db.interleave("tasks/task_1", fence_now)
        real_park(**kwargs)

    worker.control.park = park  # type: ignore[method-assign]

    trigger = Trigger(worker, lambda: sigterm(worker))
    exit_code = worker.run()
    trigger.join()

    assert "world" in frozen, "the park never read the task, so no fence was interleaved"
    assert db.doc("tasks/task_1")["current_generation"] == 2
    assert_left_alone(db, frozen["world"])
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("pools/global")["active"] == 3
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_the_attempt_says_it_stood_down(db)


# ---------------------------------------------------------------------------
# a park's announcement commits with the park, or not at all
# ---------------------------------------------------------------------------
#
# QUOTA_EXHAUSTED says "this task is parking on quota". Both parks that write
# it used to emit it first and park second, with the park's checkpoint and
# uploads before both. The uploads can take minutes. A fence that landed among
# them was met by the park, which refused and stood down, and the announcement
# of a park that never happened stayed in a stream that now belongs to a newer
# generation.
#
# The first two tests put the fence where the old code's announcement came
# next: after the last thing the park did before it (the provider publish on
# the quota park, the metrics export on the account park). The third lands the
# fence inside the park's own transaction, just after its read of the task.
# Every check made before that read passes, because nothing is fenced yet. So
# an announcement written before it is written, and the park it announces is
# then refused.


def quota_exhausted_events(db: FakeFirestore) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == EventType.QUOTA_EXHAUSTED.value]


class _SpentPool:
    """An account pool whose accounts are all spent. It never assigns one."""

    def __init__(self) -> None:
        self.assigns = 0
        self.releases = 0

    def assign(self, provider: Any, *, exclude: Any = ()) -> NoAccount:
        self.assigns += 1
        return NoAccount(reason="no_account_available")

    def release(self, *_args: Any, **_kwargs: Any) -> int:
        self.releases += 1
        return 0


def park_began(worker: Any, label: str) -> threading.Event:
    """Set once the park's own checkpoint (labelled `label`) has returned."""
    began = threading.Event()
    real_checkpoint = worker._checkpoint

    def checkpoint(name: str) -> Any:
        record = real_checkpoint(name)
        if name == label:
            began.set()
        return record

    worker._checkpoint = checkpoint  # type: ignore[method-assign]
    return began


def test_a_quota_park_the_fence_overtakes_does_not_announce_itself(db, worker_factory):
    """`_park_for_quota`: checkpoint, upload, publish the provider, ANNOUNCE, park.

    The fence lands after the provider publish. The old code then wrote
    QUOTA_EXHAUSTED into the task's stream, and the park refused.
    """
    seed_attempt(db, task_input=QUOTA_RUN, pool_active=3)
    worker, _, _ = worker_factory(**QUIET)
    parking = park_began(worker, "quota-park")
    frozen: dict[str, World] = {}
    real_publish = worker.control.update_quota_state

    def publish(**kwargs: Any) -> None:
        real_publish(**kwargs)
        # The runner loop publishes once before it decides to park. Only the
        # park's own publish, after its checkpoint, is the moment under test.
        if parking.is_set() and "world" not in frozen:
            fence(db)
            frozen["world"] = freeze(db)

    worker.control.update_quota_state = publish  # type: ignore[method-assign]

    exit_code = worker.run()

    assert "world" in frozen, "the park never published the provider, so no fence was placed"
    assert quota_exhausted_events(db) == [], (
        "QUOTA_EXHAUSTED announced a park that the fence refused: "
        f"{quota_exhausted_events(db)}"
    )
    assert_left_alone(db, frozen["world"])
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("pools/global")["active"] == 3
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_the_attempt_says_it_stood_down(db)


def test_an_account_park_the_fence_overtakes_does_not_announce_itself(db, worker_factory):
    """`_park_no_account`: checkpoint, upload, export, ANNOUNCE, park.

    claude-code, because it is the profile an account can serve, so it is the
    one that asks the pool. The pool has accounts and every one is spent. That
    is a park, not a fallback to the tenant secret.
    """
    seed_attempt(
        db,
        runner_profile="claude-code",
        task_input={"prompt": "waits for an account", "steps": 1, "sleep_seconds": 0.05},
        pool_active=3,
    )
    worker, _, _ = worker_factory(runner_profile="claude-code", **QUIET)
    pool = _SpentPool()
    worker._account_broker = pool
    parking = park_began(worker, "no-account")
    frozen: dict[str, World] = {}
    real_export = worker._export_metrics

    def export() -> None:
        real_export()
        if parking.is_set() and "world" not in frozen:
            fence(db)
            frozen["world"] = freeze(db)

    worker._export_metrics = export  # type: ignore[method-assign]

    exit_code = worker.run()

    assert pool.assigns == 1, "the worker never asked the pool, so this was not an account park"
    assert pool.releases == 0, "nothing was assigned, so nothing may be released"
    assert "world" in frozen, "the park never exported its metrics, so no fence was placed"
    assert quota_exhausted_events(db) == [], (
        "QUOTA_EXHAUSTED announced a park that the fence refused: "
        f"{quota_exhausted_events(db)}"
    )
    assert_left_alone(db, frozen["world"])
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_1")["released_at"] is None
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_the_attempt_says_it_stood_down(db)


def test_a_quota_parks_announcement_commits_with_the_park_or_not_at_all(
    store, tmp_path, log_stream
):
    """The fence lands between the park's read of the task and its commit.

    The worker owns its task when it decides to park, and it still owns it
    when the park reads the task. So an announcement written at any point
    before that read is written, including one made right after a passing
    ownership check. The park's commit is then refused, the retry finds the
    fence, and the park never happens. An announcement written in the park's
    own transaction goes with it.

    What this model does NOT catch: a transactional check made INSIDE
    `park()`, then a blind write. The model aborts that check's empty commit
    when the fence changes what it read. Firestore locks instead, so there
    the check would pass and the blind write would land. `transition`'s
    `events` are what close that case, and this test does not tell the two
    apart.
    """
    db = ContendedFirestore()
    seed_attempt(db, task_input=QUOTA_RUN, pool_active=3)
    worker, _, _ = build_worker(
        db, store, tmp_path, log_stream, txn_runner=ContendedTransactionRunner(db), **QUIET
    )
    frozen: dict[str, World] = {}

    def fence_now() -> None:
        fence(db)
        frozen["world"] = freeze(db)

    real_park = worker.control.park

    def park(**kwargs: Any) -> None:
        db.interleave("tasks/task_1", fence_now)
        real_park(**kwargs)

    worker.control.park = park  # type: ignore[method-assign]

    exit_code = worker.run()

    assert "world" in frozen, "the park never read the task, so no fence was interleaved"
    assert db.doc("tasks/task_1")["current_generation"] == 2
    assert quota_exhausted_events(db) == [], (
        "QUOTA_EXHAUSTED announced a park whose commit the fence refused: "
        f"{quota_exhausted_events(db)}"
    )
    assert_left_alone(db, frozen["world"])
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("pools/global")["active"] == 3
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_the_attempt_says_it_stood_down(db)
