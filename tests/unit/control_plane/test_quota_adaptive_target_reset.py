"""A platform admin can reset a tenant's throttled AIMD target (QUOTA-RESET).

THE CASE THAT ASKED FOR IT (owner, 2026-10-14). `u-bogdan` hit
"provider:anthropic:tenant:u-bogdan full": the pool's hard limit was 40 and
the quota's `configured_hard_max` 50, but one 429 at 09:47Z had halved the
AIMD target and the climb back is +1 per 20 consecutive successes, so the
effective limit sat at 3. After lending the tenant two more accounts the owner
wanted the target at 40, and there was no route that raises `adaptive_target`
-- `set_hard_max` only ever clamps it down, by design.

So one was added, narrowly, and these tests hold the narrowness:

  * the target is clamped into [min_target, configured_hard_max] by
    `aimd._clamp`, the single clamp, and `configured_hard_max` never moves;
  * only a PLATFORM caller may make it (the same check `set_hard_max` uses);
  * an EXHAUSTED tenant is refused unless the request also clears the
    cooldown -- raising a target the provider has just told us is spent opens
    nothing and would read as if it had;
  * the quota document, the per-tenant pool and the audit record are written
    in ONE transaction, so the scheduler never reads a cap derived from a
    state that is not the one the audit says was set;
  * `adaptive_target <= configured_hard_max` holds after any sequence of
    resets, 429s, successes and hard-max changes.

No credentials, no network: the broker runs over the shared FakeFirestore.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_common.models import ProviderState, QuotaState

from quota_broker.aimd import AimdConfig
from quota_broker.service import (
    QUOTA_AUDIT,
    QuotaBroker,
    TargetResetRefused,
    quota_to_firestore,
)
from quota_broker.settings import BrokerSettings

from .conftest import PROJECT, auth_header, core_settings, seed_tenant
from .fakes import FakeFirestore, FakeTransaction

T0 = datetime(2026, 10, 14, 9, 47, tzinfo=timezone.utc)
PROVIDER = "anthropic"
TENANT = "u-bogdan"
DOC = f"quota/{PROVIDER}:{TENANT}"
POOL = f"pools/provider:{PROVIDER}:tenant:{TENANT}"
ADMIN = "root@example.com"


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _broker(db: FakeFirestore, clock: _Clock | None = None) -> QuotaBroker:
    return QuotaBroker(
        db,
        settings=BrokerSettings(core=core_settings(), default_hard_max=50),
        now=clock or _Clock(),
    )


def _seed(db: FakeFirestore, **fields: Any) -> QuotaState:
    """The measured u-bogdan state: hard max 50, target 3, window open again."""
    state = QuotaState(
        provider=PROVIDER,
        tenant_id=TENANT,
        state=ProviderState.AVAILABLE,
        updated_at=T0,
        configured_hard_max=50,
        adaptive_target=3,
        last_429_at=T0,
        success_count=7,
        rate_limit_count=1,
    )
    for name, value in fields.items():
        setattr(state, name, value)
    db.docs[DOC] = quota_to_firestore(state)
    db.docs[POOL] = {
        "name": f"provider:{PROVIDER}:tenant:{TENANT}",
        "hard_limit": 40,
        "adaptive_target": state.adaptive_target,
        "quota_derived_limit": 3,
        "active": 3,
        "enabled": True,
        "updated_at": T0,
    }
    return state


def _audits(db: FakeFirestore) -> list[dict[str, Any]]:
    return [data for path, data in db.docs.items() if path.startswith(f"{QUOTA_AUDIT}/")]


def _pool_effective(db: FakeFirestore) -> int | None:
    from swarm_api.codec import pool_from_dict

    return pool_from_dict(POOL.split("/", 1)[1], db.docs[POOL]).effective_limit


# -- the reset itself ---------------------------------------------------------


def test_the_measured_case_raises_the_target_and_the_pool_it_caps(db):
    _seed(db)
    result = _broker(db).reset_adaptive_target(
        PROVIDER, TENANT, target=40, reason="lent two more accounts",
        requested_by=ADMIN, caller="swarm-api@example",
    )

    assert result.state.adaptive_target == 40
    assert result.state.configured_hard_max == 50
    # The success run and the rate-limit run start over from the new target.
    assert result.state.success_count == 0
    assert result.state.rate_limit_count == 0

    stored = db.docs[DOC]
    assert stored["adaptive_target"] == 40
    assert stored["configured_hard_max"] == 50
    pool = db.docs[POOL]
    assert pool["adaptive_target"] == 40
    assert pool["quota_derived_limit"] == 40
    # `active` belongs to the scheduler and is patched around, never reset.
    assert pool["active"] == 3
    assert _pool_effective(db) == 40


def test_a_target_above_the_hard_max_is_clamped_to_it_and_the_hard_max_never_moves(db):
    _seed(db)
    result = _broker(db).reset_adaptive_target(
        PROVIDER, TENANT, target=10_000, reason="more", requested_by=ADMIN
    )
    assert result.state.adaptive_target == 50
    assert result.state.configured_hard_max == 50
    assert db.docs[DOC]["configured_hard_max"] == 50
    (audit,) = _audits(db)
    assert audit["requested_target"] == 10_000
    assert audit["to_target"] == 50
    assert audit["clamped"] is True


def test_a_target_below_min_target_is_clamped_up_to_it(db):
    _seed(db)
    floor = AimdConfig().min_target
    result = _broker(db).reset_adaptive_target(
        PROVIDER, TENANT, target=0, reason="hold it low", requested_by=ADMIN
    )
    assert result.state.adaptive_target == floor


def test_a_hard_max_of_zero_pins_the_reset_at_zero(db):
    _seed(db, configured_hard_max=0, adaptive_target=0)
    result = _broker(db).reset_adaptive_target(
        PROVIDER, TENANT, target=40, reason="r", requested_by=ADMIN
    )
    assert result.state.adaptive_target == 0


def test_an_exhausted_tenant_is_refused_and_nothing_is_written(db):
    _seed(
        db,
        state=ProviderState.EXHAUSTED,
        cooldown_until=T0 + timedelta(minutes=15),
        quota_derived_limit=0,
        rate_limit_count=5,
    )
    before_doc = dict(db.docs[DOC])
    before_pool = dict(db.docs[POOL])

    with pytest.raises(TargetResetRefused) as refused:
        _broker(db).reset_adaptive_target(
            PROVIDER, TENANT, target=40, reason="r", requested_by=ADMIN
        )
    assert "clear_cooldown" in str(refused.value)
    assert db.docs[DOC] == before_doc
    assert db.docs[POOL] == before_pool
    assert _audits(db) == []


def test_an_exhausted_tenant_is_reset_when_the_request_clears_the_cooldown(db):
    _seed(
        db,
        state=ProviderState.EXHAUSTED,
        cooldown_until=T0 + timedelta(minutes=15),
        retry_after_seconds=900,
        quota_derived_limit=0,
        rate_limit_count=5,
    )
    result = _broker(db).reset_adaptive_target(
        PROVIDER, TENANT, target=40, reason="r", requested_by=ADMIN, clear_cooldown=True
    )
    assert result.state.state is ProviderState.AVAILABLE
    assert result.state.cooldown_until is None
    assert result.state.adaptive_target == 40
    assert db.docs[POOL]["quota_derived_limit"] == 40
    (audit,) = _audits(db)
    assert audit["cleared_cooldown"] is True
    assert audit["from_state"] == "EXHAUSTED"
    assert audit["to_state"] == "AVAILABLE"


def test_a_cooldown_is_left_in_force_unless_the_request_clears_it(db):
    """Raising the target is not the same decision as reopening the window."""
    until = T0 + timedelta(seconds=60)
    _seed(
        db,
        state=ProviderState.THROTTLED,
        cooldown_until=until,
        retry_after_seconds=60,
        quota_derived_limit=0,
    )
    result = _broker(db).reset_adaptive_target(
        PROVIDER, TENANT, target=40, reason="r", requested_by=ADMIN
    )
    assert result.state.adaptive_target == 40
    assert result.state.state is ProviderState.THROTTLED
    assert result.state.cooldown_until == until
    # Still capped at zero until the window reopens: the sweep retires it.
    assert db.docs[POOL]["quota_derived_limit"] == 0
    assert db.docs[POOL]["adaptive_target"] == 40
    (audit,) = _audits(db)
    assert audit["cleared_cooldown"] is False


def test_a_disabled_provider_stays_disabled(db):
    """The admin switch outranks this route: only `/enabled` leaves DISABLED."""
    _seed(db, state=ProviderState.DISABLED)
    result = _broker(db).reset_adaptive_target(
        PROVIDER, TENANT, target=40, reason="r", requested_by=ADMIN, clear_cooldown=True
    )
    assert result.state.state is ProviderState.DISABLED
    assert db.docs[POOL]["quota_derived_limit"] == 0


def test_a_reason_is_required(db):
    _seed(db)
    with pytest.raises(ValueError):
        _broker(db).reset_adaptive_target(
            PROVIDER, TENANT, target=40, reason="   ", requested_by=ADMIN
        )
    assert _audits(db) == []


# -- one transaction ------------------------------------------------------------


class _RecordingTransaction(FakeTransaction):
    def _commit(self) -> list[Any]:
        self._db.committed_writes.append(sorted(ref.path for _op, ref, _data in self._buffer))
        return super()._commit()


class _RecordingFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.committed_writes: list[list[str]] = []

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return _RecordingTransaction(self)


def test_the_quota_document_the_pool_and_the_audit_commit_together():
    db = _RecordingFirestore()
    _seed(db)
    _broker(db).reset_adaptive_target(
        PROVIDER, TENANT, target=40, reason="r", requested_by=ADMIN
    )
    together = [
        paths for paths in db.committed_writes
        if DOC in paths
    ]
    assert len(together) == 1, db.committed_writes
    (paths,) = together
    assert POOL in paths
    assert any(p.startswith(f"{QUOTA_AUDIT}/") for p in paths), paths


# -- the audit record -------------------------------------------------------------


def test_the_audit_record_says_who_from_what_to_what_why_and_when(db):
    _seed(db)
    clock = _Clock()
    clock.now = T0 + timedelta(hours=2)
    _broker(db, clock).reset_adaptive_target(
        PROVIDER, TENANT, target=40, reason="lent two more accounts",
        requested_by=ADMIN, caller="swarm-api@example",
    )
    (audit,) = _audits(db)
    assert audit["action"] == "adaptive_target_reset"
    assert audit["provider"] == PROVIDER
    assert audit["tenant_id"] == TENANT
    assert audit["requested_by"] == ADMIN
    assert audit["caller"] == "swarm-api@example"
    assert audit["from_target"] == 3
    assert audit["to_target"] == 40
    assert audit["configured_hard_max"] == 50
    assert audit["reason"] == "lent two more accounts"
    assert audit["at"] == clock.now


# -- the invariant -----------------------------------------------------------------


@pytest.mark.parametrize("seed", range(25))
def test_no_sequence_of_resets_and_signals_breaks_the_invariant(db, seed):
    rng = random.Random(seed)
    clock = _Clock()
    broker = _broker(db, clock)
    for _ in range(80):
        clock.now += timedelta(seconds=rng.randint(0, 120))
        move = rng.choice(["reset", "reset", "success", "429", "exhausted", "hard_max"])
        try:
            if move == "reset":
                broker.reset_adaptive_target(
                    PROVIDER, TENANT, target=rng.randint(0, 200), reason="r",
                    requested_by=ADMIN, clear_cooldown=rng.random() < 0.5,
                )
            elif move == "success":
                broker.observe_success(PROVIDER, TENANT)
            elif move == "429":
                broker.observe_rate_limit(PROVIDER, TENANT)
            elif move == "exhausted":
                broker.observe_exhausted(PROVIDER, TENANT)
            else:
                broker.set_hard_max(PROVIDER, TENANT, rng.randint(0, 60))
        except TargetResetRefused:
            pass
        stored = db.docs[DOC]
        assert stored["adaptive_target"] is not None
        assert 0 <= stored["adaptive_target"] <= stored["configured_hard_max"], (seed, move)
        assert db.docs[POOL]["adaptive_target"] <= stored["configured_hard_max"]
    for audit in _audits(db):
        assert audit["to_target"] <= audit["configured_hard_max"]


# -- the broker's HTTP route ----------------------------------------------------------


def _broker_client(broker: QuotaBroker, identity: Any) -> TestClient:
    from quota_broker.main import create_app

    return TestClient(create_app(broker, identity=identity), raise_server_exceptions=False)


def test_the_route_is_platform_only(db):
    from quota_broker.main import WorkerIdentity

    _seed(db)

    class TenantWorker(WorkerIdentity):
        def resolve(self, authorization):
            return TENANT, False         # the tenant's own worker, not platform

    client = _broker_client(_broker(db), TenantWorker(project_id=PROJECT))
    response = client.put(
        f"/v1/quota/{PROVIDER}/{TENANT}/adaptive-target",
        json={"target": 40, "reason": "mine"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert db.docs[DOC]["adaptive_target"] == 3
    assert _audits(db) == []


def test_a_platform_caller_resets_and_the_metric_counts_it(db):
    from quota_broker.main import WorkerIdentity

    _seed(db)
    client = _broker_client(_broker(db), WorkerIdentity(required=False))
    response = client.put(
        f"/v1/quota/{PROVIDER}/{TENANT}/adaptive-target",
        json={"target": 40, "reason": "lent two more accounts", "requested_by": ADMIN},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quota"]["adaptive_target"] == 40
    assert body["reset"]["from_target"] == 3
    assert body["reset"]["to_target"] == 40
    assert db.docs[DOC]["adaptive_target"] == 40

    metrics = client.get("/metrics").text
    assert (
        'swarm_quota_admin_changes_total{action="adaptive_target_reset",'
        'provider="anthropic"} 1.0'
    ) in metrics


def test_the_route_refuses_an_exhausted_tenant_with_409(db):
    from quota_broker.main import WorkerIdentity

    _seed(db, state=ProviderState.EXHAUSTED, cooldown_until=T0 + timedelta(minutes=15),
          quota_derived_limit=0)
    client = _broker_client(_broker(db), WorkerIdentity(required=False))
    response = client.put(
        f"/v1/quota/{PROVIDER}/{TENANT}/adaptive-target",
        json={"target": 40, "reason": "r"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "quota_exhausted"
    assert db.docs[DOC]["adaptive_target"] == 3


def test_the_route_refuses_a_missing_reason_and_an_unknown_field(db):
    from quota_broker.main import WorkerIdentity

    _seed(db)
    client = _broker_client(_broker(db), WorkerIdentity(required=False))
    url = f"/v1/quota/{PROVIDER}/{TENANT}/adaptive-target"
    assert client.put(url, json={"target": 40}).status_code == 422
    # It never changes the hard max, so it does not accept one.
    assert client.put(
        url, json={"target": 40, "reason": "r", "hard_max": 500}
    ).status_code == 422
    assert db.docs[DOC]["configured_hard_max"] == 50


# -- the swarm-api admin proxy ---------------------------------------------------------


class _FakeBroker:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response or {"quota": {"adaptive_target": 40}, "reset": {}}

    def set_adaptive_target(self, provider: str, tenant_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"provider": provider, "tenant_id": tenant_id, **kwargs})
        return self._response


def _api(api_context, broker: Any) -> TestClient:
    from swarm_api.main import create_app

    app = create_app(api_context)
    app.state.account_pool = broker
    return TestClient(app, raise_server_exceptions=False)


def test_the_admin_proxy_forwards_with_the_admins_own_identity(api_context, db):
    seed_tenant(db, TENANT)
    broker = _FakeBroker()
    response = _api(api_context, broker).put(
        f"/v1/admin/quota/{PROVIDER}/{TENANT}/adaptive-target",
        json={"target": 40, "reason": "lent two more accounts"},
        headers=auth_header("root"),
    )
    assert response.status_code == 200, response.text
    assert broker.calls == [
        {
            "provider": PROVIDER,
            "tenant_id": TENANT,
            "target": 40,
            "reason": "lent two more accounts",
            "clear_cooldown": False,
            "requested_by": ADMIN,
        }
    ]


def test_the_admin_proxy_is_admin_only(api_context, db):
    seed_tenant(db, TENANT)
    broker = _FakeBroker()
    response = _api(api_context, broker).put(
        f"/v1/admin/quota/{PROVIDER}/{TENANT}/adaptive-target",
        json={"target": 40, "reason": "mine"},
        headers=auth_header("alice"),
    )
    assert response.status_code == 403
    assert broker.calls == []


def test_the_admin_proxy_refuses_an_unknown_tenant_before_calling_the_broker(api_context):
    broker = _FakeBroker()
    response = _api(api_context, broker).put(
        f"/v1/admin/quota/{PROVIDER}/no-such-tenant/adaptive-target",
        json={"target": 40, "reason": "r"},
        headers=auth_header("root"),
    )
    assert response.status_code == 404
    assert broker.calls == []


def test_the_broker_client_puts_to_the_broker_route_and_maps_409_to_conflict():
    import json

    from swarm_api.brokerclient import BrokerClient
    from swarm_api.errors import Conflict

    calls: list[dict[str, Any]] = []

    def transport(status: int, body: dict[str, Any]):
        def _send(method, url, *, headers, body=None, timeout=30.0):
            calls.append({"method": method, "url": url,
                          "body": json.loads(body.decode()) if body else None})
            return status, reply
        reply = body
        return _send

    class _Token:
        def token(self, audience: str) -> str:
            return "t"

    ok = BrokerClient(base_url="https://broker.invalid", audience="https://broker.invalid",
                      tokens=_Token(), transport=transport(200, {"quota": {}}))
    ok.set_adaptive_target(PROVIDER, TENANT, target=40, reason="r",
                           clear_cooldown=True, requested_by=ADMIN)
    assert calls[-1]["method"] == "PUT"
    assert calls[-1]["url"] == f"https://broker.invalid/v1/quota/{PROVIDER}/{TENANT}/adaptive-target"
    assert calls[-1]["body"] == {"target": 40, "reason": "r", "clear_cooldown": True,
                                 "requested_by": ADMIN}

    refused = BrokerClient(
        base_url="https://broker.invalid", audience="https://broker.invalid",
        tokens=_Token(),
        transport=transport(409, {"code": "quota_exhausted", "message": "exhausted"}),
    )
    with pytest.raises(Conflict):
        refused.set_adaptive_target(PROVIDER, TENANT, target=40, reason="r",
                                    clear_cooldown=False, requested_by=ADMIN)
