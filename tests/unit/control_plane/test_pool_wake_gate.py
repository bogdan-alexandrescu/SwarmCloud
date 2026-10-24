"""Work waiting on the account pool wakes only as fast as accounts can take it (#2222, POOL-WAKE-GATE).

BGD, 2026-10-24: "Per-account cap, staggered wake" and "Ask the pool before a
container". Measured that morning: at 10:10Z one 5-hour reset was the
`next_eligible_at` of 28 parks, and each promotion started a container that
cloned, checkpointed about 49 MB and asked the broker, to be told there was no
account and park again. What is held here:

  * a finish wakes at most as many parks as there are free slots;
  * admission asks the broker's `choose()` BEFORE the lease and parks what no
    account can take, PROVIDER_QUOTA_EXHAUSTED at `account_assign`, holding
    nothing (invariant 1);
  * within one run, every slot admission or a wake hands out is counted, so a
    pool with two free slots admits two;
  * the prewarm sweep leaves a due no-account park parked while no account
    can take it, and promotes one whose tenant no account serves any more;
  * the waiting line says the pool, not the provider, holds the task.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quota_broker.accounts import Account, WindowReading, account_id_for
from quota_broker.accountstore import AccountStore
from quota_broker.main import acquire_hold
from scheduler.credentials import AccountPool
from scheduler.loop import POOL_RECHECK_SECONDS
from swarm_api.waiting_detail import build_waiting
from swarm_common.models import Task
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import ParkReason, TaskState

from .conftest import scheduler_settings, seed_pool, seed_task, seed_tenant

BROKER = "https://swarm-quota-broker.example.run.app"
OWNER = "eng"
TEAM = f"{OWNER}:team"
PROFILE = RUNNER_PROFILES["claude-code"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _world(db, *, accounts: bool = True, max_agents: int | None = None) -> AccountStore:
    seed_pool(db, "global", hard_limit=20)
    seed_tenant(db, OWNER, credentials=("anthropic",), max_active=20)
    store = AccountStore(db)
    if accounts:
        store.register(OWNER, "team")
        if max_agents is not None:
            store.set_max_agents(TEAM, max_agents)
    return store


def _ready(db, task_id: str) -> None:
    seed_task(db, task_id=task_id, tenant_id=OWNER, runner_profile=PROFILE.name,
              resource_class=PROFILE.resource_class, provider=PROFILE.provider)


def _no_account_park(db, task_id: str, *, eligible_at: datetime) -> None:
    seed_task(db, task_id=task_id, tenant_id=OWNER, state="PARKED",
              park_reason="PROVIDER_QUOTA_EXHAUSTED", runner_profile=PROFILE.name,
              resource_class=PROFILE.resource_class, provider=PROFILE.provider,
              next_eligible_at=eligible_at)
    db.docs[f"tasks/{task_id}"]["blocked_by"] = [{
        "reason": "PROVIDER_QUOTA_EXHAUSTED", "provider": PROFILE.provider,
        "account_pool_reason": "no_account_available", "park_phase": "account_assign",
    }]


def _finished(db, task_id: str = "task_done") -> None:
    seed_task(db, task_id=task_id, tenant_id=OWNER, state="SUCCEEDED",
              runner_profile=PROFILE.name, provider=PROFILE.provider)
    db.docs[f"account_holds/asg_{task_id}"] = {
        "account_id": TEAM, "tenant_id": OWNER, "task_id": task_id,
        "attempt_id": "att_1", "end": "released",
    }


def _scheduler(make_scheduler):
    return make_scheduler(settings=scheduler_settings(quota_broker_url=BROKER))


def _state(db, task_id: str) -> tuple[str, str | None]:
    doc = db.docs[f"tasks/{task_id}"]
    return doc["state"], doc.get("park_reason")


# --------------------------------------------------------------------------
# The run's claims
# --------------------------------------------------------------------------


def test_a_claim_shows_as_a_hold_until_released_or_forgotten():
    now = _now()
    team = Account(account_id=TEAM, owner_tenant=OWNER, label="team")
    pool = AccountPool(broker_url=BROKER, read_accounts=lambda: [team])
    pool.claim(TEAM, task_id="t1", tenant_id=OWNER, now=now)
    pool.claim(TEAM, task_id="t2", tenant_id=OWNER, now=now)
    # One claim per task: claiming t1 again moves it, it does not add a third.
    pool.claim(TEAM, task_id="t1", tenant_id=OWNER, now=now)
    seen = pool.accounts()[0]
    assert len(seen.live_holds(now)) == 2 and seen.assigned == 2
    pool.release_claim("t1")
    assert len(pool.accounts()[0].live_holds(now)) == 1
    pool.forget()
    assert pool.accounts()[0].live_holds(now) == ()


# --------------------------------------------------------------------------
# A finish wakes at most the free slots
# --------------------------------------------------------------------------


def test_a_finish_wakes_no_more_parks_than_the_account_has_free_slots(
    db, make_scheduler, dispatcher
):
    _world(db, max_agents=2)
    _finished(db)
    far = _now() + timedelta(hours=46)
    for i in range(5):
        _no_account_park(db, f"task_p{i}", eligible_at=far)

    report = _scheduler(make_scheduler).release_dependants("task_done")

    # MUTATION: without the claim after each promotion, one freed slot
    # answers yes for all five and all five start a container.
    assert report.promoted_account_waits == 2, report.to_dict()
    still = [i for i in range(5) if _state(db, f"task_p{i}") == ("PARKED", "PROVIDER_QUOTA_EXHAUSTED")]
    assert len(still) == 3
    assert len(dispatcher.dispatched) == 2


# --------------------------------------------------------------------------
# Admission asks the pool before the lease
# --------------------------------------------------------------------------


def test_admission_parks_a_task_no_account_can_take_before_any_lease(
    db, make_scheduler, dispatcher
):
    store = _world(db)
    clears = _now() + timedelta(hours=40)
    store.record_reading(TEAM, {"seven_day": WindowReading(0.99, clears)}, observed_at=_now())
    _ready(db, "task_r")

    report = _scheduler(make_scheduler).drain()

    # MUTATION: dropping the pre-lease question leases it, and the worker
    # starts a container only to park on the same answer.
    assert _state(db, "task_r") == ("PARKED", "PROVIDER_QUOTA_EXHAUSTED")
    assert dispatcher.dispatched == []
    assert db.docs["pools/global"]["active"] == 0, "invariant 1: a park holds nothing"
    assert report.held_by_pool == 1
    block = db.docs["tasks/task_r"]["blocked_by"][0]
    # The worker's own park shape, so the finish wake and prewarm treat it as theirs.
    assert block["park_phase"] == "account_assign"
    assert block["account_pool_reason"] == "no_account_available"
    assert block["provider"] == "anthropic"
    assert block["accounts"] == [{"account": "team", "windows": {
        "seven_day": {"used": 0.99, "resets_at": clears.isoformat()}}}]
    assert db.docs["tasks/task_r"]["next_eligible_at"] == clears


def test_admission_counts_the_slots_it_already_handed_out_this_run(
    db, make_scheduler, dispatcher
):
    _world(db, max_agents=2)
    for i in range(3):
        _ready(db, f"task_r{i}")

    report = _scheduler(make_scheduler).drain()

    # MUTATION: without `claim` after each lease, all three are leased onto a
    # two-agent account and the broker turns the third away after its container.
    assert len(dispatcher.dispatched) == 2, report.to_dict()
    parked = [i for i in range(3) if _state(db, f"task_r{i}")[0] == "PARKED"]
    assert len(parked) == 1
    doc = db.docs[f"tasks/task_r{parked[0]}"]
    assert doc["blocked_by"][0]["account_pool_reason"] == "accounts_full"
    assert doc["blocked_by"][0]["accounts"] == [{"account": "team", "full": "2 of 2"}]
    # A slot frees when an agent finishes: minutes, not the fifteen-minute fallback.
    assert doc["next_eligible_at"] <= _now() + timedelta(seconds=POOL_RECHECK_SECONDS + 5)


def test_a_tenant_no_account_serves_is_admitted_as_before(db, make_scheduler, dispatcher):
    _world(db, accounts=False)
    _ready(db, "task_key")
    _scheduler(make_scheduler).drain()
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_key"]


# --------------------------------------------------------------------------
# The prewarm sweep
# --------------------------------------------------------------------------


def test_prewarm_leaves_a_due_no_account_park_parked_while_no_account_can_take_it(
    db, make_scheduler, dispatcher
):
    _world(db, max_agents=1)
    assert acquire_hold(db, TEAM, tenant_id=OWNER, now=_now()) is not None
    _no_account_park(db, "task_due", eligible_at=_now() - timedelta(minutes=1))

    report = _scheduler(make_scheduler).drain()

    # MUTATION: promoting on the instant alone starts a container that parks again.
    assert _state(db, "task_due") == ("PARKED", "PROVIDER_QUOTA_EXHAUSTED")
    assert report.promoted_prewarm == 0
    assert report.held_by_pool == 1
    assert dispatcher.dispatched == []


def test_prewarm_promotes_due_parks_up_to_the_free_slots(db, make_scheduler, dispatcher):
    _world(db, max_agents=1)
    _no_account_park(db, "task_a", eligible_at=_now() - timedelta(minutes=2))
    _no_account_park(db, "task_b", eligible_at=_now() - timedelta(minutes=1))

    report = _scheduler(make_scheduler).drain()

    assert report.promoted_prewarm == 1, report.to_dict()
    # MUTATION: the promoted task counted against itself at admission (no
    # `release_claim`) parks again instead of running.
    assert len(dispatcher.dispatched) == 1
    states = sorted(_state(db, t)[0] for t in ("task_a", "task_b"))
    assert states == ["DISPATCHED", "PARKED"]


def test_prewarm_promotes_a_due_park_whose_tenant_no_account_serves_any_more(
    db, make_scheduler, dispatcher
):
    """Its worker falls back to the tenant's own key: nothing in the pool can hold it."""
    _world(db, accounts=False)
    _no_account_park(db, "task_due", eligible_at=_now() - timedelta(minutes=1))

    report = _scheduler(make_scheduler).drain()

    assert report.promoted_prewarm == 1
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_due"]


