"""A park, a finish or a retry and its lease release are ONE commit (#2064).

`ControlPlane.park()` wrote PARKED in one Firestore transaction and released
the lease in a second. On 2026-10-23 at 09:05:41Z Firestore aborted the
second on contention (`409 Aborted due to cross-transaction contention`, a
READ inside the release, which `firestore.transactional` never retries). The
worker crashed, its crash path met its own park as a fence and stood down,
and task_7782dd6c7a9e4cb3ade0 sat PARKED with lease_e4e73d61780845b19cfa
still holding capacity (invariant 1).

What these tests hold, for every path that writes a state that holds no
capacity and gives the lease back -- `handoff.park_human_required`'s PARKED,
and its READY on a person's return, included (PARK-LEASE-ATOMIC-2):

  * aborted once, then committed: the new state AND the released lease, with
    one commit carrying both, and each event once;
  * aborted for good: NEITHER. The task is still RUNNING and the lease still
    held, so the crash path's FAILED is a write this attempt still owns, not
    a fence. Never PARKED with a held lease;
  * a stale generation: nothing written and nothing released (invariant 5).

The store is the library's own `Transaction` and `_Transactional` driven by
the production `FirestoreTransactionRunner` (library_transaction.py), with
two changes made here: writes are held until the commit RPC returns, as
Firestore holds them, so a transaction that never commits leaves nothing
behind; and a read after a write raises `ReadAfterWriteError`, as the library
does, so a body that releases after it writes cannot pass here and fail in
production. An abort is injected on the pool reads, which only
`release_lease_in_transaction` makes: the read the 09:05:41Z release died on.
"""

from __future__ import annotations

import copy
import io
from datetime import timedelta
from typing import Any, Callable

import pytest

from agent_worker import control as control_mod
from agent_worker import handoff as handoff_mod
from agent_worker.control import ControlPlane, FirestoreTransactionRunner
from agent_worker.errors import ExitCode, FencedWriteRefused
from agent_worker.logs import build_logger
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from fakes import FakeDocumentRef
from library_transaction import (
    FirestoreRPCs,
    TransactionalFirestore,
    library_transaction_class,
)
from worker_seeds import TENANT, build_worker, seed_attempt

#: What Firestore said at 09:05:41Z (the library prefixes the 409 itself).
CONTENTION = "Aborted due to cross-transaction contention"

QUOTA_RUN = {
    "prompt": "burn quota",
    "steps": 1,
    "sleep_seconds": 0.05,
    "quota_exhausted": True,
    "retry_after_seconds": 1800,
}
QUIET = dict(
    heartbeat_interval_seconds=60,
    checkpoint_interval_seconds=60,
    control_poll_seconds=60,
    timeout_seconds=60,
)


def _aborted(message: str = CONTENTION) -> Exception:
    from google.api_core import exceptions as core

    return core.Aborted(message)


def _buffered_transaction_class() -> type:
    """The library's `Transaction` over the store, its writes held for the commit.

    Held by transaction id in the store, and applied by the commit RPC once it
    returns (`_CommittingRPCs`), so the budgeted subclass the runner makes of
    this class (`control._with_budget`), whose `_commit` is its own, commits
    them the same way.
    """
    from google.cloud.firestore_v1 import ReadAfterWriteError

    base = library_transaction_class()

    class Buffered(base):  # type: ignore[misc, valid-type]
        def _held(self) -> list[Callable[[], None]]:
            return self._client.pending.setdefault(self._id, [])

        def get(self, ref: Any, *args: Any, **kwargs: Any) -> Any:
            if self._client.pending.get(self._id):
                raise ReadAfterWriteError("Attempted read after write in a transaction.")
            return super().get(ref, *args, **kwargs)

        def set(self, ref: Any, data: dict[str, Any], merge: bool = False) -> None:
            self._client.calls.append(("txn.set", ref.path, {}))
            self._held().append(lambda: FakeDocumentRef.set(ref, data, merge=merge))

        def update(self, ref: Any, data: dict[str, Any]) -> None:
            self._client.calls.append(("txn.update", ref.path, {}))
            self._held().append(lambda: FakeDocumentRef.update(ref, data))

        def _clean_up(self) -> None:
            # A rolled-back or restarted transaction drops what it held.
            self._client.pending.pop(self._id, None)
            super()._clean_up()

    return Buffered


