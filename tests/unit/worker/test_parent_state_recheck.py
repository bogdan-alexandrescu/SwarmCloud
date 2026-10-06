"""A step runs only once every signed parent has SUCCEEDED (contract request 34, decision 7).

The signature covers a step's spec, not its `state`. A tenant agent can write a
parked step back to READY without touching `depends_on`, the scheduler admits
it, and the signature still verifies -- so in contract request 33's chain
`fix` could start before `post-verdict` had posted the review. The worker now
re-reads each parent named by the VERIFIED task's signed `depends_on`, through
the tenant gate, right after `_verify_spec`; one that has not SUCCEEDED parks
the task as DEPENDENCY_INCOMPLETE, gives the lease back and runs nothing. The
scheduler's dependency sweep returns it to READY when the parents finish.

What is asserted, as properties: a parked task carries the reason and the
parents it waits on; the lease and every pool it held are released; no runner
child is started, no checkpoint restored, no secret or git token read; the ids
come from the verified snapshot's `depends_on` and from nothing else; a parent
of another tenant is never used.
"""

from __future__ import annotations

from typing import Any

import pytest

import spec_keys
from agent_worker import lifecycle
from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode, TenantMismatchError
from worker_seeds import TENANT, seed_attempt
from fakes import FakeSecretClient, FakeTransactionRunner, RecordingQuotaReporter
from swarm_common.states import EventType, ParkReason, TaskState

TASK = "task_child"
PARENTS = ["task_impl", "task_review"]


def _seed_parent(db, task_id: str, state: TaskState | str, *, tenant: str = TENANT) -> None:
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id,
            "tenant_id": tenant,
            "state": state.value if isinstance(state, TaskState) else state,
        },
    )


def _seed_child(db, *, depends_on: Any = PARENTS, pool_active: int = 4) -> dict[str, Any]:
    seed_attempt(
        db,
        task_id=TASK,
        pool_active=pool_active,
        task_input={"prompt": "fix", "steps": 1, "sleep_seconds": 0.01},
    )
    doc = db.doc(f"tasks/{TASK}")
    doc["workflow_id"] = "wf_1"
    doc["step_id"] = "fix"
    doc["depends_on"] = depends_on
    spec_keys.sign_document(doc, TASK)
    return doc


class Witness:
    def __init__(self) -> None:
        self.seen: list[str] = []


