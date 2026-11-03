"""The Voice it pool, the quota broker's half (owner, 2026-11-02/03).

The owner connected a ChatGPT plan and asked that it be "reserved for the voice
it feature ... and its not used by any other agent or any other user besides
voice it", picked "A separate Voice it pool", and then: "We should allow
accounts to be in more than one pool at a time". What has to hold:

  * an account in the Voice it pool ONLY is never assigned to a task of any
    workspace -- not its owner's, not a borrower's -- even when it is the only
    free account of the provider, and is never lent;
  * a Voice it session of a person the pool does not list is refused it; the
    listed person's session gets it, renews it and gives it back;
  * the pool count the scheduler's admission asks (`accounts_serving`)
    excludes it, so it adds no workspace capacity;
  * a pool is not removed while the account holds a lease of it, and a move
    needs the platform, the typed label and at least one pool;
  * an account in BOTH pools serves a task and a session, and its
    `max_agents` holds across the two.

Every token here is built at runtime: nothing credential-shaped is a literal.
"""

from __future__ import annotations

import json
import secrets

import pytest

from quota_broker.accounts import (
    POOL_VOICE_IT,
    POOL_WORKSPACE,
    accounts_serving,
    holds_from_firestore,
)
from quota_broker.accountstore import AccountStore

OWNER = "u-bgd"
ENG = "eng"
PERSON = "bogdan@saga.xyz"
STRANGER = "someone@example.com"


class _Identity:
    """`WorkerIdentity`'s duck type: a tenant's worker, the platform, or
    swarm-voice (by its verified email)."""

    required = True

    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False
        self.email = "eng-worker@test-project.iam.gserviceaccount.com"

    def resolve(self, authorization):  # noqa: ANN001
        return (None, True) if self.platform else (self.tenant, False)

    def verified_email(self, authorization):  # noqa: ANN001
        return self.email

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant, self.platform = tenant_id, False
        self.email = f"{tenant_id}-worker@test-project.iam.gserviceaccount.com"

    def as_platform(self) -> None:
        self.tenant, self.platform = None, True
        self.email = "swarm-api@test-project.iam.gserviceaccount.com"

    def as_voice(self) -> None:
        self.tenant, self.platform = None, False
        self.email = "swarm-voice@test-project.iam.gserviceaccount.com"


class _Secrets:
    """Secret Manager without a network: the published `{base}` of each plan."""

    def __init__(self) -> None:
        self.latest: dict[str, str] = {}
        #: Each secret's worker readers, as the lending route last set them.
        self.readers: dict[str, set[str]] = {}

    def access(self, name):  # noqa: ANN001
        return self.latest[name]

    def set_worker_readers(self, name, *, readers, manages, revoke=True):  # noqa: ANN001
        before = self.readers.get(name, set())
        after = set(readers) if revoke else before | set(readers)
        self.readers[name] = after
        return sorted(after - before), sorted(before - after)


@pytest.fixture()
def accounts(broker) -> AccountStore:
    return AccountStore(broker.db)


@pytest.fixture()
def client(broker, accounts, monkeypatch):
    from fastapi.testclient import TestClient

    from quota_broker.main import create_app

    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.setenv("VOICE_SERVICE_ACCOUNTS", "swarm-voice@test-project.iam.gserviceaccount.com")
    monkeypatch.setenv("VOICE_IT_PEOPLE", PERSON)
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE", "swarm-worker-{tenant}@test-project.iam.gserviceaccount.com"
    )
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    app.state.secret_store = _Secrets()
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    c.secrets = app.state.secret_store
    return c


def _plan(client, accounts, owner: str, label: str, *, lend_to=(), pools=None) -> str:
    account = accounts.register(owner, label, provider="openai", lend_to=lend_to)
    client.secrets.latest[accounts.secret_for(account)] = json.dumps(
        {"access_token": secrets.token_hex(16), "account_id": secrets.token_hex(6)}
    )
    if pools is not None:
        client.identity.as_platform()
        moved = client.put(
            f"/v1/accounts/{account.account_id}/pools",
            json={"pools": pools, "confirm_label": label},
        )
        assert moved.status_code == 200, moved.text
    return account.account_id


