"""The worker reports provider outcomes to the quota broker; it writes no quota state.

THE DEFECT (gap audit D1, 2026-10-01). docs/quota-management.md section 3 says a
429 travels runner -> worker -> broker, and the broker applies AIMD to
`quota/{provider}:{tenant}`. The code did not: `ControlPlane.update_quota_state`
wrote that document itself, nothing called the broker's
`/v1/quota/{p}/{t}/success|rate-limit` routes, so a 429 never halved the target,
a success never counted toward an increase, and the exhaustion threshold never
ran. The broker's sweep then flipped the worker's THROTTLED (it carried no
cooldown) back to AVAILABLE on its next tick.

What these pin, against the production `ControlPlane`:

  * a 429 goes to `rate-limit`, a clean run to `success`, carrying what the
    provider said; and no `quota/` document is written by the worker at all;
  * a report the broker cannot take is logged and dropped -- it never raises,
    and a worker whose broker is down still parks, releases and exits
    (invariant 4 does not wait on the report);
  * one 429 is one report: the lifecycle reports a runner's 429 and then
    reports it again as it parks, and a park on the broker's OWN stop
    (preflight, backpressure) would report that stop back. Each would be a
    second halving for one provider answer.

The broker's side -- the halving, the increase, the threshold -- is pinned in
tests/unit/control_plane/test_quota_reports_reach_aimd.py, through the real
routes.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.models import ProviderState, utcnow
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt
from fakes import FakeFirestore, FakeTransactionRunner, RecordingQuotaReporter

PROVIDER = "anthropic"
DOC = f"quota/{PROVIDER}:{TENANT}"


def _control(db: FakeFirestore, reporter: object) -> ControlPlane:
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=1,
        logger=build_logger(
            task_id="task_1",
            attempt_id="att_1",
            tenant_id=TENANT,
            generation=1,
            runner_profile="claude-code",
            stream=io.StringIO(),
        ),
        txn_runner=FakeTransactionRunner(db),
        quota_reporter=reporter,  # type: ignore[arg-type]
    )


def _quota_writes(db: FakeFirestore) -> list[str]:
    return [path for _, path, _ in db.writes if path.startswith("quota/")]


# -- where each outcome goes --------------------------------------------------


@pytest.mark.parametrize(
    "state", [ProviderState.THROTTLED, ProviderState.EXHAUSTED], ids=lambda s: s.value
)
def test_a_429_is_reported_as_a_rate_limit_and_writes_no_document(state):
    db = FakeFirestore()
    reporter = RecordingQuotaReporter()
    reset = utcnow() + timedelta(minutes=30)

    sent = _control(db, reporter).update_quota_state(
        provider=PROVIDER, state=state, retry_after_seconds=1800, reset_at=reset
    )

    assert sent is True
    # EXHAUSTED too: every runner labels its 429 EXHAUSTED, and whether a
    # tenant is exhausted is the broker's threshold to decide.
    assert reporter.reports == [
        (
            PROVIDER,
            TENANT,
            "rate-limit",
            {"retry_after_seconds": 1800, "reset_at": reset.isoformat()},
        )
    ]
    assert DOC not in db.documents
    assert _quota_writes(db) == []


def test_a_clean_run_is_reported_as_a_success_and_writes_no_document():
    db = FakeFirestore()
    reporter = RecordingQuotaReporter()

    _control(db, reporter).update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)

    assert reporter.reports == [(PROVIDER, TENANT, "success", {})]
    assert _quota_writes(db) == []


def test_an_existing_document_is_left_exactly_as_the_broker_wrote_it():
    db = FakeFirestore()
    stored = {
        "provider": PROVIDER,
        "tenant_id": TENANT,
        "state": ProviderState.THROTTLED.value,
        "rate_limit_count": 2,
        "adaptive_target": 12,
        "configured_hard_max": 50,
    }
    db.seed(DOC, dict(stored))
    reporter = RecordingQuotaReporter()

    _control(db, reporter).update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)

    assert db.doc(DOC) == stored
    assert _quota_writes(db) == []


@pytest.mark.parametrize(
    "state",
    [ProviderState.COOLDOWN, ProviderState.DISABLED, ProviderState.UNKNOWN],
    ids=lambda s: s.value,
)
def test_a_state_the_worker_only_ever_read_from_the_broker_is_not_reported(state):
    reporter = RecordingQuotaReporter()
    assert _control(FakeFirestore(), reporter).update_quota_state(
        provider=PROVIDER, state=state
    ) is False
    assert reporter.attempts == 0


# -- one 429, one report --------------------------------------------------------


def test_the_same_429_reported_again_on_the_way_to_the_park_is_not_sent_twice():
    """The runner loop reports `quota.json`, then `_park_for_quota` reports the
    decision built from it. One provider answer, one halving."""
    reporter = RecordingQuotaReporter()
    control = _control(FakeFirestore(), reporter)

    control.update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=1800
    )
    assert control.update_quota_state(
        provider=PROVIDER,
        state=ProviderState.EXHAUSTED,
        retry_after_seconds=1800,
        reset_at=utcnow() + timedelta(seconds=1800),
    ) is False

    assert reporter.routes() == ["rate-limit"]


def test_a_fresh_429_after_the_window_closed_is_reported():
    """A short wait sleeps the window out and retries; a 429 then is new."""
    reporter = RecordingQuotaReporter()
    control = _control(FakeFirestore(), reporter)

    control.update_quota_state(
        provider=PROVIDER, state=ProviderState.THROTTLED, retry_after_seconds=0
    )
    control.update_quota_state(
        provider=PROVIDER, state=ProviderState.THROTTLED, retry_after_seconds=0
    )

    assert reporter.routes() == ["rate-limit", "rate-limit"]


def test_a_success_closes_the_window_and_is_always_sent():
    reporter = RecordingQuotaReporter()
    control = _control(FakeFirestore(), reporter)

    control.update_quota_state(
        provider=PROVIDER, state=ProviderState.THROTTLED, retry_after_seconds=600
    )
    control.update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)
    control.update_quota_state(
        provider=PROVIDER, state=ProviderState.THROTTLED, retry_after_seconds=600
    )

    assert reporter.routes() == ["rate-limit", "success", "rate-limit"]


def test_a_stop_read_back_from_the_broker_is_not_reported_back_to_it():
    """Preflight and backpressure park on the broker's own EXHAUSTED."""
    db = FakeFirestore()
    db.seed(
        "tasks/task_1",
        {"tenant_id": TENANT, "state": TaskState.RUNNING.value, "current_generation": 1},
    )
    db.seed("leases/lease_1", {"tenant_id": TENANT, "released_at": None})
    db.seed(
        DOC,
        {
            "provider": PROVIDER,
            "tenant_id": TENANT,
            "state": ProviderState.EXHAUSTED.value,
            "retry_after_seconds": 2400,
        },
    )
    reporter = RecordingQuotaReporter()
    control = _control(db, reporter)

    signals = control.poll(PROVIDER)
    assert signals.provider_paused is True
    assert control.update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=2400
    ) is False

    assert reporter.attempts == 0


