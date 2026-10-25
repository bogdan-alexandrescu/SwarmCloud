"""The pool's waiting line names every reason an account cannot take the agent (#2222, POOL-WAITLINE).

BGD, 2026-10-24, POOL-DUP-DETECT question 3: "Yes, follow-up lane". After
PR 2247 the broker withholds the later registration of one subscription
(`duplicate_withheld`) and gives a never-measured account one agent until its
first reading, but the waiting line on Work › Agents › Waiting named only a
window, a state or "full", so an account the broker would never hand out read
as nothing at all. What is held here:

  * a withheld duplicate is named as withheld, with the other side's label
    only when that account is in the waiting agent's own workspace;
  * a never-measured account at its one agent is named as waiting for its
    first reading;
  * a possible duplicate (identical readings) is named as such and is never
    called withheld, because `choose()` still hands it out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quota_broker.accounts import WindowReading
from quota_broker.accountstore import AccountStore
from quota_broker.main import acquire_hold
from swarm_api.waiting_detail import build_waiting
from swarm_common.models import Task
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import ParkReason, TaskState

from .conftest import measure, scheduler_settings, seed_pool, seed_task, seed_tenant

BROKER = "https://swarm-quota-broker.example.run.app"
OWNER = "eng"
OTHER = "ops"
PROFILE = RUNNER_PROFILES["claude-code"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _world(db) -> AccountStore:
    seed_pool(db, "global", hard_limit=20)
    seed_tenant(db, OWNER, credentials=("anthropic",), max_active=20)
    return AccountStore(db)


def _ready(db, task_id: str = "task_r") -> None:
    seed_task(db, task_id=task_id, tenant_id=OWNER, runner_profile=PROFILE.name,
              resource_class=PROFILE.resource_class, provider=PROFILE.provider)


def _park(db, make_scheduler, task_id: str = "task_r") -> dict:
    make_scheduler(settings=scheduler_settings(quota_broker_url=BROKER)).drain()
    doc = db.docs[f"tasks/{task_id}"]
    assert (doc["state"], doc.get("park_reason")) == ("PARKED", "PROVIDER_QUOTA_EXHAUSTED")
    return doc["blocked_by"][0]


def _line(block: dict, *, read_at: datetime | None = None) -> str:
    read_at = read_at or datetime(2026, 10, 24, 9, 4, tzinfo=timezone.utc)
    task = Task(
        id="task_w", tenant_id=OWNER, created_at=read_at - timedelta(hours=1),
        updated_at=read_at, state=TaskState.PARKED, runner_profile="claude-code",
        resource_class="standard", input={}, submitted_by="alice@example.com",
        park_reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
        next_eligible_at=read_at + timedelta(minutes=5), blocked_by=[block],
    )
    return build_waiting(task, events=None, as_of=read_at)["summary"]


# --------------------------------------------------------------------------
# What the scheduler's park records
# --------------------------------------------------------------------------


def test_a_wait_whose_only_free_looking_account_is_withheld_names_it_as_withheld(
    db, make_scheduler, dispatcher
):
    store = _world(db)
    store.register(OWNER, "team")
    store.register(OWNER, "team-again")
    clears = _now() + timedelta(hours=40)
    store.record_reading(f"{OWNER}:team", {"seven_day": WindowReading(0.99, clears)},
                         observed_at=_now())
    measure(store, f"{OWNER}:team-again")  # plenty of room: it looks free
    store.set_duplicate(f"{OWNER}:team", f"{OWNER}:team-again", withheld=False)
    store.set_duplicate(f"{OWNER}:team-again", f"{OWNER}:team", withheld=True)
    _ready(db)

    block = _park(db, make_scheduler)

    assert dispatcher.dispatched == []
    by_label = {a["account"]: a for a in block["accounts"]}
    # MUTATION: dropping the withheld mark leaves team-again with nothing
    # named against it, and the line reads as if it could take the agent.
    assert by_label["team-again"] == {
        "account": "team-again", "withheld": {"same_subscription_as": "team"}}
    assert "withheld" not in by_label["team"], "the kept side is not withheld"
    assert "team-again withheld: same subscription as team" in _line(block)


def test_another_workspaces_duplicate_is_named_without_its_label(
    db, make_scheduler, dispatcher
):
    store = _world(db)
    store.register(OWNER, "team")
    store.register(OTHER, "night-shift")  # not lent to eng: eng never sees its label
    measure(store, f"{OWNER}:team")
    store.set_duplicate(f"{OWNER}:team", f"{OTHER}:night-shift", withheld=True)
    _ready(db)

    block = _park(db, make_scheduler)

    assert dispatcher.dispatched == []
    assert block["account_pool_reason"] == "pool_paused"
    # MUTATION: writing the other side's label puts another tenant's account
    # name into eng's task document (BGD, Q2: "only within the same tenant").
    assert block["accounts"] == [
        {"account": "team", "withheld": {"same_subscription_as": None}}]
    assert "night-shift" not in repr(block)
    line = _line(block)
    assert "night-shift" not in line
    assert line.startswith(
        "No pool account can take an agent on anthropic: every account is paused, draining, "
        "needs a sign-in or is withheld as a duplicate (team withheld: same subscription as "
        "an account in another workspace)"
    )


def test_a_never_measured_account_at_one_agent_is_named_as_waiting_for_its_first_reading(
    db, make_scheduler, dispatcher
):
    store = _world(db)
    store.register(OWNER, "team")  # never measured
    assert acquire_hold(db, f"{OWNER}:team", tenant_id=OWNER, now=_now()) is not None
    _ready(db)

    block = _park(db, make_scheduler)

    assert dispatcher.dispatched == []
    assert block["account_pool_reason"] == "no_recent_reading"
    # MUTATION: without the first-reading fact the account is named bare --
    # max_agents is 6 and it holds 1, so it is not "full" either.
    assert block["accounts"] == [{"account": "team", "first_reading": "pending"}]
    assert "(team taking one agent until its first reading)" in _line(block)


def test_a_possible_duplicate_is_named_as_such_and_not_called_withheld(
    db, make_scheduler, dispatcher
):
    store = _world(db)
    store.register(OWNER, "team")
    measure(store, f"{OWNER}:team")
    store.set_max_agents(f"{OWNER}:team", 1)
    assert acquire_hold(db, f"{OWNER}:team", tenant_id=OWNER, now=_now()) is not None
    store.set_identical_readings(f"{OWNER}:team", None, f"{OWNER}:saga-personal")
    _ready(db)

    block = _park(db, make_scheduler)

    assert dispatcher.dispatched == []
    # Full is what holds it; the inferred pair rides along, never as withheld.
    assert block["accounts"] == [{
        "account": "team", "full": "1 of 1",
        "possible_duplicate": {"of": "saga-personal"}}]
    line = _line(block)
    assert "team full (1 of 1); possible duplicate of saga-personal (identical readings)" in line
    assert "withheld" not in line


# --------------------------------------------------------------------------
# The words, from a recorded park
# --------------------------------------------------------------------------


def test_the_line_keeps_its_shape_with_the_new_reasons_in_it():
    block = {
        "reason": "PROVIDER_QUOTA_EXHAUSTED", "provider": "anthropic",
        "account_pool_reason": "no_recent_reading", "park_phase": "account_assign",
        "accounts": [
            {"account": "saga-team", "first_reading": "pending"},
            {"account": "team", "withheld": {"same_subscription_as": "saga-personal"},
             "windows": {"seven_day": {"used": 0.98, "resets_at": "2026-10-26T08:00:00+00:00"}}},
            {"account": "devops-main", "possible_duplicate": {"of": None}},
            {"account": "ai-eng-2", "full": "6 of 6"},
        ],
    }

    # MUTATION: a reason added outside `_pool_account_words` would change
    # the line's order or its "and N more" cut.
    assert _line(block) == (
        "No pool account can take an agent on anthropic: no account has a reading recent "
        "enough to trust (saga-team taking one agent until its first reading; team withheld: "
        "same subscription as saga-personal; 7-day at 98%, clears 2026-10-26 08:00Z; "
        "devops-main possible duplicate of an account in another workspace (identical "
        "readings); and 1 more); next check 09:09Z"
    )


def test_pool_paused_keeps_its_words_when_no_account_is_withheld():
    block = {
        "reason": "PROVIDER_QUOTA_EXHAUSTED", "provider": "anthropic",
        "account_pool_reason": "pool_paused", "park_phase": "account_assign",
        "accounts": [{"account": "team", "state": "PAUSED"}],
    }

    assert _line(block).startswith(
        "No pool account can take an agent on anthropic: every account is paused, draining "
        "or needs a sign-in (team paused)"
    )
