"""Contract request 132: the worker writes `Task.attempt_totals` when an
attempt ends and `Task.current_account` when an account is assigned.

Both are READ-SIDE VIEWS ONLY (`GET /v1/tasks` serves them instead of reading
every page's attempts and account events). Each is checked twice: on the
direct-Firestore control plane and on the worker's state client against
swarm-api's REAL state routes (`attemptstate.py`), and each stored value is
compared with what swarm-api derives from the same documents today
(`attempt_totals.attempt_totals`, `task_accounts.derive`), so the stored
copy can never say something the derivation would not.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from agent_worker.control import ControlPlane
from agent_worker.logs import build_logger
from swarm_api.attempt_totals import attempt_totals
from swarm_api.codec import attempt_from_dict, task_from_dict
from swarm_api.task_accounts import derive
from swarm_common.models import utcnow
from swarm_common.states import EventType, TaskState

from control_plane.fakes import FakeFirestore as ApiFirestore
from fakes import FakeFirestore, FakeTransactionRunner
from state_routes_harness import (
    ATTEMPT,
    TASK,
    TENANT,
    RoutesOpener,
    api_client,
    remote_control,
)
from state_routes_harness import seed_attempt as seed_remote_attempt
from worker_seeds import seed_attempt

ACCOUNT_A = "acct-a"
ACCOUNT_B = "acct-b"


def _direct(db: FakeFirestore, *, generation: int = 2) -> ControlPlane:
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
        txn_runner=FakeTransactionRunner(db),
    )


def _earlier_attempt(task_id: str) -> dict:
    """The task's first attempt, ended, $0.50 over 60 s."""
    start = utcnow() - timedelta(hours=1)
    return {
        "attempt_id": "att_earlier", "task_id": task_id, "tenant_id": TENANT,
        "generation": 1, "lease_id": "lease_earlier", "backend": "cloud_run_job",
        "created_at": start, "started_at": start,
        "completed_at": start + timedelta(seconds=60), "cost_usd": 0.5,
    }


class Direct:
    """The direct-Firestore path."""

    task_id = "task_1"
    attempt_id = "att_1"

    def __init__(self) -> None:
        self.db = FakeFirestore()
        seed_attempt(self.db, state=TaskState.RUNNING, generation=2, attempt_count=2)
        self.db.seed("attempts/att_earlier", _earlier_attempt(self.task_id))
        self.control = _direct(self.db)

    def task(self) -> dict:
        return self.db.doc(f"tasks/{self.task_id}")

    def attempts(self) -> list[dict]:
        return [d for p, d in self.db.documents.items() if p.startswith("attempts/")]

    def events(self) -> list[dict]:
        return self.db.events(self.task_id)


class Remote:
    """The worker's state client against swarm-api's state routes."""

    task_id = TASK
    attempt_id = ATTEMPT

    def __init__(self) -> None:
        self.db = ApiFirestore()
        seed_remote_attempt(self.db, state="RUNNING", generation=2)
        self.db.docs["attempts/att_earlier"] = _earlier_attempt(self.task_id)
        self.control = remote_control(RoutesOpener(api_client(self.db)), generation=2)
        self.control.validate_generation()

    def task(self) -> dict:
        return self.db.docs[f"tasks/{self.task_id}"]

    def attempts(self) -> list[dict]:
        return [d for p, d in self.db.docs.items() if p.startswith("attempts/")]

    def events(self) -> list[dict]:
        prefix = f"tasks/{self.task_id}/events/"
        rows = [d for p, d in self.db.docs.items() if p.startswith(prefix)]
        return sorted(rows, key=lambda r: r["at"])


@pytest.fixture(params=["direct", "remote"])
def path(request):
    return Direct() if request.param == "direct" else Remote()


def _derived_totals(path) -> dict:
    return attempt_totals(attempt_from_dict(d) for d in path.attempts())


def _stored(path) -> dict:
    stored = dict(path.task()["attempt_totals"])
    stored.pop("as_of")
    return stored