class _CommittingRPCs(FirestoreRPCs):
    """The commit RPC applies the transaction's held writes once it returns."""

    def __init__(self, store: "AbortingFirestore") -> None:
        super().__init__()
        self._store = store

    def commit(self, *, request: Any, metadata: Any, **kwargs: Any) -> Any:
        response = super().commit(request=request, metadata=metadata, **kwargs)
        for write in self._store.pending.pop(request["transaction"], []):
            write()
        return response


class AbortingFirestore(TransactionalFirestore):
    """The store, whose transactional pool reads Firestore can abort."""

    def __init__(self) -> None:
        super().__init__()
        self._firestore_api = _CommittingRPCs(self)
        #: transaction id -> the writes it holds until its commit.
        self.pending: dict[Any, list[Callable[[], None]]] = {}
        self.pool_aborts = 0
        self.aborted = 0
        self.on_read = self._maybe_abort

    def transaction(self) -> Any:
        return _buffered_transaction_class()(self)

    def _maybe_abort(self, ref: Any, transactional: bool) -> None:
        if transactional and ref.path.startswith("pools/") and self.pool_aborts > 0:
            self.pool_aborts -= 1
            self.aborted += 1
            raise _aborted()

    def commits(self) -> int:
        return self.rpcs.names().count("commit")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """The runner's backoff between two aborted runs, not slept in a unit test."""
    monkeypatch.setattr(control_mod, "_abort_sleep", lambda _s: None, raising=False)


def _control(db: AbortingFirestore, *, generation: int = 1) -> ControlPlane:
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=generation,
        logger=build_logger(
            task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
            generation=generation, runner_profile="mock", stream=io.StringIO(),
        ),
        txn_runner=FirestoreTransactionRunner(db),
    )


def _seeded(*, task_generation: int | None = None) -> AbortingFirestore:
    db = AbortingFirestore()
    seed_attempt(db, state=TaskState.RUNNING, pool_active=3, task_generation=task_generation)
    db.seed("attempts/att_1", {"attempt_id": "att_1", "tenant_id": TENANT,
                               "completed_at": None, "exit_code": None})
    return db


def _pools(db: AbortingFirestore) -> dict[str, int]:
    return {
        path: doc["active"] for path, doc in db.documents.items() if path.startswith("pools/")
    }


# -- every path that ends the attempt and gives the lease back -------------------
#
# name -> (call, the state it leaves, the release reason's prefix, its event).


def _park(control: ControlPlane) -> Any:
    return control.park(
        reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
        next_eligible_at=utcnow() + timedelta(minutes=30),
        detail={"provider": "anthropic"},
        announce=[(EventType.QUOTA_EXHAUSTED, {"provider": "anthropic"})],
    )


def _await_children(control: ControlPlane) -> Any:
    return control.park_awaiting_children(max_resumes=4, detail={"children": ["task_2"]})


def _release_wait(control: ControlPlane) -> Any:
    return control.park_release_wait(
        detail={"release_wait": {"code": "awaiting_release"}}, max_resumes=4
    )


def _ci_pending(control: ControlPlane) -> Any:
    return control.park_ci_pending(
        code="ci_pending", head="0" * 40, pull_request=7, pending=["unit"],
        max_wakes=4, fallback_seconds=600,
    )


def _finish(control: ControlPlane) -> Any:
    return control.finish(state=TaskState.FAILED, exit_code=1, error="runner failed")


def _retry(control: ControlPlane) -> Any:
    return control.fail_retryably(exit_code=1, error="runner failed", cause="runner_error")


