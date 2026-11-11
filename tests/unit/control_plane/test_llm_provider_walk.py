"""The tiered walk: the broker's stage (b) of docs/design/llm-providers.md.

Stage (b) of LP-B1 (§8.1) is what this file holds to, one test per behaviour:

  * a lease walks its chain, plans first, then API keys where the chain
    allows them (§5.3), and the answer names its `tier`;
  * a lend over its cap is refused inside `acquire_hold`, and the answer
    names the cap (§4.6, Q7);
  * `lend_spend` accrues in the transaction that closes a hold (§4.6);
  * a lease on a key moves back to a plan when one frees (Q1, §5.3);
  * nothing available answers the soonest of the tiers' reasons (§5.1, §5.3);
  * `accounts_serving` counts both tiers when the chain allows keys.

Unit tests: the fake Firestore, no credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.accounts import (
    Account,
    Hold,
    Lend,
    WindowReading,
    accounts_serving,
)
from quota_broker.accountstore import COLLECTION, AccountStore

OWNER = "eng"
BORROWER = "research"


class _Identity:
    required = True

    def __init__(self) -> None:
        self.tenant: str | None = OWNER
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001
        return (None, True) if self.platform else (self.tenant, False)

    def verified_email(self, authorization):  # noqa: ANN001
        return "eng-worker@example-project.iam.gserviceaccount.com"

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant, self.platform = tenant_id, False


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _put(broker, account: Account) -> str:
    broker.db.collection(COLLECTION).document(account.account_id).set(account.to_firestore())
    return account.account_id


def _plan(broker, label="team", *, limited_for: timedelta | None = None, **extra) -> str:
    now = _now()
    return _put(broker, Account(
        account_id=f"{extra.get('owner_tenant', OWNER)}:{label}",
        owner_tenant=extra.pop("owner_tenant", OWNER),
        label=label,
        provider="anthropic",
        windows={"five_hour": WindowReading(0.1, now + timedelta(hours=4))},
        observed_at=now,
        limited_until=(now + limited_for) if limited_for else None,
        **extra,
    ))


def _key(broker, label="team-key", *, limited_for: timedelta | None = None, **extra) -> str:
    return _put(broker, Account(
        account_id=f"{extra.get('owner_tenant', OWNER)}:{label}",
        owner_tenant=extra.pop("owner_tenant", OWNER),
        label=label,
        provider="anthropic-api",
        limited_until=(_now() + limited_for) if limited_for else None,
        **extra,
    ))


def _task(broker, task_id: str, profile: str = "claude-code", tenant: str = OWNER) -> str:
    broker.db.collection("tasks").document(task_id).set(
        {"tenant_id": tenant, "runner_profile": profile, "model_class": None}
    )
    return task_id


def _assign(client, task_id: str, **extra):
    body = {"provider": "anthropic", "task_id": task_id, "attempt_id": "a1", **extra}
    answer = client.post("/v1/accounts/assign", json=body)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _doc(broker, account_id: str) -> dict:
    return broker.db.collection(COLLECTION).document(account_id).get().to_dict()


# -- the walk: plans first, then keys where the chain allows them --------------


def test_a_lease_with_a_free_plan_gets_the_plan(client, broker):
    plan = _plan(broker)
    _key(broker)
    answer = _assign(client, _task(broker, "t1"), key_tier=True)
    assert answer["account_id"] == plan
    assert answer["tier"] == "plan"
    assert answer["account"]["provider"] == "anthropic"


def test_with_every_plan_limited_a_lease_gets_a_key_only_where_the_chain_allows(client, broker):
    _plan(broker, limited_for=timedelta(hours=1))
    key = _key(broker)
    # A worker that does not say it can run on a key is never handed one.
    old = _assign(client, _task(broker, "t1"))
    assert old["account_id"] is None and old["tier"] is None
    # One that does is handed the key, tier api_key, with the key's provider.
    walked = _assign(client, _task(broker, "t2"), key_tier=True)
    assert walked["account_id"] == key
    assert walked["tier"] == "api_key"
    assert walked["account"]["provider"] == "anthropic-api"


def test_a_profile_that_declares_only_the_key_never_gets_a_plan(client, broker):
    _plan(broker)
    key = _key(broker)
    answer = _assign(client, _task(broker, "t1", profile="browser"), key_tier=True)
    assert answer["account_id"] == key and answer["tier"] == "api_key"


# -- the lend caps, inside acquire_hold -------------------------------------------


def test_a_lend_over_its_cap_is_refused_with_the_cap_reason(client, broker):
    from quota_broker.main import acquire_hold

    key = _key(broker, lends={BORROWER: Lend(1, 50.0)}, max_agents=5)
    now = _now()
    assert acquire_hold(broker.db, key, tenant_id=BORROWER, now=now) is not None
    refusals: list[str] = []
    assert acquire_hold(broker.db, key, tenant_id=BORROWER, now=now, refusals=refusals) is None
    assert refusals == ["lend_max_agents"]
    assert len(_doc(broker, key)["holds"]) == 1
    # The owner is not the borrower: the key is still its own.
    assert acquire_hold(broker.db, key, tenant_id=OWNER, now=now) is not None

    client.identity.as_tenant(BORROWER)
    answer = _assign(client, _task(broker, "t9", tenant=BORROWER), key_tier=True)
    assert answer["account_id"] is None
    assert answer["reason"] == "accounts_full"
    assert answer["caps"] == ["lend_max_agents"]


def test_a_lend_at_its_spend_cap_is_refused(broker):
    from quota_broker.main import acquire_hold

    month = _now().strftime("%Y-%m")
    key = _key(broker, lends={BORROWER: Lend(3, 10.0)},
               lend_spend={month: {BORROWER: 10.0}})
    refusals: list[str] = []
    assert acquire_hold(broker.db, key, tenant_id=BORROWER, now=_now(), refusals=refusals) is None
    assert refusals == ["lend_spend_cap"]


def test_lend_spend_accrues_at_hold_close(broker):
    from quota_broker.main import acquire_hold, release_hold

    key = _key(broker, lends={BORROWER: Lend(3, 10.0)})
    now = _now()
    assignment_id, _ = acquire_hold(broker.db, key, tenant_id=BORROWER, now=now)
    release_hold(broker.db, key, assignment_id=assignment_id, tenant_id=BORROWER,
                 now=now, cost_usd=4.25)
    month = now.strftime("%Y-%m")
    stored = _doc(broker, key)
    assert stored["lend_spend"] == {month: {BORROWER: 4.25}}
    assert stored["month_spend"] == {month: 4.25}
    # A second close adds to it, and the owner's own spend is the key's alone.
    owned, _ = acquire_hold(broker.db, key, tenant_id=OWNER, now=now)
    release_hold(broker.db, key, assignment_id=owned, tenant_id=OWNER, now=now, cost_usd=1.0)
    stored = _doc(broker, key)
    assert stored["lend_spend"] == {month: {BORROWER: 4.25}}
    assert stored["month_spend"] == {month: 5.25}
    assert Account.from_firestore(stored).lend_spend == {month: {BORROWER: 4.25}}


# -- back up the chain (Q1) -------------------------------------------------------


def test_a_lease_on_a_key_moves_back_to_a_plan_when_one_frees(client, broker):
    plan = _plan(broker, limited_for=timedelta(hours=1))
    key = _key(broker)
    task = _task(broker, "t1")
    on_key = _assign(client, task, key_tier=True)
    assert on_key["account_id"] == key
    proof = {"assignment_id": on_key["assignment_id"], "task_id": task, "attempt_id": "a1"}

    # The plan is still limited: the key holder stays.
    status = client.post(f"/v1/accounts/{key}/hold-status", json=proof).json()
    assert status["move"] is None

    # The plan frees: the next turn boundary answers `move`, plan_available.
    broker.db.collection(COLLECTION).document(plan).update({"limited_until": None})
    status = client.post(f"/v1/accounts/{key}/hold-status", json=proof).json()
    assert status["move"] == "plan_available"

    moved = client.post("/v1/accounts/swap", json={
        **proof, "account_id": key, "reason": "plan_available", "key_tier": True,
    }).json()
    assert moved["swapped"] is True
    assert moved["account_id"] == plan and moved["tier"] == "plan"
    assert _doc(broker, key)["holds"] == []


def test_a_move_back_with_no_plan_free_leaves_the_hold_on_the_key(client, broker):
    _plan(broker, limited_for=timedelta(hours=1))
    key = _key(broker)
    _key(broker, label="other-key")
    task = _task(broker, "t1")
    on_key = _assign(client, task, key_tier=True)
    proof = {"assignment_id": on_key["assignment_id"], "task_id": task, "attempt_id": "a1"}
    moved = client.post("/v1/accounts/swap", json={
        **proof, "account_id": on_key["account_id"], "reason": "plan_available", "key_tier": True,
    }).json()
    # Tier 1 only: never sideways to the other key.
    assert moved["swapped"] is False
    assert len(_doc(broker, on_key["account_id"])["holds"]) == 1
    assert _doc(broker, key)["holds"] == [] or on_key["account_id"] == key


# -- nothing available ------------------------------------------------------------


def test_nothing_available_answers_the_soonest_reason(client, broker):
    _plan(broker, limited_for=timedelta(hours=3))
    _key(broker, limited_for=timedelta(minutes=20))
    _key(broker, label="spent-key", exhausted_at=_now())
    answer = _assign(client, _task(broker, "t1"), key_tier=True)
    assert answer["account_id"] is None
    assert answer["reason"] == "no_account_available"
    soonest = datetime.fromisoformat(answer["next_reset_at"])
    assert timedelta(minutes=15) < soonest - _now() <= timedelta(minutes=20)


# -- accounts_serving --------------------------------------------------------------


def test_accounts_serving_counts_both_tiers_when_the_chain_allows_keys():
    plan = Account(account_id="eng:team", owner_tenant=OWNER, label="team")
    key = Account(account_id="eng:k", owner_tenant=OWNER, label="k", provider="anthropic-api")
    lent = Account(account_id="ops:k", owner_tenant="ops", label="k", provider="anthropic-api",
                   lends={OWNER: Lend(1, 5.0)})
    other_vendor = Account(account_id="eng:o", owner_tenant=OWNER, label="o", provider="openai-api",
                           voice_dedicated=False)
    every = [plan, key, lent, other_vendor]
    assert accounts_serving(every, OWNER, "anthropic") == [plan]
    assert accounts_serving(every, OWNER, "anthropic", key_tier=True) == [plan, key, lent]


def test_a_hold_of_an_exhausted_or_spent_key_is_no_candidate():
    from quota_broker.accounts import choose

    now = _now()
    month = now.strftime("%Y-%m")
    spent = Account(account_id="eng:s", owner_tenant=OWNER, label="s", provider="anthropic-api",
                    monthly_spend_cap_usd=5.0, month_spend={month: 5.0})
    gone = Account(account_id="eng:g", owner_tenant=OWNER, label="g", provider="anthropic-api",
                   exhausted_at=now)
    lent_full = Account(account_id="ops:k", owner_tenant="ops", label="k", provider="anthropic-api",
                        lends={OWNER: Lend(1, 5.0)},
                        holds=(Hold("x", OWNER, now + timedelta(hours=1)),))
    assert choose([spent, gone, lent_full], OWNER, now) is None


# -- Voice it ------------------------------------------------------------------------


def test_voice_it_live_walks_the_shared_keys_before_the_dedicated_key():
    from quota_broker.accounts import POOL_VOICE_IT, choose_voice_chain, tier_of, voice_chain

    now = _now()
    person = "owner@example.com"

    def voice_key(label: str, dedicated: bool, **extra) -> Account:
        return Account(account_id=f"{OWNER}:{label}", owner_tenant=OWNER, label=label,
                       provider="openai-api", pools=(POOL_VOICE_IT,),
                       voice_it_people=(person,), voice_dedicated=dedicated, **extra)

    dedicated = voice_key("voice", True)
    shared = voice_key("shared", False)
    listing = [dedicated, shared]
    tiers = voice_chain(listing, "openai", capability="live", key_tier=False)
    chosen, _ = choose_voice_chain(tiers, listing, person, now)
    assert chosen == shared and tier_of(chosen) == "api_key"

    # The shared key limited: tier 3, the dedicated Voice it key.
    limited = voice_key("shared", False, limited_until=now + timedelta(minutes=5))
    listing = [dedicated, limited]
    tiers = voice_chain(listing, "openai", capability="live", key_tier=False)
    chosen, _ = choose_voice_chain(tiers, listing, person, now)
    assert chosen == dedicated and tier_of(chosen) == "voice_dedicated"

    # A text conversation walks no key until swarm-voice says it can run on one.
    assert voice_chain(listing, "openai", capability="text", key_tier=False) == [[]]