def _reserve_lent(broker, client, accounts, owner: str, label: str) -> str:
    """A LENT account whose document says Voice it only. The move route
    refuses to make one (`pool_lent`), so the document is written directly:
    a document from before that rule must still serve no borrower."""
    account_id = _plan(client, accounts, owner, label, lend_to=(ENG,))
    broker.db.docs[f"accounts/{account_id}"]["pools"] = [POOL_VOICE_IT]
    broker.db.docs[f"accounts/{account_id}"]["voice_it_people"] = [PERSON]
    return account_id


def _assign(client, tenant: str):
    client.identity.as_tenant(tenant)
    return client.post("/v1/accounts/assign", json={"provider": "openai"}).json()


def _lease(client, person: str = PERSON, session: str = "vs-1"):
    client.identity.as_voice()
    return client.post(
        "/v1/voice/lease",
        json={"person": person, "voice_session_id": session, "provider": "openai"},
    )


# -- a reserved account is never a task's ------------------------------------


def test_a_voice_only_account_is_never_assigned_to_any_workspace_task(client, accounts, broker):
    """Engineering borrows it and BGD's personal workspace owns it; the only
    free openai account is still refused to both."""
    _reserve_lent(broker, client, accounts, OWNER, "chatgpt-1")
    for tenant in (ENG, OWNER):
        body = _assign(client, tenant)
        assert body["account_id"] is None, body
        assert body["reason"] == "no_accounts_registered"


def test_the_pool_admission_counts_exclude_a_voice_only_account(client, accounts, broker):
    _reserve_lent(broker, client, accounts, OWNER, "chatgpt-1")
    _plan(client, accounts, ENG, "team-plan")
    listed = accounts.list()
    assert [a.label for a in accounts_serving(listed, ENG, "openai")] == ["team-plan"]
    assert accounts_serving(listed, OWNER, "openai") == []


def test_a_voice_only_account_is_never_lent(client, accounts):
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    client.identity.as_tenant(OWNER)
    refused = client.put(f"/v1/accounts/{account_id}/lending", json={"lend_to": [ENG]})
    assert refused.status_code in (400, 422), refused.text
    assert accounts.get(account_id).lend_to == ()