# --------------------------------------------------------------------------
# The waiting line
# --------------------------------------------------------------------------


def test_the_waiting_line_names_the_pool_and_what_holds_each_account():
    read_at = datetime(2026, 10, 24, 9, 4, tzinfo=timezone.utc)
    block = {
        "reason": "PROVIDER_QUOTA_EXHAUSTED", "provider": "anthropic",
        "account_pool_reason": "no_account_available", "park_phase": "account_assign",
        "accounts": [
            {"account": "ai-eng-2", "windows": {"seven_day": {
                "used": 0.86, "resets_at": "2026-10-25T18:59:00+00:00"}}},
            {"account": "aieng-claude1", "windows": {"five_hour": {
                "used": 0.99, "resets_at": "2026-10-24T10:10:00+00:00"}}},
            {"account": "saga-team", "full": "6 of 6"},
            {"account": "devops-main", "state": "PAUSED"},
        ],
    }
    task = Task(
        id="task_w", tenant_id=OWNER, created_at=read_at - timedelta(hours=1),
        updated_at=read_at, state=TaskState.PARKED, runner_profile="claude-code",
        resource_class="standard", input={}, submitted_by="alice@example.com",
        park_reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
        next_eligible_at=read_at + timedelta(minutes=5), blocked_by=[block],
    )

    w = build_waiting(task, events=None, as_of=read_at)

    # MUTATION: falling through to the provider words says "Provider quota
    # exhausted", which on 2026-10-24 sent readers to the wrong place.
    assert w["summary"] == (
        "No pool account can take an agent on anthropic: every account is under its floor "
        "(ai-eng-2 7-day at 86%, clears 2026-10-25 18:59Z; aieng-claude1 5-hour at 99%, "
        "clears 10:10Z; saga-team full (6 of 6); and 1 more); next check 09:09Z"
    )
    assert [a["account"] for a in w["detail"]["accounts"]] == [
        "ai-eng-2", "aieng-claude1", "saga-team", "devops-main"]
