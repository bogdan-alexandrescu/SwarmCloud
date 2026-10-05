"""A worker killed between `finish`'s terminal write and its attempt end is found (#380).

`ControlPlane.finish` writes the task's terminal state first, then
`record_attempt_end`, then the lease release, and the account goes back from
the run's `finally`. A worker killed after the first write leaves the task
terminal, its attempt with no `completed_at`, the generation unchanged and the
Job exited. Nothing fences a terminal task, so the reconciler's release at the
fence never ran, and the broker's sweep releases only an attempt that is fenced
or recorded its end. The attempt's account hold counted until its 3-hour TTL.

The owner's decision, 2026-10-05: a reconciler finding, `lost_after_finish`.

Pinned here:

  L-1  The window case is repaired after the grace and not before: the
       attempt's end is recorded with cause `lost_after_finish`, and its
       stamped account hold is released through the broker's own release.
  L-2  An UNREADABLE backend listing holds the finding back, on the report,
       with nothing written (#532's rule). A terminal task alone is never
       proof (#372): a live execution of the attempt is not this rule's.
  L-3  A newer generation is untouched: not found when the snapshot sees it,
       and refused inside the repair's transaction when it arrives after.
  L-4  A hold with no stamps still waits for its TTL.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransaction, FakeTransactionRunner

from quota_broker.accounts import Hold, holds_from_firestore, holds_to_firestore
from quota_broker.main import _release_attempt_holds_everywhere

from reconciler.config import ReconcilerConfig
from reconciler.detect import FindingKind, detect_lost_after_finish
from reconciler.logs import build_logger
from reconciler.model import AttemptView, ExecutionPhase, ExecutionView, TaskView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import TaskState

TENANT = "eng"
ACCOUNT = f"{TENANT}:personal"
JOB = "projects/p/locations/us-central1/jobs/swarm-eng-mock"
EXECUTION = f"{JOB}/executions/x1"
GRACE = 300


class BrokerFirestore(FakeFirestore):
    """The worker suite's store, with the `transaction()` the broker opens."""

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)


@pytest.fixture
def db(monkeypatch) -> BrokerFirestore:
    # The broker's release runs under `@firestore.transactional`; the fake
    # transaction applies writes as they are made, so the body is run as is.
    from google.cloud import firestore

    monkeypatch.setattr(firestore, "transactional", lambda fn: fn)
    return BrokerFirestore()


class BrokerReleaser:
    """The broker's own route body (`POST /v1/holds/release-attempt`), in process."""

    def __init__(self, db: FakeFirestore) -> None:
        self._db = db
        self.calls: list[tuple[str, str]] = []

    def release_attempt(self, *, task_id: str, attempt_id: str) -> int:
        self.calls.append((task_id, attempt_id))
        result = _release_attempt_holds_everywhere(
            self._db, task_id=task_id, attempt_id=attempt_id, now=utcnow()
        )
        return int(result["released"])


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
        enable_checkpoint_gc=False,
        lost_after_finish_grace_seconds=GRACE,
    )


def seed_window(
    db: FakeFirestore,
    *,
    finished_seconds_ago: int,
    task_generation: int = 3,
    attempt_generation: int = 3,
    stamped: bool = True,
    lease_released: bool = True,
) -> dict[str, Any]:
    """A task `finish` made SUCCEEDED, whose worker died before its attempt end."""
    now = utcnow()
    finished = now - timedelta(seconds=finished_seconds_ago)
    db.seed(
        "tasks/task_1",
        {
            "id": "task_1", "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": task_generation, "current_lease_id": None,
            "attempt_count": 1, "max_attempts": 3, "updated_at": finished,
            "completed_at": finished, "cancel_requested": False, "end_cause": None,
            "last_error": None,
        },
    )
    db.seed(
        "attempts/att_1",
        {
            "attempt_id": "att_1", "task_id": "task_1", "tenant_id": TENANT,
            "generation": attempt_generation, "lease_id": "lease_1",
            "backend": "CLOUD_RUN_JOB", "execution_name": EXECUTION,
            "created_at": now - timedelta(hours=1),
            "started_at": now - timedelta(minutes=59),
        },
    )
    db.seed(
        "leases/lease_1",
        {
            "lease_id": "lease_1", "task_id": "task_1", "attempt_id": "att_1",
            "tenant_id": TENANT, "generation": attempt_generation, "pools": [],
            "units": 1, "state": TaskState.RUNNING.value,
            "created_at": now - timedelta(hours=1),
            "dispatch_deadline": now - timedelta(minutes=52),
            "expires_at": now + timedelta(minutes=5),
            "heartbeat_at": finished,
            "released_at": finished if lease_released else None,
        },
    )
    expires = now + timedelta(hours=2)
    holds = [
        Hold(
            assignment_id="asg-old", tenant_id=TENANT, expires_at=expires,
        ),
    ]
    if stamped:
        holds.insert(
            0,
            Hold(
                assignment_id="asg-1", tenant_id=TENANT, expires_at=expires,
                task_id="task_1", attempt_id="att_1",
            ),
        )
    db.seed(
        f"accounts/{ACCOUNT}",
        {"holds": holds_to_firestore(holds), "assigned": len(holds)},
    )
    return {"expires_at": expires}


