"""A Voice it session's hold through `/v1/accounts/assign` (part of #2688;
docs/design/voice-it-own-speech.md §14.1).

swarm-voice's fast Claude front (`holder: voice-front`, held for the whole
session) and its planner (`holder: voice-planner`, held per delegation) lease a
Claude account from the person's own workspace pool. What has to hold:

  * the hold is accepted only from swarm-voice, with all three fields, and
    never beside a task id;
  * the tenant is the person's own workspace, never one the body names;
  * it counts one agent on the account while the session lives: renewed by
    the voice hold route, given back by its release (invariant 3);
  * the lease hands swarm-voice the account's token, and logs none of it;
  * a task's assignment is exactly what it was.

Every token here is built at runtime: nothing credential-shaped is a literal.
"""

from __future__ import annotations

import secrets

import pytest

from quota_broker.accounts import POOL_WORKSPACE
from quota_broker.accountstore import AccountStore
from swarm_common.identity import tenant_id_for_user

ENG = "eng"
PERSON = "person@example.com"
PERSONAL = tenant_id_for_user(PERSON)
VOICE = "swarm-voice@example-project.iam.gserviceaccount.com"


class _Identity:
    """`WorkerIdentity`'s duck type: a tenant's worker or swarm-voice."""

    required = True

    def __init__(self) -> None:
        self.as_tenant(ENG)

    def resolve(self, authorization):  # noqa: ANN001
        return (self.tenant, False)

    def verified_email(self, authorization):  # noqa: ANN001
        return self.email

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant = tenant_id
        self.email = f"{tenant_id}-worker@example-project.iam.gserviceaccount.com"

    def as_voice(self) -> None:
        self.tenant, self.email = None, VOICE


class _Secrets:
    def __init__(self) -> None:
        self.latest: dict[str, str] = {}

    def access(self, name):  # noqa: ANN001
        return self.latest[name]

    def ensure_secret(self, name, *, labels, accessors, region=None):  # noqa: ANN001
        return None

    def add_version(self, name, value):  # noqa: ANN001
        self.latest[name] = value

    def set_worker_readers(self, name, *, readers, manages, revoke=True):  # noqa: ANN001
        return [], []


@pytest.fixture()
def accounts(broker) -> AccountStore:
    return AccountStore(broker.db)


@pytest.fixture()
def client(broker, accounts, monkeypatch):
    from fastapi.testclient import TestClient

    from quota_broker.main import create_app

    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.setenv("VOICE_SERVICE_ACCOUNTS", VOICE)
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE", "swarm-worker-{tenant}@example-project.iam.gserviceaccount.com"
    )
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    app.state.secret_store = _Secrets()
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    c.secrets = app.state.secret_store
    return c


def _claude(client, accounts, owner: str, label: str) -> tuple[str, str]:
    """A Claude subscription account and its token, stored as the worker reads it."""
    account = accounts.register(owner, label, provider="anthropic")
    token = "t" + secrets.token_hex(20)
    client.secrets.latest[accounts.secret_for(account)] = token + "\n"
    return account.account_id, token


def _hold(client, *, holder: str = "voice-front", session: str = "vs-1", person: str = PERSON, **extra):
    client.identity.as_voice()
    return client.post("/v1/accounts/assign", json={
        "provider": "anthropic", "holder": holder, "person": person, "voice_session_id": session, **extra})


def test_a_voice_front_hold_is_accepted_and_counted_as_one_agent(client, accounts):
    account_id, token = _claude(client, accounts, PERSONAL, "mine")
    answer = _hold(client)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["account_id"] == account_id and body["assignment_id"] and body["reason"] == ""
    assert body["credential"] == {"access_token": token}
    held = accounts.get(account_id)
    assert held.assigned == 1
    [hold] = held.holds
    assert hold.tenant_id == PERSONAL and hold.pool == POOL_WORKSPACE
    assert hold.voice_session_id == "vs-1" and hold.person == PERSON
    assert hold.task_id is None and hold.attempt_id is None