@pytest.fixture
def witness(monkeypatch) -> Witness:
    """Records every step a parked attempt must never reach."""
    w = Witness()
    original_child = lifecycle.ChildProcess

    def child(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        w.seen.append("runner child")
        return original_child(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "ChildProcess", child)

    original_restore = lifecycle.Worker._restore_checkpoint

    def restore(self, task):  # type: ignore[no-untyped-def]
        w.seen.append("checkpoint restore")
        return original_restore(self, task)

    monkeypatch.setattr(lifecycle.Worker, "_restore_checkpoint", restore)

    original_token = lifecycle.Worker._git_token

    def git_token(self):  # type: ignore[no-untyped-def]
        w.seen.append("git token")
        return original_token(self)

    monkeypatch.setattr(lifecycle.Worker, "_git_token", git_token)

    original_recheck = lifecycle._recheck_runner_input

    def recheck(runner_profile, stored):  # type: ignore[no-untyped-def]
        w.seen.append("input recheck")
        return original_recheck(runner_profile, stored)

    monkeypatch.setattr(lifecycle, "_recheck_runner_input", recheck)
    return w


@pytest.fixture
def parent_reads(monkeypatch) -> list[list[str]]:
    """The id lists `fetch_parent_states` was asked for, in call order."""
    calls: list[list[str]] = []
    original = ControlPlane.fetch_parent_states

    def fetch(self, parent_ids):  # type: ignore[no-untyped-def]
        calls.append(list(parent_ids))
        return original(self, parent_ids)

    monkeypatch.setattr(ControlPlane, "fetch_parent_states", fetch)
    return calls


def _assert_parked_without_running(db, witness: Witness, rc: int, waiting_on: list[str]) -> None:
    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.PARKED, (rc, task.get("last_error"))
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    assert task["current_lease_id"] is None
    assert task["blocked_by"][0]["waiting_on"] == sorted(waiting_on), task["blocked_by"]
    # The lease came back, and every pool it held came back down by one.
    lease = db.doc("leases/lease_1")
    assert lease["released_at"] is not None
    assert lease["release_reason"] == f"parked:{ParkReason.DEPENDENCY_INCOMPLETE.value}"
    for pool in lease["pools"]:
        assert db.doc(f"pools/{pool}")["active"] == 3, pool
    types = db.event_types(TASK)
    assert EventType.PARKED.value in types, types
    assert EventType.LEASE_RELEASED.value in types, types
    assert EventType.SUCCEEDED.value not in types, types
    # Nothing ran, and nothing the run would have needed was touched.
    assert witness.seen == [], f"a parked attempt still reached: {witness.seen}"


# ---------------------------------------------------------------------------
# The control: every parent SUCCEEDED
# ---------------------------------------------------------------------------


def test_a_step_whose_signed_parents_all_succeeded_runs(db, worker_factory, parent_reads):
    _seed_child(db)
    for parent in PARENTS:
        _seed_parent(db, parent, TaskState.SUCCEEDED)
    worker, _, _ = worker_factory(task_id=TASK)

    rc = worker.run()

    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.OK, (rc, task.get("last_error"))
    assert task["state"] == TaskState.SUCCEEDED.value
    assert parent_reads == [PARENTS], parent_reads


def test_a_step_with_no_parents_reads_none(db, worker_factory, parent_reads):
    _seed_child(db, depends_on=[])
    worker, _, _ = worker_factory(task_id=TASK)

    assert worker.run() == ExitCode.OK
    assert parent_reads == [], parent_reads


# ---------------------------------------------------------------------------
# A parent that has not SUCCEEDED parks the step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        TaskState.QUEUED,
        TaskState.READY,
        TaskState.PARKED,
        TaskState.LEASED,
        TaskState.RUNNING,
        # A failed parent parks too: the scheduler's sweep, not the worker,
        # cancels a step whose parent did not succeed.
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.DEAD_LETTERED,
    ],
)
def test_a_parent_that_has_not_succeeded_parks_the_step_without_running(
    state, db, worker_factory, witness
):
    _seed_child(db)
    _seed_parent(db, "task_impl", TaskState.SUCCEEDED)
    _seed_parent(db, "task_review", state)
    worker, _, _ = worker_factory(task_id=TASK)

    rc = worker.run()

    _assert_parked_without_running(db, witness, rc, ["task_review"])
    assert db.doc(f"tasks/{TASK}")["blocked_by"][0]["parent_states"] == {
        "task_review": state.value
    }


def test_a_parent_with_no_document_parks_the_step(db, worker_factory, witness):
    _seed_child(db)
    _seed_parent(db, "task_impl", TaskState.SUCCEEDED)
    worker, _, _ = worker_factory(task_id=TASK)

    rc = worker.run()

    _assert_parked_without_running(db, witness, rc, ["task_review"])
    assert db.doc(f"tasks/{TASK}")["blocked_by"][0]["parent_states"] == {"task_review": None}


def test_a_parent_with_a_state_the_contract_does_not_name_parks_the_step(
    db, worker_factory, witness
):
    _seed_child(db)
    _seed_parent(db, "task_impl", TaskState.SUCCEEDED)
    _seed_parent(db, "task_review", "succeeded")  # not the enum's value
    worker, _, _ = worker_factory(task_id=TASK)

    _assert_parked_without_running(db, witness, worker.run(), ["task_review"])


