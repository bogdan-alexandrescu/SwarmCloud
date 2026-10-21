"""A cancel that meets Firestore contention retries, then refuses by name (CANCEL-RETRY; #1822).

`POST /v1/tasks/{id}/cancel` answered 500 21 times in 48 h (the 2026-10-21
system analysis, epic #1822) when its transaction met contention: `Aborted`
on a transactional read, which `firestore.transactional` does not retry, or
the client's own commit retries running out, which it reports as a
`ValueError("Failed to commit transaction in N attempts.")`. Neither has
committed anything, so both are safe to run again; the caller was told
"internal error" for a condition it could simply retry.

What holds now, each pinned below:

  * the cancel's transaction is run again, a bounded number of times with a
    jittered pause between runs, inside a small time budget;
  * contention that outlasts that answers 503 `cancel_contended` with a
    `Retry-After`, in the API's ordinary refusal shape -- never a 500;
  * `DeadlineExceeded` is never retried: a commit that timed out may have
    landed, and running the cancel again could write its event twice;
  * a cancel stays idempotent: a retried cancel writes one event, and a cancel
    of a task the retry already ended is the same 409 it always was;
  * the workflow cancel, which runs the same transaction per step, answers
    the same refusal and still cancels every step that was not contended.

WHAT THE FAKE MODELS. `FaultyFirestore` is the shared `FakeFirestore` whose
transactions can be told to raise on a transactional read of one document
(once, or every time) and to abort a number of commits. The REAL
`firestore.transactional` decorator drives them, so the commit-side retries
it already does (five, no backoff) are exercised as shipped, and its
`ValueError` on exhaustion is the real one.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.api_core import exceptions as gexc

from swarm_api import store as store_module

from .conftest import auth_header, seed_task, seed_tenant
from .fakes import FakeDocumentRef, FakeFirestore, FakeTransaction


class FaultyTransaction(FakeTransaction):
    def get(self, ref: Any, **kwargs: Any) -> Any:
        if isinstance(ref, FakeDocumentRef):
            self._db.reads[ref.path] = self._db.reads.get(ref.path, 0) + 1
            fault = self._db.next_fault(ref.path)
            if fault is not None:
                raise fault
        return super().get(ref, **kwargs)

    def _commit(self) -> list[Any]:
        if self._db.commit_aborts > 0:
            self._db.commit_aborts -= 1
            self._buffer = []
            raise gexc.Aborted("commit aborted: another transaction holds the lock")
        return super()._commit()


class FaultyFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.reads: dict[str, int] = {}
        self.commit_aborts = 0
        self._faults: dict[str, list[Exception]] = {}
        self._always: dict[str, Exception] = {}

    def transaction(self, **kwargs: Any) -> FaultyTransaction:
        return FaultyTransaction(self)

    def fail_reads(self, path: str, *faults: Exception) -> None:
        """Each of `faults` is raised by one transactional read of `path`, in order."""
        self._faults.setdefault(path, []).extend(faults)

    def fail_every_read(self, path: str, fault: Exception) -> None:
        self._always[path] = fault

    def heal(self, path: str) -> None:
        self._always.pop(path, None)
        self._faults.pop(path, None)

    def next_fault(self, path: str) -> Exception | None:
        if path in self._always:
            return self._always[path]
        queued = self._faults.get(path)
        return queued.pop(0) if queued else None


@pytest.fixture
def db() -> FaultyFirestore:
    """Overrides conftest's `db`, so `client` runs against it."""
    return FaultyFirestore()


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    """Every pause the retry takes, recorded instead of slept.

    `raising=False`: before the retry existed the module had no pause to
    replace, and the test must fail on its assertions, not on this fixture.
    """
    taken: list[float] = []
    monkeypatch.setattr(store_module, "_contention_sleep", taken.append, raising=False)
    return taken


def event_types(db: FakeFirestore, task_id: str) -> list[str]:
    return [event.get("type") for event in db.collection_docs(f"tasks/{task_id}/events")]


