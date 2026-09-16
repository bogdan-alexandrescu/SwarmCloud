"""A lease stranded by a partial repair must still be reclaimed.

`invalidate_generation` and `release_lease` are separate transactions. Anything
interrupting between them leaves the lease unreleased at the OLD generation
while the task has already moved on. Detection used to skip that case entirely
-- "superseded; the generation rule handles it" -- but the generation rule acts
on executions, and a lease that never dispatched has none. The slots it held
were never returned.

Observed live on 2026-09-16: task generation 2, lease generation 1,
released_at None, five pools each holding active=1 an hour after the dispatch
deadline, and every reconciliation pass reporting findings: 0.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reconciler.detect import FindingKind, detect_stale_leases
from reconciler.model import ControlSnapshot


NOW = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)


class _Lease:
    def __init__(self, generation: int, released: bool = False) -> None:
        self.lease_id = "lease_1"
        self.task_id = "task_1"
        self.attempt_id = "att_1"
        self.tenant_id = "u-bogdan"
        self.generation = generation
        self.state = __import__("swarm_common.states", fromlist=["TaskState"]).TaskState.LEASED
        self.expires_at = NOW - timedelta(minutes=50)
        self.dispatch_deadline = NOW - timedelta(minutes=47)
        self.is_released = released

    def silent_seconds(self, now):  # noqa: ANN001
        return 3000.0


class _Task:
    def __init__(self, generation: int) -> None:
        self.task_id = "task_1"
        self.generation = generation
        self.holds_capacity = True


class _Config:
    heartbeat_grace_seconds = 120


def _snapshot(task_gen: int, lease_gen: int, released: bool = False) -> ControlSnapshot:
    snap = ControlSnapshot.__new__(ControlSnapshot)
    snap.tasks = {"task_1": _Task(task_gen)}
    snap.leases = {"lease_1": _Lease(lease_gen, released)}
    snap.attempts = {}
    snap.taken_at = NOW
    return snap


def test_superseded_lease_with_no_execution_is_reported_not_skipped():
    findings = detect_stale_leases(_snapshot(task_gen=2, lease_gen=1), {}, _Config(), now=NOW)
    kinds = [f.kind for f in findings]
    assert FindingKind.ORPHAN_LEASE in kinds, (
        "a lease stranded at an old generation with no execution must be reclaimed; "
        "skipping it leaks its slots for ever"
    )


def test_superseded_lease_WITH_an_execution_is_still_left_to_the_generation_rule():
    """The original safety property: do not double-handle a live execution."""
    class _Exec:
        is_active = True
        backend = "CLOUD_RUN_JOB"

    findings = detect_stale_leases(
        _snapshot(task_gen=2, lease_gen=1), {"att_1": _Exec()}, _Config(), now=NOW
    )
    assert FindingKind.ORPHAN_LEASE not in [f.kind for f in findings]


def test_a_released_lease_is_never_reported():
    findings = detect_stale_leases(
        _snapshot(task_gen=2, lease_gen=1, released=True), {}, _Config(), now=NOW
    )
    assert findings == []
