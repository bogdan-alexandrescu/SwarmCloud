"""A no-account park waits for the EARLIEST account, and a freed account re-asks (#1885).

Measured 2026-10-21: a claude-code step parked at `account_assign` with
`no_account_available` and `next_check_at` 46 h out -- another account's
seven-day clear. Another step was assigned an account 53 minutes later, and
the park was still waiting 2 h 48 min after that.

Two causes, both held here:

  * `eligibility()` took its minimum over accounts blocked by a WINDOW and
    nothing else. An account blocked by a reading too old to trust, or by the
    tenant's unreadable mark, had no window instant, dropped out of the
    minimum, and the answer became the far seven-day clear;
  * nothing but `next_eligible_at` ever resumed such a park. The finish event
    now re-asks the pool for the parks the finished task's account serves.

What is held here:

    eligibility returns the earliest instant across candidates blocked by different reasons
    a park never computes an instant later than the earliest window clear
    a finished task wakes the no-account park of a tenant its account serves
    ... and of a tenant it is lent to
    ... and not one the account does not serve
    ... and not one the pool still cannot serve
    ... and not a PROVIDER_QUOTA_EXHAUSTED park that is not a no-account park
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.accounts import (
    DEFAULT_STALE_AFTER,
    Account,
    AccountState,
    Unavailable,
    WindowReading,
    account_id_for,
    eligibility,
)
from quota_broker.accountstore import AccountStore
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import scheduler_settings, seed_pool, seed_task, seed_tenant

NOW = datetime(2026, 10, 21, 16, 8, tzinfo=timezone.utc)
TENANT = "eng"

WEEK = NOW + timedelta(hours=46)
FIVE = NOW + timedelta(hours=2)
#: When a mark reported ten minutes ago lapses.
MARK = NOW - timedelta(minutes=10) + DEFAULT_STALE_AFTER


def _account(label: str, *, five=0.0, week=0.0, observed=NOW, state=AccountState.AVAILABLE,
             unreadable_since: datetime | None = None) -> Account:
    return Account(
        account_id=account_id_for(TENANT, label),
        owner_tenant=TENANT,
        label=label,
        state=state,
        windows={
            "five_hour": WindowReading(five, FIVE),
            "seven_day": WindowReading(week, WEEK),
        },
        observed_at=observed,
        unreadable_by={TENANT: unreadable_since} if unreadable_since else {},
    )


#: One account per kind of block, each with the instant it could end.
ACCOUNTS = {
    # A seven-day window spent: clears in 46 h. The incident's answer.
    "week": _account("week", week=0.99),
    # A five-hour window spent: clears in 2 h.
    "five": _account("five", five=0.99),
    # 0.20 left, above the floor, but read 45 minutes ago and halved below it:
    # the broker's next usage poll, minutes away and undated.
    "quiet": _account("quiet", five=0.80, observed=NOW - timedelta(minutes=45)),
    # Room, but this tenant could not read it ten minutes ago: the mark lapses
    # in twenty.
    "marked": _account("marked", unreadable_since=NOW - timedelta(minutes=10)),
    # Marked AND spent for the week: the later of the two.
    "marked-spent": _account(
        "marked-spent", week=0.99, unreadable_since=NOW - timedelta(minutes=10)
    ),
    # A person's decision. No instant.
    "paused": _account("paused", state=AccountState.PAUSED),
}

NAA = Unavailable.NO_ACCOUNT_AVAILABLE
NRR = Unavailable.NO_RECENT_READING

#: (pool, exclude, expected reason, expected instant)
TABLE = [
    # MUTATION: a minimum over nothing, or a fixed fallback, loses the 46 h.
    (["week"], (), NAA, WEEK),
    # MUTATION: `max(resets)` instead of `min(resets)` answers WEEK.
    (["week", "five"], (), NAA, FIVE),
    # MUTATION: the pre-#1885 rule -- resets first -- answers NAA at WEEK, the
    # incident: a reading the next poll refreshes slept behind a 7-day clear.
    (["week", "quiet"], (), NRR, WEEK),
    # MUTATION: dropping unreadable-marked accounts from the minimum (the
    # pre-#1885 `mine` filter) answers WEEK.
    (["week", "marked"], (), NAA, MARK),
    # MUTATION: counting a PAUSED account's windows, or calling the pool paused.
    (["week", "paused"], (), NAA, WEEK),
    # MUTATION: inventing an instant for a person's decision.
    (["paused"], (), Unavailable.POOL_PAUSED, None),
    # MUTATION: the pre-#1885 rule answers None: no instant for a lapsing mark.
    (["marked"], (), NAA, MARK),
    # MUTATION: `min(lapse, clear)` answers MARK for an account still spent.
    (["marked-spent"], (), NAA, WEEK),
    # MUTATION: dressing the poll up as a window instant.
    (["quiet"], (), NRR, None),
    # MUTATION: any one kind of block left out of the minimum answers later.
    (["week", "five", "quiet", "marked", "paused"], (), NRR, MARK),
    # MUTATION: the pre-#1885 rule saw only the paused account and answered
    # POOL_PAUSED, waiting on a person over a mark that lapses in 20 minutes.
    (["paused", "marked"], (), NAA, MARK),
    # MUTATION: counting an account THIS attempt excluded answers FIVE; the
    # exclusion dies with the attempt and the tenant's mark is what dates it.
    (["week", "five"], (account_id_for(TENANT, "five"),), NAA, WEEK),
]


@pytest.mark.parametrize(
    "pool, exclude, reason, instant",
    TABLE,
    ids=["+".join(row[0]) + ("-excl" if row[1] else "") for row in TABLE],
)
def test_eligibility_returns_the_earliest_instant_across_candidates_blocked_by_different_reasons(
    pool, exclude, reason, instant
):
    accounts = [ACCOUNTS[label] for label in pool]

    got_reason, got_instant = eligibility(accounts, TENANT, NOW, exclude=exclude)

    assert got_reason is reason  # MUTATION: see the row's comment in TABLE.
    assert got_instant == instant  # MUTATION: see the row's comment in TABLE.


@pytest.mark.parametrize(
    "pool", [row[0] for row in TABLE if not row[1]], ids=["+".join(row[0]) for row in TABLE if not row[1]]
)
def test_a_park_never_computes_an_instant_later_than_the_earliest_window_clear(pool):
    """Whatever else blocks the pool, the answer is never later than the first
    window of an assignable candidate to clear: that account can take the
    agent then at the latest (once a mark, if any, has lapsed)."""
    accounts = [ACCOUNTS[label] for label in pool]
    clears = [
        clear
        for a in accounts
        if a.state is AccountState.AVAILABLE and not a.unreadable_by
        for clear in [a.next_reset(NOW)]
        if clear is not None
    ]

    _, instant = eligibility(accounts, TENANT, NOW)

    if clears:
        # MUTATION: `max(resets)`, or skipping any window-blocked candidate,
        # puts the instant after the earliest clear.
        assert instant is not None and instant <= min(clears)
    if instant is not None:
        # MUTATION: an instant in the past would wake a task into the same answer.
        assert instant > NOW


# --------------------------------------------------------------------------
# The finish event re-asks the pool for the parks its account serves
# --------------------------------------------------------------------------

BROKER = "https://swarm-quota-broker.example.run.app"
OWNER = "eng"
BORROWER = "u-verify"
STRANGER = "research"
FAR = datetime.now(timezone.utc) + timedelta(hours=46)


def _world(db) -> None:
    seed_pool(db, "global", hard_limit=10)
    for tenant in (OWNER, BORROWER, STRANGER):
        seed_tenant(db, tenant, credentials=("anthropic",), max_active=10)


def _finished(db, task_id: str = "task_done", *, account_id: str) -> None:
    """A task that ended, and the hold record the broker wrote for its account."""
    seed_task(db, task_id=task_id, tenant_id=OWNER, state="SUCCEEDED",
              runner_profile="claude-code", provider="anthropic")
    db.docs[f"account_holds/asg_{task_id}"] = {
        "account_id": account_id,
        "tenant_id": OWNER,
        "task_id": task_id,
        "attempt_id": "att_1",
        "end": "released",
    }


def _no_account_park(db, task_id: str, tenant_id: str, *, phase: str = "account_assign") -> None:
    profile = RUNNER_PROFILES["claude-code"]
    seed_task(
        db,
        task_id=task_id,
        tenant_id=tenant_id,
        state="PARKED",
        park_reason="PROVIDER_QUOTA_EXHAUSTED",
        runner_profile=profile.name,
        resource_class=profile.resource_class,
        provider=profile.provider,
        next_eligible_at=FAR,
    )
    db.docs[f"tasks/{task_id}"]["blocked_by"] = [
        {
            "reason": "PROVIDER_QUOTA_EXHAUSTED",
            "provider": profile.provider,
            "account_pool_reason": "no_account_available",
            "park_phase": phase,
        }
    ]


def _pool_scheduler(make_scheduler):
    return make_scheduler(settings=scheduler_settings(quota_broker_url=BROKER))


def test_a_finished_task_wakes_the_no_account_park_of_a_tenant_its_account_serves(
    db, make_scheduler, dispatcher
):
    _world(db)
    AccountStore(db).register(OWNER, "team")
    AccountStore(db).register(STRANGER, "solo")
    _finished(db, account_id=f"{OWNER}:team")
    _no_account_park(db, "task_owner", OWNER)
    _no_account_park(db, "task_stranger", STRANGER)

    report = _pool_scheduler(make_scheduler).release_dependants("task_done")

    # MUTATION: dropping `_wake_account_parks` from `release_dependants` leaves
    # it PARKED until its 46 h `next_eligible_at`.
    assert db.docs["tasks/task_owner"]["state"] != "PARKED", report.to_dict()
    assert report.promoted_account_waits == 1, report.to_dict()
    # MUTATION: skipping the `served` filter wakes the stranger too: its own
    # account would take it, but the account that came free does not serve it.
    assert db.docs["tasks/task_stranger"]["state"] == "PARKED"
    # Woken into the ordinary admission pass, which took the lease (invariant 2).
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_owner"]


def test_a_finished_task_wakes_the_park_of_a_tenant_its_account_is_lent_to(
    db, make_scheduler, dispatcher
):
    _world(db)
    seed_tenant(db, BORROWER, credentials=(), max_active=10)
    AccountStore(db).register(OWNER, "team", lend_to=[BORROWER])
    _finished(db, account_id=f"{OWNER}:team")
    _no_account_park(db, "task_borrower", BORROWER)

    _pool_scheduler(make_scheduler).release_dependants("task_done")

    # MUTATION: serving only `owner_tenant` leaves the borrower PARKED.
    assert db.docs["tasks/task_borrower"]["state"] != "PARKED"


def test_a_finished_task_does_not_wake_a_park_the_pool_still_cannot_serve(
    db, make_scheduler, dispatcher
):
    _world(db)
    AccountStore(db).register(OWNER, "team")
    AccountStore(db).record_reading(
        f"{OWNER}:team",
        {"seven_day": WindowReading(0.99, FAR)},
        observed_at=datetime.now(timezone.utc),
    )
    _finished(db, account_id=f"{OWNER}:team")
    _no_account_park(db, "task_owner", OWNER)

    report = _pool_scheduler(make_scheduler).release_dependants("task_done")

    # MUTATION: promoting without asking `choose()` starts a container that can
    # only park again, once per parked task per finish.
    assert db.docs["tasks/task_owner"]["state"] == "PARKED"
    assert report.promoted_account_waits == 0
    assert dispatcher.dispatched == []
    # Invariant 1: nothing was reserved for a task that stayed parked.
    assert db.docs["pools/global"]["active"] == 0


def test_a_finished_task_does_not_wake_a_quota_park_that_is_not_a_no_account_park(
    db, make_scheduler, dispatcher
):
    """A 429 park says the PROVIDER throttled this tenant; an account coming
    free says nothing about that."""
    _world(db)
    AccountStore(db).register(OWNER, "team")
    _finished(db, account_id=f"{OWNER}:team")
    _no_account_park(db, "task_throttled", OWNER, phase="run")

    _pool_scheduler(make_scheduler).release_dependants("task_done")

    # MUTATION: dropping the `park_phase` check in `account_park_provider`.
    assert db.docs["tasks/task_throttled"]["state"] == "PARKED"


def test_a_finish_that_held_no_account_wakes_nothing(db, make_scheduler, dispatcher):
    _world(db)
    AccountStore(db).register(OWNER, "team")
    seed_task(db, task_id="task_done", tenant_id=OWNER, state="SUCCEEDED")
    _no_account_park(db, "task_owner", OWNER)

    _pool_scheduler(make_scheduler).release_dependants("task_done")

    # MUTATION: waking every park of the finished task's tenant regardless of
    # which account, if any, it held.
    assert db.docs["tasks/task_owner"]["state"] == "PARKED"