def _human_park(control: ControlPlane) -> Any:
    return handoff_mod.park_human_required(
        control, max_resumes=handoff_mod.MAX_HUMAN_RESUMES, detail={"cause": "login"}
    )


def _returned(db: AbortingFirestore) -> None:
    """A person returned control to THIS attempt while it parked (LB-RETURN):
    the human park re-queues READY instead of parking."""
    db.documents["tasks/task_1"]["metadata"] = {
        handoff_mod.HUMAN_RETURNED_METADATA_KEY: {
            "attempt_id": "att_1", "generation": 1, "by": "alice@example.com",
        },
    }


PATHS: dict[str, tuple[Callable[[ControlPlane], Any], TaskState, str, EventType]] = {
    "park": (_park, TaskState.PARKED, "parked:", EventType.PARKED),
    "park_awaiting_children": (_await_children, TaskState.PARKED, "parked:", EventType.PARKED),
    "park_release_wait": (_release_wait, TaskState.PARKED, "parked:", EventType.PARKED),
    "park_ci_pending": (_ci_pending, TaskState.PARKED, "parked:", EventType.PARKED),
    "finish": (_finish, TaskState.FAILED, "terminal:", EventType.FAILED),
    "fail_retryably": (_retry, TaskState.READY, "retry:", EventType.RETRYING),
    "park_human_required": (_human_park, TaskState.PARKED, "parked:", EventType.PARKED),
    "park_human_required_returned": (
        _human_park, TaskState.READY, "requeued:human_returned", EventType.READY,
    ),
}

#: name -> what the path finds on the task before it runs, beyond `_seeded`.
SEEDS: dict[str, Callable[[AbortingFirestore], None]] = {
    "park_human_required_returned": _returned,
}


def _seeded_for(name: str, *, task_generation: int | None = None) -> AbortingFirestore:
    db = _seeded(task_generation=task_generation)
    SEEDS.get(name, lambda _db: None)(db)
    return db


@pytest.mark.parametrize("name", sorted(PATHS))
def test_an_end_aborted_once_then_committed_leaves_the_state_and_the_released_lease(name):
    call, state, release_prefix, event = PATHS[name]
    db = _seeded_for(name)
    db.pool_aborts = 1
    control = _control(db)

    call(control)

    assert db.aborted == 1, "the abort was never injected, so nothing was retried"
    task = db.doc("tasks/task_1")
    # MUTATION: leave the release in its own transaction, which an aborted
    # read crashes -- `call` raises Aborted and nothing below is reached.
    assert task["state"] == state.value
    # MUTATION: drop `current_lease_id: None` from the end's payload.
    assert task["current_lease_id"] is None
    lease = db.doc("leases/lease_1")
    # MUTATION: skip `release_lease_in_transaction` in the combined body.
    assert lease["released_at"] is not None
    assert lease["release_reason"].startswith(release_prefix)
    # MUTATION: decrement the pools in a separate transaction that is not retried.
    assert set(_pools(db).values()) == {2}, _pools(db)
    # MUTATION: commit the state and the release separately: two commits.
    assert db.commits() == 1, db.rpcs.names()
    types = db.event_types("task_1")
    # MUTATION: write the events with ids minted inside the body, or emit them
    # after a retried release as well: a type appears twice.
    assert types.count(event.value) == 1, types
    assert types.count(EventType.LEASE_RELEASED.value) == 1, types
    attempt = db.doc("attempts/att_1")
    # MUTATION: leave the attempt's end out of the combined transaction and
    # make it best effort again.
    assert attempt["completed_at"] is not None
    assert control.lease_released is True
    # MUTATION: never record the committed end on the control plane.
    assert control.committed_end is state


