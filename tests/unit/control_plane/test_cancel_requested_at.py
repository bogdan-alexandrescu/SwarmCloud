"""The first cancel of a task records when it was asked for (#627).

The reconciler enforces a cancel the execution ignores once it is older than
`cancel_enforce_after_seconds` (`reconciler.detect.detect_cancel_overdue`).
`updated_at` cannot be that clock: the worker moves it on every checkpoint
pointer it writes, so a worker that ignores the cancel and keeps
checkpointing would keep its cancel looking fresh for ever. So the API writes
`cancel_requested_at` on the FIRST cancel, and a second press does not move it.
"""

from __future__ import annotations

from .conftest import auth_header, seed_task, seed_tenant


def test_the_first_cancel_records_its_time_and_a_second_keeps_it(client, db) -> None:
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_held", tenant_id="eng", state="DISPATCHED")

    first = client.post("/v1/tasks/task_held/cancel", headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    asked = db.docs["tasks/task_held"].get("cancel_requested_at")
    assert asked is not None, "the first cancel did not record when it was requested"

    second = client.post("/v1/tasks/task_held/cancel", headers=auth_header("alice"))
    assert second.status_code == 200, second.text
    assert db.docs["tasks/task_held"]["cancel_requested_at"] == asked, (
        "a second press moved the cancel's time, restarting the reconciler's bound"
    )