def test_the_hold_counts_while_the_session_lives_and_frees_on_release(client, accounts):
    """Renewed by the voice hold route, the account stays full for a task of
    the same workspace; released, it serves the task (invariant 3)."""
    account_id, _token = _claude(client, accounts, PERSONAL, "mine")
    accounts.set_max_agents(account_id, 1)
    body = _hold(client).json()
    hold = {"assignment_id": body["assignment_id"], "voice_session_id": "vs-1"}
    before = accounts.get(account_id).holds[0].expires_at

    renewed = client.post(f"/v1/voice/lease/{account_id}/renew", json=hold)
    assert renewed.status_code == 200, renewed.text
    assert accounts.get(account_id).holds[0].expires_at >= before

    client.identity.as_tenant(PERSONAL)
    busy = client.post("/v1/accounts/assign", json={"provider": "anthropic"}).json()
    assert busy["account_id"] is None, busy
    assert _hold(client, holder="voice-planner", session="vs-2").json()["account_id"] is None

    # Another session cannot give this one's hold back.
    client.identity.as_voice()
    other = client.post(f"/v1/voice/lease/{account_id}/release", json={**hold, "voice_session_id": "vs-2"})
    assert other.json()["released"] is False
    released = client.post(f"/v1/voice/lease/{account_id}/release", json=hold)
    assert released.json()["released"] is True
    assert accounts.get(account_id).holds == () and accounts.get(account_id).assigned == 0

    client.identity.as_tenant(PERSONAL)
    assert client.post("/v1/accounts/assign", json={"provider": "anthropic"}).json()["account_id"] == account_id


def test_the_tenant_is_the_persons_own_workspace(client, accounts):
    """An account of another workspace, not lent to the person's, is never
    the person's; the body has no way to name a tenant."""
    _claude(client, accounts, ENG, "team")
    refused = _hold(client).json()
    assert refused["account_id"] is None and refused["reason"] == "no_accounts_registered"
    assert _hold(client, tenant_id=ENG).status_code == 422


def test_an_excluded_account_is_not_offered_again(client, accounts):
    first, _ = _claude(client, accounts, PERSONAL, "one")
    second, _ = _claude(client, accounts, PERSONAL, "two")
    got = _hold(client).json()["account_id"]
    again = _hold(client, session="vs-2", exclude=[got]).json()["account_id"]
    assert {got, again} == {first, second}


def test_only_swarm_voice_may_take_a_voice_hold(client, accounts):
    account_id, _ = _claude(client, accounts, PERSONAL, "mine")
    client.identity.as_tenant(PERSONAL)
    refused = client.post("/v1/accounts/assign", json={
        "provider": "anthropic", "holder": "voice-front", "person": PERSON, "voice_session_id": "vs-1"})
    assert refused.status_code in (401, 403), refused.text
    assert accounts.get(account_id).holds == ()


@pytest.mark.parametrize("body", [
    {"holder": "voice-front", "person": PERSON},
    {"holder": "voice-front", "voice_session_id": "vs-1"},
    {"person": PERSON, "voice_session_id": "vs-1"},
    {"holder": "someone-else", "person": PERSON, "voice_session_id": "vs-1"},
    {"holder": "voice-front", "person": PERSON, "voice_session_id": "vs-1", "task_id": "t-1"},
])
def test_a_partial_or_mixed_voice_hold_is_refused(client, accounts, body):
    account_id, _ = _claude(client, accounts, PERSONAL, "mine")
    client.identity.as_voice()
    refused = client.post("/v1/accounts/assign", json={"provider": "anthropic", **body})
    assert refused.status_code == 422, refused.text
    assert accounts.get(account_id).holds == ()


def test_a_tasks_assignment_is_unchanged(client, accounts):
    account_id, token = _claude(client, accounts, ENG, "team")
    client.identity.as_tenant(ENG)
    body = client.post("/v1/accounts/assign", json={
        "provider": "anthropic", "task_id": "t-1", "attempt_id": "a-1"}).json()
    assert body["account_id"] == account_id
    assert set(body) == {"account_id", "assignment_id", "secret", "account", "reason", "next_reset_at"}
    assert body["secret"] == accounts.secret_for(accounts.get(account_id))
    assert token not in str(body)
    [hold] = accounts.get(account_id).holds
    assert hold.tenant_id == ENG and hold.task_id == "t-1" and hold.pool == POOL_WORKSPACE
    assert hold.voice_session_id is None and hold.person is None


def test_the_voice_hold_never_logs_the_token(client, accounts, caplog):
    _account_id, token = _claude(client, accounts, PERSONAL, "mine")
    caplog.set_level("DEBUG")
    assert _hold(client).status_code == 200
    assert token not in caplog.text