@pytest.mark.parametrize("name", sorted(PATHS))
def test_an_end_aborted_for_good_leaves_the_task_running_and_the_lease_held(name):
    call, _state, _prefix, event = PATHS[name]
    db = _seeded_for(name)
    db.pool_aborts = 10_000
    pools = _pools(db)
    control = _control(db)

    from google.api_core import exceptions as core

    with pytest.raises(core.Aborted):
        call(control)

    task = db.doc("tasks/task_1")
    # MUTATION: commit the state write before the release (the #2064 shape):
    # the task is left PARKED or terminal with its lease held.
    assert task["state"] == TaskState.RUNNING.value
    assert task["current_lease_id"] == "lease_1"
    # MUTATION: decrement the pools outside the transaction that aborted.
    assert db.doc("leases/lease_1")["released_at"] is None
    assert _pools(db) == pools
    types = db.event_types("task_1")
    # MUTATION: emit the end's event on its own, before the combined commit.
    assert event.value not in types, types
    assert EventType.LEASE_RELEASED.value not in types, types
    # MUTATION: write the attempt's end outside the combined transaction.
    assert db.doc("attempts/att_1")["completed_at"] is None
    assert control.lease_released is False
    assert control.committed_end is None


@pytest.mark.parametrize("name", sorted(PATHS))
def test_a_stale_generations_end_writes_nothing_and_releases_nothing(name):
    call, _state, _prefix, _event = PATHS[name]
    db = _seeded_for(name, task_generation=2)
    before = copy.deepcopy(db.documents)
    control = _control(db, generation=1)

    with pytest.raises(FencedWriteRefused):
        call(control)

    # MUTATION: move the fence read after the release, or out of the combined
    # transaction: the pools come down for a lease a newer attempt holds.
    assert db.documents == before


def test_a_commit_aborted_for_good_writes_nothing():
    """Firestore's own commit retries (five, `firestore.transactional`) run
    out: the park, its events, the attempt's end and the release are one
    commit that never landed."""
    db = _seeded()
    pools = _pools(db)

    def refuse() -> None:
        raise _aborted()

    db.rpcs.on_commit = refuse
    control = _control(db)

    with pytest.raises(ValueError):
        _park(control)

    # MUTATION: write the task, the events or the release outside the commit.
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_1")["released_at"] is None
    assert _pools(db) == pools
    assert db.event_types("task_1") == []


# -- the runner -----------------------------------------------------------------


def test_the_runner_retries_an_aborted_read_a_bounded_number_of_runs():
    """`firestore.transactional` retries an ABORTED commit and never a read. The
    runner runs the whole body again on an abort raised anywhere before the
    commit, `TRANSACTION_ABORTED_RUNS` times, and then raises the abort."""
    db = _seeded()
    db.pool_aborts = 10_000
    runs = getattr(control_mod, "TRANSACTION_ABORTED_RUNS", None)
    control = _control(db)

    from google.api_core import exceptions as core

    with pytest.raises(core.Aborted):
        control.release_lease("parked:provider_quota_exhausted")

    # MUTATION: catch nothing in `FirestoreTransactionRunner.run`: one run.
    assert runs == 5
    # MUTATION: retry without a bound: the abort never surfaces.
    assert db.aborted == runs
    assert db.rpcs.names().count("begin") == runs


def test_the_runner_stops_retrying_at_the_calls_deadline(monkeypatch):
    """A call with a budget keeps it: no run starts after its deadline."""
    db = _seeded()
    db.pool_aborts = 10_000
    clock = [0.0]
    monkeypatch.setattr(control_mod, "_abort_clock", lambda: clock[0], raising=False)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(control_mod, "_abort_sleep", sleep, raising=False)

    class _Retry:
        timeout = 1.0

    runner = FirestoreTransactionRunner(db)

    def body(txn: Any) -> None:
        txn.get(db.document("pools/global"))

    from google.api_core import exceptions as core

    with pytest.raises(core.Aborted):
        runner.run(body, call_options={"retry": _Retry(), "timeout": 1.0})

    # MUTATION: ignore the deadline: five runs where the budget allows three
    # (runs at 0, 0.25 and 0.75 s; a fourth after 1.0 s more would start at
    # 1.75 s, past the 1.0 s deadline).
    assert db.aborted == 3


