"""A worker's provider outcome reaches AIMD, through the broker's own routes.

Gap audit D1 (2026-10-01): AIMD was not in the data path. The worker wrote
`quota/{provider}:{tenant}` itself and nothing called the broker's report
routes, so the control law in `quota_broker.aimd` -- halve on a 429, +1 after a
run of successes, EXHAUSTED at the threshold -- never ran on a real 429.

Here the production `ControlPlane.update_quota_state` reports through a
transport that is the broker's real FastAPI app (`create_app`, a TestClient),
the broker writes to the in-memory Firestore, and the assertions read what the
broker wrote and what a worker then polls. Nothing in between is mocked: the
only stand-in is the identity check, which answers "tenant eng's worker" the
way a verified ID token from `swarm-agent-worker-eng` does.

Also pinned here, because the broker is now where they live: the rate-limit
run rules CP-10 (#85) used to apply in the worker.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from agent_worker.control import ControlPlane
from agent_worker.logs import build_logger
from agent_worker.quota import decide, signal_from_control
from swarm_common.models import ProviderState, utcnow
from swarm_common.states import TaskState

from quota_broker.aimd import AimdConfig
from quota_broker.main import WorkerIdentity, create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import PROJECT, core_settings
from .fakes import FakeFirestore

TENANT = "eng"
PROVIDER = "anthropic"
DOC = f"quota/{PROVIDER}:{TENANT}"
TENANT_POOL = f"pools/provider:{PROVIDER}:tenant:{TENANT}"


class _TenantWorker(WorkerIdentity):
    """A verified token from tenant eng's worker service account."""

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return TENANT, False


class _BrokerOverHttp:
    """`control.QuotaReporter`, sending each report to the broker's real app."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self.statuses: list[int] = []

    def report(
        self, *, provider: str, tenant_id: str, route: str, body: Mapping[str, Any]
    ) -> None:
        response = self._client.post(
            f"/v1/quota/{provider}/{tenant_id}/{route}", json=dict(body)
        )
        self.statuses.append(response.status_code)
        if response.status_code != 200:
            raise RuntimeError(f"broker answered {response.status_code}: {response.text}")


def _broker(db: FakeFirestore, **aimd: Any) -> QuotaBroker:
    return QuotaBroker(
        db,
        settings=BrokerSettings(
            core=core_settings(), default_hard_max=40, aimd=AimdConfig(**aimd)
        ),
    )


def _worker(db: FakeFirestore, transport: _BrokerOverHttp, n: int = 1) -> ControlPlane:
    """One attempt's control plane. `n` makes it a different worker."""
    task_id, attempt_id, lease_id = f"task_{n}", f"att_{n}", f"lease_{n}"
    db.document(f"tasks/{task_id}").set(
        {"tenant_id": TENANT, "state": TaskState.RUNNING.value, "current_generation": 1}
    )
    db.document(f"leases/{lease_id}").set({"tenant_id": TENANT, "released_at": None})
    return ControlPlane(
        db,
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        tenant_id=TENANT,
        generation=1,
        logger=build_logger(
            task_id=task_id,
            attempt_id=attempt_id,
            tenant_id=TENANT,
            generation=1,
            runner_profile="claude-code",
            stream=io.StringIO(),
        ),
        quota_reporter=transport,
    )


@pytest.fixture
def wired():
    def _build(**aimd: Any) -> tuple[FakeFirestore, QuotaBroker, _BrokerOverHttp]:
        db = FakeFirestore()
        broker = _broker(db, **aimd)
        client = TestClient(
            create_app(broker, identity=_TenantWorker(project_id=PROJECT)),
            raise_server_exceptions=False,
        )
        return db, broker, _BrokerOverHttp(client)

    return _build


# -- the control law, end to end -------------------------------------------------


def test_a_429_report_halves_the_target(wired):
    db, broker, transport = wired()
    before = broker.get(PROVIDER, TENANT).adaptive_target
    assert before == 40

    assert _worker(db, transport).update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=600
    ) is True

    assert transport.statuses == [200]
    stored = db.docs[DOC]
    assert stored["adaptive_target"] == 20, "a single 429 must halve the target"
    assert stored["rate_limit_count"] == 1
    # THROTTLED, with a cooldown the sweep honours -- not the worker's
    # EXHAUSTED label, and not a state the sweep flips back on its next tick.
    assert stored["state"] == ProviderState.THROTTLED.value
    assert stored["cooldown_until"] > utcnow() + timedelta(seconds=500)
    broker.sweep()
    assert db.docs[DOC]["state"] == ProviderState.THROTTLED.value
    assert db.docs[TENANT_POOL]["quota_derived_limit"] == 0
    assert db.docs[TENANT_POOL]["adaptive_target"] == 20


