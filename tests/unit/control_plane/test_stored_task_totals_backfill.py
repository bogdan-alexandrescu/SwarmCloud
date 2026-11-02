"""Contract request 132: the one-time backfill of a task's stored attempt
totals and current account (`scripts/backfill-task-views.sh`, which runs
`task_accounts.backfill_task_views`).

A task written before the fields has neither; `GET /v1/tasks` derives both
for it on every page. The backfill writes, for each such task, exactly what
the writers would have written: the totals of its attempt documents as of its
current attempt, and the account its current attempt's events leave it on.
It is a dry run unless told to apply, never overwrites a copy a writer left,
and never reads across tenants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_api import task_accounts
from swarm_api.attempt_totals import served_totals
from swarm_api.codec import task_from_dict
from swarm_api.task_accounts import backfill_task_views, served_account

from .test_stored_task_totals_list import _seed, _stored_account, _stored_totals
from .fakes import FakeFirestore, FakeQuery

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "backfill-task-views.sh"


@pytest.fixture(autouse=True)
def _events_group(monkeypatch):
    monkeypatch.setattr(FakeFirestore, "collection_group",
                        lambda self, path: FakeQuery(self, path), raising=False)
    task_accounts.FINISHED.clear()


def _task(db, i: int) -> dict:
    return db.docs[f"tasks/task_{i:03d}"]


def test_a_dry_run_counts_and_writes_nothing(db):
    _seed(db, rows=10, stored=False)
    before = {p: dict(d) for p, d in db.docs.items() if p.startswith("tasks/")}
    report = backfill_task_views(db, apply=False)
    assert report["tasks_read"] == 10
    assert report["totals_to_write"] == 10
    assert report["accounts_to_write"] == 10
    assert report["written"] == 0
    # MUTATION: write in the dry run.
    assert {p: d for p, d in db.docs.items() if p.startswith("tasks/")} == before


def test_apply_writes_what_the_writers_would_have_written(db):
    _seed(db, rows=10, stored=False)
    report = backfill_task_views(db, apply=True)
    assert report["written"] == 10
    for i in range(10):
        doc = _task(db, i)
        assert doc["attempt_totals"] == _stored_totals(i)
        assert doc["current_account"] == _stored_account(i)
        task = task_from_dict(doc)
        # The list serves both without a read.
        assert served_totals(task) is not None
        assert served_account(task) is not None


def test_a_second_run_finds_nothing_and_a_writers_copy_is_never_overwritten(db):
    _seed(db, rows=6, stored=False)
    written_by_a_writer = {**_stored_totals(2), "attempts": 99}
    _task(db, 2)["attempt_totals"] = written_by_a_writer
    report = backfill_task_views(db, apply=True)
    assert report["totals_to_write"] == 5
    assert report["accounts_to_write"] == 6
    assert _task(db, 2)["attempt_totals"] == written_by_a_writer
    again = backfill_task_views(db, apply=True)
    assert again["totals_to_write"] == 0
    assert again["accounts_to_write"] == 0
    assert again["written"] == 0


def test_a_task_with_no_attempt_at_its_current_generation_is_left_to_the_reader(db):
    _seed(db, rows=3, stored=False)
    db.docs.pop("attempts/att_001")
    db.docs.pop("events/ev_001")
    report = backfill_task_views(db, apply=True)
    assert report["totals_to_write"] == 2
    assert report["accounts_to_write"] == 2
    assert "attempt_totals" not in _task(db, 1)
    assert "current_account" not in _task(db, 1)


def test_attempts_and_events_of_another_tenant_are_never_counted(db):
    _seed(db, rows=2, stored=False)
    # A document in another workspace naming this task's id.
    db.docs["attempts/att_foreign"] = {**db.docs["attempts/att_000"],
                                       "attempt_id": "att_foreign", "tenant_id": "other",
                                       "cost_usd": 100.0}
    backfill_task_views(db, apply=True)
    assert _task(db, 0)["attempt_totals"] == _stored_totals(0)


def test_one_tenant_only(db):
    _seed(db, rows=4, stored=False)
    _task(db, 3)["tenant_id"] = "other"
    report = backfill_task_views(db, apply=True, tenant_id="eng")
    assert report["tasks_read"] == 3
    assert "attempt_totals" not in _task(db, 3)


def test_the_script_is_a_typed_dry_run_first_wrapper():
    text = SCRIPT.read_text()
    assert SCRIPT.stat().st_mode & 0o111, "the script must be executable"
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0] == "set -euo pipefail"
    assert "unset SWARM_ASSUME_YES" in text
    assert "backfill_task_views" in text
    # The apply path asks for the project id typed, after a dry run's plan.
    assert 'confirm "' in text and '"${PROJECT_ID}"' in text
    # Without --apply it is a dry run; the plan is printed before the prompt.
    assert text.index("SWARM_BACKFILL_APPLY") < text.index('confirm "')