def test_the_park_reads_no_secret(db, worker_factory, witness):
    _seed_child(db)
    _seed_parent(db, "task_impl", TaskState.RUNNING)
    _seed_parent(db, "task_review", TaskState.READY)
    secrets = FakeSecretClient()
    worker, _, _ = worker_factory(task_id=TASK, secret_client=secrets)

    _assert_parked_without_running(db, witness, worker.run(), PARENTS)
    assert secrets.accessed == [], secrets.accessed


# ---------------------------------------------------------------------------
# The ids come from the verified, signed `depends_on`, and only from it
# ---------------------------------------------------------------------------


def test_the_parents_read_are_the_verified_snapshots_not_a_fresh_read(
    db, worker_factory, witness, parent_reads, monkeypatch
):
    """A write landing just after verification does not change which parents are read.

    It empties the STORED `depends_on` the moment `_verify_spec` returns. A
    worker that re-fetched the task for the parent check would see no parents
    and run; one that uses the verified snapshot parks.
    """
    doc = _seed_child(db)
    _seed_parent(db, "task_impl", TaskState.SUCCEEDED)
    _seed_parent(db, "task_review", TaskState.RUNNING)
    original_verify = lifecycle.Worker._verify_spec

    def verify_spec(self, task, create_time):  # type: ignore[no-untyped-def]
        original_verify(self, task, create_time)
        doc["depends_on"] = []

    monkeypatch.setattr(lifecycle.Worker, "_verify_spec", verify_spec)
    worker, _, _ = worker_factory(task_id=TASK)

    rc = worker.run()

    assert parent_reads == [PARENTS], parent_reads
    _assert_parked_without_running(db, witness, rc, ["task_review"])


def test_dropping_an_unfinished_parent_from_depends_on_is_refused_not_run(
    db, worker_factory, witness, parent_reads
):
    """The rewrite that would dodge the re-check breaks the signature instead."""
    doc = _seed_child(db)
    _seed_parent(db, "task_impl", TaskState.SUCCEEDED)
    _seed_parent(db, "task_review", TaskState.RUNNING)
    doc["depends_on"] = ["task_impl"]
    worker, _, _ = worker_factory(task_id=TASK)

    rc = worker.run()

    task = db.doc(f"tasks/{TASK}")
    assert rc == ExitCode.FAILED, rc
    assert task["end_cause"] == "spec_signature_invalid", task.get("end_cause")
    assert parent_reads == [], "parents were read for a spec that failed verification"
    assert witness.seen == [], witness.seen


def test_parents_named_only_in_an_unsigned_field_are_never_read(
    db, worker_factory, parent_reads
):
    """`metadata` keys outside the signed set are not a source of parent ids."""
    doc = _seed_child(db, depends_on=["task_impl"])
    _seed_parent(db, "task_impl", TaskState.SUCCEEDED)
    _seed_parent(db, "task_review", TaskState.RUNNING)
    doc.setdefault("metadata", {})["depends_on"] = ["task_review"]
    doc["blocked_by"] = [{"waiting_on": ["task_review"]}]
    worker, _, _ = worker_factory(task_id=TASK)

    assert worker.run() == ExitCode.OK
    assert parent_reads == [["task_impl"]], parent_reads


def test_the_parent_check_runs_after_verification_and_before_the_input_recheck(
    db, worker_factory, monkeypatch
):
    _seed_child(db)
    for parent in PARENTS:
        _seed_parent(db, parent, TaskState.SUCCEEDED)
    order: list[str] = []
    original_verify = lifecycle.Worker._verify_spec
    original_fetch = ControlPlane.fetch_parent_states
    original_recheck = lifecycle._recheck_runner_input

    def verify_spec(self, task, create_time):  # type: ignore[no-untyped-def]
        order.append("verify_spec")
        return original_verify(self, task, create_time)

    def fetch(self, parent_ids):  # type: ignore[no-untyped-def]
        order.append("parent_states")
        return original_fetch(self, parent_ids)

    def recheck(runner_profile, stored):  # type: ignore[no-untyped-def]
        order.append("recheck_runner_input")
        return original_recheck(runner_profile, stored)

    monkeypatch.setattr(lifecycle.Worker, "_verify_spec", verify_spec)
    monkeypatch.setattr(ControlPlane, "fetch_parent_states", fetch)
    monkeypatch.setattr(lifecycle, "_recheck_runner_input", recheck)
    worker, _, _ = worker_factory(task_id=TASK)

    assert worker.run() == ExitCode.OK
    assert order == ["verify_spec", "parent_states", "recheck_runner_input"], order