def live_holds(db: FakeFirestore) -> list[tuple[str, str]]:
    doc = db.doc(f"accounts/{ACCOUNT}")
    holds = holds_from_firestore(doc["holds"])
    assert doc["assigned"] == len(holds)
    return sorted((h.task_id or "", h.attempt_id or "") for h in holds)


def execution(**overrides: Any) -> ExecutionView:
    shape: dict[str, Any] = dict(
        name=EXECUTION, backend="CLOUD_RUN_JOB", phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(hours=1), task_id="task_1", attempt_id="att_1",
        tenant_id=TENANT, generation=3, parent=JOB,
    )
    shape.update(overrides)
    return ExecutionView(**shape)


def build(
    db: FakeFirestore,
    config: ReconcilerConfig,
    *,
    executions: list[Any] | None = None,
    list_raises: Exception | None = None,
    releaser: BrokerReleaser | None = None,
    store_class: type[ControlStore] = ControlStore,
) -> tuple[Reconciler, BrokerReleaser]:
    logger = build_logger(stream=io.StringIO())
    store = store_class(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    backend = FakeBackend(
        executions=executions or [], journal=db.writes, list_raises=list_raises
    )
    releaser = releaser or BrokerReleaser(db)
    rec = Reconciler(
        store=store, backends=[backend], config=config, logger=logger,
        hold_releaser=releaser,
    )
    return rec, releaser


def lost(report: Any) -> list[Any]:
    return [o for o in report.outcomes if o.kind == FindingKind.LOST_AFTER_FINISH.value]


# ---------------------------------------------------------------------------
# L-1: after the grace, and not before
# ---------------------------------------------------------------------------


def test_the_window_case_is_repaired_after_the_grace(db, config):
    seed_window(db, finished_seconds_ago=GRACE + 60)
    task_before = dict(db.doc("tasks/task_1"))
    rec, releaser = build(db, config)

    report = rec.run_once()

    outcomes = lost(report)
    assert len(outcomes) == 1, [o.as_dict() for o in report.outcomes]
    attempt = db.doc("attempts/att_1")
    assert attempt["completed_at"] is not None
    assert attempt["exit_code"] is None
    assert attempt["error"].startswith("lost_after_finish:"), attempt["error"]
    assert releaser.calls == [("task_1", "att_1")]
    assert live_holds(db) == [("", "")], "the attempt's stamped hold was not released"
    assert "released 1 account hold(s) of att_1" in outcomes[0].actions
    # The task is the worker's: its state, generation and end cause stand.
    assert db.doc("tasks/task_1") == task_before


def test_the_window_case_is_not_repaired_inside_the_grace(db, config):
    seed_window(db, finished_seconds_ago=GRACE - 60)
    attempt_before = dict(db.doc("attempts/att_1"))
    rec, releaser = build(db, config)

    report = rec.run_once()

    assert lost(report) == []
    assert db.doc("attempts/att_1") == attempt_before
    assert releaser.calls == []
    assert live_holds(db) == [("", ""), ("task_1", "att_1")]


def test_the_window_with_its_lease_still_held_is_repaired_too(db, config):
    """The lease `finish` never got to release: the lease rules return the slot
    as they always did, and this rule ends the attempt and its hold."""
    seed_window(db, finished_seconds_ago=GRACE + 60, lease_released=False)
    rec, releaser = build(db, config)

    report = rec.run_once()

    kinds = [o.kind for o in report.outcomes]
    assert kinds.count("lost_after_finish") == 1, kinds
    assert "orphan_lease" in kinds, kinds
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("attempts/att_1")["completed_at"] is not None
    assert live_holds(db) == [("", "")]


def test_an_attempt_that_recorded_its_end_is_not_found(db, config):
    seed_window(db, finished_seconds_ago=GRACE + 60)
    db.doc("attempts/att_1")["completed_at"] = utcnow() - timedelta(seconds=GRACE)
    rec, releaser = build(db, config)

    report = rec.run_once()

    assert lost(report) == []
    assert releaser.calls == []


def test_a_dry_run_writes_nothing(db, config):
    seed_window(db, finished_seconds_ago=GRACE + 60)
    attempt_before = dict(db.doc("attempts/att_1"))
    rec, releaser = build(db, replace(config, dry_run=True))

    report = rec.run_once()

    assert [o.skipped for o in lost(report)] == ["dry_run"]
    assert db.doc("attempts/att_1") == attempt_before
    assert releaser.calls == []


# ---------------------------------------------------------------------------
# L-2: unreadable holds; a terminal task alone is not proof
# ---------------------------------------------------------------------------


def test_an_unreadable_backend_holds_the_finding(db, config):
    seed_window(db, finished_seconds_ago=GRACE + 600)
    attempt_before = dict(db.doc("attempts/att_1"))
    rec, releaser = build(db, config, list_raises=OSError("listing refused"))

    report = rec.run_once()

    assert lost(report) == []
    assert [s.kind for s in report.suppressed] == ["lost_after_finish"]
    assert report.suppressed[0].backend == "CLOUD_RUN_JOB"
    assert db.doc("attempts/att_1") == attempt_before
    assert releaser.calls == []
    assert live_holds(db) == [("", ""), ("task_1", "att_1")]


def test_a_live_execution_of_the_attempt_is_not_this_rules(db, config):
    """#372: the task is terminal, and its Job is still running the agent."""
    seed_window(db, finished_seconds_ago=GRACE + 600)
    rec, releaser = build(db, config, executions=[execution()])

    report = rec.run_once()

    assert lost(report) == []
    assert db.doc("attempts/att_1").get("completed_at") is None
    assert releaser.calls == []


def test_a_live_execution_named_only_by_its_recorded_name_is_not_this_rules(db, config):
    """No ids on the listing (the #372 shape), but the exact name the attempt recorded."""
    seed_window(db, finished_seconds_ago=GRACE + 600)
    rec, releaser = build(
        db, config, executions=[execution(task_id=None, attempt_id=None, tenant_id=None)]
    )

    report = rec.run_once()

    assert lost(report) == []
    assert releaser.calls == []


def test_an_ended_execution_of_the_attempt_does_not_hold_it(db, config):
    """The control for the two above: the same execution, over."""
    seed_window(db, finished_seconds_ago=GRACE + 60)
    over = execution(phase=ExecutionPhase.SUCCEEDED, ended=True)
    rec, releaser = build(db, config, executions=[over])

    report = rec.run_once()

    assert len(lost(report)) == 1
    assert releaser.calls == [("task_1", "att_1")]


# ---------------------------------------------------------------------------
# L-3: a newer generation is never touched
# ---------------------------------------------------------------------------


def test_an_attempt_of_an_older_generation_is_not_found(db, config):
    """Fenced already: the broker's sweep owns it, and nothing here writes."""
    seed_window(db, finished_seconds_ago=GRACE + 60, task_generation=4)
    attempt_before = dict(db.doc("attempts/att_1"))
    rec, releaser = build(db, config)

    report = rec.run_once()

    assert lost(report) == []
    assert db.doc("attempts/att_1") == attempt_before
    assert releaser.calls == []


class GenerationMovesAfterTheSnapshot(ControlStore):
    """The task is fenced between the pass's read and the repair's transaction."""

    def record_lost_attempt_end(self, *args: Any, **kwargs: Any) -> bool:
        task = self._db.doc("tasks/task_1")
        task["current_generation"] = int(task["current_generation"]) + 1
        return super().record_lost_attempt_end(*args, **kwargs)


def test_a_generation_that_moved_before_the_repair_is_refused(db, config):
    seed_window(db, finished_seconds_ago=GRACE + 60)
    attempt_before = dict(db.doc("attempts/att_1"))
    rec, releaser = build(db, config, store_class=GenerationMovesAfterTheSnapshot)

    report = rec.run_once()

    outcomes = lost(report)
    assert len(outcomes) == 1
    assert any("did NOT record" in a for a in outcomes[0].actions), outcomes[0].actions
    assert db.doc("attempts/att_1") == attempt_before
    assert releaser.calls == [], "a hold was released for an attempt the repair did not end"
    assert live_holds(db) == [("", ""), ("task_1", "att_1")]


# ---------------------------------------------------------------------------
# L-4: an unstamped hold waits for its TTL
# ---------------------------------------------------------------------------


def test_a_hold_with_no_stamps_still_waits_for_its_ttl(db, config):
    seeded = seed_window(db, finished_seconds_ago=GRACE + 60, stamped=False)
    rec, releaser = build(db, config)

    report = rec.run_once()

    assert len(lost(report)) == 1
    assert releaser.calls == [("task_1", "att_1")]
    assert live_holds(db) == [("", "")]
    (hold,) = holds_from_firestore(db.doc(f"accounts/{ACCOUNT}")["holds"])
    assert hold.expires_at == seeded["expires_at"], "an unstamped hold's TTL moved"
    assert not any("account hold" in a for a in lost(report)[0].actions)


# ---------------------------------------------------------------------------
# The rule itself, and its setting
# ---------------------------------------------------------------------------


def _task(**overrides: Any) -> TaskView:
    now = utcnow()
    shape: dict[str, Any] = dict(
        task_id="task_1", tenant_id=TENANT, state=TaskState.FAILED, generation=3,
        lease_id=None, runner_profile="mock", resource_class="standard",
        updated_at=now - timedelta(seconds=GRACE + 1),
        completed_at=now - timedelta(seconds=GRACE + 1),
    )
    shape.update(overrides)
    return TaskView(**shape)


def _attempt(**overrides: Any) -> AttemptView:
    shape: dict[str, Any] = dict(
        attempt_id="att_1", task_id="task_1", tenant_id=TENANT, generation=3,
        backend="CLOUD_RUN_JOB", execution_name=EXECUTION, created_at=None,
    )
    shape.update(overrides)
    return AttemptView(**shape)


def test_the_rule_finds_the_window_and_names_its_attempt(config):
    (finding,) = detect_lost_after_finish([(_task(), _attempt())], [], config)

    assert finding.kind is FindingKind.LOST_AFTER_FINISH
    assert (finding.task_id, finding.attempt_id, finding.generation) == ("task_1", "att_1", 3)
    assert finding.lease_id is None, "this rule releases no lease"


@pytest.mark.parametrize(
    "task, attempt",
    [
        (_task(state=TaskState.RUNNING), _attempt()),
        (_task(generation=4), _attempt()),
        (_task(), _attempt(task_id="task_2")),
        (_task(), _attempt(completed_at=utcnow())),
        (_task(completed_at=None, updated_at=None), _attempt()),
    ],
    ids=["not-terminal", "newer-generation", "another-task", "ended", "no-clock"],
)
def test_the_rule_refuses_anything_but_the_window(config, task, attempt):
    assert detect_lost_after_finish([(task, attempt)], [], config) == []


def test_two_lost_attempts_are_two_findings(config):
    pairs = [
        (_task(), _attempt()),
        (_task(task_id="task_2"), _attempt(attempt_id="att_2", task_id="task_2")),
    ]
    assert len(detect_lost_after_finish(pairs, [], config)) == 2


def test_the_grace_is_a_setting_with_a_default(monkeypatch):
    from swarm_common.config import Settings

    default = ReconcilerConfig(project_id="p", region="r", firestore_database="swarm")
    assert default.lost_after_finish_grace_seconds == 300
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("LOST_AFTER_FINISH_GRACE_SECONDS", "420")
    assert ReconcilerConfig.from_env(Settings.from_env()).lost_after_finish_grace_seconds == 420
