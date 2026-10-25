"""An account holds at most `max_agents` agents at once (#2222, POOL-ACCOUNT-CAP).

BGD, 2026-10-24, "Per-account cap, staggered wake". `choose()` hands a new
agent the account with the most headroom, and headroom does not move until
the next reading, so with nothing capping it every new agent went to the same
account: at 10:38Z that day four of eng's six agents went to aieng-claude1 the
moment its 5-hour window reset. So:

  * AN ACCOUNT HOLDING ITS `max_agents` LIVE HOLDS IS NO CANDIDATE, and the
    next account is chosen. Expired holds do not count.
  * THE CAP IS RE-CHECKED WHERE THE HOLD COMMITS, so two assigns racing for an
    account's last slot cannot both take it.
  * A POOL WHOSE ACCOUNTS ARE ALL FULL ANSWERS `accounts_full`: a wait of
    minutes, not a window clear.
  * THE LISTING SAYS, PER ACCOUNT, whether it can take a new agent and what
    stops it -- the morning's listing said "7 available" with every account
    refusing.
  * The default is 6; the owner may set 1 to 50.

Unit tests: the fake Firestore, no credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import measure

from quota_broker.accounts import (
    DEFAULT_HOLD_TTL,
    DEFAULT_MAX_AGENTS,
    Account,
    AccountState,
    Hold,
    Unavailable,
    WindowReading,
    account_id_for,
    choose,
    eligibility,
    validate_max_agents,
)
from quota_broker.accountstore import AccountStore
from quota_broker.main import acquire_hold, account_to_api

ENG = "eng"
RESEARCH = "research"
NOW = datetime(2026, 10, 24, 10, 38, tzinfo=timezone.utc)


def _holds(n: int, *, at: datetime = NOW, ttl: timedelta = DEFAULT_HOLD_TTL) -> tuple[Hold, ...]:
    return tuple(
        Hold(assignment_id=f"a{i}", tenant_id=ENG, expires_at=at + ttl, assigned_at=at)
        for i in range(n)
    )


def _acct(label: str, *, five_hour=0.05, seven_day=0.40, holds=(), max_agents=DEFAULT_MAX_AGENTS,
          five_resets_in=timedelta(hours=3), week_resets_in=timedelta(days=4),
          state=AccountState.AVAILABLE, observed=NOW) -> Account:
    return Account(
        account_id=account_id_for(ENG, label),
        owner_tenant=ENG,
        label=label,
        state=state,
        windows={
            "five_hour": WindowReading(five_hour, NOW + five_resets_in),
            "seven_day": WindowReading(seven_day, NOW + week_resets_in),
        },
        observed_at=observed,
        holds=holds,
        assigned=len(holds),
        max_agents=max_agents,
    )


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


def test_the_default_cap_is_six_and_a_document_without_one_reads_it():
    # MUTATION: a default of 0 or None refuses or uncaps every account written before this.
    assert DEFAULT_MAX_AGENTS == 6
    stored = _acct("one", max_agents=3).to_firestore()
    assert stored["max_agents"] == 3
    assert Account.from_firestore(stored).max_agents == 3
    old = dict(stored)
    del old["max_agents"]
    assert Account.from_firestore(old).max_agents == 6


@pytest.mark.parametrize("bad", [0, -1, 51, "6", 2.5, True, None])
def test_a_cap_outside_one_to_fifty_is_refused(bad):
    from quota_broker.accounts import AccountError

    with pytest.raises(AccountError):
        validate_max_agents(bad)
    assert validate_max_agents(1) == 1 and validate_max_agents(50) == 50


def test_a_malformed_stored_cap_takes_the_account_out_of_the_pool(broker):
    """A cap nobody chose must not be read as some other number."""
    store = AccountStore(broker.db)
    store.register(ENG, "bad")
    broker.db.document(f"accounts/{ENG}:bad").update({"max_agents": "six"})
    assert store.list() == []


# --------------------------------------------------------------------------
# choose() and eligibility()
# --------------------------------------------------------------------------


def test_a_full_account_is_skipped_and_the_next_one_is_chosen():
    # `aieng-claude1` has the most headroom, so only the cap can skip it.
    claude1 = _acct("aieng-claude1", five_hour=0.0, seven_day=0.30, holds=_holds(6))
    team = _acct("saga-team", five_hour=0.05, seven_day=0.37)
    assert claude1.headroom(NOW) > team.headroom(NOW)
    assert claude1.is_full(NOW)
    # MUTATION: dropping `not a.is_full(now)` from choose() hands out the seventh slot.
    assert choose([claude1, team], ENG, NOW).account_id == team.account_id


def test_one_short_of_the_cap_is_still_offered():
    claude1 = _acct("aieng-claude1", five_hour=0.0, seven_day=0.30, holds=_holds(5))
    team = _acct("saga-team", five_hour=0.05)
    assert choose([claude1, team], ENG, NOW).account_id == claude1.account_id


def test_expired_holds_do_not_count_against_the_cap():
    """A killed worker never releases; its hold expiring is what frees the slot."""
    stale_holds = _holds(6, at=NOW - DEFAULT_HOLD_TTL - timedelta(minutes=1))
    claude1 = _acct("aieng-claude1", five_hour=0.0, holds=stale_holds)
    assert not claude1.is_full(NOW)
    assert choose([claude1], ENG, NOW).account_id == claude1.account_id


def test_an_owner_cap_below_six_is_applied():
    small = _acct("small", five_hour=0.0, holds=_holds(2), max_agents=2)
    other = _acct("other", five_hour=0.5)
    assert small.headroom(NOW) > other.headroom(NOW)
    assert choose([small, other], ENG, NOW).account_id == other.account_id


def test_a_pool_whose_accounts_with_room_are_all_full_answers_accounts_full():
    pool = [_acct("a", holds=_holds(6)), _acct("b", holds=_holds(6))]
    assert choose(pool, ENG, NOW) is None
    # MUTATION: without the full branch the answer is no_account_available
    # with no instant, which the worker parks for fifteen minutes.
    assert eligibility(pool, ENG, NOW) == (Unavailable.ACCOUNTS_FULL, None)


def test_accounts_full_carries_a_window_that_clears_sooner():
    full = _acct("full", holds=_holds(6))
    spent = _acct("spent", seven_day=0.98, week_resets_in=timedelta(hours=2))
    reason, instant = eligibility([full, spent], ENG, NOW)
    assert reason is Unavailable.ACCOUNTS_FULL
    assert instant == NOW + timedelta(hours=2)


def test_a_pool_spent_by_its_windows_alone_is_still_no_account_available():
    spent = _acct("spent", seven_day=0.98, week_resets_in=timedelta(hours=40))
    assert eligibility([spent], ENG, NOW) == (
        Unavailable.NO_ACCOUNT_AVAILABLE, NOW + timedelta(hours=40))


# --------------------------------------------------------------------------
# The hold transaction
# --------------------------------------------------------------------------


def test_the_hold_transaction_refuses_the_slot_a_racing_assign_took(broker):
    """`choose()` read a listing with one free slot; another assign took it
    before this transaction committed. The transaction counts again."""
    store = AccountStore(broker.db)
    store.register(ENG, "one")
    store.set_max_agents(f"{ENG}:one", 1)
    first = acquire_hold(broker.db, f"{ENG}:one", tenant_id=ENG, now=NOW)
    assert first is not None and first[1] == 1
    # MUTATION: without `_raw_has_room` this commits a second hold.
    assert acquire_hold(broker.db, f"{ENG}:one", tenant_id=ENG, now=NOW) is None
    assert len(store.get(f"{ENG}:one").live_holds(NOW)) == 1


# --------------------------------------------------------------------------
# The listing
# --------------------------------------------------------------------------


def test_the_listing_says_per_account_whether_it_can_take_an_agent_and_why_not():
    weekly = _acct("devops-main", seven_day=0.98, week_resets_in=timedelta(days=2))
    five = _acct("aieng-claude1", five_hour=0.99, seven_day=0.40,
                 five_resets_in=timedelta(minutes=45))
    full = _acct("saga-team", holds=_holds(6))
    open_ = _acct("ai-eng-2", five_hour=0.04, seven_day=0.86)

    w = account_to_api(weekly, now=NOW)
    assert w["can_take_new_agent"] is False
    assert w["blocked_by"] == [{
        "window": "seven_day", "used": 0.98, "floor": 0.05,
        "resets_at": (NOW + timedelta(days=2)).isoformat(), "aged_reading": False,
    }]
    assert w["next_eligible_at"] == (NOW + timedelta(days=2)).isoformat()

    f = account_to_api(five, now=NOW)
    assert f["can_take_new_agent"] is False
    assert [b["window"] for b in f["blocked_by"]] == ["five_hour"]
    assert f["blocked_by"][0]["floor"] == 0.15
    assert f["next_eligible_at"] == (NOW + timedelta(minutes=45)).isoformat()

    c = account_to_api(full, now=NOW)
    assert c["can_take_new_agent"] is False
    assert c["blocked_by"] == [{"cap": 6, "max_agents": 6}]
    # A full account frees when an agent finishes, which no clock says.
    assert c["next_eligible_at"] is None
    assert c["max_agents"] == 6

    o = account_to_api(open_, now=NOW)
    assert o["can_take_new_agent"] is True
    assert o["blocked_by"] == [] and o["next_eligible_at"] is None


def test_a_paused_account_with_room_cannot_take_an_agent():
    paused = _acct("paused", state=AccountState.PAUSED)
    body = account_to_api(paused, now=NOW)
    assert body["blocked_by"] == []
    assert body["can_take_new_agent"] is False


def test_an_aged_reading_says_its_window_blocks_only_at_half():
    aged = _acct("aged", seven_day=0.88, observed=NOW - timedelta(hours=2))
    body = account_to_api(aged, now=NOW)
    # 12% left, counted as 6%, is above the 5% floor: not blocked.
    assert body["can_take_new_agent"] is True
    older = _acct("older", seven_day=0.92, observed=NOW - timedelta(hours=2))
    blocked = account_to_api(older, now=NOW)["blocked_by"]
    assert blocked and blocked[0]["aged_reading"] is True


# --------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------


class _Identity:
    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)


@pytest.fixture()
def accounts(broker) -> AccountStore:
    return AccountStore(broker.db)


@pytest.fixture()
def client(broker, accounts, monkeypatch):
    from fastapi.testclient import TestClient

    from quota_broker.main import create_app

    monkeypatch.delenv("PROJECT_ID", raising=False)
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    return c


def _cap(client, account_id: str, value):
    return client.put(f"/v1/accounts/{account_id}/max-agents", json={"max_agents": value})


def _assign(client, n: int):
    return client.post(
        "/v1/accounts/assign",
        json={"provider": "anthropic", "task_id": f"task_{n}", "attempt_id": f"att_{n}"},
    )


def test_the_owner_sets_the_cap_and_the_listing_shows_it(client, accounts):
    accounts.register(ENG, "team")
    r = _cap(client, f"{ENG}:team", 2)
    assert r.status_code == 200, r.text
    assert r.json()["account"]["max_agents"] == 2
    assert accounts.get(f"{ENG}:team").max_agents == 2
    listed = client.get("/v1/accounts", params={"tenant_id": ENG}).json()["accounts"]
    assert listed[0]["max_agents"] == 2 and listed[0]["can_take_new_agent"] is True


@pytest.mark.parametrize("value", [0, 51, "six"])
def test_a_cap_the_route_cannot_accept_is_refused(client, accounts, value):
    accounts.register(ENG, "team")
    assert _cap(client, f"{ENG}:team", value).status_code == 422
    assert accounts.get(f"{ENG}:team").max_agents == 6


def test_only_the_owner_may_set_the_cap(client, accounts):
    accounts.register(RESEARCH, "shared", lend_to=[ENG])
    r = _cap(client, f"{RESEARCH}:shared", 50)
    assert r.status_code in (401, 403), r.text
    assert accounts.get(f"{RESEARCH}:shared").max_agents == 6


def test_re_registering_keeps_the_owners_cap(client, accounts):
    accounts.register(ENG, "team")
    _cap(client, f"{ENG}:team", 3)
    accounts.register(ENG, "team", lend_to=["smoke"])
    assert accounts.get(f"{ENG}:team").max_agents == 3


def test_the_assign_past_the_cap_parks_on_accounts_full(client, accounts, broker):
    accounts.register(ENG, "team")
    measure(accounts, f"{ENG}:team")  # one agent until measured (#2222)
    _cap(client, f"{ENG}:team", 2)
    taken = [_assign(client, n).json() for n in range(3)]
    assert [t["account_id"] for t in taken[:2]] == [f"{ENG}:team"] * 2
    assert taken[2]["account_id"] is None
    assert taken[2]["reason"] == "accounts_full"
    assert taken[2]["next_reset_at"] is None
    assert len(broker.db.docs[f"accounts/{ENG}:team"]["holds"]) == 2
