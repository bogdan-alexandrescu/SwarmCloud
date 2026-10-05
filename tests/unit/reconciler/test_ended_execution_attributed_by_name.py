"""An ended execution is proof the worker is gone even when it carries no attempt id (#532).

#532 asks that an execution that ENDED -- completion time set, a failed or
succeeded count -- be read as proof that its worker is gone, and the lease
repaired through the lost-worker path: fenced, released, requeued. Main did
that only for an execution whose environment or labels carried the attempt id.

The #372 shape is an execution under our own job that came back from the
listing with its TASK id and no attempt id. Such an execution is still the
attempt's own: the scheduler recorded its exact name on the attempt document
(`attempt.execution_name`). When it had ended, the lease read as having no
execution at all, so its stale_lease and missing_execution waited on a GET by
name. With that GET refused, as it may have been on 2026-10-03, the lease was
held on every pass. That is the five-hour shape #532 reports, with the backend
fully readable and the proof sitting in the listing.

Pinned here:

  N-1  A listed, ended execution named exactly as the attempt recorded is that
       attempt's, and a stale lease over it is repaired as a lost worker with
       no probe.
  N-2  The attribution goes no wider than the name. Another execution of the
       same task, an execution that names a different task, one Cloud Run is
       still reconciling, and one at another generation are each held.
  N-3  #450 is unchanged: an attempt whose execution is MISSING from the
       listing is still held when the GET is refused.
"""

from __future__ import annotations

import io
from typing import Any

from fakes import FakeFirestore, FakeTransactionRunner
from test_backend_identity import FakeJobsClient, run_execution, run_job
from test_reconciler_missing_execution_needs_proof import (
    EXECUTION,
    JOB,
    PROJECT,
    REGION,
    ProbedExecutions,
    ids_env,
    seed,
)
from test_reconciler_safety import TENANT

from reconciler.backends import CloudRunBackend
from reconciler.config import ReconcilerConfig
from reconciler.detect import ended_executions_by_attempt
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore

import pytest


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region=REGION,
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
        enable_checkpoint_gc=False,
    )


def refused() -> Exception:
    """The GET by name answers 403."""
    from google.api_core import exceptions as gapi_exceptions

    return gapi_exceptions.PermissionDenied("run.executions.get denied")


def task_only_env(**overrides: str) -> dict[str, str]:
    """The #372 shape: the task id came back, the attempt id did not."""
    return ids_env(ATTEMPT_ID="", **overrides)


def ended(**overrides: Any) -> Any:
    """The attempt's own execution as Cloud Run listed it: over, failed."""
    shape: dict[str, Any] = dict(
        name=EXECUTION, env=task_only_env(), running=0, failed=1, completed=True
    )
    shape.update(overrides)
    return run_execution(**shape)


def run(db: FakeFirestore, config: ReconcilerConfig, listed: list[Any]) -> tuple[Any, list[str]]:
    job = run_job(name=JOB, labels={"managed-by": "swarm-scheduler", "swarm-tenant": TENANT})
    logger = build_logger(stream=io.StringIO())
    backend = CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([job]),
        executions_client=ProbedExecutions({JOB: listed}, get_raises=refused()),
        logger=logger,
    )
    terminated: list[str] = []
    backend.terminate = lambda execution: terminated.append(execution.name) or True  # type: ignore[method-assign]
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    rec = Reconciler(store=store, backends=[backend], config=config, logger=logger)
    return rec.run_once(), terminated


def held(db: FakeFirestore, report: Any) -> None:
    assert [o.as_dict() for o in report.outcomes if o.released] == []
    assert db.documents["leases/lease_1"]["released_at"] is None
    assert db.documents["tasks/task_1"]["current_generation"] == 1
    assert db.documents["tasks/task_1"]["state"] == "RUNNING"
    assert report.suppressed, "a hold must be on the report, not silent"


# ---------------------------------------------------------------------------
# N-1: the recorded name ties an ended execution to its attempt
# ---------------------------------------------------------------------------


def test_an_ended_execution_named_as_the_attempt_recorded_is_proof(db, config):
    seed(db, heartbeat_seconds_ago=300)
    before = db.documents["pools/global"]["active"]

    report, terminated = run(db, config, [ended()])

    assert report.suppressed == [], [s.as_dict() for s in report.suppressed]
    assert [o.kind for o in report.outcomes] == ["stale_lease"], (
        [o.as_dict() for o in report.outcomes]
    )
    outcome = report.outcomes[0]
    assert outcome.released is True
    assert outcome.invalidated_to == 2
    assert outcome.repaired_to == "READY"
    assert "has ended" in outcome.reason
    assert terminated == [], "an ended execution has nothing left to kill"
    assert db.documents["leases/lease_1"]["released_at"] is not None
    assert db.documents["pools/global"]["active"] == before - 1, "capacity was not returned"
    assert db.documents["tasks/task_1"]["current_generation"] == 2
    assert db.documents["tasks/task_1"]["state"] == "READY"


