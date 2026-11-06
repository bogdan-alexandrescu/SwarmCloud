"""The frozen batched admission, `acquire_leases_in_transaction` (contract request 139).

Accepted by the owner 2026-11-06 on PR 3493 ("frozen-batch-function"): the
batch lives beside `acquire_lease_in_transaction` and shares its per-task
checks, lease document and task write, instead of the scheduler restating
them. This holds the batch to the single function, document for document,
and holds its `before_writes` hook to running before the first write.

No emulator, no credentials: a dict stands in for Firestore.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from swarm_common.admission import (
    AdmissionConfig,
    AdmissionDenied,
    AdmissionRequest,
    acquire_lease_in_transaction,
    acquire_leases_in_transaction,
)
from swarm_common.models import Lease, Task, pool_names_for
from swarm_common.states import TaskState

TENANT = "eng"
BACKEND = "GKE_AUTOPILOT"
NOW = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)


class _Snap:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class _Ref:
    def __init__(self, path: str) -> None:
        self.path = path


class _Collection:
    def __init__(self, name: str) -> None:
        self._name = name

    def document(self, doc_id: str) -> _Ref:
        return _Ref(f"{self._name}/{doc_id}")


class _Db:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs

    def collection(self, name: str) -> _Collection:
        return _Collection(name)


class _Txn:
    """Applies writes, counts them per path, and refuses a read after a write."""

    def __init__(self, db: _Db) -> None:
        self._db = db
        self.written = False
        self.writes: list[str] = []

    def get(self, ref: _Ref) -> _Snap:
        assert not self.written, "a read after a write: Firestore raises ReadAfterWriteError"
        return _Snap(self._db.docs.get(ref.path))

    def set(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.written = True
        self.writes.append(ref.path)
        self._db.docs[ref.path] = dict(data)

    def update(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.written = True
        self.writes.append(ref.path)
        self._db.docs.setdefault(ref.path, {}).update(data)


def _task(task_id: str) -> Task:
    return Task(
        id=task_id,
        tenant_id=TENANT,
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.READY,
        runner_profile="claude-code",
        resource_class="standard",
        provider="anthropic",
        input={},
        submitted_by="alice@example.com",
    )


def _db(task_ids: list[str], *, limit: int = 50) -> _Db:
    required = pool_names_for(
        tenant_id=TENANT, provider="anthropic", resource_class="standard",
        runner_profile="claude-code", backend=BACKEND,
    )
    docs: dict[str, dict[str, Any]] = {
        f"pools/{name}": {"hard_limit": limit, "active": 0, "enabled": True} for name in required
    }
    for task_id in task_ids:
        docs[f"tasks/{task_id}"] = {
            "state": TaskState.READY.value, "current_generation": 2, "attempt_count": 2,
        }
    return _Db(docs)


def _without_ids(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in ("lease_id", "attempt_id", "current_lease_id")}


def test_the_batch_writes_the_single_functions_lease_and_task_documents():
    single, batch = _db(["task_a"]), _db(["task_a"])
    one = acquire_lease_in_transaction(
        _Txn(single), db=single, task=_task("task_a"), units=1, backend=BACKEND,
        config=AdmissionConfig(), now=NOW,
    )
    [many] = acquire_leases_in_transaction(
        _Txn(batch), db=batch, requests=[AdmissionRequest(_task("task_a"), 1, BACKEND)],
        config=AdmissionConfig(), now=NOW,
    )
    assert isinstance(many, Lease)
    assert many.generation == one.generation == 3, "fencing generation is current + 1"
    assert _without_ids(batch.docs[f"leases/{many.lease_id}"]) == _without_ids(
        single.docs[f"leases/{one.lease_id}"])
    assert _without_ids(batch.docs["tasks/task_a"]) == _without_ids(single.docs["tasks/task_a"])
    assert batch.docs["tasks/task_a"]["current_lease_id"] == many.lease_id
    pools = {p: d for p, d in batch.docs.items() if p.startswith("pools/")}
    assert pools == {p: d for p, d in single.docs.items() if p.startswith("pools/")}


def test_each_pool_is_written_once_with_the_batchs_sum():
    ids = ["task_a", "task_b", "task_c"]
    db = _db(ids)
    txn = _Txn(db)
    outcomes = acquire_leases_in_transaction(
        txn, db=db, requests=[AdmissionRequest(_task(i), 1, BACKEND) for i in ids],
        config=AdmissionConfig(), now=NOW,
    )
    assert all(isinstance(o, Lease) for o in outcomes)
    pool_writes = [p for p in txn.writes if p.startswith("pools/")]
    assert len(pool_writes) == len(set(pool_writes)), "each pool written once"
    assert db.docs["pools/global"]["active"] == 3


def test_a_task_that_does_not_fit_takes_nothing_and_is_returned_denied():
    db = _db(["task_a", "task_b"], limit=1)
    outcomes = acquire_leases_in_transaction(
        _Txn(db), db=db,
        requests=[AdmissionRequest(_task("task_a"), 1, BACKEND),
                  AdmissionRequest(_task("task_b"), 1, BACKEND)],
        config=AdmissionConfig(), now=NOW,
    )
    assert isinstance(outcomes[0], Lease)
    assert isinstance(outcomes[1], AdmissionDenied)
    assert db.docs["tasks/task_b"]["state"] == TaskState.READY.value
    assert db.docs["pools/global"]["active"] == 1


def test_missing_cancelled_and_repeated_requests_are_denied_with_the_single_reasons():
    db = _db(["task_a", "task_c"])
    db.docs["tasks/task_c"]["cancel_requested"] = True
    outcomes = acquire_leases_in_transaction(
        _Txn(db), db=db,
        requests=[AdmissionRequest(_task("task_a"), 1, BACKEND),
                  AdmissionRequest(_task("task_a"), 1, BACKEND),
                  AdmissionRequest(_task("task_gone"), 1, BACKEND),
                  AdmissionRequest(_task("task_c"), 1, BACKEND)],
        config=AdmissionConfig(), now=NOW,
    )
    assert isinstance(outcomes[0], Lease)
    reasons = [o.reasons[0]["reason"] for o in outcomes[1:]]  # type: ignore[union-attr]
    assert reasons == ["not_ready", "task_missing", "cancel_requested"]
    assert db.docs["pools/global"]["active"] == 1


def test_before_writes_runs_after_every_read_and_before_the_first_write():
    db = _db(["task_a", "task_b"], limit=1)
    txn = _Txn(db)
    seen: list[Any] = []

    def hook(outcomes: list[Any]) -> None:
        assert not txn.written, "the hook must be free to read"
        txn.get(_Ref("workflows/wf"))
        seen.append([type(o).__name__ for o in outcomes])

    acquire_leases_in_transaction(
        txn, db=db,
        requests=[AdmissionRequest(_task("task_a"), 1, BACKEND),
                  AdmissionRequest(_task("task_b"), 1, BACKEND)],
        config=AdmissionConfig(), now=NOW, before_writes=hook,
    )
    assert seen == [["Lease", "AdmissionDenied"]]
    assert txn.written


def test_an_empty_batch_reads_and_writes_nothing():
    db = _db([])
    txn = _Txn(db)
    assert acquire_leases_in_transaction(
        txn, db=db, requests=[], config=AdmissionConfig(), now=NOW) == []
    assert txn.writes == []
