"""`metadata.startup_refunds` is total to read, reserved at the API, and its
operator override is bounded (#67, security review on PR #290, MAJOR).

THE DEFECT. `startup_refunds_used` read `metadata.startup_refunds` through
`_int_or_none`, which only catches `TypeError`/`ValueError`. A caller could put
anything JSON allows in `task.metadata` (the key was not reserved), and
`int(float("inf"))` raises `OverflowError` -- uncaught, straight out of
`TaskView.from_doc`. `ControlStore.snapshot` calls `TaskView.from_doc` for
every task in the four concurrency states in one unguarded loop, so a single
task anywhere with `metadata.startup_refunds = Infinity` crashed the
reconciler's pass for every task of every tenant.

THE FIX, three parts:

  * The read is now total (`reconciler.model._startup_refunds_int`): only a
    real, non-negative `int` -- never a `bool`, never a `float` even when
    finite and whole, never anything past Firestore's int64 range -- counts.
    Everything else reads as 0, and nothing here ever raises.
  * `snapshot()` also no longer trusts any single task document with the rest
    of the loop: a document that raises anything `_MALFORMED_TASK_DOC` names is
    skipped and logged by id, the way `quota_broker.accountstore.list` skips a
    malformed account (#180), and every OTHER task in the pass still comes
    back.
  * The key is reserved at the API
    (`swarm_api.validation.RESERVED_METADATA_KEYS`), so a caller cannot submit
    it going forward; the reconciler's own write path is untouched, because it
    writes directly to Firestore rather than through submission.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import io
import math
from typing import Any

import pytest

from reconciler.config import ReconcilerConfig, STARTUP_REFUND_LIMIT_MAX
from reconciler.logs import build_logger
from reconciler.model import STARTUP_REFUNDS_KEY, TaskView, startup_refunds_used
from reconciler.store import ControlStore
from swarm_api.errors import ValidationFailed
from swarm_api.validation import (
    RESERVED_METADATA_KEYS,
    STARTUP_REFUNDS_METADATA_KEY,
    reject_reserved_metadata,
)
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task
from .fakes import FakeFirestore

PROJECT = "saga-agents-staging"
REGION = "us-central1"
TENANT = "eng"

#: The reconciler's key is a bare Python string ("startup_refunds"); every
#: value here that should NOT count as a real refund, per the owner's brief on
#: PR #290's review, and what each is trying to smuggle past a naive reader:
UNREADABLE_VALUES: list[Any] = [
    pytest.param(math.inf, id="inf"),
    pytest.param(math.nan, id="nan"),
    pytest.param("3", id="numeric-string"),
    pytest.param(True, id="bool-true"),
    pytest.param(10**30, id="too-large-for-int64"),
]


# --------------------------------------------------------------------------
# The pure function: `startup_refunds_used` never raises, and is total
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", UNREADABLE_VALUES)
def test_an_unreadable_startup_refunds_value_counts_as_zero_and_never_raises(value):
    assert startup_refunds_used({STARTUP_REFUNDS_KEY: value}) == 0


def test_a_real_refund_count_is_read_back_unchanged():
    assert startup_refunds_used({STARTUP_REFUNDS_KEY: 2}) == 2
    assert startup_refunds_used({STARTUP_REFUNDS_KEY: 0}) == 0


def test_a_negative_count_is_not_a_real_refund_either():
    # Never produced by the reconciler's own write path (`count_startup_end`
    # never goes below 0), but the read must not trust a document that says
    # otherwise -- a negative "used" would let a caller manufacture extra
    # refunds by writing -1000000 rather than a large positive number.
    assert startup_refunds_used({STARTUP_REFUNDS_KEY: -1}) == 0


def test_absent_or_non_mapping_metadata_is_zero():
    assert startup_refunds_used(None) == 0
    assert startup_refunds_used({}) == 0
    assert startup_refunds_used("not-a-dict") == 0


def test_task_view_from_doc_does_not_raise_on_any_unreadable_value():
    """The exact crash: before the fix, `int(float("inf"))` raised OverflowError
    straight out of `from_doc`, which is called once per task in `snapshot()`'s
    unguarded loop."""
    for value in (math.inf, math.nan, "3", True, 10**30):
        view = TaskView.from_doc(
            {
                "id": "task_x",
                "tenant_id": TENANT,
                "state": "RUNNING",
                "metadata": {STARTUP_REFUNDS_KEY: value},
            },
            "task_x",
        )
        assert view.startup_refunds == 0, value


# --------------------------------------------------------------------------
# `ControlStore.snapshot()` is robust to a malformed task document
# --------------------------------------------------------------------------

def _store(db: FakeFirestore) -> ControlStore:
    return ControlStore(db, logger=build_logger(stream=io.StringIO()))


@pytest.mark.parametrize("value", UNREADABLE_VALUES)
def test_snapshot_reads_a_task_with_an_unreadable_startup_refunds_value_as_zero_used(
    db: FakeFirestore, value
):
    seed_task(db, task_id="task_bad_refund00000001", tenant_id=TENANT, state="RUNNING")
    db.docs["tasks/task_bad_refund00000001"]["metadata"] = {STARTUP_REFUNDS_KEY: value}
    # A second, ordinary task, to prove the pass is not merely surviving --
    # it is still reading everything else correctly.
    seed_task(db, task_id="task_healthy0000000000001", tenant_id=TENANT, state="LEASED")

    snapshot = _store(db).snapshot()

    assert set(snapshot.tasks) == {"task_bad_refund00000001", "task_healthy0000000000001"}
    assert snapshot.tasks["task_bad_refund00000001"].startup_refunds == 0
    assert snapshot.tasks["task_healthy0000000000001"].startup_refunds == 0


def test_snapshot_survives_five_tasks_each_carrying_a_different_unreadable_value(
    db: FakeFirestore,
):
    """All five values from the brief, on FIVE separate tasks, in one pass --
    proving the pass does not merely survive the first one and then stop."""
    for i, value in enumerate([math.inf, math.nan, "3", True, 10**30]):
        task_id = f"task_multi{i:016d}"
        seed_task(db, task_id=task_id, tenant_id=TENANT, state="RUNNING")
        db.docs[f"tasks/{task_id}"]["metadata"] = {STARTUP_REFUNDS_KEY: value}
    seed_task(db, task_id="task_control0000000000001", tenant_id=TENANT, state="DISPATCHED")

    snapshot = _store(db).snapshot()

    assert len(snapshot.tasks) == 6
    assert "task_control0000000000001" in snapshot.tasks
    for i in range(5):
        assert snapshot.tasks[f"task_multi{i:016d}"].startup_refunds == 0


def test_a_task_document_malformed_in_an_unrelated_field_is_skipped_not_fatal(
    db: FakeFirestore,
):
    """The general robustness the review asked for, beyond the one field: a task
    document broken in a way `TaskView.from_doc` cannot parse at all (here,
    `attempt_count` is not an integer) must not abort the whole pass, the way
    `quota_broker.accountstore.list` skips one malformed account (#180)."""
    seed_task(db, task_id="task_malformed000000000001", tenant_id=TENANT, state="RUNNING")
    db.docs["tasks/task_malformed000000000001"]["attempt_count"] = "not-a-number"
    seed_task(db, task_id="task_fine00000000000000001", tenant_id=TENANT, state="LEASED")

    snapshot = _store(db).snapshot()

    assert "task_malformed000000000001" not in snapshot.tasks
    assert "task_fine00000000000000001" in snapshot.tasks
    # Recorded as unreadable, not silently absent: an execution naming this
    # task id must not be read as orphaned just because this pass could not
    # decode its document (see `detect._unreadable_tasks`).
    assert "task_malformed000000000001" in snapshot.unreadable_tasks


# --------------------------------------------------------------------------
# Reserved at the API
# --------------------------------------------------------------------------

def test_startup_refunds_key_matches_the_reconciler_exactly():
    """The seam: a string mismatch here would reserve nothing the reconciler
    actually writes, the same seam `test_input_from_is_reserved.py` holds for
    `INPUT_FROM_METADATA_KEY` against the worker's own constant."""
    assert STARTUP_REFUNDS_METADATA_KEY == STARTUP_REFUNDS_KEY == "startup_refunds"
    assert STARTUP_REFUNDS_METADATA_KEY in RESERVED_METADATA_KEYS


def test_reject_reserved_metadata_refuses_startup_refunds_whatever_its_value():
    for value in (3, 0, "3", None, {}, -1):
        with pytest.raises(ValidationFailed) as exc:
            reject_reserved_metadata({"unit": "payments", "startup_refunds": value})
        assert exc.value.code == "invalid_dispatch"
        assert exc.value.detail == {"reserved_metadata_keys": ["startup_refunds"]}
        assert "metadata.startup_refunds" in exc.value.message


def test_a_plain_task_submission_carrying_startup_refunds_is_refused_with_a_422(
    client, db: FakeFirestore
):
    before = db.paths("tasks/")

    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "input": {"prompt": "hi"},
            "metadata": {"unit": "payments", "startup_refunds": 3},
        },
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["reserved_metadata_keys"] == ["startup_refunds"]
    assert "metadata.startup_refunds" in body["message"]
    # Nothing was created by the refused submission.
    assert db.paths("tasks/") == before