def test_an_attributed_ended_execution_is_still_proof(db, config):
    """The control for N-1's fixture: the same lease and listing, with the
    attempt id present, was already repaired on main. Only the attribution
    differs between this test and the one above."""
    seed(db, heartbeat_seconds_ago=300)

    report, _ = run(db, config, [ended(env=ids_env())])

    assert [o.released for o in report.outcomes] == [True]


# ---------------------------------------------------------------------------
# N-2: no wider than the name
# ---------------------------------------------------------------------------


def test_another_execution_of_the_same_task_is_not_proof(db, config):
    """An earlier or a CI execution of the same task is not this attempt's."""
    seed(db, heartbeat_seconds_ago=300)

    report, _ = run(db, config, [ended(name=f"{JOB}/executions/x0")])

    held(db, report)


def test_an_execution_naming_another_task_is_not_proof(db, config):
    seed(db, heartbeat_seconds_ago=300)

    report, _ = run(db, config, [ended(env=task_only_env(TASK_ID="task_2"))])

    held(db, report)


def test_an_execution_still_reconciling_is_not_proof(db, config):
    seed(db, heartbeat_seconds_ago=300)

    report, _ = run(db, config, [ended(reconciling=True)])

    held(db, report)


def test_an_execution_of_another_generation_is_not_proof(db, config):
    seed(db, heartbeat_seconds_ago=300)

    report, _ = run(db, config, [ended(env=task_only_env(GENERATION="7"))])

    held(db, report)


# ---------------------------------------------------------------------------
# N-3: #450 stands
# ---------------------------------------------------------------------------


def test_a_missing_execution_is_still_held_without_proof_of_absence(db, config):
    seed(db, heartbeat_seconds_ago=None)

    report, _ = run(db, config, [])

    held(db, report)
    assert [s.kind for s in report.suppressed] == ["missing_execution"]


# ---------------------------------------------------------------------------
# The rule itself, without the pass around it
# ---------------------------------------------------------------------------


def _view(**overrides: Any) -> ExecutionView:
    shape: dict[str, Any] = dict(
        name=EXECUTION,
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.FAILED,
        created_at=None,
        task_id="task_1",
        attempt_id=None,
        tenant_id=TENANT,
        generation=None,
        ended=True,
    )
    shape.update(overrides)
    return ExecutionView(**shape)


def _attempt(**overrides: Any) -> Any:
    from reconciler.model import AttemptView

    shape: dict[str, Any] = dict(
        attempt_id="att_1",
        task_id="task_1",
        tenant_id=TENANT,
        generation=1,
        backend="CLOUD_RUN_JOB",
        execution_name=EXECUTION,
        created_at=None,
    )
    shape.update(overrides)
    return AttemptView(**shape)


def test_the_rule_attributes_by_name_and_carries_the_attempts_generation():
    found = ended_executions_by_attempt([_view()], {}, {"att_1": _attempt()})

    assert list(found) == ["att_1"]
    assert found["att_1"].attempt_id == "att_1"
    assert found["att_1"].generation == 1


@pytest.mark.parametrize(
    "execution, attempt",
    [
        (_view(backend="GKE_JOB"), _attempt()),
        (_view(tenant_id="other"), _attempt()),
        (_view(claim_refused=True, task_id=None), _attempt()),
        (_view(ended=False, phase=ExecutionPhase.RUNNING), _attempt()),
        (
            _view(name=f"{JOB}/executions/pending-att_1"),
            _attempt(execution_name=f"{JOB}/executions/pending-att_1"),
        ),
    ],
    ids=["other-backend", "other-tenant", "claim-refused", "not-ended", "placeholder"],
)
def test_the_rule_refuses_anything_but_the_attempts_own_execution(execution, attempt):
    assert ended_executions_by_attempt([execution], {}, {"att_1": attempt}) == {}


def test_the_rule_stands_aside_for_an_attempt_with_an_active_execution():
    active = _view(name=f"{JOB}/executions/x2", attempt_id="att_1", ended=False,
                   phase=ExecutionPhase.RUNNING)
    found = ended_executions_by_attempt([_view()], {"att_1": active}, {"att_1": _attempt()})

    assert found == {}