# -- the crash path ---------------------------------------------------------------


def _worker(db: AbortingFirestore, store: Any, tmp_path: Any, log_stream: io.StringIO) -> Any:
    worker, _, _ = build_worker(
        db, store, tmp_path, log_stream, txn_runner=FirestoreTransactionRunner(db), **QUIET
    )
    return worker


def test_a_quota_park_whose_release_is_aborted_for_good_is_not_left_parked_holding(
    store, tmp_path, log_stream
):
    """The 09:05:41Z sequence. Every release read during the park is aborted.
    The park writes nothing, the worker crashes, and its crash path's FAILED
    is a write this attempt still owns: the task ends and the slot comes back."""
    db = AbortingFirestore()
    seed_attempt(db, task_input=QUOTA_RUN, simulated={"provider": "anthropic"}, pool_active=3)
    worker = _worker(db, store, tmp_path, log_stream)
    real_park = worker.control.park

    def park(**kwargs: Any) -> None:
        db.pool_aborts = 10_000
        try:
            real_park(**kwargs)
        finally:
            db.pool_aborts = 0

    worker.control.park = park  # type: ignore[method-assign]

    exit_code = worker.run()

    assert db.aborted > 0, "no release read was aborted"
    task = db.doc("tasks/task_1")
    # MUTATION: the two-transaction park: PARKED commits, the release aborts,
    # and the crash path is fenced by the park (exit 70, lease held).
    assert exit_code == ExitCode.FAILED
    assert task["state"] == TaskState.FAILED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("pools/global")["active"] == 2
    assert "FENCED on the way out" not in log_stream.getvalue()
    assert EventType.PARKED.value not in db.event_types("task_1")


def test_a_crash_after_the_workers_own_park_committed_releases_nothing_twice(
    store, tmp_path, log_stream
):
    """The park committed -- PARKED, the attempt's end and the lease release in
    one commit -- and the worker crashed after it. Its crash path finds its OWN
    end, not a newer attempt: nothing is left to release, the attempt keeps the
    park's exit 75, and the worker exits as parked rather than fenced."""
    db = AbortingFirestore()
    seed_attempt(db, task_input=QUOTA_RUN, simulated={"provider": "anthropic"}, pool_active=3)
    worker = _worker(db, store, tmp_path, log_stream)
    real_park = worker.control.park

    def park(**kwargs: Any) -> None:
        real_park(**kwargs)
        raise RuntimeError("crashed after the park committed")

    worker.control.park = park  # type: ignore[method-assign]

    exit_code = worker.run()

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    lease = db.doc("leases/lease_1")
    # MUTATION: release the lease after the park's commit instead of in it.
    assert lease["released_at"] is not None
    assert db.doc("pools/global")["active"] == 2
    # MUTATION: send the crash path to `_stand_down` when this attempt's own
    # end committed: it exits 70 and overwrites the attempt's park record.
    assert exit_code == ExitCode.PARKED
    assert "FENCED on the way out" not in log_stream.getvalue()
    assert db.doc("attempts/att_1")["exit_code"] == ExitCode.PARKED
    assert worker.control.lease_released is True
    assert db.event_types("task_1").count(EventType.LEASE_RELEASED.value) == 1


def test_the_buffered_store_raises_on_a_read_after_a_write():
    """The control for the store: a body that writes and then reads is refused
    here, as the library refuses it, so a release placed after the end's
    writes could not pass these tests."""
    from google.cloud.firestore_v1 import ReadAfterWriteError

    db = _seeded()
    runner = FirestoreTransactionRunner(db)

    def body(txn: Any) -> None:
        txn.update(db.document("tasks/task_1"), {"state": "PARKED"})
        txn.get(db.document("leases/lease_1"))

    with pytest.raises(ReadAfterWriteError):
        runner.run(body)
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value



