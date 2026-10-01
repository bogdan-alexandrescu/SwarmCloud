"""One poisoned document never stops a reconciler snapshot (#294, S0).

`ControlStore.snapshot()` reads every active task, every unreleased lease and
the attempt each lease names, in one pass that serves every tenant. The task
loop was made total for a malformed document by #290; the lease and attempt
loops were not, so a single `generation` or `units` of NaN or Infinity --
`int(float("nan"))` is a `ValueError`, `int(float("inf"))` an
`OverflowError` -- raised straight out of the pass and no tenant's work was
reconciled until somebody found the document by hand.

What is asserted: the pass returns; every healthy document is still read;
the poisoned one is skipped and its id logged; and nothing is concluded about
the task a skipped lease or attempt belongs to -- it is withheld from the
snapshot and recorded as unreadable, so no rule can call it leaseless and
requeue it on the strength of a lease this pass could not decode.
"""

from __future__ import annotations

import io
import json
import math
from datetime import datetime, timezone
from typing import Any

import pytest

from reconciler.logs import build_logger
from reconciler.store import ControlStore

from .conftest import seed_task
from .fakes import FakeFirestore

TENANT = "eng"
NON_FINITE = [math.nan, math.inf, -math.inf]


def _store(db: FakeFirestore, stream: io.StringIO) -> ControlStore:
    return ControlStore(db, logger=build_logger(stream=stream))


def _seed_lease(
    db: FakeFirestore, lease_id: str, task_id: str, attempt_id: str, **extra: Any
) -> None:
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "tenant_id": TENANT,
        "generation": 1,
        "pools": [],
        "units": 1,
        "state": "LEASED",
        "created_at": datetime.now(timezone.utc),
        "released_at": None,
        **extra,
    }


def _seed_attempt(db: FakeFirestore, attempt_id: str, task_id: str, **extra: Any) -> None:
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": TENANT,
        "generation": 1,
        "backend": "cloud_run",
        "created_at": datetime.now(timezone.utc),
        **extra,
    }


def _logged(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _healthy(db: FakeFirestore) -> None:
    seed_task(db, task_id="task_healthy000000000001", tenant_id="other", state="RUNNING")
    _seed_lease(db, "lease_healthy", "task_healthy000000000001", "att_healthy")
    _seed_attempt(db, "att_healthy", "task_healthy000000000001")


@pytest.mark.parametrize("value", NON_FINITE)
def test_a_task_whose_caller_data_is_non_finite_does_not_stop_the_snapshot(value):
    """Caller data -- input and metadata, anywhere in them -- on a stored task."""
    db = FakeFirestore()
    seed_task(db, task_id="task_poison0000000000001", tenant_id=TENANT, state="RUNNING")
    doc = db.docs["tasks/task_poison0000000000001"]
    doc["input"] = {"prompt": "p", "n": value}
    doc["metadata"] = {"startup_refunds": value, "score": value, "deep": [{"x": value}]}
    _healthy(db)

    snapshot = _store(db, io.StringIO()).snapshot()

    assert "task_healthy000000000001" in snapshot.tasks
    assert "lease_healthy" in snapshot.leases
    assert "att_healthy" in snapshot.attempts


@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize("field", ["attempt_count", "max_attempts", "current_generation"])
def test_a_task_with_a_non_finite_counter_is_skipped_and_logged_by_id(field, value):
    db = FakeFirestore()
    seed_task(db, task_id="task_poison0000000000001", tenant_id=TENANT, state="RUNNING")
    db.docs["tasks/task_poison0000000000001"][field] = value
    _healthy(db)
    stream = io.StringIO()

    snapshot = _store(db, stream).snapshot()

    assert "task_poison0000000000001" not in snapshot.tasks
    assert "task_poison0000000000001" in snapshot.unreadable_tasks
    assert "task_healthy000000000001" in snapshot.tasks
    assert any(r.get("task_id") == "task_poison0000000000001" for r in _logged(stream))


@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize("field", ["generation", "units"])
def test_a_lease_with_a_non_finite_number_is_skipped_and_its_task_withheld(field, value):
    db = FakeFirestore()
    seed_task(db, task_id="task_poison0000000000001", tenant_id=TENANT, state="LEASED")
    _seed_lease(db, "lease_poison", "task_poison0000000000001", "att_poison", **{field: value})
    _healthy(db)
    stream = io.StringIO()

    snapshot = _store(db, stream).snapshot()

    assert "lease_poison" not in snapshot.leases
    assert "lease_healthy" in snapshot.leases
    assert "task_healthy000000000001" in snapshot.tasks
    # Not judged leaseless on the strength of a lease the pass could not read.
    assert "task_poison0000000000001" not in snapshot.tasks
    assert "task_poison0000000000001" in snapshot.unreadable_tasks
    assert any(r.get("lease_id") == "lease_poison" for r in _logged(stream))


@pytest.mark.parametrize("value", NON_FINITE)
def test_an_attempt_with_a_non_finite_generation_is_skipped_and_its_task_withheld(value):
    db = FakeFirestore()
    seed_task(db, task_id="task_poison0000000000001", tenant_id=TENANT, state="RUNNING")
    _seed_lease(db, "lease_poison", "task_poison0000000000001", "att_poison")
    _seed_attempt(db, "att_poison", "task_poison0000000000001", generation=value)
    _healthy(db)
    stream = io.StringIO()

    snapshot = _store(db, stream).snapshot()

    assert "att_poison" not in snapshot.attempts
    assert "att_healthy" in snapshot.attempts
    assert "task_healthy000000000001" in snapshot.tasks
    assert "task_poison0000000000001" not in snapshot.tasks
    assert "task_poison0000000000001" in snapshot.unreadable_tasks
    assert any(r.get("attempt_id") == "att_poison" for r in _logged(stream))
