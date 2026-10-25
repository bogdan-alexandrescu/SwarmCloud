"""The lifecycle's 429 report names the pool account the attempt holds (#2302).

`ControlPlane.update_quota_state` carries the account to the broker (pinned in
tests/unit/control_plane/test_account_429_scope.py); these pin the call sites
that hand it over. A runner's 429 on a held account names that account; a
stop the worker READ from the broker (preflight, backpressure) names none,
because it was no account's refusal; an attempt on the tenant's own key names
none. And the park goes ahead exactly as before (invariant 4).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker.quota import QuotaSignal, decide
from swarm_common.models import ProviderState

from worker_seeds import seed_attempt, seed_tenant
from fakes import FakeSecretClient
from test_account_lease import (
    ACCOUNT_ID,
    ACCOUNT_SECRET,
    TOKEN,
    TENANT_SECRET,
    FakeBroker,
    _pool_worker,
)


class _Stop(Exception):
    """Ends `_park_for_quota` at the report, so nothing past it needs faking."""


def _recording(worker) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def update_quota_state(**kwargs: Any) -> bool:
        calls.append(kwargs)
        raise _Stop

    worker.control.update_quota_state = update_quota_state
    worker._checkpoint = lambda *a, **k: None
    worker._upload_outputs = lambda *a, **k: {}
    return calls


def _decision(source: str):
    return decide(
        QuotaSignal(
            provider="anthropic",
            state=ProviderState.EXHAUSTED,
            source=source,
            retry_after_seconds=1800,
            detail="claude-code reported a provider rate limit",
        ),
        max_in_worker_retry_delay_seconds=60,
    )


def _held_worker(db, worker_factory, tmp_path):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    secrets = FakeSecretClient({ACCOUNT_SECRET: TOKEN, TENANT_SECRET: "tenant-key"})
    worker, _, _ = _pool_worker(worker_factory, secrets, FakeBroker(), tmp_path)
    worker._build_child_env()
    assert worker._account is not None and worker._account.account_id == ACCOUNT_ID
    return worker


def test_a_runner_429_on_a_held_account_names_it(db, worker_factory, tmp_path):
    worker = _held_worker(db, worker_factory, tmp_path)
    calls = _recording(worker)

    with pytest.raises(_Stop):
        worker._park_for_quota(_decision("runner"), source="runner")

    assert calls[0]["account"] is worker._account
    assert calls[0]["message"] == "claude-code reported a provider rate limit"


def test_a_stop_read_from_the_broker_names_no_account(db, worker_factory, tmp_path):
    worker = _held_worker(db, worker_factory, tmp_path)
    calls = _recording(worker)

    with pytest.raises(_Stop):
        worker._park_for_quota(_decision("control_plane"), source="preflight")

    assert calls[0]["account"] is None


def test_an_attempt_on_the_tenants_own_key_names_no_account(db, worker_factory):
    seed_attempt(db, runner_profile="mock")
    worker, _, _ = worker_factory()
    calls = _recording(worker)

    with pytest.raises(_Stop):
        worker._park_for_quota(_decision("runner"), source="runner")

    assert calls[0]["account"] is None
