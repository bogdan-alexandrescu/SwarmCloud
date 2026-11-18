"""Delete, rotate, resume and the exhausted probe: the broker's stage (c) of
docs/design/llm-providers.md (§4.3, §4.4, §4.5, §5.2; the owner's Q3, Q4, Q7
and Q9 of 9 November 2026).

  * DELETE closes the key's holds and writes `key_deletions/<account id>`
    (secret name, who, when; never the value) in ONE transaction, then
    destroys every version of its secret and deletes the secret; a failed
    destroy answers 502 naming the step and leaves the record for the sweep.
    An imported workspace key leaves the workspace's own secret alone; the
    dedicated Voice it key under its old name keeps an empty secret; a plan's
    removal still retains its secret.
  * ROTATE writes a new version and destroys the superseded one only after
    the hold TTL, touching no hold, cap or lend.
  * RESUME puts a paused or draining key back in service and clears its
    credit mark.
  * THE PROBE asks each key marked credit gone, once an hour, and returns
    the one that serves again.

Unit tests: the fake Firestore, no credentials, no emulator. Every key is
built at runtime: nothing credential-shaped is a literal.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.accounts import (
    DEFAULT_HOLD_TTL,
    HOLD_LOG_COLLECTION,
    AccountState,
    key_secret_name,
    secret_name,
)
from quota_broker.accountstore import COLLECTION, AccountStore
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore, FakeTransaction

OWNER = "eng"
BORROWER = "research"
ADMIN = "root@example.com"
DELETER = "eng-worker@example-project.iam.gserviceaccount.com"
PROJECT = "example-project"


def _anthropic_key() -> str:
    return "sk-" + "ant-api03-" + secrets.token_hex(24)


def _openai_key() -> str:
    return "sk-proj-" + secrets.token_hex(24)


# -- fakes --------------------------------------------------------------------


class _Identity:
    required = True

    def __init__(self) -> None:
        self.tenant: str | None = OWNER
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001
        return (None, True) if self.platform else (self.tenant, False)

    def verified_email(self, authorization):  # noqa: ANN001
        return DELETER

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant, self.platform = tenant_id, False

    def as_platform(self) -> None:
        self.tenant, self.platform = None, True


class _Secrets:
    """Secret Manager without a network, with numbered versions."""

    def __init__(self) -> None:
        #: secret -> [[number, state, value]]
        self.versions: dict[str, list[list]] = {}
        self.deleted: list[str] = []
        self.readers: dict[str, set[str]] = {}
        self.workspace_keys: list[tuple[str, str]] = []
        self.fail_destroy: Exception | None = None
        self.fail_delete: Exception | None = None
        self.calls: list[tuple[str, str]] = []

    def seed(self, name: str, value: str) -> None:
        self.versions.setdefault(name, []).append([len(self.versions.get(name, [])) + 1, "ENABLED", value])

    def access(self, name):  # noqa: ANN001
        enabled = [v for v in self.versions.get(name, []) if v[1] == "ENABLED"]
        if not enabled:
            raise KeyError(name)
        return enabled[-1][2]

    def ensure_secret(self, name, *, labels, accessors, region=None):  # noqa: ANN001
        self.versions.setdefault(name, [])
        self.readers.setdefault(name, set())

    def add_version(self, name, value):  # noqa: ANN001
        versions = self.versions.setdefault(name, [])
        number = len(versions) + 1
        versions.append([number, "ENABLED", value])
        return f"projects/{PROJECT}/secrets/{name}/versions/{number}"

    def set_worker_readers(self, name, *, readers, manages, revoke=True):  # noqa: ANN001
        before = self.readers.get(name, set())
        after = set(readers) if revoke else before | set(readers)
        self.readers[name] = after
        return sorted(after - before), sorted(before - after)

    def workspace_provider_keys(self):
        return list(self.workspace_keys)

    def destroy_all_versions(self, name):  # noqa: ANN001
        self.calls.append(("destroy_all_versions", name))
        if self.fail_destroy is not None:
            raise self.fail_destroy
        count = 0
        for version in self.versions.get(name, []):
            if version[1] != "DESTROYED":
                version[1] = "DESTROYED"
                count += 1
        return count

    def destroy_versions_before(self, name, keep):  # noqa: ANN001
        self.calls.append(("destroy_versions_before", name))
        count = 0
        for version in self.versions.get(name, []):
            if version[0] < keep and version[1] != "DESTROYED":
                version[1] = "DESTROYED"
                count += 1
        return count

    def delete_key_secret(self, name):  # noqa: ANN001
        self.calls.append(("delete_key_secret", name))
        if self.fail_delete is not None:
            raise self.fail_delete
        self.deleted.append(name)
        self.versions.pop(name, None)
        return True

    def states(self, name: str) -> list[str]:
        return [v[1] for v in self.versions.get(name, [])]


class _Validator:
    def __init__(self) -> None:
        from quota_broker import api_key

        self.default = api_key.KeyCheck(api_key.CHECK_VALID)
        self.serves: dict[str, bool] = {}
        self.probed: list[str] = []

    def check(self, provider, key):  # noqa: ANN001
        return self.default

    def probe(self, provider, key):  # noqa: ANN001
        self.probed.append(key)
        return self.serves.get(key, False)


class _RecordingTransaction(FakeTransaction):
    """Remembers which documents each COMMIT wrote, as one set per commit."""

    def _commit(self):
        self._db.commits.append({ref.path for _op, ref, _data in self._buffer})
        return super()._commit()


class _Firestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.commits: list[set[str]] = []

    def transaction(self, **kwargs):
        return _RecordingTransaction(self)


@pytest.fixture()
def db() -> _Firestore:
    return _Firestore()


@pytest.fixture()
def broker(db) -> QuotaBroker:
    return QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))


@pytest.fixture()
def accounts(broker) -> AccountStore:
    return AccountStore(broker.db)


@pytest.fixture()
def client(broker, accounts, monkeypatch):
    from fastapi.testclient import TestClient

    from quota_broker.main import create_app

    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.setenv("VOICE_SERVICE_ACCOUNTS", "swarm-voice@example-project.iam.gserviceaccount.com")
    monkeypatch.setenv("VOICE_IT_PEOPLE", ADMIN)
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE", "swarm-worker-{tenant}@example-project.iam.gserviceaccount.com"
    )
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    app.state.secret_store = _Secrets()
    app.state.key_validator = _Validator()
    app.state.broker_service_account = "swarm-quota-broker@example-project.iam.gserviceaccount.com"
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    c.secrets = app.state.secret_store
    c.validator = app.state.key_validator
    return c


def _add_key(client, *, provider="anthropic-api", label="team-key", credential=None, **extra):
    body = {
        "owner_tenant": OWNER,
        "label": label,
        "provider": provider,
        "credential": credential if credential is not None else _anthropic_key(),
        "added_by": ADMIN,
        **extra,
    }
    return client.post("/v1/accounts", json=body)


def _doc(broker, collection: str, doc_id: str) -> dict | None:
    snap = broker.db.collection(collection).document(doc_id).get()
    return snap.to_dict() if getattr(snap, "exists", False) else None


def _hold(broker, account_id: str, tenant: str = OWNER) -> str:
    from quota_broker.main import acquire_hold

    granted = acquire_hold(broker.db, account_id, tenant_id=tenant,
                           now=datetime.now(timezone.utc), task_id="task-1", attempt_id="a-1")
    assert granted is not None
    return granted[0]


def _sweep(client):
    client.identity.as_platform()
    response = client.post("/v1/quota/sweep")
    client.identity.as_tenant(OWNER)
    assert response.status_code == 200, response.text
    return response.json()


ACCOUNT = f"{OWNER}:team-key"
SECRET = key_secret_name(OWNER, "team-key")


# -- delete -------------------------------------------------------------------


def test_delete_closes_holds_and_writes_the_record_in_one_transaction_then_destroys_the_secret(
    client, broker
):
    assert _add_key(client).status_code == 201
    assignment = _hold(broker, ACCOUNT)
    broker.db.commits.clear()

    response = client.delete(f"/v1/accounts/{ACCOUNT}")

    assert response.status_code == 200, response.text
    assert response.json()["secret"] == "destroyed"
    first = broker.db.commits[0]
    assert first == {
        f"{COLLECTION}/{ACCOUNT}",
        f"{HOLD_LOG_COLLECTION}/{assignment}",
        f"key_deletions/{ACCOUNT}",
    }
    assert _doc(broker, HOLD_LOG_COLLECTION, assignment)["end"] == "account_removed"
    # Every version destroyed, THEN the secret deleted.
    assert [c for c, _ in client.secrets.calls] == ["destroy_all_versions", "delete_key_secret"]
    assert client.secrets.deleted == [SECRET]
    record = _doc(broker, "key_deletions", ACCOUNT)
    assert record["done"] is True and record["secret"] == SECRET
    assert record["deleted_by"] == DELETER and record["deleted_at"] is not None


@pytest.mark.parametrize("fail, step", [("fail_destroy", "destroy_versions"),
                                        ("fail_delete", "delete_secret")])
def test_a_failed_destroy_answers_502_naming_the_step_and_leaves_the_record_not_done(
    client, broker, fail, step
):
    assert _add_key(client).status_code == 201
    setattr(client.secrets, fail, PermissionError("refused"))

    response = client.delete(f"/v1/accounts/{ACCOUNT}")

    assert response.status_code == 502, response.text
    assert response.json()["step"] == step
    assert _doc(broker, COLLECTION, ACCOUNT) is None, "the account is removed all the same"
    record = _doc(broker, "key_deletions", ACCOUNT)
    assert record["done"] is False
    assert record["failed_step"] == step and record["error"] == "PermissionError"


def test_the_sweep_finishes_a_record_not_done(client, broker):
    assert _add_key(client).status_code == 201
    client.secrets.fail_delete = PermissionError("refused")
    assert client.delete(f"/v1/accounts/{ACCOUNT}").status_code == 502
    client.secrets.fail_delete = None

    result = _sweep(client)

    assert result["key_deletions"]["finished"] == [ACCOUNT]
    assert _doc(broker, "key_deletions", ACCOUNT)["done"] is True
    assert client.secrets.deleted == [SECRET]
    # Done is done: the next tick does not touch it again.
    calls = list(client.secrets.calls)
    assert _sweep(client)["key_deletions"]["finished"] == []
    assert client.secrets.calls == calls


def test_a_label_with_an_owed_deletion_finishes_it_before_a_new_key_is_stored(client, broker):
    assert _add_key(client).status_code == 201
    client.secrets.fail_destroy = PermissionError("refused")
    assert client.delete(f"/v1/accounts/{ACCOUNT}").status_code == 502

    refused = _add_key(client)
    assert refused.status_code == 422, refused.text
    assert _doc(broker, COLLECTION, ACCOUNT) is None

    client.secrets.fail_destroy = None
    assert _add_key(client).status_code == 201
    assert _doc(broker, "key_deletions", ACCOUNT)["done"] is True


def test_an_imported_keys_delete_leaves_the_workspace_secret_alone(client, broker):
    source = f"swarm-tenant-{OWNER}-anthropic"
    client.secrets.workspace_keys = [(OWNER, "anthropic")]
    client.secrets.seed(source, _anthropic_key())
    client.identity.as_platform()
    imported = client.post("/v1/accounts/import-workspace-keys", json={"added_by": ADMIN})
    assert imported.status_code == 200, imported.text
    account_id = imported.json()["imported"][0]["account_id"]

    response = client.delete(f"/v1/accounts/{account_id}")

    assert response.status_code == 200, response.text
    assert response.json()["workspace_secret"] == f"{source} still holds the value"
    assert client.secrets.deleted == [key_secret_name(OWNER, "workspace-anthropic")]
    assert source not in {name for _, name in client.secrets.calls}
    assert client.secrets.states(source) == ["ENABLED"]
    assert _doc(broker, "key_deletions", account_id)["imported_from"] == source


def test_the_old_name_voice_it_key_has_its_versions_destroyed_and_keeps_an_empty_secret(
    client, broker, accounts
):
    old = secret_name(OWNER, "voice-key")
    accounts.register(OWNER, "voice-key", provider="openai-api", voice_dedicated=True)
    broker.db.collection(COLLECTION).document(f"{OWNER}:voice-key").update({"secret": old})
    client.secrets.seed(old, _openai_key())
    client.secrets.seed(old, _openai_key())
    client.identity.as_platform()

    response = client.delete(f"/v1/accounts/{OWNER}:voice-key")

    assert response.status_code == 200, response.text
    assert response.json()["secret"] == "emptied"
    assert response.json()["versions_destroyed"] == 2
    assert client.secrets.deleted == []
    assert client.secrets.states(old) == ["DESTROYED", "DESTROYED"]


def test_a_plans_removal_still_retains_its_secret(client, broker, accounts):
    accounts.register(OWNER, "team-plan")

    response = client.delete(f"/v1/accounts/{OWNER}:team-plan")

    assert response.status_code == 200, response.text
    assert response.json() == {"removed": f"{OWNER}:team-plan", "secret": "retained"}
    assert client.secrets.calls == []
    assert _doc(broker, "key_deletions", f"{OWNER}:team-plan") is None


# -- rotate -------------------------------------------------------------------


def test_rotate_destroys_the_superseded_version_only_after_the_hold_ttl(client, broker):
    lends = {BORROWER: {"max_agents": 2, "monthly_spend_cap_usd": 5.0}}
    assert _add_key(client, lends=lends).status_code == 201
    assignment = _hold(broker, ACCOUNT)
    before = _doc(broker, COLLECTION, ACCOUNT)
    replacement = _anthropic_key()

    response = _add_key(client, credential=replacement, added_by="dana@example.com")

    assert response.status_code == 201, response.text
    assert response.json()["replaced"] is True
    assert client.secrets.states(SECRET) == ["ENABLED", "ENABLED"]
    assert json.loads(client.secrets.access(SECRET))["api_key"] == replacement
    after = _doc(broker, COLLECTION, ACCOUNT)
    for field in ("holds", "lends", "max_agents", "per_workspace_max", "pools",
                  "state", "monthly_spend_cap_usd", "registered_at"):
        assert after.get(field) == before.get(field), field
    assert after["key_added_by"] == "dana@example.com"
    assert after["key_replaced_at"] is not None
    assert [h["assignment_id"] for h in after["holds"]] == [assignment]

    # Not at once: a sweep inside the TTL leaves both versions.
    _sweep(client)
    assert client.secrets.states(SECRET) == ["ENABLED", "ENABLED"]

    rotation = _doc(broker, "key_rotations", ACCOUNT)
    assert rotation["keep_version"] == 2
    assert rotation["destroy_after"] - rotation["replaced_at"] == DEFAULT_HOLD_TTL
    broker.db.collection("key_rotations").document(ACCOUNT).update(
        {"destroy_after": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    result = _sweep(client)

    assert result["key_rotations"]["destroyed"] == {ACCOUNT: 1}
    assert client.secrets.states(SECRET) == ["DESTROYED", "ENABLED"]
    assert _doc(broker, "key_rotations", ACCOUNT)["done"] is True


# -- resume -------------------------------------------------------------------


@pytest.mark.parametrize("state", ["paused", "draining"])
def test_resume_puts_a_paused_or_draining_key_back_and_clears_its_credit_mark(
    client, broker, accounts, state
):
    assert _add_key(client).status_code == 201
    assert client.put(f"/v1/accounts/{ACCOUNT}/state", json={"state": state}).status_code == 200
    broker.db.collection(COLLECTION).document(ACCOUNT).update(
        {"exhausted_at": datetime.now(timezone.utc)}
    )

    response = client.put(f"/v1/accounts/{ACCOUNT}/state", json={"state": "available"})

    assert response.status_code == 200, response.text
    account = accounts.get(ACCOUNT)
    assert account.state is AccountState.AVAILABLE
    assert account.exhausted_at is None


# -- the hourly exhausted probe ----------------------------------------------


def test_the_probe_returns_a_key_that_serves_again_and_leaves_one_that_does_not(
    client, broker, accounts
):
    serving, spent = _anthropic_key(), _anthropic_key()
    assert _add_key(client, label="serving", credential=serving).status_code == 201
    assert _add_key(client, label="spent", credential=spent).status_code == 201
    marked = datetime.now(timezone.utc) - timedelta(hours=2)
    for label in ("serving", "spent"):
        broker.db.collection(COLLECTION).document(f"{OWNER}:{label}").update({"exhausted_at": marked})
    client.validator.serves[serving] = True

    result = _sweep(client)["exhausted_probe"]

    assert result["served_again"] == [f"{OWNER}:serving"]
    assert result["still_exhausted"] == [f"{OWNER}:spent"]
    assert accounts.get(f"{OWNER}:serving").exhausted_at is None
    assert accounts.get(f"{OWNER}:spent").exhausted_at is not None
    # Once an hour: the next tick does not ask the vendor again.
    asked = len(client.validator.probed)
    assert _sweep(client)["exhausted_probe"]["still_exhausted"] == []
    assert len(client.validator.probed) == asked


def test_a_key_marked_within_the_hour_is_not_probed_yet(client, broker):
    assert _add_key(client).status_code == 201
    broker.db.collection(COLLECTION).document(ACCOUNT).update(
        {"exhausted_at": datetime.now(timezone.utc) - timedelta(minutes=5)}
    )
    _sweep(client)
    assert client.validator.probed == []


def test_the_probe_is_a_one_token_completion_and_a_refusal_is_not_serving():
    import io
    import urllib.error

    from quota_broker import api_key

    key = _anthropic_key()
    sent = []

    class _Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):  # noqa: ANN001
        sent.append(request)
        if len(sent) == 2:
            raise urllib.error.HTTPError(request.full_url, 429, "credit", {}, io.BytesIO(b""))
        return _Answer()

    validator = api_key.HttpKeyValidator(opener=opener)
    assert validator.probe(api_key.ANTHROPIC_API, key) is True
    assert validator.probe(api_key.ANTHROPIC_API, key) is False
    body = json.loads(sent[0].data)
    assert body["max_tokens"] == 1 and sent[0].full_url == api_key.PROBE_URL[api_key.ANTHROPIC_API]


# -- nothing credential-shaped anywhere ----------------------------------------


def test_no_key_value_in_any_answer_record_audit_line_or_log(client, broker, caplog):
    caplog.set_level(logging.DEBUG)
    first, second = _anthropic_key(), _anthropic_key()
    answers = [_add_key(client, credential=first), _add_key(client, credential=second)]
    broker.db.collection(COLLECTION).document(ACCOUNT).update(
        {"exhausted_at": datetime.now(timezone.utc) - timedelta(hours=2)}
    )
    client.validator.serves[second] = True
    client.identity.as_platform()
    answers.append(client.post("/v1/quota/sweep"))
    client.identity.as_tenant(OWNER)
    client.secrets.fail_delete = PermissionError("refused")
    answers.append(client.delete(f"/v1/accounts/{ACCOUNT}"))
    client.secrets.fail_delete = None
    client.identity.as_platform()
    answers.append(client.post("/v1/quota/sweep"))

    stored = json.dumps({path: doc for path, doc in broker.db.docs.items()}, default=str)
    logged = "\n".join(
        f"{r.getMessage()} {json.dumps(r.__dict__, default=str)}" for r in caplog.records
    )
    for key in (first, second):
        for answer in answers:
            assert key not in answer.text
        assert key not in stored
        assert key not in logged
    assert any("audit: a deleted API key's secret was destroyed" in r.getMessage()
               for r in caplog.records)


# -- the store's own guards ----------------------------------------------------


class _Version:
    def __init__(self, name: str, state: str = "ENABLED") -> None:
        self.name = name
        self.state = state


class _Client:
    def __init__(self, secrets_: dict[str, list[_Version]]) -> None:
        self.secrets = secrets_
        self.destroyed: list[str] = []
        self.deleted: list[str] = []

    def list_secret_versions(self, request):
        return list(self.secrets.get(request["parent"].split("/secrets/")[1], []))

    def destroy_secret_version(self, request):
        self.destroyed.append(request["name"])

    def delete_secret(self, request):
        self.deleted.append(request["name"])


def _store(versions: dict[str, list[_Version]]):
    from quota_broker.secretstore import SecretManagerStore

    client = _Client(versions)
    return SecretManagerStore(PROJECT, client=client, project_number="123456789012"), client


@pytest.mark.parametrize("name", [
    secret_name(OWNER, "team-plan"),
    secret_name(OWNER, "team-plan") + "-refresh",
    f"swarm-tenant-{OWNER}-anthropic",
    "agents-shared-key",
    "swarm-account---key-x",
])
def test_the_store_deletes_only_a_keys_own_secret(name):
    store, client = _store({})
    with pytest.raises(ValueError):
        store.delete_key_secret(name)
    assert client.deleted == []


def test_the_store_deletes_a_keys_secret_and_destroys_every_live_version():
    versions = [
        _Version(f"projects/{PROJECT}/secrets/{SECRET}/versions/1", "DESTROYED"),
        _Version(f"projects/{PROJECT}/secrets/{SECRET}/versions/2"),
        _Version(f"projects/{PROJECT}/secrets/{SECRET}/versions/3", "DISABLED"),
    ]
    store, client = _store({SECRET: versions})
    assert store.destroy_all_versions(SECRET) == 2
    assert store.delete_key_secret(SECRET) is True
    assert client.deleted == [f"projects/{PROJECT}/secrets/{SECRET}"]


def test_the_store_never_destroys_a_workspace_secret():
    source = f"swarm-tenant-{OWNER}-anthropic"
    store, client = _store({source: [_Version(f"projects/{PROJECT}/secrets/{source}/versions/1")]})
    with pytest.raises(ValueError):
        store.destroy_all_versions(source)
    assert client.destroyed == []


def test_a_rotation_keeps_the_version_it_wrote():
    versions = [_Version(f"projects/{PROJECT}/secrets/{SECRET}/versions/{n}") for n in (1, 2, 3)]
    store, client = _store({SECRET: versions})
    assert store.destroy_versions_before(SECRET, 3) == 2
    assert client.destroyed == [v.name for v in versions[:2]]
    with pytest.raises(ValueError):
        store.destroy_versions_before(SECRET, 9)
