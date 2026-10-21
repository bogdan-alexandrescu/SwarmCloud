"""An execution Cloud Run will not cancel, that no lease holds, is backed off (#1822).

MEASURED 2026-10-21: four Cloud Run executions (`swarm-job-eng-mock-5lrt8`,
`-rsdxp`, `-ww2gb`, `-r5877`, first failure 2026-10-04T10:33:07Z) never got a
`completion_time`. Every pass since, the reconciler tried to terminate each,
Cloud Run answered `400 ... cannot be cancelled because it is not running`,
and the pass logged "termination failed; NOT releasing the slot" -- ~11.5k
ERRORs a day and 8 Cloud Run calls a minute -- while no lease held any of them.

Pinned here:

  W-1  A "not running" refusal with NO lease naming the execution is recorded
       as wedged, logged as ONE error, and not tried again before its backoff.
  W-2  The backoff doubles per refusal and is capped at six hours.
  W-3  The same refusal WITH a lease naming the execution is today's
       behaviour: tried every pass, the slot kept, no wedged record.
  W-4  A confirmed stop of a wedged execution forgets its record.
  W-5  Any other kill failure is not a wedge, lease or no lease.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner
from test_reconciler_safety import running_execution, seed_running_task

from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import (
    WEDGED_BACKOFF_BASE_SECONDS,
    WEDGED_BACKOFF_CAP_SECONDS,
    WEDGED_EXECUTION,
    Reconciler,
)
from reconciler.store import ControlStore
from swarm_common.models import utcnow

TERMINATION_FAILED = "termination failed; NOT releasing the slot"
JOB = "projects/p/locations/us-central1/jobs/swarm-job-eng-mock"
WEDGED = f"{JOB}/executions/swarm-job-eng-mock-5lrt8"


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
    )


def not_running(name: str = WEDGED) -> Exception:
    from google.api_core import exceptions as gapi_exceptions

    return gapi_exceptions.BadRequest(
        f"Execution '{name.rsplit('/', 1)[-1]}' cannot be cancelled because it is not running."
    )


def leaseless_execution() -> ExecutionView:
    """5lrt8 as the listing returns it: running_count 0, no completion time,
    naming an attempt no lease holds and a task long gone."""
    return ExecutionView(
        name=WEDGED,
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(days=17),
        task_id="task_gone",
        attempt_id="att_gone",
        tenant_id="eng",
        generation=1,
        parent=JOB,
    )


class World:
    def __init__(self, db: FakeFirestore, config: ReconcilerConfig, backend: FakeBackend) -> None:
        self.db = db
        self.backend = backend
        self.stream = io.StringIO()
        logger = build_logger(stream=self.stream)
        store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
        self.reconciler = Reconciler(
            store=store, backends=[backend], config=config, logger=logger,
            hold_releaser=None, finish_announcer=None,
        )

    def run(self) -> Any:
        return self.reconciler.run_once()

    def kills(self) -> int:
        return sum(1 for entry in self.backend.journal if entry[0] == "terminate")

    def lines(self, message: str, severity: str | None = None) -> list[dict[str, Any]]:
        found = [json.loads(line) for line in self.stream.getvalue().splitlines() if line.strip()]
        return [
            line for line in found
            if line["message"] == message and (severity is None or line["severity"] == severity)
        ]

    def records(self) -> list[dict[str, Any]]:
        return [
            doc for path, doc in self.db.documents.items()
            if path.startswith(f"{ControlStore.WEDGED_COLLECTION}/")
        ]

    def due_now(self) -> None:
        """Move every wedged record's next try into the past."""
        for path, doc in self.db.documents.items():
            if path.startswith(f"{ControlStore.WEDGED_COLLECTION}/"):
                doc["next_try"] = utcnow() - timedelta(seconds=1)


def wedged_world(db: FakeFirestore, config: ReconcilerConfig, **backend: Any) -> World:
    return World(
        db,
        config,
        FakeBackend(
            executions=[leaseless_execution()],
            terminate_raises=backend.pop("terminate_raises", not_running()),
            **backend,
        ),
    )


# ---------------------------------------------------------------------------
# W-1
# ---------------------------------------------------------------------------