# --------------------------------------------------------------------------
# attempt_totals: written when an attempt ends
# --------------------------------------------------------------------------

def test_a_finish_writes_the_tasks_attempt_totals_in_its_own_commit(path):
    path.control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    path.control.record_spend({"total_cost_usd": 0.25})
    assert path.task().get("attempt_totals") is None

    path.control.finish(state=TaskState.SUCCEEDED, exit_code=0)

    stored = path.task()["attempt_totals"]
    assert stored["as_of"] == {"attempt_id": path.attempt_id, "generation": 2}
    assert stored["attempts"] == 2
    assert stored["attempts_with_cost"] == 2
    assert stored["cost_usd_total"] == 0.75
    assert stored["cost_incomplete"] is False
    assert stored["duration_incomplete"] is False
    # Exactly what swarm-api derives from the same attempt documents.
    assert _stored(path) == _derived_totals(path)


def test_spend_recorded_after_the_end_corrects_the_stored_totals(path):
    """The worker's teardown records spend after the end's commit
    (`lifecycle._record_spend` runs from `_cleanup`): the stored copy follows."""
    path.control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    path.control.finish(state=TaskState.FAILED, exit_code=1, error="boom")
    assert path.task()["attempt_totals"]["cost_incomplete"] is True

    path.control.record_spend({"total_cost_usd": 0.25})

    stored = path.task()["attempt_totals"]
    assert stored["cost_usd_total"] == 0.75 and stored["cost_incomplete"] is False
    assert _stored(path) == _derived_totals(path)


def test_a_retry_writes_the_totals_beside_its_ready(path):
    path.control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    path.control.fail_retryably(exit_code=1, error="transient", cause="runner_failed",
                               retry_delay_seconds=5)

    assert path.task()["state"] == "READY"
    assert path.task()["attempt_totals"]["attempts"] == 2
    assert _stored(path) == _derived_totals(path)


# --------------------------------------------------------------------------
# current_account: written when an account is assigned
# --------------------------------------------------------------------------

def _assigned(account_id: str, **extra) -> dict:
    return {"cause": "account_assigned", "account_id": account_id, "provider": "anthropic",
            **extra}


def test_an_account_assignment_writes_the_tasks_current_account(path):
    path.control.emit(EventType.RUNNING, _assigned(ACCOUNT_A))

    assert path.task()["current_account"] == {
        "status": "assigned", "account_id": ACCOUNT_A, "provider": "anthropic",
        "attempt_id": path.attempt_id, "generation": 2, "swapped_from": None, "swaps": [],
    }


def test_an_unreadable_account_and_its_replacement_are_what_derive_serves(path):
    path.control.emit(EventType.RUNNING, _assigned(ACCOUNT_A))
    path.control.emit(EventType.RETRYING, {"cause": "account_unreadable",
                                          "account_id": ACCOUNT_A, "provider": "anthropic"})
    gave_back = path.task()["current_account"]
    assert gave_back["status"] == "not_assigned_yet" and gave_back["account_id"] is None
    assert gave_back["swaps"] == [{"account_id": ACCOUNT_A, "cause": "unreadable"}]

    path.control.emit(EventType.RUNNING, _assigned(ACCOUNT_B))

    stored = path.task()["current_account"]
    assert stored["account_id"] == ACCOUNT_B and stored["swapped_from"] == ACCOUNT_A
    # The same answer swarm-api's read derives from the task's events today.
    task = task_from_dict({**path.task(), "runner_profile": "claude-code"})
    assert stored == derive(task, path.events())


def test_another_event_writes_no_account(path):
    path.control.emit(EventType.RUNNING, {"cause": "something_else"})
    assert path.task().get("current_account") is None


def test_a_superseded_attempt_writes_no_account(path):
    path.task()["current_generation"] = 3
    path.control.emit(EventType.RUNNING, _assigned(ACCOUNT_A))
    assert path.task().get("current_account") is None