# -- a terminal write Firestore aborts on contention (#2650) -----------------------
#
# Dev acceptance on b3d45cdc7 (2026-10-31): the mock tasks that failed together
# ended `FAILED runner_error` with no exit code and `last_error` "409 Aborted
# due to cross-transaction contention": every run of their finish was aborted,
# the abort went up as "worker crashed", and the crash path wrote its text.


def _exit_two(message: str) -> dict[str, Any]:
    return {"prompt": "fail", "steps": 1, "sleep_seconds": 0.05,
            "fail": True, "fail_message": message, "exit_code": 2}


def _abort_the_first_finishes(db: AbortingFirestore, worker: Any, calls: int) -> list[int]:
    """Every pool read of the first `calls` finishes is aborted, every run."""
    real_finish = worker.control.finish
    made: list[int] = []

    def finish(**kwargs: Any) -> Any:
        made.append(1)
        db.pool_aborts = 10_000 if len(made) <= calls else 0
        try:
            return real_finish(**kwargs)
        finally:
            db.pool_aborts = 0

    worker.control.finish = finish  # type: ignore[method-assign]
    return made


@pytest.fixture
def _no_terminal_pause(monkeypatch):
    from agent_worker import lifecycle as lifecycle_mod

    monkeypatch.setattr(
        lifecycle_mod, "_terminal_contention_sleep", lambda _s: None, raising=False
    )


def _assert_the_runners_end(db: AbortingFirestore, message: str) -> None:
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "runner_error"
    # MUTATION: no second round and no decided end in the crash path: the
    # crash path's FAILED carries the abort's text and no exit code.
    assert CONTENTION not in (task["last_error"] or "")
    assert message in task["last_error"], task["last_error"]
    assert task["result_summary"]["exit_code"] == 2
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("pools/global")["active"] == 2


def test_a_runners_failure_whose_finish_is_aborted_once_is_written_as_the_runners(
    store, tmp_path, log_stream, _no_terminal_pause
):
    db = AbortingFirestore()
    message = "acceptance fail_message " + "x" * 8
    seed_attempt(db, task_input=_exit_two(message), pool_active=3)
    worker = _worker(db, store, tmp_path, log_stream)
    made = _abort_the_first_finishes(db, worker, 1)

    assert worker.run() == ExitCode.FAILED

    assert db.aborted == control_mod.TRANSACTION_ABORTED_RUNS
    assert len(made) == 2
    _assert_the_runners_end(db, message)


def test_contention_past_every_round_still_ends_with_the_runners_failure(
    store, tmp_path, log_stream, _no_terminal_pause
):
    """The crash path writes the end the attempt had decided, not the abort."""
    from agent_worker import lifecycle as lifecycle_mod

    db = AbortingFirestore()
    message = "acceptance exit 2 " + "y" * 8
    seed_attempt(db, task_input=_exit_two(message), pool_active=3)
    worker = _worker(db, store, tmp_path, log_stream)
    rounds = getattr(lifecycle_mod, "TERMINAL_CONTENTION_ROUNDS", 4)
    made = _abort_the_first_finishes(db, worker, rounds)

    assert worker.run() == ExitCode.FAILED

    assert len(made) == rounds + 1
    assert "worker crashed" in log_stream.getvalue()
    _assert_the_runners_end(db, message)


def test_only_a_contention_abort_is_written_again():
    from google.api_core import exceptions as core

    from agent_worker.lifecycle import _terminal_write_contended

    assert _terminal_write_contended(_aborted())
    assert _terminal_write_contended(ValueError("Failed to commit transaction in 5 attempts."))
    try:
        try:
            raise _aborted()
        except Exception as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert _terminal_write_contended(outer)
    assert not _terminal_write_contended(core.InvalidArgument("invalid nested entity"))
    assert not _terminal_write_contended(ValueError("something else"))
    assert not _terminal_write_contended(RuntimeError("boom"))
