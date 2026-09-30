"""No writer after creation touches a field the step-spec signature covers.

Contract request 34, sections 2 and 7. The three signed fields are written
once, in the write that creates the task, and never again; and a retry is
another attempt at the same document, never a re-sign. So every later writer
must leave the covered fields exactly as swarm-api signed them:

  * the scheduler's `return_to_ready_after_failed_dispatch` (a retry, and the
    last-attempt FAILED);
  * the reconciler's `repair_task_state` (a requeue and a FAILED);
  * swarm-api's own writes to an existing task, `Store.request_cancel` and
    `Store.cancel_workflow` -- the only writes swarm-api makes to a task it
    did not just create.

ASSERTED AS THE PROPERTY, not as a list of keys each writer happens to send:
after the write, the stored document still verifies under the signature it
was created with, and its three signature fields are byte-identical. A future
writer that patches `input` or `metadata.dispatch` turns this red.
"""

from __future__ import annotations

import copy
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_common.models import Lease, Task
from swarm_common.states import TaskState

from .spec_signer import LocalSpecSigner
from .test_spec_signing_submission import signed_client, signed_context, signer  # noqa: F401

SIGNATURE_FIELDS = ("spec_signature", "spec_key_version", "spec_format")
NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


def _assert_still_signed(signer: LocalSpecSigner, before: dict[str, Any], after: dict[str, Any],
                         task_id: str) -> None:
    for field in SIGNATURE_FIELDS:
        assert after.get(field) == before.get(field), f"a later writer changed {field}"
    assert signer.verifies(after, task_id), (
        "a write after creation changed a field the step-spec signature covers; the worker "
        "would refuse this task as rewritten"
    )


# ---------------------------------------------------------------------------
# The scheduler's retry
# ---------------------------------------------------------------------------


def _scheduler_task(db, signer: LocalSpecSigner, *, attempts: int) -> tuple[Task, Lease, dict]:
    task = Task(
        id="task_x", tenant_id="eng", runner_profile="browser", resource_class="browser",
        state=TaskState.READY, created_at=NOW, updated_at=NOW,
        input={"prompt": "browse"}, submitted_by="alice@saga.xyz",
        attempt_count=attempts - 1, max_attempts=3,
        metadata={"dispatch": {"strategy": "none", "carrier": "checkpoints"}},
        depends_on=["task_parent"], workflow_id="wf_1", step_id="browse",
    )
    lease = Lease(
        lease_id="lease_x", task_id="task_x", attempt_id="att_x", tenant_id="eng",
        generation=attempts, units=2, pools=["global"], state=TaskState.LEASED,
        created_at=NOW, dispatch_deadline=NOW + timedelta(seconds=300),
        expires_at=NOW + timedelta(seconds=120),
    )
    stored = task.to_firestore()
    signer.sign_document(stored, task.id)
    stored.update({
        "state": TaskState.LEASED.value, "attempt_count": attempts,
        "current_lease_id": lease.lease_id, "current_generation": attempts,
    })
    db.collection("tasks").document("task_x").set(stored)
    return task, lease, copy.deepcopy(stored)


@pytest.mark.parametrize("attempts,expected", [(1, "READY"), (3, "FAILED")])
def test_the_schedulers_dispatch_retry_writes_no_covered_field(db, attempts, expected):
    from scheduler.store import SchedulerStore

    signer = LocalSpecSigner()
    task, lease, before = _scheduler_task(db, signer, attempts=attempts)
    SchedulerStore(db, now=lambda: NOW).return_to_ready_after_failed_dispatch(
        task, lease, "gke_create_job_failed"
    )
    after = db.collection("tasks").document("task_x").get().to_dict()
    assert after["state"] == expected
    _assert_still_signed(signer, before, after, "task_x")


# ---------------------------------------------------------------------------
# The reconciler's repair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "to_state,attempt_count",
    [(TaskState.READY, 1), (TaskState.FAILED, 3)],
    ids=["requeue", "fail"],
)
def test_the_reconcilers_repair_writes_no_covered_field(db, to_state, attempt_count):
    from reconciler.logs import build_logger
    from reconciler.store import ControlStore

    from .test_reconciler_startup_refund import LEASE, TASK, seed, task_doc

    signer = LocalSpecSigner()
    seed(db, attempt_count=attempt_count, metadata={"dispatch": {"strategy": "none"}})
    signer.sign_document(task_doc(db), TASK)
    before = copy.deepcopy(task_doc(db))
    ControlStore(db, logger=build_logger(stream=io.StringIO())).repair_task_state(
        TASK,
        to_state=to_state,
        expected_lease_id=LEASE,
        error="reconciled: the worker went silent",
        only_from=(TaskState.DISPATCHED, TaskState.STARTING, TaskState.RUNNING),
        startup_refund_limit=3,
    )
    after = task_doc(db)
    assert after["state"] != before["state"], "the repair wrote nothing; the test proves nothing"
    _assert_still_signed(signer, before, after, TASK)


# ---------------------------------------------------------------------------
# swarm-api's own writes to an existing task
# ---------------------------------------------------------------------------


def _tasks(db) -> dict[str, dict[str, Any]]:
    return {
        key.split("/", 1)[1]: doc
        for key, doc in db.docs.items()
        if key.startswith("tasks/") and key.count("/") == 1
    }


def test_swarm_apis_task_cancel_writes_no_covered_field(signed_client, db, signer):  # noqa: F811
    from .conftest import auth_header

    response = signed_client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "x"}},
    )
    assert response.status_code == 201, response.text
    (task_id, doc), = _tasks(db).items()
    before = copy.deepcopy(doc)
    response = signed_client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice"))
    assert response.status_code in (200, 202), response.text
    after = _tasks(db)[task_id]
    assert after != before, "the cancel wrote nothing; the test proves nothing"
    _assert_still_signed(signer, before, after, task_id)


def test_swarm_apis_workflow_cancel_writes_no_covered_field(signed_client, db, signer):  # noqa: F811
    from .conftest import auth_header

    body = {
        "on_step_failure": "continue",
        "steps": [
            {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"}},
            {"step_id": "b", "runner_profile": "mock", "input": {"prompt": "b"},
             "depends_on": ["a"], "input_from": {"a": "a.md"}},
        ],
    }
    response = signed_client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    workflow_id = response.json()["workflow"]["workflow_id"]
    before = copy.deepcopy(_tasks(db))
    response = signed_client.post(
        f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("alice")
    )
    assert response.status_code in (200, 202), response.text
    after = _tasks(db)
    assert after != before, "the cancel wrote nothing; the test proves nothing"
    for task_id in before:
        _assert_still_signed(signer, before[task_id], after[task_id], task_id)
