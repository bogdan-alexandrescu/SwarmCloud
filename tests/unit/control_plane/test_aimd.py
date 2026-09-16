"""AIMD control law.

The invariant that must never break:

    adaptive_target <= configured_hard_max

`configured_hard_max` is an admin's statement about what the platform is allowed
to do to a provider. AIMD is allowed to propose a number below it and nothing
else. The tests below try to break that from every direction: a long success
run, a hard max lowered underneath a high target, a corrupt stored value, and a
randomised sequence of successes and 429s.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import ProviderState, QuotaState

from .conftest import PROJECT

from quota_broker.aimd import (
    AimdConfig,
    current_target,
    initial_state,
    quota_derived_limit_for,
    record_exhausted,
    record_rate_limit,
    record_success,
    refresh,
)

T0 = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
CONFIG = AimdConfig(
    additive_increase=1,
    multiplicative_decrease=0.5,
    success_threshold=5,
    min_target=1,
    default_cooldown_seconds=30,
    max_cooldown_seconds=900,
    exhaustion_threshold=5,
)


def state(hard_max: int = 10, target: int | None = None, **kwargs) -> QuotaState:
    base = initial_state("anthropic", "eng", configured_hard_max=hard_max, now=T0)
    if target is not None:
        base.adaptive_target = target
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


# -- the required case: never above the hard max --------------------------

def test_sustained_success_never_exceeds_the_hard_max():
    current = state(hard_max=10, target=1)
    for step in range(2000):
        current = record_success(current, now=T0 + timedelta(seconds=step), config=CONFIG)
        assert current.adaptive_target <= current.configured_hard_max, (
            f"AIMD proposed {current.adaptive_target} over a hard max of "
            f"{current.configured_hard_max} after {step} successes"
        )
    # It should have actually climbed, or the test would pass vacuously.
    assert current.adaptive_target == 10


def test_a_lowered_hard_max_clamps_an_already_high_target():
    current = state(hard_max=50, target=50)
    current.configured_hard_max = 4          # an admin tightens the ceiling
    assert current_target(current, CONFIG) == 4
    after = record_success(current, now=T0, config=CONFIG)
    assert after.adaptive_target <= 4


def test_a_corrupt_stored_target_is_clamped_on_read():
    # A hand-edited document, or a rollback to an older hard max.
    current = state(hard_max=8, target=9999)
    assert current_target(current, CONFIG) == 8
    assert record_success(current, now=T0, config=CONFIG).adaptive_target <= 8


def test_zero_hard_max_pins_the_target_to_zero():
    """An operator's 0 outranks min_target: it means 'stop', not 'go slowly'."""
    current = state(hard_max=0, target=5)
    assert current_target(current, CONFIG) == 0
    assert record_success(current, now=T0, config=CONFIG).adaptive_target == 0


@pytest.mark.parametrize("seed", range(25))
def test_random_signal_sequences_never_break_the_invariant(seed):
    rng = random.Random(seed)
    current = state(hard_max=rng.randint(0, 40), target=None)
    moment = T0
    for _ in range(300):
        moment += timedelta(seconds=rng.randint(1, 120))
        roll = rng.random()
        if roll < 0.7:
            current = record_success(current, now=moment, config=CONFIG)
        elif roll < 0.92:
            current = record_rate_limit(current, now=moment, config=CONFIG)
        else:
            current = record_exhausted(current, now=moment, config=CONFIG)
        if rng.random() < 0.1:
            current.configured_hard_max = rng.randint(0, 40)
            current = record_success(current, now=moment, config=CONFIG)

        assert current.adaptive_target is not None
        assert 0 <= current.adaptive_target <= current.configured_hard_max
        assert quota_derived_limit_for(current, CONFIG) <= current.configured_hard_max


# -- the control law itself -----------------------------------------------

def test_increase_is_additive_and_requires_a_sustained_run():
    current = state(hard_max=100, target=10)
    for step in range(CONFIG.success_threshold - 1):
        current = record_success(current, now=T0 + timedelta(seconds=step), config=CONFIG)
        assert current.adaptive_target == 10, "one success must not move the target"
    current = record_success(current, now=T0 + timedelta(seconds=99), config=CONFIG)
    assert current.adaptive_target == 11


