"""The quota document dates its adaptive target's last change (`target_changed_at`).

The owner's follow-up of 2 November 2026 ("add-target-changed-at"). The
readers that date a lowered target (the scheduler's refusal record, swarm-api's
pool `lowered_by`) had only the last cut or the last admin override to go by,
so a target the climb had raised since was dated by the cut, and the
document's `updated_at` moves on every success. `QuotaBroker._mutate` now
writes `target_changed_at` only when the committed target differs from the
stored one -- a cut, a climb, an override, the clamp -- and carries it forward
unchanged on every other write.

Driven through the shipped `QuotaBroker` over the shared FakeFirestore with a
fake clock: no credentials, no emulator, no sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_common.models import ProviderState, QuotaState

from quota_broker.aimd import AimdConfig
from quota_broker.service import TARGET_CHANGED_AT, QuotaBroker, quota_to_firestore
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


def _broker(db: FakeFirestore, clock: _Clock) -> QuotaBroker:
    return QuotaBroker(
        db,
        settings=BrokerSettings(core=core_settings(), default_hard_max=50, aimd=AimdConfig()),
        now=clock,
    )


def _seed(db: FakeFirestore, *, target: int = 5) -> None:
    """A workspace cut to `target` at T0, hard max 50, pool 40, no
    `target_changed_at` yet: a document written before the field."""
    state = QuotaState(
        provider=PROVIDER,
        tenant_id=TENANT,
        state=ProviderState.AVAILABLE,
        updated_at=T0,
        configured_hard_max=50,
        adaptive_target=target,
        last_429_at=T0,
        rate_limit_count=0,
    )
    db.docs[DOC] = quota_to_firestore(state)
    db.docs[POOL] = {
        "name": f"provider:{PROVIDER}:tenant:{TENANT}",
        "hard_limit": 40,
        "adaptive_target": target,
        "quota_derived_limit": target,
        "active": 2,
        "enabled": True,
        "updated_at": T0,
    }


def test_a_success_that_leaves_the_target_alone_writes_no_target_changed_at(db):
    _seed(db)
    clock = _Clock()
    clock.now = T0 + timedelta(seconds=10)

    _broker(db, clock).observe_success(PROVIDER, TENANT)

    assert db.docs[DOC]["adaptive_target"] == 5
    assert db.docs[DOC]["updated_at"] == clock.now
    assert TARGET_CHANGED_AT not in db.docs[DOC]


def test_a_climb_writes_target_changed_at_and_a_later_quiet_success_keeps_it(db):
    _seed(db)
    clock = _Clock()
    broker = _broker(db, clock)
    climbed_at = T0 + INTERVAL
    clock.now = climbed_at

    broker.observe_success(PROVIDER, TENANT)

    assert db.docs[DOC]["adaptive_target"] == 6
    assert db.docs[DOC][TARGET_CHANGED_AT] == climbed_at

    clock.now = climbed_at + timedelta(seconds=30)
    broker.observe_success(PROVIDER, TENANT)

    assert db.docs[DOC]["adaptive_target"] == 6
    assert db.docs[DOC]["updated_at"] == clock.now
    assert db.docs[DOC][TARGET_CHANGED_AT] == climbed_at


def test_a_cut_dates_the_target_with_the_rate_limits_own_instant(db):
    _seed(db, target=20)
    clock = _Clock()
    clock.now = T0 + timedelta(seconds=40)

    _broker(db, clock).observe_rate_limit(PROVIDER, TENANT)

    assert db.docs[DOC]["adaptive_target"] == 10
    assert db.docs[DOC][TARGET_CHANGED_AT] == clock.now == db.docs[DOC]["last_429_at"]


def test_an_override_and_a_clamp_write_it_and_a_max_that_leaves_the_target_does_not(db):
    _seed(db, target=20)
    clock = _Clock()
    broker = _broker(db, clock)

    # A raised maximum leaves the target where it was.
    clock.now = T0 + timedelta(seconds=10)
    broker.set_hard_max(PROVIDER, TENANT, 60, requested_by="root@example.com", reason="more")
    assert db.docs[DOC]["adaptive_target"] == 20
    assert TARGET_CHANGED_AT not in db.docs[DOC]

    # A lowered one clamps it, and the clamp is a change.
    clock.now = T0 + timedelta(seconds=20)
    broker.set_hard_max(PROVIDER, TENANT, 12, requested_by="root@example.com", reason="less")
    assert db.docs[DOC]["adaptive_target"] == 12
    assert db.docs[DOC][TARGET_CHANGED_AT] == clock.now

    # An admin override is a change.
    clock.now = T0 + timedelta(seconds=30)
    broker.reset_adaptive_target(PROVIDER, TENANT, target=8, reason="lent fewer accounts",
                                 requested_by="root@example.com")
    assert db.docs[DOC]["adaptive_target"] == 8
    assert db.docs[DOC][TARGET_CHANGED_AT] == clock.now