def test_a_lent_account_is_not_reserved_until_its_loan_ends(client, accounts):
    """Reserved means never lent: a borrower's worker reads the account's
    secret while the loan stands, so the move to Voice it only is refused
    until the owner ends the loan, which revokes the borrower's read."""
    account_id = _plan(client, accounts, OWNER, "chatgpt-1")
    secret = accounts.secret_for(accounts.get(account_id))
    client.identity.as_tenant(OWNER)
    lent = client.put(f"/v1/accounts/{account_id}/lending", json={"lend_to": [ENG]})
    assert lent.status_code == 200, lent.text
    assert len(client.secrets.readers[secret]) == 2

    client.identity.as_platform()
    path = f"/v1/accounts/{account_id}/pools"
    refused = client.put(path, json={"pools": [POOL_VOICE_IT], "confirm_label": "chatgpt-1"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "pool_lent"
    assert accounts.get(account_id).pools == (POOL_WORKSPACE,)
    # Both pools keep it a workspace account, so a loan may stand.
    both = client.put(path, json={"pools": [POOL_WORKSPACE, POOL_VOICE_IT], "confirm_label": "chatgpt-1"})
    assert both.status_code == 200, both.text

    client.identity.as_tenant(OWNER)
    assert client.put(f"/v1/accounts/{account_id}/lending", json={"lend_to": []}).status_code == 200
    client.identity.as_platform()
    moved = client.put(path, json={"pools": [POOL_VOICE_IT], "confirm_label": "chatgpt-1"})
    assert moved.status_code == 200, moved.text
    reserved = accounts.get(account_id)
    assert reserved.pools == (POOL_VOICE_IT,) and reserved.lend_to == ()
    # Only the owner's own worker is left reading it; no borrower.
    assert client.secrets.readers[secret] == {client.app.state.worker_service_account(OWNER)}


def test_a_reserved_account_is_listed_to_its_owner_only_when_asked(client, accounts):
    """A Voice-it-only account serves no tenant, so the default listing --
    what admission, capacity and setup read -- leaves it out; Capacity ›
    Accounts and the pool move ask with `include_reserved` and see it, its
    owner only."""
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    client.identity.as_platform()

    def listed(tenant: str, **params) -> list[str]:
        response = client.get("/v1/accounts", params={"tenant_id": tenant, **params})
        assert response.status_code == 200, response.text
        return [a["account_id"] for a in response.json()["accounts"]]

    assert listed(OWNER) == []
    assert listed(OWNER, include_reserved="true") == [account_id]
    assert listed(ENG, include_reserved="true") == []
    # So the move back to the workspace pool is reachable.
    back = client.put(f"/v1/accounts/{account_id}/pools",
                      json={"pools": [POOL_WORKSPACE], "confirm_label": "chatgpt-1"})
    assert back.status_code == 200, back.text
    assert listed(OWNER) == [account_id]


# -- a Voice it session ------------------------------------------------------


def test_a_person_not_in_the_pool_is_refused(client, accounts):
    _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    body = _lease(client, person=STRANGER).json()
    assert body["account_id"] is None and body["credential"] is None
    assert body["reason"] == "not_in_voice_pool"


def test_a_workspace_only_account_is_never_leased_to_voice_it(client, accounts):
    _plan(client, accounts, OWNER, "chatgpt-1")
    body = _lease(client).json()
    assert body["account_id"] is None
    assert body["reason"] == "no_voice_account_registered"


def test_the_listed_person_leases_renews_and_releases(client, accounts):
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    leased = _lease(client)
    assert leased.status_code == 200, leased.text
    body = leased.json()
    assert body["account_id"] == account_id
    assert body["credential"]["access_token"]
    holds = accounts.get(account_id).holds
    assert len(holds) == 1 and holds[0].pool == POOL_VOICE_IT
    assert holds[0].tenant_id == "" and holds[0].voice_session_id == "vs-1"

    # Held: a second session finds none free.
    assert _lease(client, session="vs-2").json()["reason"] == "no_voice_account_available"

    renewed = client.post(
        f"/v1/voice/lease/{account_id}/renew",
        json={"assignment_id": body["assignment_id"], "voice_session_id": "vs-1"},
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["credential"]["access_token"]

    # Another session cannot release this one's hold.
    other = client.post(
        f"/v1/voice/lease/{account_id}/release",
        json={"assignment_id": body["assignment_id"], "voice_session_id": "vs-2"},
    )
    assert other.json()["released"] is False
    released = client.post(
        f"/v1/voice/lease/{account_id}/release",
        json={"assignment_id": body["assignment_id"], "voice_session_id": "vs-1"},
    )
    assert released.json()["released"] is True
    assert accounts.get(account_id).holds == ()
    again = client.post(
        f"/v1/voice/lease/{account_id}/renew",
        json={"assignment_id": body["assignment_id"], "voice_session_id": "vs-1"},
    )
    assert again.status_code == 409


def test_a_lost_race_for_one_account_leases_the_next_free_one(client, accounts, monkeypatch):
    """Another session took the chosen account's last slot between the
    listing and the hold: the session gets the other free account, not a
    refusal."""
    import quota_broker.main as broker_main

    first = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    second = _plan(client, accounts, OWNER, "chatgpt-2", pools=[POOL_VOICE_IT])
    real = broker_main.acquire_hold
    lost: list[str] = []

    def racing(db, account_id, **kwargs):  # noqa: ANN001
        if not lost:
            lost.append(account_id)
            return None
        return real(db, account_id, **kwargs)

    monkeypatch.setattr(broker_main, "acquire_hold", racing)
    body = _lease(client).json()
    assert len(lost) == 1
    assert body["account_id"] in {first, second} - set(lost), body


def test_a_failed_credential_read_leaves_no_hold(client, accounts):
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    client.secrets.latest.clear()
    failed = _lease(client)
    assert failed.status_code == 422
    assert accounts.get(account_id).holds == ()


def test_only_swarm_voice_may_lease(client, accounts):
    _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    client.identity.as_tenant(ENG)
    refused = client.post(
        "/v1/voice/lease", json={"person": PERSON, "voice_session_id": "vs-1"}
    )
    assert refused.status_code == 403


def test_the_lease_never_logs_the_token(client, accounts, caplog):
    _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    with caplog.at_level("DEBUG"):
        token = _lease(client).json()["credential"]["access_token"]
    assert token not in caplog.text


def test_the_preferences_question_leases_nothing(client, accounts):
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    client.identity.as_platform()
    body = client.get("/v1/voice/pool", params={"person": PERSON}).json()
    assert body["account"]["label"] == "chatgpt-1" and body["reason"] == ""
    assert accounts.get(account_id).holds == ()
    assert client.get("/v1/voice/pool", params={"person": STRANGER}).json() == {
        "account": None,
        "reason": "not_in_voice_pool",
    }


# -- moving between pools ----------------------------------------------------


def test_a_move_needs_the_platform_the_label_and_one_pool(client, accounts):
    account_id = _plan(client, accounts, OWNER, "chatgpt-1")
    client.identity.as_tenant(OWNER)
    path = f"/v1/accounts/{account_id}/pools"
    assert client.put(path, json={"pools": [POOL_VOICE_IT], "confirm_label": "chatgpt-1"}).status_code == 403
    client.identity.as_platform()
    assert client.put(path, json={"pools": [POOL_VOICE_IT], "confirm_label": "chatgpt"}).status_code == 422
    assert client.put(path, json={"pools": [], "confirm_label": "chatgpt-1"}).status_code == 422
    assert client.put(path, json={"pools": ["elsewhere"], "confirm_label": "chatgpt-1"}).status_code == 422
    moved = client.put(path, json={"pools": [POOL_VOICE_IT, POOL_WORKSPACE], "confirm_label": "chatgpt-1"})
    assert moved.status_code == 200, moved.text
    served = moved.json()["account"]
    assert served["pools"] == [POOL_WORKSPACE, POOL_VOICE_IT]
    assert served["voice_it_people"] == [PERSON]


def test_a_move_refuses_to_remove_a_pool_while_leased_from_it(client, accounts):
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_WORKSPACE, POOL_VOICE_IT])
    assert _lease(client).json()["account_id"] == account_id
    client.identity.as_platform()
    refused = client.put(
        f"/v1/accounts/{account_id}/pools",
        json={"pools": [POOL_WORKSPACE], "confirm_label": "chatgpt-1"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "pool_in_use"
    assert accounts.get(account_id).pools == (POOL_WORKSPACE, POOL_VOICE_IT)
    # The pool with no lease of it may go.
    kept = client.put(
        f"/v1/accounts/{account_id}/pools",
        json={"pools": [POOL_VOICE_IT], "confirm_label": "chatgpt-1"},
    )
    assert kept.status_code == 200, kept.text


def test_an_account_in_both_pools_serves_both_within_its_agent_limit(client, accounts, broker):
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", lend_to=(ENG,),
                       pools=[POOL_WORKSPACE, POOL_VOICE_IT])
    accounts.set_max_agents(account_id, 2)
    # Out of the "one agent before its first reading" rule, so the cap decides.
    from datetime import datetime, timedelta, timezone

    from quota_broker.accounts import WindowReading

    now = datetime.now(timezone.utc)
    accounts.record_reading(
        account_id,
        {"five_hour": WindowReading(utilization=0.1, resets_at=now + timedelta(hours=3))},
        observed_at=now,
    )
    task = _assign(client, ENG)
    assert task["account_id"] == account_id
    session = _lease(client).json()
    assert session["account_id"] == account_id
    # Two holds, one of each pool, on one count: the cap is reached.
    holds = holds_from_firestore(broker.db.docs[f"accounts/{account_id}"]["holds"])
    assert sorted(h.pool for h in holds) == [POOL_VOICE_IT, POOL_WORKSPACE]
    assert _assign(client, ENG)["account_id"] is None
    assert _lease(client, session="vs-2").json()["account_id"] is None
    listing = accounts.get(account_id)
    assert len(listing.live_holds(now)) == 2 <= listing.max_agents