def test_decrease_is_multiplicative_and_immediate():
    current = state(hard_max=100, target=40)
    after = record_rate_limit(current, now=T0, config=CONFIG)
    assert after.adaptive_target == 20, "a single 429 must halve the target at once"
    assert after.state is ProviderState.THROTTLED
    assert after.success_count == 0, "the success run restarts after a 429"


def test_a_429_resets_a_nearly_complete_success_run():
    current = state(hard_max=100, target=10)
    for step in range(CONFIG.success_threshold - 1):
        current = record_success(current, now=T0 + timedelta(seconds=step), config=CONFIG)
    current = record_rate_limit(current, now=T0 + timedelta(seconds=50), config=CONFIG)
    current = record_success(current, now=T0 + timedelta(seconds=60), config=CONFIG)
    assert current.adaptive_target == 5, "the halved target must not immediately rebound"


def test_repeated_rate_limits_reach_exhausted_and_stop_the_work():
    current = state(hard_max=32, target=32)
    for step in range(CONFIG.exhaustion_threshold):
        current = record_rate_limit(current, now=T0 + timedelta(seconds=step), config=CONFIG)
    assert current.state is ProviderState.EXHAUSTED
    # This is the bridge onto the pool: an exhausted provider drives the
    # effective limit to zero, so nothing new is admitted against it.
    assert quota_derived_limit_for(current, CONFIG) == 0
    assert current.effective_limit == 0


def test_target_never_falls_below_min_target():
    current = state(hard_max=100, target=2)
    for step in range(30):
        current = record_rate_limit(current, now=T0 + timedelta(seconds=step), config=CONFIG)
    assert current.adaptive_target == CONFIG.min_target


# -- retry_after / reset_at ------------------------------------------------

def test_retry_after_sets_the_cooldown():
    current = record_rate_limit(state(), now=T0, retry_after_seconds=45, config=CONFIG)
    assert current.retry_after_seconds == 45
    assert current.cooldown_until == T0 + timedelta(seconds=45)


def test_reset_at_is_used_when_retry_after_is_absent():
    reset = T0 + timedelta(seconds=120)
    current = record_rate_limit(state(), now=T0, reset_at=reset, config=CONFIG)
    assert current.cooldown_until == reset
    assert current.reset_at == reset


def test_an_absurd_retry_after_is_capped():
    current = record_rate_limit(state(), now=T0, retry_after_seconds=86_400, config=CONFIG)
    assert current.retry_after_seconds == CONFIG.max_cooldown_seconds


def test_cooldown_expiry_restores_availability_without_jumping_to_the_hard_max():
    current = record_rate_limit(
        state(hard_max=40, target=40), now=T0, retry_after_seconds=60, config=CONFIG
    )
    assert quota_derived_limit_for(current, CONFIG) == 0

    still_cooling = refresh(current, now=T0 + timedelta(seconds=30), config=CONFIG)
    assert still_cooling.state is ProviderState.THROTTLED

    recovered = refresh(current, now=T0 + timedelta(seconds=61), config=CONFIG)
    assert recovered.state is ProviderState.AVAILABLE
    assert recovered.cooldown_until is None
    # Back to work, but at the halved target: jumping straight back to 40 is
    # how the next wall of 429s gets earned.
    assert recovered.adaptive_target == 20
    assert quota_derived_limit_for(recovered, CONFIG) == 20


def test_disabled_is_never_raised_by_a_success():
    current = state(hard_max=10, target=10)
    current.state = ProviderState.DISABLED
    after = record_success(current, now=T0, config=CONFIG)
    assert after.state is ProviderState.DISABLED
    assert quota_derived_limit_for(after, CONFIG) == 0
    assert refresh(after, now=T0 + timedelta(days=1), config=CONFIG).state is (
        ProviderState.DISABLED
    )


# -- the broker: state lands on the pools ---------------------------------

def test_exhausted_provider_drives_the_pool_limit_to_zero(broker, db):
    broker.observe_success("anthropic", "eng")
    pool = db.docs["pools/provider:anthropic:tenant:eng"]
    assert pool["quota_derived_limit"] > 0

    for _ in range(broker.config.exhaustion_threshold):
        broker.observe_rate_limit("anthropic", "eng", retry_after_seconds=60)

    pool = db.docs["pools/provider:anthropic:tenant:eng"]
    assert pool["quota_derived_limit"] == 0

    from swarm_api.codec import pool_from_dict

    assert pool_from_dict("provider:anthropic:tenant:eng", pool).effective_limit == 0


