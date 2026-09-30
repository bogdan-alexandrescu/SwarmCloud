"""#362 step 0: what the scheduler already writes about a READY task behind a full pool.

`waiting_for` (swarm_api/waiting.py) is a live reading computed on the GET.
It sits beside `blocked_by`, which is the scheduler's record from its LAST
pass, and the UI labels that bar "last scheduler pass". That label is only
honest if the scheduler really does write the refusing pool into `blocked_by`
on a denial and really does clear it on admission. This file holds it to both,
through the shipped `drain()` over the in-memory Firestore, with no patching.
"""

from __future__ import annotations

from .conftest import seed_pool, seed_task, seed_tenant


def test_drain_names_the_full_tenant_pool_and_leasing_clears_it(db, make_scheduler, dispatcher):
    seed_tenant(db, "eng", max_active=20)
    seed_pool(db, "tenant:eng", hard_limit=20, active=20)
    seed_pool(db, "global", hard_limit=100, active=0)
    seed_task(db, task_id="task_waiting", tenant_id="eng")

    make_scheduler().drain()

    doc = db.docs["tasks/task_waiting"]
    assert doc["state"] == "READY"
    assert dispatcher.dispatched == []
    assert [(b["pool"], b["reason"]) for b in doc["blocked_by"]] == [
        ("tenant:eng", "TENANT_LIMIT")
    ]
    assert doc["blocked_by"][0]["limit"] == 20
    assert doc["blocked_by"][0]["active"] == 20

    # One slot frees; the next pass admits the task and the record is cleared.
    db.docs["pools/tenant:eng"]["active"] = 19
    make_scheduler().drain()

    doc = db.docs["tasks/task_waiting"]
    assert doc["state"] != "READY"
    assert doc["current_lease_id"]
    assert doc["blocked_by"] == []
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_waiting"]