def test_a_leaseless_not_running_refusal_is_marked_wedged_once_and_not_retried(db, config):
    world = wedged_world(db, config)

    first = world.run()
    assert world.kills() == 1
    outcome = first.outcomes[0]
    assert outcome.released is False and outcome.terminated is False
    assert outcome.skipped and outcome.skipped.startswith("execution_wedged")
    [record] = world.records()
    assert record["execution"] == WEDGED
    assert record["refusals"] == 1
    assert record["next_try"] - record["last_refused"] == timedelta(
        seconds=WEDGED_BACKOFF_BASE_SECONDS
    )
    # MUTATION: logging the wedge as an ordinary failed kill makes this 0.
    errors = world.lines(WEDGED_EXECUTION, "ERROR")
    assert len(errors) == 1 and errors[0]["execution"] == WEDGED
    assert "not running" in errors[0]["why"]

    second = world.run()
    third = world.run()
    # MUTATION: dropping the next_try check (asking every pass, as before
    # #1822) makes this 3.
    assert world.kills() == 1, "a wedged execution was asked again before its backoff"
    assert [o.skipped for o in (*second.outcomes, *third.outcomes)] == [
        "wedged_backoff",
        "wedged_backoff",
    ]
    # MUTATION: logging the wedge on every pass makes either count grow.
    assert len(world.lines(WEDGED_EXECUTION, "ERROR")) == 1
    assert world.lines(TERMINATION_FAILED) == []


# ---------------------------------------------------------------------------
# W-2
# ---------------------------------------------------------------------------


def test_the_backoff_doubles_per_refusal_and_is_capped_at_six_hours(db, config):
    world = wedged_world(db, config)
    world.run()

    world.due_now()
    world.run()
    assert world.kills() == 2, "a wedged execution past its backoff must be tried again"
    [record] = world.records()
    assert record["refusals"] == 2
    # MUTATION: a constant backoff leaves this at the base.
    assert record["next_try"] - record["last_refused"] == timedelta(
        seconds=2 * WEDGED_BACKOFF_BASE_SECONDS
    )
    # The retry is not a second "first time": no new ERROR.
    assert len(world.lines(WEDGED_EXECUTION, "ERROR")) == 1

    record["refusals"] = 40
    world.due_now()
    world.run()
    [record] = world.records()
    # MUTATION: removing the cap makes this 2**40 base periods.
    assert record["next_try"] - record["last_refused"] == timedelta(
        seconds=WEDGED_BACKOFF_CAP_SECONDS
    )
    assert WEDGED_BACKOFF_CAP_SECONDS == 6 * 3600


# ---------------------------------------------------------------------------
# W-3
# ---------------------------------------------------------------------------


def test_a_refusal_with_a_lease_naming_the_execution_keeps_todays_behaviour(db, config):
    """Invariant 3: a slot comes back only on a confirmed stop."""
    seed_running_task(db, pool_active=2)
    world = World(
        db,
        config,
        FakeBackend(executions=[running_execution()], terminate_raises=not_running()),
    )

    for _ in range(3):
        report = world.run()
        outcome = report.outcomes[0]
        assert outcome.released is False
        assert outcome.skipped and outcome.skipped.startswith("termination_failed")

    # MUTATION: backing off a held execution too (ignoring the lease) makes
    # this 1.
    assert world.kills() == 3, "an execution a lease names must be tried every pass"
    # MUTATION: recording it as wedged regardless of the lease makes this 1.
    assert world.records() == []
    assert len(world.lines(TERMINATION_FAILED, "ERROR")) == 3
    assert world.lines(WEDGED_EXECUTION) == []
    assert db.doc("pools/global")["active"] == 2
    assert db.doc("leases/lease_1")["released_at"] is None


# ---------------------------------------------------------------------------
# W-4, W-5
# ---------------------------------------------------------------------------


def test_a_confirmed_stop_forgets_the_wedged_record(db, config):
    world = wedged_world(db, config)
    world.run()
    assert len(world.records()) == 1

    world.backend.terminate_raises = None
    world.due_now()
    report = world.run()

    assert report.outcomes[0].terminated is True
    # MUTATION: never clearing the record leaves it behind.
    assert world.records() == []


def test_any_other_kill_failure_is_not_a_wedge(db, config):
    world = wedged_world(db, config, terminate_raises=RuntimeError("Cloud Run said no"))

    world.run()
    world.run()

    # MUTATION: treating every failed kill as a wedge records one and backs off.
    assert world.records() == []
    assert world.kills() == 2
    assert len(world.lines(TERMINATION_FAILED, "ERROR")) == 2
