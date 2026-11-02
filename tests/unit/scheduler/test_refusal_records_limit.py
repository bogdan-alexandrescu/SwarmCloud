"""A refusal records the limit it applied, its ceiling and what lowered it (#3047).

On 24 October 2026 admission refused up to 48 steps on the provider
concurrency limit while 6 or 7 agents ran against a ceiling of 40, and
nothing recorded the limit it had applied. Pinned here, against the production
`SchedulerStore` over the in-memory Firestore:

  L-1  A refusal under an adaptive target of 7 and a ceiling of 40 writes, on
       the task's `blocked_by`, the limit's name, the value applied (7), the
       ceiling (40), the units in use and the input that lowered it, dated by
       the rate limit that cut it -- never by the quota document's last write,
       which every success bumps and which is served as `quota_written_at`.
  L-2  The drain report sums the same per limit, and `drain finished` logs it.
  L-3  An override (an admin's target reset after the last rate limit) is
       named as such and dates the target; instants are compared as datetimes.
  L-4  A limit at its ceiling names no input; the keys admission wrote are kept.
  L-5  Admission is unchanged by the recording: the same tasks are leased and
       refused, the pools' `active` counts are the ones admission wrote, and a
       pool read that fails leaves the refusal standing with its own figures.
  L-6  Nothing but numbers, names and instants is copied from the broker's
       quota document and audit: no caller, reason or other field.
"""

from __future__ import annotations

