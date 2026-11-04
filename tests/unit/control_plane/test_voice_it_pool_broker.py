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
from datetime import datetime, timedelta, timezone

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

    def ensure_secret(self, name, *, labels, accessors, region=None):  # noqa: ANN001
        self.readers.setdefault(name, set())
        self.accessors = getattr(self, "accessors", {})
        self.accessors[name] = list(accessors)

    def add_version(self, name, value):  # noqa: ANN001
        self.latest[name] = value

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


def _key(client, accounts, owner: str, label: str) -> str:
    """An OpenAI developer API key account in the Voice it pool, its `{base}`
    written as the registration route writes it."""
    from quota_broker import openai_api_key

    account = accounts.register(owner, label, provider=openai_api_key.PROVIDER)
    client.secrets.latest[accounts.secret_for(account)] = openai_api_key.serialise(
        "sk-" + secrets.token_hex(24)
    )
    client.identity.as_platform()
    moved = client.put(
        f"/v1/accounts/{account.account_id}/pools",
        json={"pools": [POOL_VOICE_IT], "confirm_label": label},
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
        json={"person": person, "voice_session_id": session, "provider": "openai",
              "capability": "live"},
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
    _key(client, accounts, OWNER, "key-1")
    body = _lease(client, person=STRANGER).json()
    assert body["account_id"] is None and body["credential"] is None
    assert body["reason"] == "not_in_voice_pool"


def test_a_workspace_only_account_is_never_leased_to_voice_it(client, accounts):
    _plan(client, accounts, OWNER, "chatgpt-1")
    body = _lease(client).json()
    assert body["account_id"] is None
    assert body["reason"] == "no_voice_account_registered"


def test_the_listed_person_leases_renews_and_releases(client, accounts):
    account_id = _key(client, accounts, OWNER, "key-1")
    # One session at a time on this key, so the second below finds none free.
    accounts.set_max_agents(account_id, 1)
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

    first = _key(client, accounts, OWNER, "key-1")
    second = _key(client, accounts, OWNER, "key-2")
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
    account_id = _key(client, accounts, OWNER, "key-1")
    client.secrets.latest.clear()
    failed = _lease(client)
    assert failed.status_code == 422
    assert accounts.get(account_id).holds == ()


def test_only_swarm_voice_may_lease(client, accounts):
    _key(client, accounts, OWNER, "key-1")
    client.identity.as_tenant(ENG)
    refused = client.post(
        "/v1/voice/lease", json={"person": PERSON, "voice_session_id": "vs-1"}
    )
    assert refused.status_code == 403


def test_the_lease_never_logs_the_token(client, accounts, caplog):
    _key(client, accounts, OWNER, "key-1")
    with caplog.at_level("DEBUG"):
        token = _lease(client).json()["credential"]["access_token"]
    assert token not in caplog.text


def test_the_preferences_question_leases_nothing(client, accounts):
    account_id = _key(client, accounts, OWNER, "key-1")
    client.identity.as_platform()
    body = client.get("/v1/voice/pool", params={"person": PERSON}).json()
    assert body["account"]["label"] == "key-1" and body["reason"] == ""
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
    """A plan in both pools holding a task's lease cannot leave the workspace
    pool until that lease ends; the Voice it pool, which it holds nothing of,
    may go."""
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_WORKSPACE, POOL_VOICE_IT])
    assert _assign(client, OWNER)["account_id"] == account_id
    client.identity.as_platform()
    refused = client.put(
        f"/v1/accounts/{account_id}/pools",
        json={"pools": [POOL_VOICE_IT], "confirm_label": "chatgpt-1"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "pool_in_use"
    assert accounts.get(account_id).pools == (POOL_WORKSPACE, POOL_VOICE_IT)
    kept = client.put(
        f"/v1/accounts/{account_id}/pools",
        json={"pools": [POOL_WORKSPACE], "confirm_label": "chatgpt-1"},
    )
    assert kept.status_code == 200, kept.text


def test_a_plan_in_both_pools_serves_tasks_and_never_a_live_session(client, accounts, broker):
    """OpenAI refused the ChatGPT plan for GPT-Live on 4 November 2026 (403
    on /v1/live/sessions): a plan stays in the pools for codex steps, and a
    Voice it session is never handed one, even when it is the only free
    account in the Voice it pool."""
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", lend_to=(ENG,),
                       pools=[POOL_WORKSPACE, POOL_VOICE_IT])
    task = _assign(client, ENG)
    assert task["account_id"] == account_id
    session = _lease(client).json()
    assert session["account_id"] is None and session["credential"] is None
    assert session["reason"] == "no_voice_account_registered"
    holds = holds_from_firestore(broker.db.docs[f"accounts/{account_id}"]["holds"])
    assert [h.pool for h in holds] == [POOL_WORKSPACE]


# -- an OpenAI developer API key: the Live-capable kind ------------------------


def _register_key(client, *, label: str = "voice-key", lend_to=(), credential: str | None = None):
    client.identity.as_platform()
    return client.post(
        "/v1/accounts",
        json={
            "owner_tenant": OWNER,
            "label": label,
            "provider": "openai-api",
            "lend_to": list(lend_to),
            "credential": credential if credential is not None else "sk-proj-" + secrets.token_hex(24),
        },
    )


def test_a_registered_key_is_in_the_voice_it_pool_only_and_read_by_the_broker_alone(
    client, accounts, caplog
):
    client.app.state.broker_service_account = "swarm-quota-broker@test-project.iam.gserviceaccount.com"
    key = "sk-proj-" + secrets.token_hex(24)
    with caplog.at_level("DEBUG"):
        registered = _register_key(client, credential=key)
    assert registered.status_code == 201, registered.text
    assert key not in registered.text and key not in caplog.text
    account = accounts.get(f"{OWNER}:voice-key")
    assert account.provider == "openai-api"
    assert account.pools == (POOL_VOICE_IT,)
    assert account.voice_it_people == (PERSON,)
    base = accounts.secret_for(account)
    # One secret, the broker its only reader: no worker, no `-refresh`.
    assert client.secrets.accessors == {
        base: ["serviceAccount:swarm-quota-broker@test-project.iam.gserviceaccount.com"]
    }
    assert key in client.secrets.latest[base]
    served = registered.json()["account"]
    assert served["measured"] is False and served["windows"] == {}


def test_a_key_is_refused_when_lent_or_not_a_key_or_from_a_workspace(client, accounts):
    client.identity.as_tenant(OWNER)
    from_tenant = client.post("/v1/accounts", json={
        "owner_tenant": OWNER, "label": "voice-key", "provider": "openai-api",
        "credential": "sk-proj-" + secrets.token_hex(24)})
    assert from_tenant.status_code == 403, from_tenant.text
    lent = _register_key(client, lend_to=(ENG,))
    assert lent.status_code == 422, lent.text
    assert "never lent" in lent.json()["message"]
    pasted = "eyJ" + secrets.token_hex(24)
    wrong = _register_key(client, credential=pasted)
    assert wrong.status_code == 422, wrong.text
    assert pasted not in wrong.text
    assert accounts.list() == []
    assert client.secrets.latest == {}


def test_a_key_never_moves_into_the_workspace_pool_nor_serves_a_task(client, accounts):
    account_id = _key(client, accounts, OWNER, "key-1")
    client.identity.as_platform()
    for pools in ([POOL_WORKSPACE], [POOL_WORKSPACE, POOL_VOICE_IT]):
        refused = client.put(
            f"/v1/accounts/{account_id}/pools", json={"pools": pools, "confirm_label": "key-1"}
        )
        assert refused.status_code == 422, refused.text
    assert accounts.get(account_id).pools == (POOL_VOICE_IT,)
    for tenant in (OWNER, ENG):
        client.identity.as_tenant(tenant)
        for provider in ("openai", "openai-api"):
            body = client.post("/v1/accounts/assign", json={"provider": provider}).json()
            assert body.get("account_id") is None, body


def test_the_lending_route_never_binds_a_worker_to_a_key(client, accounts):
    # Even an empty `lend_to` from the owner would bind the owner's worker as
    # a reader of `{base}`: every agent of that tenant could read the key.
    account_id = _key(client, accounts, OWNER, "key-1")
    client.identity.as_tenant(OWNER)
    for lend_to in ([], [ENG]):
        refused = client.put(f"/v1/accounts/{account_id}/lending", json={"lend_to": lend_to})
        assert refused.status_code == 422, refused.text
        assert "OpenAI API key" in refused.json()["message"]
    assert not any(client.secrets.readers.values()), client.secrets.readers
    assert accounts.get(account_id).lend_to == ()


def test_a_plan_registration_never_reuses_a_key_label(client, accounts):
    # Registering a plan over a key's label would rewrite its provider and
    # bind the owner's worker to `{base}`, whose older version holds the key.
    account_id = _key(client, accounts, OWNER, "key-1")
    before = dict(client.secrets.latest)
    expires = datetime.now(timezone.utc) + timedelta(hours=8)
    credential = json.dumps({"claudeAiOauth": {
        "accessToken": "at-" + secrets.token_hex(12),
        "refreshToken": "rt-" + secrets.token_hex(12),
        "expiresAt": int(expires.timestamp() * 1000)}})
    client.identity.as_platform()
    refused = client.post("/v1/accounts", json={
        "owner_tenant": OWNER, "label": "key-1", "provider": "anthropic",
        "credential": credential})
    assert refused.status_code == 422, refused.text
    assert "OpenAI API key" in refused.json()["message"]
    assert accounts.get(account_id).provider == "openai-api"
    assert not any(client.secrets.readers.values()), client.secrets.readers
    assert client.secrets.latest == before


def test_a_live_session_leases_the_key_and_never_the_plan(client, accounts):
    _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    key_id = _key(client, accounts, OWNER, "key-1")
    accounts.set_max_agents(key_id, 1)
    leased = _lease(client).json()
    assert leased["account_id"] == key_id
    credential = leased["credential"]
    # The key alone: no plan account id, so swarm-voice sends no ChatGPT-Account-Id.
    assert credential["kind"] == "api_key" and "account_id" not in credential
    assert credential["access_token"].startswith("sk-")
    # The key is held; the plan is still never offered.
    second = _lease(client, session="vs-2").json()
    assert second["account_id"] is None
    assert second["reason"] == "no_voice_account_available"
    renewed = client.post(
        f"/v1/voice/lease/{key_id}/renew",
        json={"assignment_id": leased["assignment_id"], "voice_session_id": "vs-1"},
    )
    assert renewed.json()["credential"] == credential


def test_a_key_takes_sessions_up_to_its_limit_with_no_reading(client, accounts):
    """A key has no usage window to read, so the 'one agent before its first
    reading' rule would hold it to one session forever: its `max_agents`
    decides instead."""
    key_id = _key(client, accounts, OWNER, "key-1")
    accounts.set_max_agents(key_id, 2)
    assert _lease(client, session="vs-1").json()["account_id"] == key_id
    assert _lease(client, session="vs-2").json()["account_id"] == key_id
    assert _lease(client, session="vs-3").json()["account_id"] is None


def test_a_lease_with_only_a_plan_in_the_pool_is_refused_before_any_hold(client, accounts):
    account_id = _plan(client, accounts, OWNER, "chatgpt-1", pools=[POOL_VOICE_IT])
    body = _lease(client).json()
    assert body["account_id"] is None and body["assignment_id"] is None
    assert body["reason"] == "no_voice_account_registered"
    assert accounts.get(account_id).holds == ()
    client.identity.as_platform()
    asked = client.get("/v1/voice/pool", params={"person": PERSON}).json()
    assert asked == {"account": None, "reason": "no_voice_account_registered"}


def test_an_unknown_capability_is_refused(client, accounts):
    _key(client, accounts, OWNER, "key-1")
    client.identity.as_voice()
    refused = client.post(
        "/v1/voice/lease",
        json={"person": PERSON, "voice_session_id": "vs-1", "capability": "text"},
    )
    assert refused.status_code == 422, refused.text


def test_a_key_is_neither_refreshed_nor_polled(client, accounts):
    from datetime import datetime, timezone

    from quota_broker.accounts import due_for_refresh
    from quota_broker.usagepoll import UsagePoller

    _key(client, accounts, OWNER, "key-1")
    plan_id = _plan(client, accounts, OWNER, "chatgpt-1")
    listed = accounts.list()
    assert [a.account_id for a in due_for_refresh(listed, datetime.now(timezone.utc))] == [plan_id]
    asked: list[str] = []

    def fetch(token):  # noqa: ANN001
        asked.append(token)
        return {}

    import logging

    poller = UsagePoller(client.secrets, accounts, logger=logging.getLogger("t"),
                         fetch=fetch, fetch_plan=fetch)
    outcomes = poller.poll_round(listed)
    assert [o.account_id for o in outcomes] == [plan_id]
    assert len(asked) == 1