# --------------------------------------------------------------------------
# The reconciler's own write path is untouched by the reservation
# --------------------------------------------------------------------------

def test_the_reconciler_still_writes_startup_refunds_directly_to_firestore(
    db: FakeFirestore,
):
    """Reservation guards SUBMISSION only. The reconciler's repair path writes
    `metadata.startup_refunds` straight to the task document -- never through
    `reject_reserved_metadata` -- and that must keep working."""
    task_id = "task_writer00000000000001"
    seed_task(db, task_id=task_id, tenant_id=TENANT, state="DISPATCHED")
    store = ControlStore(db, logger=build_logger(stream=io.StringIO()))

    result = store.repair_task_state(
        task_id,
        expected_lease_id=None,
        to_state=TaskState.READY,
        error="worker ended before it started",
        startup_refund_limit=3,
        fenced_generation=0,
    )

    assert result is not None
    stored = db.docs[f"tasks/{task_id}"]
    assert stored["metadata"][STARTUP_REFUNDS_KEY] == 1


# --------------------------------------------------------------------------
# The operator override on `STARTUP_REFUND_LIMIT` is bounded
# --------------------------------------------------------------------------

def test_startup_refund_limit_defaults_to_three_and_is_clamped_to_ten(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.delenv("STARTUP_REFUND_LIMIT", raising=False)
    assert ReconcilerConfig.from_env().startup_refund_limit == 3

    monkeypatch.setenv("STARTUP_REFUND_LIMIT", "1000000")
    assert ReconcilerConfig.from_env().startup_refund_limit == STARTUP_REFUND_LIMIT_MAX
    assert STARTUP_REFUND_LIMIT_MAX == 10


def test_startup_refund_limit_negative_override_still_floors_at_zero(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("STARTUP_REFUND_LIMIT", "-5")
    assert ReconcilerConfig.from_env().startup_refund_limit == 0


def test_startup_refund_limit_within_bounds_passes_through(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("STARTUP_REFUND_LIMIT", "7")
    assert ReconcilerConfig.from_env().startup_refund_limit == 7