import logging
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import (  # noqa: E402
    RecordingDispatcher,
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeFirestore  # noqa: E402

from scheduler import limits as limits_mod  # noqa: E402
from scheduler.dispatch import BackendRouter  # noqa: E402
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.store import SchedulerStore  # noqa: E402

TENANT = "eng"
PROVIDER = "anthropic"
POOL = f"provider:{PROVIDER}:tenant:{TENANT}"
CUT_AT = datetime(2026, 10, 24, 10, 30, tzinfo=timezone.utc)
#: The broker's last write, hours after the cut: a success that left the target
#: at 7 still bumps `updated_at`.
WRITTEN_AT = datetime(2026, 10, 24, 15, 58, tzinfo=timezone.utc)


def _db(*, target: int | None = 7, in_use: int = 7, ceiling: int = 40, tasks: int = 2) -> FakeFirestore:
    db = FakeFirestore()
    seed_tenant(db, TENANT, max_active=100)
    seed_pool(db, "global", hard_limit=100)
    seed_pool(db, f"provider:{PROVIDER}", hard_limit=ceiling)
    seed_pool(db, POOL, hard_limit=ceiling, active=in_use, adaptive_target=target)
    db.docs[f"quota/{PROVIDER}:{TENANT}"] = {
        "provider": PROVIDER,
        "tenant_id": TENANT,
        "state": "available",
        "configured_hard_max": 50,
        "adaptive_target": target,
        "updated_at": WRITTEN_AT,
        "last_429_at": CUT_AT,
    }
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for i in range(tasks):
        seed_task(db=db, task_id=f"task_l{i}", tenant_id=TENANT, provider=PROVIDER,
                  created_at=base + timedelta(seconds=i))
    return db


def _scheduler(store: SchedulerStore) -> Scheduler:
    settings = scheduler_settings()
    dispatcher = RecordingDispatcher()
    return Scheduler(
        settings=settings,
        store=store,
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(),
    )


def _blocker(db: FakeFirestore, task_id: str, pool: str = POOL) -> dict:
    rows = [b for b in db.docs[f"tasks/{task_id}"].get("blocked_by") or [] if b.get("pool") == pool]
    assert rows, db.docs[f"tasks/{task_id}"].get("blocked_by")
    return rows[0]


# --------------------------------------------------------------------------
# L-1
# --------------------------------------------------------------------------

def test_a_refusal_under_an_adaptive_target_of_7_records_it_beside_the_ceiling_of_40():
    db = _db()
    _scheduler(SchedulerStore(db)).drain()

    block = _blocker(db, "task_l0")
    assert block["reason"] == "PROVIDER_CONCURRENCY_LIMIT"
    assert block["limit_name"] == POOL
    assert block["applied"] == 7
    assert block["ceiling"] == 40
    assert block["in_use"] == 7
    assert block["lowered_by"] == [
        {
            "input": "adaptive_target",
            "value": 7,
            "set_at": CUT_AT.isoformat(),
            "set_by": "rate_limit",
            "quota_written_at": WRITTEN_AT.isoformat(),
            "last_rate_limited_at": CUT_AT.isoformat(),
        }
    ]
    assert block["inputs_read_at"]


# --------------------------------------------------------------------------
# L-2
# --------------------------------------------------------------------------

def test_the_drain_report_sums_each_limit_and_drain_finished_logs_it(caplog):
    db = _db(tasks=3)
    with caplog.at_level(logging.INFO, logger="scheduler.loop"):
        report = _scheduler(SchedulerStore(db)).drain()

    assert report.denied == 3
    row = report.limits[POOL]
    assert row["refused"] == 3
    assert row["reason"] == "PROVIDER_CONCURRENCY_LIMIT"
    assert row["applied"] == 7
    assert row["ceiling"] == 40
    assert row["in_use"] == 7
    assert [entry["input"] for entry in row["lowered_by"]] == ["adaptive_target"]
    assert report.to_dict()["limits"][POOL]["applied"] == 7
    finished = [r.getMessage() for r in caplog.records if r.getMessage().startswith("drain finished")]
    assert finished and "'limits'" in finished[-1] and "'ceiling': 40" in finished[-1]


# --------------------------------------------------------------------------
# L-3
# --------------------------------------------------------------------------

def test_an_admin_target_set_after_the_last_rate_limit_is_named_an_override():
    db = _db()
    reset_at = CUT_AT + timedelta(minutes=5)
    db.docs["quota_audit/audit_1"] = {
        "action": "adaptive_target_reset",
        "provider": PROVIDER,
        "tenant_id": TENANT,
        "to_target": 7,
        "at": reset_at,
        "requested_by": "admin@example.com",
        "reason": "lent it fewer accounts",
    }
    # An older reset, superseded by the newer one.
    db.docs["quota_audit/audit_0"] = {
        "action": "adaptive_target_reset", "provider": PROVIDER, "tenant_id": TENANT,
        "to_target": 30, "at": reset_at - timedelta(hours=1),
    }
    _scheduler(SchedulerStore(db)).drain()

    inputs = {e["input"]: e for e in _blocker(db, "task_l0")["lowered_by"]}
    assert inputs["override"] == {
        "input": "override", "value": 7, "set_at": reset_at.isoformat(),
        "action": "adaptive_target_reset",
    }
    # The target is dated by the override that set it, not by the cut.
    assert inputs["adaptive_target"]["set_at"] == reset_at.isoformat()
    assert inputs["adaptive_target"]["set_by"] == "override"


def test_an_override_and_a_cut_written_with_different_offsets_are_ordered_as_instants():
    # 10:20 at UTC-05:00 is 15:20 UTC, AFTER the 10:30 UTC cut; as strings
    # "2026-10-24T10:20:00-05:00" sorts before "2026-10-24T10:30:00+00:00".
    reading = limits_mod.PoolReading(
        ceiling=40, adaptive_target=7, quota_derived_limit=None, read_at=None,
        last_rate_limited_at=CUT_AT.isoformat(),
        override=(7, "2026-10-24T10:20:00-05:00", "adaptive_target_reset"),
    )

    inputs = {e["input"]: e for e in limits_mod.lowered_by(reading, 7)}

    assert inputs["override"]["set_at"] == "2026-10-24T10:20:00-05:00"
    assert inputs["adaptive_target"]["set_by"] == "override"


def test_an_override_a_later_rate_limit_moved_is_not_named():
    db = _db()
    db.docs["quota_audit/audit_1"] = {
        "action": "adaptive_target_reset", "provider": PROVIDER, "tenant_id": TENANT,
        "to_target": 7, "at": CUT_AT - timedelta(minutes=5),
    }
    _scheduler(SchedulerStore(db)).drain()

    assert [e["input"] for e in _blocker(db, "task_l0")["lowered_by"]] == ["adaptive_target"]


def test_a_refusal_dates_a_climbed_target_by_target_changed_at():
    """The owner's follow-up of 2026-11-02: the broker writes
    `target_changed_at` only when the target moves, so a target a climb raised
    since the cut is dated by the climb, not by the cut."""
    db = _db()
    climbed_at = CUT_AT + timedelta(hours=2)
    db.docs[f"quota/{PROVIDER}:{TENANT}"]["target_changed_at"] = climbed_at
    _scheduler(SchedulerStore(db)).drain()

    assert _blocker(db, "task_l0")["lowered_by"] == [
        {
            "input": "adaptive_target",
            "value": 7,
            "set_at": climbed_at.isoformat(),
            "set_by": "climb",
            "quota_written_at": WRITTEN_AT.isoformat(),
            "last_rate_limited_at": CUT_AT.isoformat(),
        }
    ]


def test_a_target_last_changed_by_the_cut_is_dated_by_it():
    db = _db()
    db.docs[f"quota/{PROVIDER}:{TENANT}"]["target_changed_at"] = CUT_AT
    _scheduler(SchedulerStore(db)).drain()

    [entry] = _blocker(db, "task_l0")["lowered_by"]
    assert (entry["set_at"], entry["set_by"]) == (CUT_AT.isoformat(), "rate_limit")


def test_an_override_still_holding_the_target_names_it_at_target_changed_at():
    db = _db()
    reset_at = CUT_AT + timedelta(minutes=5)
    changed_at = reset_at + timedelta(microseconds=40)
    db.docs[f"quota/{PROVIDER}:{TENANT}"]["target_changed_at"] = changed_at
    db.docs["quota_audit/audit_1"] = {
        "action": "adaptive_target_reset", "provider": PROVIDER, "tenant_id": TENANT,
        "to_target": 7, "at": reset_at,
    }
    _scheduler(SchedulerStore(db)).drain()

    inputs = {e["input"]: e for e in _blocker(db, "task_l0")["lowered_by"]}
    assert inputs["adaptive_target"]["set_at"] == changed_at.isoformat()
    assert inputs["adaptive_target"]["set_by"] == "override"
    assert inputs["override"]["set_at"] == reset_at.isoformat()


def test_a_max_raise_that_left_the_target_alone_is_not_an_override():
    """A pool raise through swarm-api moves the broker's maximum and writes a
    `hard_max_set` row; one that left the target at 7 did not set it."""
    db = _db()
    db.docs["quota_audit/audit_1"] = {
        "action": "hard_max_set", "provider": PROVIDER, "tenant_id": TENANT,
        "from_target": 7, "to_target": 7, "at": CUT_AT + timedelta(hours=1),
    }
    _scheduler(SchedulerStore(db)).drain()

    [entry] = _blocker(db, "task_l0")["lowered_by"]
    assert (entry["input"], entry["set_by"]) == ("adaptive_target", "rate_limit")


# --------------------------------------------------------------------------
# L-4
# --------------------------------------------------------------------------

def test_a_limit_at_its_ceiling_names_no_input_and_keeps_admissions_keys():
    db = _db(target=None, in_use=40)
    _scheduler(SchedulerStore(db)).drain()

    block = _blocker(db, "task_l0")
    assert block["pool"] == POOL and block["limit"] == 40 and block["active"] == 40
    assert block["applied"] == 40 and block["ceiling"] == 40
    assert block["lowered_by"] == []


def test_a_quota_derived_zero_is_named_with_the_provider_state():
    db = _db(target=None, in_use=0)
    db.docs[f"pools/{POOL}"]["quota_derived_limit"] = 0
    db.docs[f"quota/{PROVIDER}:{TENANT}"]["state"] = "cooldown"
    _scheduler(SchedulerStore(db)).drain()

    block = _blocker(db, "task_l0")
    assert block["applied"] == 0
    assert block["lowered_by"] == [
        {"input": "quota_derived_limit", "value": 0, "set_at": CUT_AT.isoformat(),
         "set_by": "rate_limit", "quota_written_at": WRITTEN_AT.isoformat(),
         "provider_state": "cooldown"}
    ]


# --------------------------------------------------------------------------
# L-5
# --------------------------------------------------------------------------

def test_admission_is_unchanged_by_the_recording():
    # Room for exactly one more under the target: one lease, one refusal.
    db = _db(target=7, in_use=6)
    report = _scheduler(SchedulerStore(db)).drain()

    # Every pass that tries task_l1 refuses it; none leases it.
    assert report.leased == 1 and report.denied >= 1
    assert db.docs[f"pools/{POOL}"]["active"] == 7
    assert db.docs["pools/global"]["active"] > 0
    assert db.docs["tasks/task_l0"]["state"] != "READY"
    assert db.docs["tasks/task_l1"]["state"] == "READY"


def test_a_pool_that_cannot_be_read_leaves_the_refusal_with_its_own_figures(monkeypatch):
    db = _db()

    def boom(*_a, **_k):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(limits_mod, "read_pool", boom)
    report = _scheduler(SchedulerStore(db)).drain()

    assert report.denied == 2
    block = _blocker(db, "task_l0")
    assert block["applied"] == 7 and block["in_use"] == 7
    assert block["ceiling"] is None and block["lowered_by"] == []


# --------------------------------------------------------------------------
# L-6
# --------------------------------------------------------------------------

def test_nothing_but_numbers_names_and_instants_is_copied_from_the_broker():
    db = _db()
    fake = secrets.token_hex(16)
    db.docs[f"quota/{PROVIDER}:{TENANT}"]["api_key_hint"] = fake
    db.docs["quota_audit/audit_1"] = {
        "action": "adaptive_target_reset", "provider": PROVIDER, "tenant_id": TENANT,
        "to_target": 7, "at": CUT_AT + timedelta(minutes=1),
        "requested_by": "admin@example.com", "caller": fake, "reason": fake,
    }
    report = _scheduler(SchedulerStore(db)).drain()

    written = repr(db.docs["tasks/task_l0"]["blocked_by"]) + repr(report.to_dict())
    assert fake not in written
    assert "admin@example.com" not in written
