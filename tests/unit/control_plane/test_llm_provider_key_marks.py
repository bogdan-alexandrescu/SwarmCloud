"""A key's credit-gone report, its running spend and the broker's one verdict.

Three follow-ups to docs/design/llm-providers.md §4.6, §5.1 and §5.2, one
test per behaviour:

  * a key holder's credit-gone report under the key's own provider is taken
    and marks the key `exhausted_at` (§5.2), not a rate limit; a provider
    that is not the held account's is still refused; a plan's rate-limit
    report is unchanged;
  * a key holder's `hold-status` carries `cost_usd`, its running spend,
    bounded as a release's is, and the running spend counts toward the
    key's month cap: the grant, the hold-status `move` and the listing see
    it before the hold closes (§4.6);
  * the listing's `can_take_new_agent` and `blocked_by` mark an exhausted
    key and a key over its month cap, each with its own reason (§5.1);
  * a key whose credit returns (the hourly probe) is offered again.

Unit tests: the fake Firestore, no credentials, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker import api_key
from quota_broker.accounts import Account, Lend, WindowReading
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


def _month() -> str:
    return _now().strftime("%Y-%m")


def _put(broker, account: Account) -> str:
    broker.db.collection(COLLECTION).document(account.account_id).set(account.to_firestore())
    return account.account_id


def _plan(broker, label="team", **extra) -> str:
    now = _now()
    return _put(broker, Account(
        account_id=f"{OWNER}:{label}",
        owner_tenant=OWNER,
        label=label,
        provider="anthropic",
        windows={"five_hour": WindowReading(0.1, now + timedelta(hours=4))},
        observed_at=now,
        **extra,
    ))


def _key(broker, label="team-key", provider="anthropic-api", **extra) -> str:
    return _put(broker, Account(
        account_id=f"{OWNER}:{label}",
        owner_tenant=OWNER,
        label=label,
        provider=provider,
        **extra,
    ))


def _task(broker, task_id: str, profile: str = "claude-code", tenant: str = OWNER) -> str:
    broker.db.collection("tasks").document(task_id).set(
        {"tenant_id": tenant, "runner_profile": profile, "model_class": None}
    )
    return task_id


def _assign(client, task_id: str, **extra):
    body = {"provider": "anthropic", "task_id": task_id, "attempt_id": "a1",
            "key_tier": True, **extra}
    answer = client.post("/v1/accounts/assign", json=body)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _held(client, broker, task_id: str = "t1", tenant: str = OWNER) -> tuple[str, dict]:
    """The key an attempt was granted, and the proof that it holds it.
    Registers a plain key when the test registered none."""
    if broker.db.collection(COLLECTION).document(f"{OWNER}:team-key").get().to_dict() is None:
        _key(broker)
    granted = _assign(client, _task(broker, task_id, tenant=tenant))
    assert granted["tier"] == "api_key", granted
    proof = {"assignment_id": granted["assignment_id"], "task_id": task_id, "attempt_id": "a1"}
    return granted["account_id"], proof


def _doc(broker, account_id: str) -> dict:
    return broker.db.collection(COLLECTION).document(account_id).get().to_dict()


def _listed(client, account_id: str) -> dict:
    answer = client.get("/v1/accounts")
    assert answer.status_code == 200, answer.text
    return next(a for a in answer.json()["accounts"] if a["account_id"] == account_id)


# -- 1. a credit-gone report under the key's own provider --------------------------


def test_a_key_holders_credit_gone_report_under_its_own_provider_marks_the_key(client, broker):
    key, proof = _held(client, broker)
    before = broker.current("anthropic", OWNER).adaptive_target
    answer = client.post(
        "/v1/quota/anthropic-api/eng/exhausted",
        json={"account": {**proof, "account_id": key}, "message": "credit balance exhausted"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["scope"] == "account"
    assert answer.json()["tenant_target_decreased"] is False
    stored = _doc(broker, key)
    # Credit gone (§5.2): marked, and NOT a rate limit with a clock.
    assert isinstance(stored.get("exhausted_at"), datetime)
    assert stored.get("limited_until") is None
    # The tenant's AIMD target is the vendor's, and untouched.
    assert broker.current("anthropic", OWNER).adaptive_target == before


def test_a_credit_gone_report_under_a_provider_the_held_account_is_not_is_refused(client, broker):
    key, proof = _held(client, broker)
    ref = {"account": {**proof, "account_id": key}}
    # The held key is anthropic-api: an openai-api report on it is refused.
    refused = client.post("/v1/quota/openai-api/eng/exhausted", json=ref)
    assert refused.status_code == 422, refused.text
    # A key provider with no account named is refused as before.
    assert client.post("/v1/quota/anthropic-api/eng/exhausted", json={}).status_code == 422
    # A key provider naming a hold the caller does not have is refused.
    stale = {"account": {**proof, "attempt_id": "a0", "account_id": key}}
    assert client.post("/v1/quota/anthropic-api/eng/exhausted", json=stale).status_code == 422
    assert _doc(broker, key).get("exhausted_at") is None


def test_a_plans_rate_limit_report_is_unchanged(client, broker):
    plan = _plan(broker)
    _plan(broker, label="other", assigned=1)
    granted = _assign(client, _task(broker, "t1"))
    assert granted["account_id"] == plan
    proof = {"assignment_id": granted["assignment_id"], "task_id": "t1", "attempt_id": "a1"}
    answer = client.post(
        "/v1/quota/anthropic/eng/rate-limit",
        json={"account": {**proof, "account_id": plan}, "retry_after_seconds": 60},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["scope"] == "account"
    stored = _doc(broker, plan)
    assert isinstance(stored.get("limited_until"), datetime)
    assert stored.get("exhausted_at") is None
    # And a key provider is still not a rate-limit route's provider.
    assert client.post("/v1/quota/anthropic-api/eng/rate-limit", json={}).status_code == 422


# -- 2. the running spend on hold-status ---------------------------------------------


def test_hold_status_takes_the_running_spend_and_records_it_on_the_hold(client, broker):
    key, proof = _held(client, broker)
    answer = client.post(f"/v1/accounts/{key}/hold-status", json={**proof, "cost_usd": 2.5})
    assert answer.status_code == 200, answer.text
    assert answer.json()["move"] is None
    (hold,) = _doc(broker, key)["holds"]
    assert hold["running_cost_usd"] == 2.5
    # Without the figure the read is as it was.
    assert client.post(f"/v1/accounts/{key}/hold-status", json=proof).status_code == 200


@pytest.mark.parametrize("cost", [-0.01, 1e12, "nan"])
def test_hold_status_refuses_a_running_spend_out_of_bounds(client, broker, cost):
    key, proof = _held(client, broker)
    answer = client.post(f"/v1/accounts/{key}/hold-status", json={**proof, "cost_usd": cost})
    assert answer.status_code == 422
    (hold,) = _doc(broker, key)["holds"]
    assert "running_cost_usd" not in hold


def test_hold_status_with_a_stale_proof_is_refused_and_records_nothing(client, broker):
    key, proof = _held(client, broker)
    stale = {**proof, "attempt_id": "a0", "cost_usd": 1.0}
    assert client.post(f"/v1/accounts/{key}/hold-status", json=stale).status_code == 403
    (hold,) = _doc(broker, key)["holds"]
    assert "running_cost_usd" not in hold


def test_the_running_spend_counts_toward_the_month_cap(client, broker):
    from quota_broker.main import acquire_hold

    key = _key(broker, monthly_spend_cap_usd=10.0, max_agents=5,
               month_spend={_month(): 4.0})
    _, proof = _held(client, broker)
    # 4 closed + 3 running: under the cap, the attempt stays.
    status = client.post(f"/v1/accounts/{key}/hold-status", json={**proof, "cost_usd": 3.0})
    assert status.json()["move"] is None
    assert _listed(client, key)["can_take_new_agent"] is True
    # 4 closed + 6.5 running: at the cap before the hold closes. The holder
    # is answered `move` at this turn boundary, a new grant is refused, and
    # the listing says the cap holds it.
    status = client.post(f"/v1/accounts/{key}/hold-status", json={**proof, "cost_usd": 6.5})
    assert status.status_code == 200
    assert status.json()["move"] == "exhausted"
    refusals: list[str] = []
    assert acquire_hold(broker.db, key, tenant_id=OWNER, now=_now(), refusals=refusals) is None
    assert refusals == ["spend_cap"]
    listed = _listed(client, key)
    assert listed["can_take_new_agent"] is False
    assert any(b.get("reason") == "spend_cap" for b in listed["blocked_by"])
    # The close counts the same figure once: the running one goes with the hold.
    moved = client.post("/v1/accounts/swap", json={
        **proof, "account_id": key, "reason": "exhausted", "key_tier": True, "cost_usd": 6.5,
    })
    assert moved.status_code == 200, moved.text
    stored = _doc(broker, key)
    assert stored["holds"] == []
    assert stored["month_spend"] == {_month(): 10.5}


def test_a_borrowers_running_spend_counts_toward_its_lend_cap(client, broker):
    key = _key(broker, lends={BORROWER: Lend(3, 5.0)}, max_agents=5)
    client.identity.as_tenant(BORROWER)
    _, proof = _held(client, broker, tenant=BORROWER)
    status = client.post(f"/v1/accounts/{key}/hold-status", json={**proof, "cost_usd": 5.0})
    assert status.json()["move"] == "exhausted"
    # The owner's own use of the key is not the borrower's cap.
    client.identity.as_tenant(OWNER)
    assert _listed(client, key)["can_take_new_agent"] is True


def test_a_plan_holders_running_spend_is_not_recorded(client, broker):
    plan = _plan(broker)
    granted = _assign(client, _task(broker, "t1"))
    assert granted["account_id"] == plan
    proof = {"assignment_id": granted["assignment_id"], "task_id": "t1", "attempt_id": "a1"}
    answer = client.post(f"/v1/accounts/{plan}/hold-status", json={**proof, "cost_usd": 3.0})
    assert answer.status_code == 200
    (hold,) = _doc(broker, plan)["holds"]
    assert "running_cost_usd" not in hold


# -- 3. one verdict for every reader ----------------------------------------------------


def test_the_listing_marks_an_exhausted_key_unable_to_take_a_new_agent(client, broker):
    key = _key(broker, exhausted_at=_now())
    listed = _listed(client, key)
    assert listed["can_take_new_agent"] is False
    (mark,) = [b for b in listed["blocked_by"] if b.get("reason") == "exhausted"]
    assert mark["exhausted_at"] == listed["exhausted_at"]


def test_the_listing_marks_a_key_over_its_month_cap_unable_to_take_a_new_agent(client, broker):
    key = _key(broker, monthly_spend_cap_usd=20.0, month_spend={_month(): 20.0})
    listed = _listed(client, key)
    assert listed["can_take_new_agent"] is False
    (mark,) = [b for b in listed["blocked_by"] if b.get("reason") == "spend_cap"]
    assert mark["monthly_spend_cap_usd"] == 20.0
    assert mark["spent_usd"] == 20.0


def test_the_listing_leaves_a_key_under_its_cap_and_a_plan_as_they_were(client, broker):
    key = _key(broker, monthly_spend_cap_usd=20.0, month_spend={_month(): 19.0})
    plan = _plan(broker)
    for account_id in (key, plan):
        listed = _listed(client, account_id)
        assert listed["can_take_new_agent"] is True
        assert listed["blocked_by"] == []


def test_a_key_whose_credit_returns_is_used_again(client, broker, accounts):
    from quota_broker.main import probe_exhausted_keys

    key, proof = _held(client, broker)
    report = {"account": {**proof, "account_id": key}}
    assert client.post("/v1/quota/anthropic-api/eng/exhausted", json=report).status_code == 200
    assert _listed(client, key)["can_take_new_agent"] is False
    released = client.post(f"/v1/accounts/{key}/release",
                           json={"assignment_id": proof["assignment_id"]})
    assert released.status_code == 200, released.text
    # Nothing else serves the next attempt while the key's credit is gone.
    assert _assign(client, _task(broker, "t2"))["account_id"] is None

    value = "sk-" + "ant-api03-" + secrets.token_hex(24)

    class _Secrets:
        def access(self, name):  # noqa: ANN001
            return api_key.serialise(api_key.ANTHROPIC_API, value)

    class _Validator:
        def probe(self, provider, key):  # noqa: ANN001
            return key == value

    later = _now() + timedelta(hours=2)
    result = probe_exhausted_keys(broker.db, accounts, _Secrets(), _Validator(), now=later)
    assert result["served_again"] == [key]
    assert _listed(client, key)["can_take_new_agent"] is True
    assert _assign(client, _task(broker, "t3"))["account_id"] == key
