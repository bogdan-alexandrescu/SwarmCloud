"""`Store.request_cancel` decides on the state it writes against, not an older one.

F-9 in the wf_ebb3ab2d65664707a559 incident analysis. Latent: never observed in
production, found by reading. `request_cancel` read the task, chose a branch
from the state it read, and then wrote with a blind `ref.update(...)`. Anything
another process committed between the read and the write was overwritten by a
decision made about a document that no longer existed. Two interleavings matter,
and each test below drives one of them through the real route:

  1. CANCEL-OUTRIGHT AGAINST ADMISSION. The API reads QUEUED, so it decides
     "idle, cancel it outright". Before its write lands the scheduler admits
     the task: lease minted, pools incremented, execution started. The API then
     writes `state=CANCELLED, completed_at=now` over a DISPATCHED task. The
     result is a terminal task that still holds an unreleased lease with a live
     execution behind it. Every pool it reserved stays counted, and nothing in
     the task's own record says a container is running. (In the incident's
     wording the common case gets overwritten again by `mark_dispatched`'s
     blind write; this test drives the case where it does not, which is the
     one that strands capacity.)

  2. FLAG-ONLY AGAINST THE WORKER FINISHING. The API reads RUNNING, so it
     decides "flag it". The worker then commits SUCCEEDED. The API writes
     `cancel_requested=true` onto a SUCCEEDED task, records a `cancelled`
     event against it, and answers 200. A caller is told a stop was requested
     for work that had already finished, which is the claim the 409 exists to
     refuse.

WHAT THE FAKE MODELS, AND WHAT IT DOES NOT. `ContendedFirestore` is the shared
`FakeFirestore` with two additions, both local to this file so that no other
suite's fake changes under it:

  * a one-shot hook that runs a concurrent writer IMMEDIATELY AFTER the API's
    first read of the task document. It fires on a plain `DocumentReference.get`
    and on a transactional `get` alike. That is what makes the first test red
    on the old code for the right reason: the old code's read is not
    transactional, and the interleaving still happens to it.
  * a transaction commit that raises `Aborted` when a document the transaction
    read has changed since it read it. The REAL `firestore.transactional`
    decorator catches that and re-runs the body against fresh reads.

Production Firestore server SDKs use pessimistic locking rather than this
optimistic check: the transaction's read takes a lock, and the concurrent
writer waits on it or the younger transaction is aborted and retried. Both
schemes guarantee the same thing, and it is the only thing asserted here: the
branch that commits was chosen from a state nobody changed in between. This
single-threaded fake cannot model blocking, so it models the abort-and-retry
outcome. There is no emulator-backed test in this repository to copy the
pattern from (the CI integration job starts an emulator that no test uses), so
none is added here.

WHAT MUST NOT CHANGE, pinned third. The fix must never "resolve" the race by
releasing capacity from the API. `request_cancel`'s docstring and CONTRACT.md
invariant 1 say why: a task holding capacity has a container that may be
running, and a pool decremented here would free a slot that container still
occupies.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Callable

import pytest
from google.api_core import exceptions as gexc

from .conftest import auth_header, seed_pool, seed_task, seed_tenant
from .fakes import FakeCollectionRef, FakeDocumentRef, FakeFirestore, FakeTransaction

IDLE = {"SUBMITTED", "QUEUED", "READY", "PARKED"}


class _WatchedRef(FakeDocumentRef):
    """A document reference that tells the store it was just read."""

    def get(self, *args: Any, **kwargs: Any):
        snap = super().get(*args, **kwargs)
        self._db.after_read(self.path)
        return snap


class _WatchedCollection(FakeCollectionRef):
    def document(self, doc_id: str | None = None) -> FakeDocumentRef:
        return _WatchedRef(self._db, f"{self._path}/{doc_id or uuid.uuid4().hex}")


class ContendedTransaction(FakeTransaction):
    """`FakeTransaction`, plus: a commit aborts if its read set changed."""

    def __init__(self, db: "ContendedFirestore") -> None:
        super().__init__(db)
        self._read_set: dict[str, Any] = {}

    def _clean_up(self) -> None:
        super()._clean_up()
        self._read_set = {}

    def get(self, ref: Any, **kwargs: Any) -> Any:
        result = super().get(ref, **kwargs)
        if isinstance(ref, FakeDocumentRef):
            # What was READ -- the snapshot taken before the hook ran -- not
            # what the document holds now.
            self._read_set[ref.path] = result.to_dict()
        return result

    def _commit(self) -> list[Any]:
        for path, seen in self._read_set.items():
            if self._db.docs.get(path) != seen:
                self._db.aborts.append(path)
                self._buffer = []
                raise gexc.Aborted(f"{path} changed after this transaction read it")
        return super()._commit()


class ContendedFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.aborts: list[str] = []
        self._after_first_read: dict[str, Callable[[], None]] = {}

    def collection(self, path: str) -> FakeCollectionRef:
        return _WatchedCollection(self, path)

    def transaction(self, **kwargs: Any) -> ContendedTransaction:
        return ContendedTransaction(self)

    def interleave(self, path: str, writer: Callable[[], None]) -> None:
        """Run `writer` once, right after the next read of `path` returns."""
        self._after_first_read[path] = writer

    def after_read(self, path: str) -> None:
        writer = self._after_first_read.pop(path, None)
        if writer is not None:
            writer()


@pytest.fixture
def db() -> ContendedFirestore:
    """Overrides conftest's `db`, so `client` and `make_scheduler` share it."""
    return ContendedFirestore()