# -- a report that fails is not the worker's problem ----------------------------


def test_a_report_the_broker_cannot_take_is_logged_and_dropped():
    reporter = RecordingQuotaReporter(fail=OSError("connection refused"))
    control = _control(FakeFirestore(), reporter)

    assert control.update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=1800
    ) is False
    assert reporter.attempts == 1
    # Not remembered as delivered: the park's own report tries again.
    assert control.update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=1800
    ) is False
    assert reporter.attempts == 2


def test_no_broker_configured_reports_nothing_and_writes_nothing():
    db = FakeFirestore()
    control = _control(db, None)
    assert control.update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=60
    ) is False
    assert _quota_writes(db) == []


def test_a_broker_outage_does_not_stop_the_quota_park(db, worker_factory):
    """Invariant 4: checkpoint, park, release, exit -- whether or not the
    broker heard about the 429."""
    seed_attempt(
        db,
        pool_active=4,
        task_input={
            "prompt": "burn quota",
            "steps": 1,
            "sleep_seconds": 0.05,
            "quota_exhausted": True,
            "retry_after_seconds": 1800,
        },
        simulated={"provider": "anthropic"},
    )
    reporter = RecordingQuotaReporter(fail=OSError("broker unreachable"))
    worker, _, _ = worker_factory(quota_reporter=reporter)

    assert worker.run() == ExitCode.PARKED

    assert reporter.attempts >= 1, "the worker never tried to report the 429"
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    lease = db.doc("leases/lease_1")
    assert lease["released_at"] is not None
    for pool in lease["pools"]:
        assert db.doc(f"pools/{pool}")["active"] == 3, pool
    assert _quota_writes(db) == []