def test_one_tenants_exhaustion_does_not_cap_the_shared_provider_pool(broker, db):
    broker.observe_success("anthropic", "research")
    for _ in range(broker.config.exhaustion_threshold):
        broker.observe_rate_limit("anthropic", "eng")

    assert db.docs["pools/provider:anthropic:tenant:eng"]["quota_derived_limit"] == 0
    # research brought its own key; its work must keep flowing.
    assert db.docs["pools/provider:anthropic:tenant:research"]["quota_derived_limit"] > 0
    assert db.docs["pools/provider:anthropic"]["quota_derived_limit"] is None


def test_every_tenant_exhausted_caps_the_shared_provider_pool(broker, db):
    for tenant in ("eng", "research"):
        for _ in range(broker.config.exhaustion_threshold):
            broker.observe_rate_limit("anthropic", tenant)
    assert db.docs["pools/provider:anthropic"]["quota_derived_limit"] == 0


def test_broker_hard_max_change_clamps_immediately(broker, db):
    broker.observe_success("anthropic", "eng")
    result = broker.set_hard_max("anthropic", "eng", 3)
    assert result.configured_hard_max == 3
    assert result.adaptive_target <= 3
    assert db.docs["pools/provider:anthropic:tenant:eng"]["quota_derived_limit"] <= 3


def test_broker_sweep_retires_an_expired_cooldown(db):
    from quota_broker.service import QuotaBroker
    from quota_broker.settings import BrokerSettings

    from .conftest import core_settings

    clock = {"now": T0}
    broker = QuotaBroker(
        db,
        settings=BrokerSettings(core=core_settings(), default_hard_max=20),
        now=lambda: clock["now"],
    )
    broker.observe_rate_limit("anthropic", "eng", retry_after_seconds=60)
    assert db.docs["pools/provider:anthropic:tenant:eng"]["quota_derived_limit"] == 0

    # Nothing can run while the cap is zero, so no success can arrive to clear
    # the state. The sweep is what notices the window reopened.
    clock["now"] = T0 + timedelta(seconds=120)
    report = broker.sweep()
    assert report["recovered"] == 1
    assert db.docs["pools/provider:anthropic:tenant:eng"]["quota_derived_limit"] > 0


# -- the broker's HTTP surface ---------------------------------------------

def test_a_worker_may_not_report_quota_for_another_tenant(broker):
    """Otherwise a 429 report is a one-line denial of service on any tenant.

    The tenant is derived from the CALLER's service account, never from the
    path, so tenant A's worker reporting a rate limit against tenant B is
    refused rather than believed.
    """
    from fastapi.testclient import TestClient

    from quota_broker.main import WorkerIdentity, create_app

    class FixedIdentity(WorkerIdentity):
        def resolve(self, authorization):
            return "eng", False           # always tenant eng's worker, not platform

    client = TestClient(create_app(broker, identity=FixedIdentity(project_id=PROJECT)),
                        raise_server_exceptions=False)

    mine = client.post("/v1/quota/anthropic/eng/rate-limit", json={})
    assert mine.status_code == 200

    theirs = client.post("/v1/quota/anthropic/research/rate-limit", json={})
    assert theirs.status_code == 403
    assert theirs.json()["code"] == "forbidden"

    # ...and a worker may not raise its own ceiling either.
    assert client.put(
        "/v1/quota/anthropic/eng/hard-max", json={"hard_max": 10_000}
    ).status_code == 403


def test_an_unknown_provider_is_a_validation_error_not_a_permission_error(broker):
    """403 would send an operator hunting for an IAM grant that was never the
    problem. The provider simply is not in the frozen catalogue."""
    from fastapi.testclient import TestClient

    from quota_broker.main import WorkerIdentity, create_app

    client = TestClient(create_app(broker, identity=WorkerIdentity(required=False)),
                        raise_server_exceptions=False)

    response = client.get("/v1/quota/mystery-llm/eng")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert "anthropic" in response.json()["message"]


# -- the shared provider pool is not a cross-tenant lever -------------------