def seed_idle(db: FaultyFirestore, task_id: str = "task_c") -> str:
    seed_tenant(db, "eng")
    seed_task(db, task_id=task_id, tenant_id="eng", state="READY")
    return f"tasks/{task_id}"


# --------------------------------------------------------------------------
# The task cancel
# --------------------------------------------------------------------------

def test_one_aborted_read_then_success_answers_200(client, db, pauses) -> None:
    path = seed_idle(db)
    db.fail_reads(path, gexc.Aborted("transaction lock timeout"))

    response = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))

    # MUTATION: drop `gexc.Aborted` from `_CONTENDED` in store.py and this is a 500.
    assert response.status_code == 200, response.text
    assert response.json()["task"]["state"] == "CANCELLED"
    assert db.docs[path]["state"] == "CANCELLED"
    # MUTATION: retry without a fresh transaction or after a partial write and
    # the cancel lands twice; one run committed, so one event.
    assert event_types(db, "task_c") == ["cancelled"]
    # MUTATION: retry at once, with no pause, and this list is empty.
    assert len(pauses) == 1 and pauses[0] >= 0, pauses
    assert db.reads[path] == 2, "one aborted run and one that committed"


def test_the_client_running_out_of_commit_retries_is_retried(client, db, pauses) -> None:
    """`firestore.transactional` retries an aborted COMMIT five times itself and
    then raises `ValueError("Failed to commit transaction in 5 attempts.")`.
    Nothing committed, so that is contention too, and is run again."""
    path = seed_idle(db)
    db.commit_aborts = 5  # the decorator's whole budget, for the first run only

    response = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))

    # MUTATION: stop matching the "Failed to commit transaction" message in
    # `_is_contention` and this is a 500.
    assert response.status_code == 200, response.text
    assert db.docs[path]["state"] == "CANCELLED"
    assert event_types(db, "task_c") == ["cancelled"]
    assert len(pauses) == 1, pauses


def test_persistent_contention_answers_cancel_contended_with_retry_after(
    client, db, pauses
) -> None:
    path = seed_idle(db)
    db.fail_every_read(path, gexc.Aborted("transaction lock timeout"))

    response = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))

    # MUTATION: re-raise the last `Aborted` instead of `CancelContended` and
    # this is the 500 the epic counted.
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "cancel_contended", body
    # MUTATION: make `CancelContended` an `UpstreamUnavailable` instead of a
    # `RateLimited` and the handler writes no Retry-After.
    assert int(response.headers["Retry-After"]) >= 1
    assert body["detail"]["retry_after_seconds"] > 0
    # Bounded: the runs the retry allows and no more, with a pause between
    # each pair and the pauses inside the budget.
    # MUTATION: loop `while True` and this test never returns.
    attempts = store_module.CANCEL_TRANSACTION_ATTEMPTS
    assert db.reads[path] == attempts
    assert len(pauses) == attempts - 1
    assert sum(pauses) <= store_module.CANCEL_RETRY_BUDGET_SECONDS
    # Nothing was written: the refusal is retryable because nothing committed.
    assert db.docs[path]["state"] == "READY"
    assert db.docs[path]["cancel_requested"] is False
    assert event_types(db, "task_c") == []


def test_the_caller_can_retry_after_the_refusal(client, db, pauses) -> None:
    path = seed_idle(db)
    db.fail_every_read(path, gexc.Aborted("transaction lock timeout"))
    refused = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))
    assert refused.status_code == 503, refused.text

    db.heal(path)
    retried = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))

    assert retried.status_code == 200, retried.text
    assert event_types(db, "task_c") == ["cancelled"]