def test_a_success_report_adds_one_once_the_run_is_complete(wired):
    # One success is a complete run here, so one report shows the +1.
    db, broker, transport = wired(success_threshold=1)
    worker = _worker(db, transport)
    worker.update_quota_state(
        provider=PROVIDER, state=ProviderState.THROTTLED, retry_after_seconds=0
    )
    assert db.docs[DOC]["adaptive_target"] == 20

    assert worker.update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE) is True

    stored = db.docs[DOC]
    assert stored["adaptive_target"] == 21, "a success must add one"
    assert stored["state"] == ProviderState.AVAILABLE.value
    assert db.docs[TENANT_POOL]["adaptive_target"] == 21


def test_the_default_run_needs_its_full_length_before_the_one(wired):
    db, broker, transport = wired()
    worker = _worker(db, transport)
    worker.update_quota_state(
        provider=PROVIDER, state=ProviderState.THROTTLED, retry_after_seconds=0
    )
    threshold = broker.config.success_threshold
    for _ in range(threshold - 1):
        worker.update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)
    assert db.docs[DOC]["adaptive_target"] == 20
    worker.update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)
    assert db.docs[DOC]["adaptive_target"] == 21


def test_the_exhaustion_threshold_stops_the_tenant_and_the_next_worker_parks(wired):
    db, broker, transport = wired()
    threshold = broker.config.exhaustion_threshold
    # Different workers, each meeting its own 429: one report each.
    for n in range(1, threshold + 1):
        assert _worker(db, transport, n).update_quota_state(
            provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=600
        ) is True
        expected = ProviderState.EXHAUSTED if n == threshold else ProviderState.THROTTLED
        assert db.docs[DOC]["state"] == expected.value, n

    assert db.docs[TENANT_POOL]["quota_derived_limit"] == 0

    # The next worker on the tenant reads the broker's stop and parks before
    # it starts an agent (invariant 4), and does not report the stop back.
    nxt = _worker(db, transport, threshold + 1)
    signal = signal_from_control(nxt.poll(PROVIDER), PROVIDER)
    assert signal is not None
    assert decide(signal, max_in_worker_retry_delay_seconds=45).park is True
    reported = len(transport.statuses)
    nxt.update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=600
    )
    assert len(transport.statuses) == reported
    assert db.docs[DOC]["rate_limit_count"] == threshold


def test_the_worker_writes_no_quota_document_the_broker_writes_it(wired):
    db, _broker_, transport = wired()
    writes: list[str] = []

    class _Watching(_BrokerOverHttp):
        def report(self, **kwargs: Any) -> None:
            before = dict(db.docs)
            super().report(**kwargs)
            writes.extend(p for p in db.docs if p.startswith("quota/") and p not in before)

    worker = _worker(db, _Watching(transport._client))
    worker.update_quota_state(provider=PROVIDER, state=ProviderState.THROTTLED)
    # The only quota document is the one the broker's route created.
    assert writes == [DOC]


# -- the rate-limit run (CP-10, #85), now the broker's ---------------------------


def test_a_document_whose_first_report_is_a_success_counts_no_429(wired):
    db, _, transport = wired()
    _worker(db, transport).update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)
    assert db.docs[DOC]["rate_limit_count"] == 0
    assert db.docs[DOC]["last_429_at"] is None


def test_a_success_ends_the_run_so_scattered_429s_never_exhaust(wired):
    """`exhaustion_threshold` counts CONSECUTIVE 429s, as its docs say."""
    db, broker, transport = wired()
    worker = _worker(db, transport)
    for _ in range(broker.config.exhaustion_threshold * 2):
        worker.update_quota_state(
            provider=PROVIDER, state=ProviderState.THROTTLED, retry_after_seconds=0
        )
        worker.update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE)
    stored = db.docs[DOC]
    assert stored["rate_limit_count"] == 0
    assert stored["state"] == ProviderState.AVAILABLE.value
    assert stored["last_429_at"] is not None, "the time of the last 429 is kept"


def test_a_seeded_count_with_no_429_behind_it_is_read_as_zero(wired):
    """The worker used to create documents claiming a 429 nobody saw."""
    db, broker, transport = wired()
    db.document(DOC).set(
        {
            "provider": PROVIDER,
            "tenant_id": TENANT,
            "state": ProviderState.AVAILABLE.value,
            "updated_at": utcnow() - timedelta(hours=25),
            "configured_hard_max": 40,
            "rate_limit_count": broker.config.exhaustion_threshold - 1,
            "success_count": 0,
        }
    )
    _worker(db, transport).update_quota_state(
        provider=PROVIDER, state=ProviderState.THROTTLED, retry_after_seconds=30
    )
    stored = db.docs[DOC]
    assert stored["rate_limit_count"] == 1
    assert stored["state"] == ProviderState.THROTTLED.value