def test_a_tenant_with_no_quota_document_is_not_halted_by_another_tenants_429(
    broker, db
):
    """`list(provider=...)` only sees tenants that already have a DOCUMENT.

    A tenant that has never reported has none, so `all(stopped)` was evaluated
    over a subset: one tenant hitting its own 429 threshold drove the SHARED
    `provider:anthropic` pool to 0, and `pool_names_for` puts that pool in every
    tenant's admission list with `effective_limit` 0 meaning an absolute stop.
    It was self-sustaining too -- with the pool at 0 no work runs, so no success
    can arrive to clear it, and the only other way out is a platform-only sweep.
    """
    from .conftest import seed_tenant

    seed_tenant(db, "eng")
    seed_tenant(db, "research")             # registered, enabled, never reported

    for _ in range(broker.config.exhaustion_threshold):
        broker.observe_rate_limit("anthropic", "eng")

    assert db.docs["pools/provider:anthropic:tenant:eng"]["quota_derived_limit"] == 0
    assert db.docs["pools/provider:anthropic"]["quota_derived_limit"] is None, (
        "research has never touched anthropic; its work must keep flowing"
    )
    assert "pools/provider:anthropic:tenant:research" not in db.docs


def test_the_shared_pool_still_caps_when_every_enabled_tenant_is_stopped(broker, db):
    from .conftest import seed_tenant

    seed_tenant(db, "eng")
    seed_tenant(db, "research")

    for name in ("eng", "research"):
        for _ in range(broker.config.exhaustion_threshold):
            broker.observe_rate_limit("anthropic", name)

    assert db.docs["pools/provider:anthropic"]["quota_derived_limit"] == 0


def test_a_disabled_tenant_does_not_hold_the_shared_pool_open(broker, db):
    """A disabled tenant cannot be admitted anyway, so it is not evidence that
    the provider is still usable."""
    from .conftest import seed_tenant

    seed_tenant(db, "eng")
    seed_tenant(db, "archived", enabled=False)

    for _ in range(broker.config.exhaustion_threshold):
        broker.observe_rate_limit("anthropic", "eng")

    assert db.docs["pools/provider:anthropic"]["quota_derived_limit"] == 0


# -- the caller's service account is pinned to THIS project -----------------

def test_a_lookalike_service_account_in_another_project_is_not_a_tenant():
    """A service account named `swarm-agent-worker-eng` in an ATTACKER'S own
    project produces a genuine Google-signed OIDC token. With the project
    unpinned it satisfied the pattern and was authorized as tenant `eng`, able
    to report rate limits and exhaustion against them."""
    from quota_broker.main import worker_sa_pattern

    pattern = worker_sa_pattern(PROJECT)
    assert pattern.match(
        f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com"
    ).group("tenant") == "eng"
    # The other provisioning path's spelling is real too.
    assert pattern.match(
        f"swarm-t-eng@{PROJECT}.iam.gserviceaccount.com"
    ).group("tenant") == "eng"

    assert pattern.match("swarm-agent-worker-eng@evil-project.iam.gserviceaccount.com") is None
    assert pattern.match("swarm-t-eng@evil-project.iam.gserviceaccount.com") is None
    assert pattern.match(f"someone-else@{PROJECT}.iam.gserviceaccount.com") is None


def test_the_pattern_refuses_to_be_built_without_a_project():
    from quota_broker.main import worker_sa_pattern

    with pytest.raises(ValueError):
        worker_sa_pattern("")


# -- REQUIRE_OIDC is a local-development switch only ------------------------

def test_disabling_oidc_is_refused_outside_local_development():
    """With it off, `resolve` returns (None, True) for everyone: a full platform
    caller, able to set any tenant's hard max and run the sweep."""
    from quota_broker.main import WorkerIdentity

    with pytest.raises(ValueError) as exc:
        WorkerIdentity(required=False, hardened=True, project_id=PROJECT,
                       audience="https://broker.example")
    assert "REQUIRE_OIDC" in str(exc.value)

    # Still available where it is meant to be.
    assert WorkerIdentity(required=False).resolve(None) == (None, True)


def test_an_unpinned_audience_is_refused_outside_local_development():
    from quota_broker.main import WorkerIdentity

    with pytest.raises(ValueError) as exc:
        WorkerIdentity(required=True, hardened=True, project_id=PROJECT)
    assert "BROKER_AUDIENCE" in str(exc.value)

    WorkerIdentity(
        required=True, hardened=True, project_id=PROJECT, audience="https://broker.example"
    )