# ---------------------------------------------------------------------------
# The tenant gate
# ---------------------------------------------------------------------------


def test_a_parent_of_another_tenant_stops_the_worker_writing_nothing(
    db, worker_factory, witness
):
    _seed_child(db)
    _seed_parent(db, "task_impl", TaskState.SUCCEEDED)
    _seed_parent(db, "task_review", TaskState.SUCCEEDED, tenant="other-tenant")
    worker, _, _ = worker_factory(task_id=TASK)
    parent_before = dict(db.doc("tasks/task_review"))

    rc = worker.run()

    assert rc == ExitCode.TENANT_MISMATCH, rc
    task = db.doc(f"tasks/{TASK}")
    # Neither run nor parked: its SUCCEEDED state was another tenant's, and
    # the worker does not act on it. The reconciler reclaims the silent lease.
    assert task["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("tasks/task_review") == parent_before
    assert witness.seen == [], witness.seen


def test_a_malformed_depends_on_is_refused_not_skipped(db, worker_factory, witness):
    _seed_child(db, depends_on="task_impl")
    _seed_parent(db, "task_impl", TaskState.SUCCEEDED)
    worker, _, _ = worker_factory(task_id=TASK)

    rc = worker.run()

    task = db.doc(f"tasks/{TASK}")
    assert rc != ExitCode.OK, rc
    assert task["state"] == TaskState.FAILED.value
    assert witness.seen == [], witness.seen


# ---------------------------------------------------------------------------
# ControlPlane.fetch_parent_states on its own
# ---------------------------------------------------------------------------


def _control(db) -> ControlPlane:
    import io

    from agent_worker.logs import build_logger

    logger = build_logger(
        task_id=TASK,
        attempt_id="att_1",
        tenant_id=TENANT,
        generation=1,
        runner_profile="mock",
        stream=io.StringIO(),
    )
    return ControlPlane(
        db,
        task_id=TASK,
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=1,
        logger=logger,
        txn_runner=FakeTransactionRunner(db),
        quota_reporter=RecordingQuotaReporter(),
    )


def test_fetch_parent_states_reads_each_distinct_parent_once(db):
    _seed_parent(db, "a", TaskState.SUCCEEDED)
    _seed_parent(db, "b", TaskState.RUNNING)
    _seed_parent(db, "c", "not-a-state")

    states = _control(db).fetch_parent_states(["b", "a", "b", "missing", "c"])

    assert states == {
        "b": TaskState.RUNNING,
        "a": TaskState.SUCCEEDED,
        "missing": None,
        "c": None,
    }
    assert list(states) == ["b", "a", "missing", "c"]


def test_fetch_parent_states_refuses_another_tenants_parent(db):
    _seed_parent(db, "a", TaskState.SUCCEEDED)
    _seed_parent(db, "theirs", TaskState.SUCCEEDED, tenant="other-tenant")

    with pytest.raises(TenantMismatchError) as caught:
        _control(db).fetch_parent_states(["a", "theirs"])
    assert caught.value.document_id == "theirs"


def test_fetch_parent_states_refuses_a_parent_with_no_tenant(db):
    db.seed("tasks/orphan", {"id": "orphan", "state": TaskState.SUCCEEDED.value})

    with pytest.raises(TenantMismatchError):
        _control(db).fetch_parent_states(["orphan"])
