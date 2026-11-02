"""A throttled workspace's AIMD target climbs back on the clock, to the pool's ceiling.

THE CASE (owner, 2 November 2026). One workspace-wide refusal halves the target;
the climb back was +1 per `success_threshold` (20) consecutive clean task
finishes, so from 7 back to 40 took about 660 tasks -- days. And `_clamp` held
the target under the quota document's `configured_hard_max` (50) only, while
the console showed the pool's own `hard_limit` (40, 24 October), so halving
started from 50. The owner picked both changes:

  * CLAMP TO THE POOL'S CEILING. The ceiling is the lower of the quota
    document's maximum and the per-tenant pool's hard limit, and setting the
    maximum moves the pool's hard limit with it.
  * TIME-BASED RECOVERY. On top of the success-based climb, the target rises by
    one for every `recovery_interval_seconds` (300) without a workspace-wide
    refusal, up to the ceiling, worked out when the target is read from the
    time of the last cut or climb stored on the quota document. A refusal still
    halves at once and restarts that clock. Each climb is audited.

Driven through the shipped `QuotaBroker` over the shared FakeFirestore with a
fake clock: no credentials, no emulator, no sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_common.models import ProviderState, QuotaState

from quota_broker.aimd import AimdConfig
from quota_broker.service import QUOTA_AUDIT, QuotaBroker, quota_to_firestore
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
PROVIDER = "anthropic"
TENANT = "eng"
DOC = f"quota/{PROVIDER}:{TENANT}"
POOL = f"pools/provider:{PROVIDER}:tenant:{TENANT}"
INTERVAL = timedelta(seconds=300)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _broker(db: FakeFirestore, clock: _Clock, aimd: AimdConfig | None = None) -> QuotaBroker:
    return QuotaBroker(
        db,
        settings=BrokerSettings(
            core=core_settings(), default_hard_max=50, aimd=aimd or AimdConfig()
        ),
        now=clock,
    )


def _seed(
    db: FakeFirestore,
    *,
    target: int | None = 5,
    hard_max: int = 50,
    pool_limit: int | None = 40,
    **fields: Any,
) -> None:
    """A workspace just cut to `target` at T0, hard max 50, pool 40."""
    state = QuotaState(
        provider=PROVIDER,
        tenant_id=TENANT,
        state=ProviderState.AVAILABLE,
        updated_at=T0,
        configured_hard_max=hard_max,
        adaptive_target=target,
        last_429_at=T0,
        rate_limit_count=0,
    )
    for name, value in fields.items():
        setattr(state, name, value)
    db.docs[DOC] = quota_to_firestore(state)
    if pool_limit is not None:
        db.docs[POOL] = {
            "name": f"provider:{PROVIDER}:tenant:{TENANT}",
            "hard_limit": pool_limit,
            "adaptive_target": target,
            "quota_derived_limit": target,
            "active": 2,
            "enabled": True,
            "updated_at": T0,
        }


def _target(db: FakeFirestore) -> int:
    return int(db.docs[DOC]["adaptive_target"])


def _recoveries(db: FakeFirestore) -> list[dict[str, Any]]:
    return [
        d for p, d in db.docs.items()
        if p.startswith(f"{QUOTA_AUDIT}/") and d.get("action") == "adaptive_target_recovered"
    ]


# -- time-based recovery ----------------------------------------------------------


def test_a_target_cut_to_5_is_back_at_the_ceiling_after_35_quiet_intervals_and_not_before(db):
    _seed(db)
    clock = _Clock()
    broker = _broker(db, clock)

    # One sweep per tick, the way Cloud Scheduler drives it, one second short
    # of each interval and then on it.
    for tick in range(1, 35):
        clock.now = T0 + tick * INTERVAL - timedelta(seconds=1)
        broker.sweep()
        assert _target(db) == 5 + tick - 1, tick
        clock.now = T0 + tick * INTERVAL
        broker.sweep()
        assert _target(db) == 5 + tick, tick
    assert _target(db) == 39
    assert db.docs[POOL]["adaptive_target"] == 39

    clock.now = T0 + 35 * INTERVAL
    broker.sweep()
    assert _target(db) == 40
    assert db.docs[POOL]["adaptive_target"] == 40
    assert db.docs[POOL]["quota_derived_limit"] == 40
    # `active` is the scheduler's and is patched around.
    assert db.docs[POOL]["active"] == 2

    clock.now = T0 + timedelta(days=3)
    broker.sweep()
    assert _target(db) == 40


def test_the_climb_is_worked_out_on_read_however_late_the_read_comes(db):
    """No timer: one read 17 intervals and a bit after the cut climbs by 17."""
    _seed(db)
    clock = _Clock()
    clock.now = T0 + 17 * INTERVAL + timedelta(seconds=299)
    broker = _broker(db, clock)

    assert broker.current(PROVIDER, TENANT).adaptive_target == 22
    # The read alone writes nothing; the next write commits it.
    assert _target(db) == 5
    broker.sweep()
    assert _target(db) == 22
    # The part-interval carries: one more second is the 18th interval.
    clock.now += timedelta(seconds=1)
    broker.sweep()
    assert _target(db) == 23


def test_a_refusal_in_between_halves_at_once_and_restarts_the_count(db):
    _seed(db, target=10)
    clock = _Clock()
    broker = _broker(db, clock)

    clock.now = T0 + 2 * INTERVAL + timedelta(seconds=120)
    broker.observe_rate_limit(PROVIDER, TENANT, retry_after_seconds=0)
    # 10 + 2 quiet intervals = 12, halved = 6, at once.
    assert _target(db) == 6
    cut = clock.now

    clock.now = cut + INTERVAL - timedelta(seconds=1)
    broker.sweep()
    assert _target(db) == 6, "the 120 s before the refusal must not count"
    clock.now = cut + INTERVAL
    broker.sweep()
    assert _target(db) == 7
    clock.now = cut + 3 * INTERVAL
    broker.sweep()
    assert _target(db) == 9


def test_an_exhausted_report_halves_and_restarts_the_count_too(db):
    _seed(db, target=20)
    clock = _Clock()
    broker = _broker(db, clock)
    clock.now = T0 + 4 * INTERVAL
    broker.observe_exhausted(PROVIDER, TENANT, retry_after_seconds=0)
    assert _target(db) == 12
    clock.now += INTERVAL - timedelta(seconds=1)
    broker.sweep()
    assert _target(db) == 12


def test_the_success_based_climb_still_works(db):
    _seed(db, target=5)
    clock = _Clock()
    broker = _broker(db, clock)
    clock.now = T0 + timedelta(seconds=10)
    for _ in range(AimdConfig().success_threshold):
        broker.observe_success(PROVIDER, TENANT)
    assert _target(db) == 6
    # The success climb restarts the clock: no time climb a full interval
    # after the cut, only one after the climb.
    clock.now = T0 + INTERVAL
    broker.sweep()
    assert _target(db) == 6
    clock.now = T0 + timedelta(seconds=10) + INTERVAL
    broker.sweep()
    assert _target(db) == 7


def test_with_the_clock_off_only_successes_climb(db):
    _seed(db, target=5)
    clock = _Clock()
    broker = _broker(db, clock, AimdConfig(recovery_interval_seconds=0))
    clock.now = T0 + timedelta(days=2)
    broker.sweep()
    assert _target(db) == 5
    for _ in range(20):
        broker.observe_success(PROVIDER, TENANT)
    assert _target(db) == 6


def test_a_disabled_provider_never_climbs_on_the_clock(db):
    _seed(db, target=5, state=ProviderState.DISABLED)
    clock = _Clock()
    clock.now = T0 + timedelta(hours=5)
    _broker(db, clock).sweep()
    assert _target(db) == 5


def test_each_time_based_climb_is_recorded_in_the_quota_audit(db):
    _seed(db)
    clock = _Clock()
    broker = _broker(db, clock)
    clock.now = T0 + 3 * INTERVAL
    broker.sweep()
    clock.now = T0 + 4 * INTERVAL
    broker.sweep()

    first, second = sorted(_recoveries(db), key=lambda a: a["at"])
    assert first["from_target"] == 5 and first["to_target"] == 8
    assert first["intervals"] == 3 and first["interval_seconds"] == 300
    assert first["since"] == T0
    assert first["configured_hard_max"] == 50
    assert first["pool_hard_limit"] == 40 and first["ceiling"] == 40
    assert first["requested_by"] is None and first["caller"] is None
    assert first["provider"] == PROVIDER and first["tenant_id"] == TENANT
    assert first["at"] == T0 + 3 * INTERVAL
    assert second["from_target"] == 8 and second["to_target"] == 9

    # A sweep with nothing earned writes no record.
    clock.now = T0 + 4 * INTERVAL + timedelta(seconds=10)
    broker.sweep()
    assert len(_recoveries(db)) == 2


def test_a_climb_and_a_refusal_in_one_report_are_both_on_record(db):
    _seed(db, target=10)
    clock = _Clock()
    clock.now = T0 + 6 * INTERVAL
    _broker(db, clock).observe_rate_limit(PROVIDER, TENANT, retry_after_seconds=0)
    (climb,) = _recoveries(db)
    assert climb["from_target"] == 10 and climb["to_target"] == 16
    assert _target(db) == 8


def test_recover_over_time_never_climbs_past_the_ceiling_or_moves_the_clock_past_it():
    from quota_broker.aimd import recover_over_time

    state = QuotaState(
        provider=PROVIDER, tenant_id=TENANT, state=ProviderState.AVAILABLE,
        updated_at=T0, configured_hard_max=50, adaptive_target=38, last_429_at=T0,
    )
    raised, clock, spent = recover_over_time(
        state, clock_at=T0, now=T0 + 10 * INTERVAL, pool_hard_limit=40
    )
    assert raised.adaptive_target == 40
    assert spent == 2
    assert clock == T0 + 2 * INTERVAL


# -- the ceiling is the lower of the two --------------------------------------------


def test_halving_starts_from_the_pool_s_ceiling_not_the_documents(db):
    """24 October: pool 40, document 50, target at the top: a refusal gives 20."""
    _seed(db, target=50, last_429_at=None)
    clock = _Clock()
    _broker(db, clock).observe_rate_limit(PROVIDER, TENANT, retry_after_seconds=0)
    assert _target(db) == 20


@pytest.mark.parametrize(
    ("hard_max", "pool_limit", "ceiling"), [(50, 40, 40), (30, 60, 30), (50, None, 50)]
)
def test_the_target_never_exceeds_the_lower_of_the_two_ceilings(db, hard_max, pool_limit, ceiling):
    _seed(db, target=5, hard_max=hard_max, pool_limit=pool_limit)
    clock = _Clock()
    broker = _broker(db, clock)
    for hours in (1, 3, 9, 30):
        clock.now = T0 + timedelta(hours=hours)
        broker.sweep()
        for _ in range(25):
            broker.observe_success(PROVIDER, TENANT)
        assert _target(db) <= ceiling
        assert broker.current(PROVIDER, TENANT).adaptive_target <= ceiling
    assert _target(db) == ceiling


def test_an_admin_reset_above_the_pool_is_clamped_to_the_pool(db):
    _seed(db, target=3)
    result = _broker(db, _Clock()).reset_adaptive_target(
        PROVIDER, TENANT, target=45, reason="lent two more accounts", requested_by="root@example.com"
    )
    assert result.state.adaptive_target == 40
    assert result.audit["clamped"] is True


def test_setting_the_hard_max_moves_the_pool_s_hard_limit_with_it(db):
    _seed(db, target=30)
    clock = _Clock()
    broker = _broker(db, clock)

    broker.set_hard_max(PROVIDER, TENANT, 25, requested_by="root@example.com", reason="less")
    assert db.docs[DOC]["configured_hard_max"] == 25
    assert db.docs[POOL]["hard_limit"] == 25
    assert _target(db) == 25
    (audit,) = [
        d for p, d in db.docs.items()
        if p.startswith(f"{QUOTA_AUDIT}/") and d.get("action") == "hard_max_set"
    ]
    assert audit["from_pool_hard_limit"] == 40 and audit["to_pool_hard_limit"] == 25

    broker.set_hard_max(PROVIDER, TENANT, 45)
    assert db.docs[POOL]["hard_limit"] == 45
    assert db.docs[POOL]["active"] == 2


def test_setting_the_hard_max_creates_the_pool_at_that_limit(db):
    _seed(db, target=30, pool_limit=None)
    _broker(db, _Clock()).set_hard_max(PROVIDER, TENANT, 35)
    assert db.docs[POOL]["hard_limit"] == 35


# -- the setting ------------------------------------------------------------------------


def test_the_recovery_interval_is_a_setting_with_a_300_second_default(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "swarm-test")
    monkeypatch.delenv("AIMD_RECOVERY_INTERVAL_SECONDS", raising=False)
    assert BrokerSettings.from_env().aimd.recovery_interval_seconds == 300
    monkeypatch.setenv("AIMD_RECOVERY_INTERVAL_SECONDS", "120")
    assert BrokerSettings.from_env().aimd.recovery_interval_seconds == 120
    with pytest.raises(ValueError):
        AimdConfig(recovery_interval_seconds=-1)
