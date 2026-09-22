"""NOT YET ALIVE is not SILENT, and only one clock may judge the difference.

A lease is written with `heartbeat_at: None` (`swarm_common/admission.py:224`).
`LeaseView.silent_seconds` therefore falls back to `created_at`, so during the
window between admission and the worker's first control-plane write BOTH
liveness clocks -- `heartbeat_grace_seconds` (90) and `expires_at`
(`created_at + lease_timeout_seconds`, 120) -- are really measuring how long
the dispatch has been in flight. That is what `dispatch_deadline`
(`created_at + dispatch_timeout_seconds`, 300) exists to measure, and the two
answers disagree by 180 seconds.

MEASURED, 2026-09-22, from the live Firestore event streams in
saga-agents-staging: `dispatched` -> `starting` (the worker's first
control-plane write) on Cloud Run Jobs --

    claude-code   n=41   min 46.2s   p50 122.6s   p90 159.0s   max 226.2s
    mock          n=27   min 68.5s   p50 250.1s   p90 254.5s   max 256.1s

55 of those 68 attempts exceed the 90s grace and 44 exceed the 120s lease
timeout. NONE of them exceeds the 300s dispatch deadline.

WATCHED LIVE, task_b5dc2568713a40158851: fenced 183s after its lease was taken,
with the reconciler's own reason string recording the disagreement --

    lease silent for 183s (grace 90s, expired=True, dispatch_overdue=False)

`dispatch_overdue=False` is the whole defect in one field: the dispatch window
still had 117 seconds to run when the liveness rule killed an attempt whose
container had not finished booting. The task completed on generation 3, roughly
twelve minutes after it was submitted.

The worker already heartbeats before the agent starts -- `lifecycle.py:332`, the
first statement after `advance_to_running()`, moved there on 2026-09-19 for this
same reason. It cannot help here: the whole window is before any worker code
runs at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reconciler.config import ReconcilerConfig
from reconciler.detect import FindingKind, detect_stale_leases
from reconciler.model import ControlSnapshot, LeaseView, TaskView
from swarm_common.admission import AdmissionConfig
from swarm_common.states import TaskState

NOW = datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc)

#: The frozen contract's own numbers, not a restatement of them.
ADMISSION = AdmissionConfig()

CONFIG = ReconcilerConfig(
    project_id="saga-agents-staging",
    region="us-central1",
    firestore_database="swarm",
)


def _snapshot(
    *,
    age_seconds: float,
    heartbeat_age_seconds: float | None = None,
    lease_state: TaskState = TaskState.DISPATCHED,
    task_state: TaskState = TaskState.DISPATCHED,
    with_dispatch_deadline: bool = True,
) -> ControlSnapshot:
    """One task and its lease, built the way admission and dispatch write them."""
    created_at = NOW - timedelta(seconds=age_seconds)
    heartbeat_at = (
        None if heartbeat_age_seconds is None else NOW - timedelta(seconds=heartbeat_age_seconds)
    )
    # `expires_at` is re-extended from the last heartbeat (`control.py:547-553`),
    # and from `created_at` when there has never been one (`admission.py:199`).
    anchor = heartbeat_at or created_at
    doc = {
        "lease_id": "lease_1",
        "task_id": "task_1",
        "attempt_id": "att_1",
        "tenant_id": "eng",
        "generation": 1,
        "pools": ["global", "tenant:eng"],
        "units": 1,
        "state": lease_state.value,
        "created_at": created_at,
        "expires_at": anchor + timedelta(seconds=ADMISSION.lease_timeout_seconds),
        "heartbeat_at": heartbeat_at,
        "released_at": None,
    }
    if with_dispatch_deadline:
        doc["dispatch_deadline"] = created_at + timedelta(
            seconds=ADMISSION.dispatch_timeout_seconds
        )

    return ControlSnapshot(
        tasks={
            "task_1": TaskView(
                task_id="task_1",
                tenant_id="eng",
                state=task_state,
                generation=1,
                lease_id="lease_1",
                runner_profile="claude-code",
                resource_class="standard",
                updated_at=created_at,
            )
        },
        leases={"lease_1": LeaseView.from_doc(doc)},
        taken_at=NOW,
    )


# The measured claude-code cold starts, plus the mock profile's worst case. Each
# is a real observation from the live event stream; each is past both liveness
# clocks and inside the dispatch window.
@pytest.mark.parametrize(
    ("elapsed", "what"),
    [
        (122.6, "claude-code p50"),
        (159.0, "claude-code p90"),
        (226.2, "claude-code max"),
        (256.1, "mock max"),
        (
            float(ADMISSION.dispatch_timeout_seconds),
            "exactly on the dispatch deadline, which has not yet PASSED",
        ),
    ],
)
def test_a_lease_whose_worker_has_not_started_is_not_reclaimed_inside_the_dispatch_window(
    elapsed: float, what: str
) -> None:
    snapshot = _snapshot(age_seconds=elapsed)
    lease = snapshot.leases["lease_1"]

    # The premise: both liveness clocks have already run out at this point, so
    # this is not a test that passes by never reaching the rule.
    assert lease.silent_seconds(NOW) > CONFIG.heartbeat_grace_seconds
    assert lease.expires_at is not None and NOW > lease.expires_at

    findings = detect_stale_leases(snapshot, {}, CONFIG, now=NOW)

    assert findings == [], (
        f"{what} ({elapsed}s): a container that has not finished booting has never "
        "had the chance to heartbeat, so it is NOT YET ALIVE, not silent. Only the "
        f"dispatch deadline ({ADMISSION.dispatch_timeout_seconds}s) may judge this "
        f"window; got {[f.kind.value for f in findings]}"
    )


def test_a_lease_whose_worker_has_not_started_IS_reclaimed_once_the_deadline_passes() -> None:
    """The other half. Suppressing the liveness clocks must not strand the slot."""
    snapshot = _snapshot(age_seconds=ADMISSION.dispatch_timeout_seconds + 1)

    findings = detect_stale_leases(snapshot, {}, CONFIG, now=NOW)

    assert [f.kind for f in findings] == [FindingKind.STALE_LEASE], (
        "past the dispatch deadline the dispatch has provably failed and the slot "
        f"must come back; got {[f.kind.value for f in findings]}"
    )
    assert findings[0].detail["dispatch_overdue"] is True


def test_a_worker_that_proved_it_was_alive_and_then_went_silent_is_still_reclaimed() -> None:
    """Liveness detection must survive the fix.

    Once `heartbeat_at` is set the fallback to `created_at` is gone and silence
    means what it says, so the grace applies again -- while the dispatch
    deadline, which has not passed here, does not.
    """
    snapshot = _snapshot(age_seconds=200, heartbeat_age_seconds=150)
    lease = snapshot.leases["lease_1"]
    assert lease.dispatch_deadline is not None and NOW < lease.dispatch_deadline

    findings = detect_stale_leases(snapshot, {}, CONFIG, now=NOW)

    assert [f.kind for f in findings] == [FindingKind.STALE_LEASE], (
        "a worker that heartbeated and then stopped is genuinely silent; the "
        "not-yet-alive rule must not swallow it"
    )


def test_a_lease_with_no_dispatch_deadline_keeps_the_old_liveness_behaviour() -> None:
    """A document written before the field existed still has to be reclaimable.

    There is no deadline to defer to, so deferring would mean no clock at all
    and a slot held for ever -- the exact leak `detect_stale_leases` exists to
    close.
    """
    snapshot = _snapshot(age_seconds=200, with_dispatch_deadline=False)
    assert snapshot.leases["lease_1"].dispatch_deadline is None

    findings = detect_stale_leases(snapshot, {}, CONFIG, now=NOW)

    assert [f.kind for f in findings] == [FindingKind.STALE_LEASE]


def test_the_measured_cold_starts_fit_inside_the_frozen_dispatch_timeout() -> None:
    """The number the fix leans on, pinned to the evidence.

    If `dispatch_timeout_seconds` is ever lowered below the observed worst case,
    deferring to it stops being safe and this fails rather than silently
    reintroducing the fence.
    """
    worst_observed_cold_start_seconds = 256.1  # mock, n=27, 2026-09-22
    assert ADMISSION.dispatch_timeout_seconds > worst_observed_cold_start_seconds
    assert ADMISSION.lease_timeout_seconds < worst_observed_cold_start_seconds, (
        "if this ever stops being true the liveness clock alone would be enough "
        "and this whole rule can be deleted"
    )