def test_deadline_exceeded_is_not_retried(client, db, pauses) -> None:
    """A deadline may have expired after the commit landed: a second run could
    write a second event, so it is never retried, and answers as before."""
    path = seed_idle(db)
    db.fail_every_read(path, gexc.DeadlineExceeded("deadline exceeded"))

    response = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))

    # MUTATION: add `gexc.DeadlineExceeded` to `_CONTENDED` and the read runs
    # CANCEL_TRANSACTION_ATTEMPTS times and the answer is 503.
    assert db.reads[path] == 1
    assert pauses == []
    assert response.status_code == 500


def test_another_value_error_is_not_retried(api_store, db, pauses) -> None:
    """Only the client's retry-exhaustion message is contention; any other
    `ValueError` is a bug, and is raised as it was."""
    path = seed_idle(db)
    db.fail_reads(path, ValueError("Cannot commit a read-only transaction"))

    # MUTATION: retry every `ValueError` and this raises nothing (the second
    # run succeeds).
    with pytest.raises(ValueError, match="read-only"):
        api_store.request_cancel_with_target("eng", "task_c", by="alice@example.com")
    assert db.reads[path] == 1
    assert pauses == []


def test_a_cancel_after_a_retried_cancel_answers_as_today(client, db, pauses) -> None:
    path = seed_idle(db)
    db.fail_reads(path, gexc.Aborted("transaction lock timeout"))
    first = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))
    assert first.status_code == 200, first.text

    again = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))

    # MUTATION: treat the Conflict of an already-terminal task as contention
    # and this is 503 instead of the 409 a second cancel always answered.
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "conflict"
    assert event_types(db, "task_c") == ["cancelled"]


# --------------------------------------------------------------------------
# The workflow cancel: the same transaction, once per step
# --------------------------------------------------------------------------

def two_step_workflow(client) -> tuple[str, list[str]]:
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={"steps": [
            {"step_id": "one", "runner_profile": "mock"},
            {"step_id": "two", "runner_profile": "mock"},
        ]},
    )
    assert created.status_code in (200, 201), created.text
    body = created.json()
    workflow_id = body["workflow"]["workflow_id"]
    steps = {s["step_id"]: s["task_id"] for s in body["workflow"]["steps"]}
    return workflow_id, [steps["one"], steps["two"]]


def test_a_workflow_step_aborted_once_is_still_cancelled(client, db, pauses) -> None:
    workflow_id, (one, two) = two_step_workflow(client)
    db.fail_reads(f"tasks/{two}", gexc.Aborted("transaction lock timeout"))

    response = client.post(f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("alice"))

    # MUTATION: catch contention only in the task route, not in the store
    # function both routes call, and this is a 500.
    assert response.status_code == 200, response.text
    assert sorted(response.json()["tasks_cancelled"]) == sorted([one, two])
    assert db.docs[f"tasks/{two}"]["state"] == "CANCELLED"


def test_a_contended_workflow_step_answers_the_refusal_and_the_rest_are_cancelled(
    client, db, pauses
) -> None:
    workflow_id, (one, two) = two_step_workflow(client)
    db.fail_every_read(f"tasks/{one}", gexc.Aborted("transaction lock timeout"))

    response = client.post(f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("alice"))

    # MUTATION: let `CancelContended` escape `Store.cancel_workflow` and the
    # step after the contended one is never cancelled.
    assert response.status_code == 503, response.text
    assert int(response.headers["Retry-After"]) >= 1
    body = response.json()
    assert body["code"] == "cancel_contended", body
    assert body["detail"]["tasks_contended"] == [one]
    assert body["detail"]["tasks_cancelled"] == [two]
    assert db.docs[f"tasks/{two}"]["state"] == "CANCELLED"
    assert db.docs[f"tasks/{one}"]["state"] != "CANCELLED"

    # The caller's retry finishes the job; the step already ended reads as
    # already terminal, as it always did.
    db.heal(f"tasks/{one}")
    retried = client.post(f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("alice"))
    assert retried.status_code == 200, retried.text
    assert retried.json()["tasks_cancelled"] == [one]
    assert retried.json()["tasks_already_terminal"] == [two]