def cancelled_events(db: FakeFirestore, task_id: str) -> list[dict[str, Any]]:
    return [
        event
        for event in db.collection_docs(f"tasks/{task_id}/events")
        if event.get("type") == "cancelled"
    ]


# --------------------------------------------------------------------------
# 1. Cancel-outright, raced by admission
# --------------------------------------------------------------------------

def test_a_cancel_that_loses_the_race_to_admission_becomes_a_request(
    client, db, make_scheduler, dispatcher
) -> None:
    """The scheduler admits between the API's read and its write.

    The correct result is the one the API would have produced had it read
    after admission: DISPATCHED, flagged, the lease untouched, and
    `released_immediately: false` -- because a container is now starting and
    only the worker or the reconciler may give its capacity back.
    """
    seed_pool(db, "global", hard_limit=10)
    submitted = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {}},
    )
    assert submitted.status_code in (200, 201), submitted.text
    task_id = submitted.json()["task"]["id"]
    assert db.docs[f"tasks/{task_id}"]["state"] in IDLE, (
        "the task is not idle before the cancel, so the cancel-outright branch "
        "this test races is never chosen"
    )

    db.interleave(f"tasks/{task_id}", make_scheduler().drain)
    response = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice"))

    # Not vacuous: the concurrent writer really did admit and start it.
    assert [d["task_id"] for d in dispatcher.dispatched] == [task_id], (
        "the interleaved drain did not dispatch the task; this test raced nothing"
    )
    stored = db.docs[f"tasks/{task_id}"]
    lease_id = stored["current_lease_id"]
    assert lease_id, "the task lost its lease pointer while its lease is still live"
    lease = db.docs[f"leases/{lease_id}"]
    assert lease.get("released_at") is None, (
        "the lease was released by the cancel; the API must never give back "
        "capacity a container may still occupy"
    )

    assert stored["state"] == "DISPATCHED", (
        f"the task is {stored['state']} while holding live lease {lease_id}: the "
        "API wrote a decision made about the QUEUED document it read over the "
        "DISPATCHED one the scheduler committed"
    )
    assert stored["cancel_requested"] is True
    assert stored.get("completed_at") is None

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["released_immediately"] is False, (
        "the caller was told the task stopped at once, but a container was "
        "admitted and is still running"
    )
    assert body["task"]["state"] == "DISPATCHED"

    events = cancelled_events(db, task_id)
    assert len(events) == 1, (
        f"{len(events)} cancelled events; a retried transaction must write its "
        "event once, with the flag, not once per attempt"
    )
    assert events[0]["detail"]["phase"] == "cancel_requested"
    assert events[0]["detail"]["from_state"] == "DISPATCHED", (
        "the event records the state the decision was NOT made against"
    )


# --------------------------------------------------------------------------
# 2. Flag-only, raced by the worker finishing
# --------------------------------------------------------------------------

def test_a_cancel_that_loses_the_race_to_the_worker_finishing_is_a_conflict(
    client, db
) -> None:
    """The worker commits SUCCEEDED between the API's read and its write.

    Once the task is terminal the API answers 409 for it, and a late request
    must end the same way: no flag on a finished task and no `cancelled` event
    in its history.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_live", tenant_id="eng", state="RUNNING")
    db.docs["tasks/task_live"]["current_lease_id"] = "lease_live"

    def worker_finishes() -> None:
        # What `control.finish(SUCCEEDED)` commits to the task document: the
        # terminal state first, the lease pointer cleared.
        doc = db.docs["tasks/task_live"]
        doc["state"] = "SUCCEEDED"
        doc["completed_at"] = doc["updated_at"]
        doc["current_lease_id"] = None

    db.interleave("tasks/task_live", worker_finishes)
    response = client.post("/v1/tasks/task_live/cancel", headers=auth_header("alice"))

    assert db.docs["tasks/task_live"]["state"] == "SUCCEEDED", (
        "the interleaved finish did not land; this test raced nothing"
    )
    assert response.status_code == 409, (
        f"{response.status_code}: the cancel of a task that had already "
        "SUCCEEDED was accepted, because the API wrote against the RUNNING "
        "document it read"
    )
    assert response.json()["detail"]["state"] == "SUCCEEDED"
    assert db.docs["tasks/task_live"]["cancel_requested"] is False
    assert cancelled_events(db, "task_live") == [], (
        "a `cancelled` event was recorded on a task that finished SUCCEEDED"
    )


# --------------------------------------------------------------------------
# 3. What the fix must not do (green before and after; a guard, not a repro)
# --------------------------------------------------------------------------

def test_cancelling_a_task_that_holds_capacity_touches_no_lease_and_no_pool(
    client, db, make_scheduler
) -> None:
    """Invariant 1, restated at the API boundary.

    Passes on the code before this change too. It is here so that the stranded
    tasks of wf_ebb3ab2d65664707a559 are never "fixed" by releasing from the
    API: a pool decremented here frees a slot a live container still occupies,
    and a hand decrement without `released_at` is decremented again when the
    lease is later released, which `max(0, ...)` then hides.
    """
    seed_pool(db, "global", hard_limit=10)
    submitted = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {}},
    )
    task_id = submitted.json()["task"]["id"]
    make_scheduler().drain()
    assert db.docs[f"tasks/{task_id}"]["state"] == "DISPATCHED"
    leases_before = copy.deepcopy(db.dump("leases/"))
    pools_before = copy.deepcopy(db.dump("pools/"))
    assert leases_before and pools_before, "nothing was admitted; the guard is vacuous"

    response = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert db.dump("leases/") == leases_before
    assert db.dump("pools/") == pools_before
