"""API keys beside plans: the broker's stage (a) of docs/design/llm-providers.md.

The owner's design (merged; ten answers of 9 November 2026, §9) makes a
developer API key the pool's second account kind. Stage (a) of LP-B1 (§8.1)
is what this file holds to:

  * the `anthropic-api` kind and `VENDOR_OF`; a key validated by shape and
    by one vendor call before it is stored, a 401 refusing it and a 429
    adding it as limited;
  * its one secret, `swarm-account-<tenant>--key-<label>`, and a NEW plan's
    label refused when it carries that mark;
  * `exhausted_at`, `per_workspace_max` (both kinds, Q6, enforced where the
    hold commits), `monthly_spend_cap_usd` and `voice_dedicated`;
  * `lends`, a cap on every lend (Q7: "a cap for that lease should be set and
    tracked and enforced"), replacing `lend_to`, with today's lends carried
    over at the account's own `max_agents`;
  * the import of each workspace's own provider key as a pool key (Q7).

Unit tests: the fake Firestore, no credentials, no emulator. Every key and
token here is built at runtime: nothing credential-shaped is a literal.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.accounts import (
    POOL_VOICE_IT,
    POOL_WORKSPACE,
    VENDOR_OF,
    Account,
    AccountError,
    Lend,
    key_secret_name,
    plan_labels_with_key_mark,
    refuse_plan_label,
    validate_lends,
)
from quota_broker.accountstore import COLLECTION, AccountStore

OWNER = "eng"
BORROWER = "research"
PERSON = "owner@example.com"
ADMIN = "root@example.com"
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


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
        self.email = "eng-worker@example-project.iam.gserviceaccount.com"

    def resolve(self, authorization):  # noqa: ANN001
        return (None, True) if self.platform else (self.tenant, False)

    def verified_email(self, authorization):  # noqa: ANN001
        return self.email

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant, self.platform = tenant_id, False

    def as_platform(self) -> None:
        self.tenant, self.platform = None, True


class _Secrets:
    """Secret Manager without a network."""

    def __init__(self) -> None:
        self.latest: dict[str, str] = {}
        self.readers: dict[str, set[str]] = {}
        self.accessors: dict[str, list[str]] = {}
        self.workspace_keys: list[tuple[str, str]] = []

    def access(self, name):  # noqa: ANN001
        return self.latest[name]

    def ensure_secret(self, name, *, labels, accessors, region=None):  # noqa: ANN001
        self.readers.setdefault(name, set())
        self.accessors[name] = list(accessors)

    def add_version(self, name, value):  # noqa: ANN001
        self.latest[name] = value

    def set_worker_readers(self, name, *, readers, manages, revoke=True):  # noqa: ANN001
        before = self.readers.get(name, set())
        after = set(readers) if revoke else before | set(readers)
        self.readers[name] = after
        return sorted(after - before), sorted(before - after)

    def workspace_provider_keys(self):
        return list(self.workspace_keys)


class _Validator:
    """The vendor's list-models call, answered from a table: a key -> outcome."""

    def __init__(self) -> None:
        from quota_broker import api_key

        self.answers: dict[str, object] = {}
        self.default = api_key.KeyCheck(api_key.CHECK_VALID)
        self.calls: list[str] = []

    def check(self, provider, key):  # noqa: ANN001
        self.calls.append(provider)
        answer = self.answers.get(key, self.default)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture()
def accounts(broker) -> AccountStore:
    return AccountStore(broker.db)


@pytest.fixture()
def client(broker, accounts, monkeypatch):
    from fastapi.testclient import TestClient

    from quota_broker.main import create_app

    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.setenv("VOICE_SERVICE_ACCOUNTS", "swarm-voice@example-project.iam.gserviceaccount.com")
    monkeypatch.setenv("VOICE_IT_PEOPLE", PERSON)
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


def _worker(tenant: str) -> str:
    return f"serviceAccount:swarm-worker-{tenant}@example-project.iam.gserviceaccount.com"


def _add_key(client, *, provider="anthropic-api", label="team-key", credential=None,
             owner=OWNER, **extra):
    body = {
        "owner_tenant": owner,
        "label": label,
        "provider": provider,
        "credential": credential if credential is not None else _anthropic_key(),
        "added_by": ADMIN,
        **extra,
    }
    return client.post("/v1/accounts", json=body)


def _plan_credential() -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=8)
    return json.dumps({"claudeAiOauth": {
        "accessToken": "at-" + secrets.token_hex(12),
        "refreshToken": "rt-" + secrets.token_hex(12),
        "expiresAt": int(expires.timestamp() * 1000)}})


def _doc(broker, account_id: str) -> dict:
    return broker.db.collection(COLLECTION).document(account_id).get().to_dict()


# -- the kind, the vendor table and the key's shape ---------------------------


def test_every_provider_has_its_vendor_and_a_key_shares_its_plans_vendor():
    assert VENDOR_OF == {
        "anthropic": "anthropic", "anthropic-api": "anthropic",
        "openai": "openai", "openai-api": "openai",
    }
    key = Account(account_id="eng:k", owner_tenant=OWNER, label="k", provider="anthropic-api")
    assert key.vendor == "anthropic" and key.is_api_key
    # A key has no window to read and no pair to refresh.
    assert key.measured is False and key.refreshes is False


def test_an_anthropic_key_is_parsed_by_shape_and_a_refusal_never_quotes_it():
    from quota_broker import api_key
    from quota_broker.oauth import CredentialError

    good = _anthropic_key()
    assert api_key.parse_api_key("anthropic-api", f"  {good}\n") == good
    for pasted in (_openai_key(), "sk-ant-" + "x", "sk-ant-" + secrets.token_hex(8) + " tail",
                   "eyJ" + secrets.token_hex(24)):
        with pytest.raises(CredentialError) as refused:
            api_key.parse_api_key("anthropic-api", pasted)
        assert pasted not in str(refused.value)
    # The OpenAI kind keeps its own check.
    with pytest.raises(CredentialError):
        api_key.parse_api_key("openai-api", good.replace("sk-", "pk-", 1))
    assert api_key.read_key("anthropic-api", api_key.serialise("anthropic-api", good)) == good


@pytest.mark.parametrize("status, outcome", [(200, "valid"), (401, "refused"),
                                             (403, "refused"), (429, "limited")])
def test_the_vendors_status_decides_the_add(status, outcome):
    from quota_broker import api_key

    assert api_key.check_from_status(status).outcome == outcome
    assert api_key.check_from_status(429, "120").retry_after == 120
    assert api_key.check_from_status(429).retry_after == api_key.DEFAULT_RETRY_AFTER_SECONDS


def test_a_vendor_that_cannot_answer_neither_accepts_nor_refuses():
    from quota_broker import api_key

    with pytest.raises(api_key.KeyCheckUnavailable):
        api_key.check_from_status(503)


def test_the_validator_sends_each_vendors_header_and_never_the_key_in_an_error():
    import urllib.error

    from quota_broker import api_key

    key = _anthropic_key()
    seen = []

    def opener(request, timeout):  # noqa: ANN001
        seen.append((request.full_url, dict(request.header_items())))
        raise urllib.error.URLError("unreachable")

    with pytest.raises(api_key.KeyCheckUnavailable) as failed:
        api_key.HttpKeyValidator(opener=opener).check("anthropic-api", key)
    assert key not in str(failed.value)
    url, headers = seen[0]
    assert url == api_key.MODELS_URL["anthropic-api"]
    assert headers["X-api-key"] == key and headers["Anthropic-version"] == api_key.ANTHROPIC_VERSION


# -- the secret name and the plan-label refusal --------------------------------


def test_a_keys_secret_carries_the_key_mark():
    assert key_secret_name(OWNER, "team-key") == "swarm-account-eng--key-team-key"
    with pytest.raises(AccountError):
        key_secret_name("a--b", "team-key")


@pytest.mark.parametrize("label", ["key-team", "key-1", "team--key-x"])
def test_a_plan_label_with_the_key_mark_is_refused(label):
    with pytest.raises(AccountError):
        refuse_plan_label(label)
    assert refuse_plan_label("team-key") == "team-key"


def test_the_plans_already_carrying_the_mark_are_listed():
    plan = Account(account_id="eng:key-old", owner_tenant=OWNER, label="key-old")
    key = Account(account_id="eng:key-k", owner_tenant=OWNER, label="key-k", provider="anthropic-api")
    clean = Account(account_id="eng:team", owner_tenant=OWNER, label="team")
    assert plan_labels_with_key_mark([plan, key, clean]) == ["eng:key-old"]


def test_a_new_plan_with_the_key_mark_is_refused_at_registration(client, accounts):
    client.identity.as_platform()
    refused = client.post("/v1/accounts", json={
        "owner_tenant": OWNER, "label": "key-team", "provider": "anthropic",
        "credential": _plan_credential()})
    assert refused.status_code == 422, refused.text
    assert "key-" in refused.json()["message"]
    assert accounts.list() == [] and client.secrets.latest == {}
    signin = client.post("/v1/accounts/authorize", json={
        "owner_tenant": OWNER, "label": "key-team", "provider": "anthropic"})
    assert signin.status_code == 422, signin.text


# -- the api-key registration path --------------------------------------------


def test_an_anthropic_key_is_stored_in_its_key_secret_and_never_answered(client, accounts, caplog):
    key = _anthropic_key()
    client.identity.as_tenant(OWNER)
    with caplog.at_level("DEBUG"):
        added = _add_key(client, credential=key)
    assert added.status_code == 201, added.text
    assert key not in added.text and key not in caplog.text
    account = accounts.get(f"{OWNER}:team-key")
    assert account.provider == "anthropic-api"
    assert account.voice_dedicated is False
    assert account.pools == (POOL_WORKSPACE,)
    assert account.lends == {} and account.lend_to == ()
    secret = accounts.secret_for(account)
    assert secret == "swarm-account-eng--key-team-key"
    assert key in client.secrets.latest[secret]
    # No `-refresh` half; the owner's worker reads it, as it reads a plan's.
    assert set(client.secrets.latest) == {secret}
    assert _worker(OWNER) in client.secrets.accessors[secret]
    assert client.validator.calls == ["anthropic-api"]
    served = added.json()["account"]
    assert served["kind"] == "api_key" and served["vendor"] == "anthropic"
    assert served["measured"] is False


def test_a_key_the_provider_refuses_is_not_stored(client, accounts):
    from quota_broker import api_key

    key = _anthropic_key()
    client.validator.answers[key] = api_key.KeyCheck(api_key.CHECK_REFUSED)
    refused = _add_key(client, credential=key)
    assert refused.status_code == 422, refused.text
    assert refused.json()["message"].startswith("the provider refused this key")
    assert key not in refused.text
    assert accounts.list() == [] and client.secrets.latest == {}


def test_a_key_that_cannot_be_checked_is_not_stored(client, accounts):
    from quota_broker import api_key

    key = _anthropic_key()
    client.validator.answers[key] = api_key.KeyCheckUnavailable("the provider answered HTTP 503")
    refused = _add_key(client, credential=key)
    assert refused.status_code == 422, refused.text
    assert accounts.list() == [] and client.secrets.latest == {}


def test_a_key_the_provider_rate_limits_is_added_as_limited(client, accounts):
    from quota_broker import api_key

    key = _anthropic_key()
    client.validator.answers[key] = api_key.KeyCheck(api_key.CHECK_LIMITED, retry_after=600)
    added = _add_key(client, credential=key)
    assert added.status_code == 201, added.text
    account = accounts.get(f"{OWNER}:team-key")
    assert account.limited_until is not None
    assert account.limited_until > datetime.now(timezone.utc) + timedelta(minutes=9)


def test_a_key_is_lent_only_with_caps(client, accounts):
    bare = _add_key(client, lend_to=[BORROWER])
    assert bare.status_code == 422, bare.text
    no_spend = _add_key(client, lends={BORROWER: {"max_agents": 2}})
    assert no_spend.status_code == 422, no_spend.text
    assert "monthly_spend_cap_usd" in no_spend.json()["message"]
    assert accounts.list() == [] and client.secrets.latest == {}

    lent = _add_key(client, lends={BORROWER: {"max_agents": 2, "monthly_spend_cap_usd": 40}})
    assert lent.status_code == 201, lent.text
    account = accounts.get(f"{OWNER}:team-key")
    assert account.lends == {BORROWER: Lend(2, 40.0, cap_set=True)}
    assert account.lend_to == (BORROWER,)
    assert _worker(BORROWER) in client.secrets.readers[accounts.secret_for(account)]
    assert lent.json()["account"]["lends"] == {
        BORROWER: {"max_agents": 2, "monthly_spend_cap_usd": 40.0, "cap_set": True}}


def test_sharing_a_key_with_voice_it_is_a_platform_admins(client, accounts):
    client.identity.as_tenant(OWNER)
    refused = _add_key(client, share_with_voice_it=True)
    assert refused.status_code == 403, refused.text
    client.identity.as_platform()
    shared = _add_key(client, provider="openai-api", credential=_openai_key(),
                      voice_dedicated=False, share_with_voice_it=True)
    assert shared.status_code == 201, shared.text
    account = accounts.get(f"{OWNER}:team-key")
    assert account.pools == (POOL_WORKSPACE, POOL_VOICE_IT)
    assert account.voice_dedicated is False


def test_there_is_no_dedicated_anthropic_key_and_one_dedicated_openai_key(client, accounts):
    client.identity.as_platform()
    anthropic = _add_key(client, voice_dedicated=True)
    assert anthropic.status_code == 422, anthropic.text
    first = _add_key(client, provider="openai-api", label="voice-key", credential=_openai_key())
    assert first.status_code == 201, first.text
    assert accounts.get(f"{OWNER}:voice-key").voice_dedicated is True
    # The dedicated key is the broker's alone, under the key mark.
    secret = accounts.secret_for(accounts.get(f"{OWNER}:voice-key"))
    assert secret == "swarm-account-eng--key-voice-key"
    assert client.secrets.accessors[secret] == [
        "serviceAccount:swarm-quota-broker@example-project.iam.gserviceaccount.com"]
    second = _add_key(client, provider="openai-api", label="voice-key-2", credential=_openai_key())
    assert second.status_code == 422, second.text
    assert "already the dedicated Voice it key" in second.json()["message"]


def test_an_openai_key_registered_before_the_field_reads_as_the_dedicated_one(accounts, broker):
    stored = accounts.register(OWNER, "voice-key", provider="openai-api")
    raw = _doc(broker, stored.account_id)
    del raw["voice_dedicated"]
    broker.db.collection(COLLECTION).document(stored.account_id).set(raw)
    assert accounts.get(stored.account_id).voice_dedicated is True
    plan = accounts.register(OWNER, "team", provider="anthropic")
    assert accounts.get(plan.account_id).voice_dedicated is False


# -- the fields, stored and read ------------------------------------------------


def test_each_new_field_is_stored_and_read_back():
    account = Account(
        account_id="eng:k", owner_tenant=OWNER, label="k", provider="anthropic-api",
        lends={BORROWER: Lend(3, 25.5)},
        per_workspace_max={OWNER: 2, BORROWER: 1},
        monthly_spend_cap_usd=200.0,
        exhausted_at=NOW,
        voice_dedicated=False,
    )
    stored = account.to_firestore()
    assert stored["lend_to"] == [BORROWER]
    assert stored["lends"] == {BORROWER: {"max_agents": 3, "monthly_spend_cap_usd": 25.5,
                                          "cap_set": True}}
    read = Account.from_firestore(stored)
    assert read.lends == {BORROWER: Lend(3, 25.5)}
    assert read.per_workspace_max == {OWNER: 2, BORROWER: 1}
    assert read.monthly_spend_cap_usd == 200.0
    assert read.exhausted_at == NOW
    assert read.voice_dedicated is False


@pytest.mark.parametrize("field, bad", [
    ("lends", {BORROWER: {"monthly_spend_cap_usd": 5}}),
    ("lends", {BORROWER: {"max_agents": 0, "monthly_spend_cap_usd": 5}}),
    ("lends", ["research"]),
    ("per_workspace_max", {OWNER: 0}),
    ("per_workspace_max", {OWNER: "two"}),
    ("monthly_spend_cap_usd", -1),
    ("monthly_spend_cap_usd", "lots"),
])
def test_a_malformed_cap_takes_the_account_out_of_the_pool(accounts, broker, field, bad):
    """A cap nobody chose must not be read as no cap."""
    accounts.register(OWNER, "k", provider="anthropic-api")
    broker.db.collection(COLLECTION).document(f"{OWNER}:k").update({field: bad})
    assert accounts.list() == []


def test_every_lend_carries_a_cap():
    with pytest.raises(AccountError, match="carries no cap"):
        validate_lends({BORROWER: {}}, owner_tenant=OWNER, key=False)
    with pytest.raises(AccountError, match="monthly_spend_cap_usd"):
        validate_lends({BORROWER: {"max_agents": 1}}, owner_tenant=OWNER, key=True)
    with pytest.raises(AccountError, match="flat"):
        validate_lends({BORROWER: {"max_agents": 1, "monthly_spend_cap_usd": 5}},
                       owner_tenant=OWNER, key=False)
    with pytest.raises(AccountError, match="own workspace"):
        validate_lends({OWNER: {"max_agents": 1}}, owner_tenant=OWNER, key=False)
    assert validate_lends({BORROWER: {"max_agents": 4}}, owner_tenant=OWNER, key=False) == {
        BORROWER: Lend(4, None, cap_set=True)}


# -- today's lends, carried over -------------------------------------------------


def test_a_document_lent_before_lends_reads_with_the_accounts_own_cap(accounts, broker):
    accounts.register(OWNER, "team", lend_to=[BORROWER])
    accounts.set_max_agents(f"{OWNER}:team", 4)
    raw = _doc(broker, f"{OWNER}:team")
    del raw["lends"]
    broker.db.collection(COLLECTION).document(f"{OWNER}:team").set(raw)

    account = accounts.get(f"{OWNER}:team")
    assert account.lends == {BORROWER: Lend(4, None, cap_set=False)}
    assert account.may_serve(BORROWER)


def test_the_carry_over_route_writes_the_cap_and_lists_marked_plans(client, accounts, broker):
    accounts.register(OWNER, "team", lend_to=[BORROWER])
    accounts.register(OWNER, "key-old")  # registered before the refusal existed
    raw = _doc(broker, f"{OWNER}:team")
    del raw["lends"]
    broker.db.collection(COLLECTION).document(f"{OWNER}:team").set(raw)

    client.identity.as_tenant(OWNER)
    assert client.post("/v1/accounts/carry-over-lends").status_code == 403
    client.identity.as_platform()
    answer = client.post("/v1/accounts/carry-over-lends")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"carried_over": [f"{OWNER}:team"],
                             "plans_with_key_mark": [f"{OWNER}:key-old"]}
    assert _doc(broker, f"{OWNER}:team")["lends"] == {
        BORROWER: {"max_agents": 6, "monthly_spend_cap_usd": None, "cap_set": False}}
    again = client.post("/v1/accounts/carry-over-lends").json()
    assert again["carried_over"] == []


# -- the lending route ------------------------------------------------------------


def _plan(client, accounts, label="team"):
    account = accounts.register(OWNER, label)
    client.secrets.latest[accounts.secret_for(account)] = secrets.token_hex(16)
    return account.account_id


def test_a_plan_lent_with_caps_stores_them(client, accounts):
    account_id = _plan(client, accounts)
    client.identity.as_tenant(OWNER)
    lent = client.put(f"/v1/accounts/{account_id}/lending",
                      json={"lends": {BORROWER: {"max_agents": 2}}})
    assert lent.status_code == 200, lent.text
    account = accounts.get(account_id)
    assert account.lends == {BORROWER: Lend(2, None, cap_set=True)}
    assert account.lend_to == (BORROWER,)
    assert _worker(BORROWER) in client.secrets.readers[accounts.secret_for(account)]


def test_a_plans_bare_list_is_carried_over_and_keeps_a_set_cap(client, accounts):
    account_id = _plan(client, accounts)
    client.identity.as_tenant(OWNER)
    client.put(f"/v1/accounts/{account_id}/lending",
               json={"lends": {BORROWER: {"max_agents": 2}}})
    widened = client.put(f"/v1/accounts/{account_id}/lending",
                         json={"lend_to": [BORROWER, "ops"]})
    assert widened.status_code == 200, widened.text
    assert accounts.get(account_id).lends == {
        BORROWER: Lend(2, None, cap_set=True),
        "ops": Lend(6, None, cap_set=False),
    }
    both = client.put(f"/v1/accounts/{account_id}/lending",
                      json={"lend_to": [BORROWER], "lends": {BORROWER: {"max_agents": 1}}})
    assert both.status_code == 422, both.text


def test_a_lend_without_a_cap_is_refused(client, accounts):
    account_id = _plan(client, accounts)
    client.identity.as_tenant(OWNER)
    refused = client.put(f"/v1/accounts/{account_id}/lending", json={"lends": {BORROWER: {}}})
    assert refused.status_code == 422, refused.text
    assert accounts.get(account_id).lends == {}


def test_a_keys_lending_needs_a_spend_cap(client, accounts):
    assert _add_key(client).status_code == 201
    account_id = f"{OWNER}:team-key"
    client.identity.as_tenant(OWNER)
    bare = client.put(f"/v1/accounts/{account_id}/lending", json={"lend_to": [BORROWER]})
    assert bare.status_code == 422, bare.text
    lent = client.put(f"/v1/accounts/{account_id}/lending", json={
        "lends": {BORROWER: {"max_agents": 1, "monthly_spend_cap_usd": 10}}})
    assert lent.status_code == 200, lent.text
    assert accounts.get(account_id).lends == {BORROWER: Lend(1, 10.0)}


# -- per_workspace_max ---------------------------------------------------------------


def test_per_workspace_max_is_set_on_either_kind_and_enforced_where_the_hold_commits(
    client, accounts, broker
):
    from quota_broker.main import acquire_hold

    account_id = _plan(client, accounts)
    client.identity.as_tenant(OWNER)
    client.put(f"/v1/accounts/{account_id}/lending",
               json={"lends": {BORROWER: {"max_agents": 3}}})
    stranger = client.put(f"/v1/accounts/{account_id}/per-workspace-max",
                          json={"per_workspace_max": {"ops": 1}})
    assert stranger.status_code == 422, stranger.text
    bad = client.put(f"/v1/accounts/{account_id}/per-workspace-max",
                     json={"per_workspace_max": {OWNER: 0}})
    assert bad.status_code == 422, bad.text
    capped = client.put(f"/v1/accounts/{account_id}/per-workspace-max",
                        json={"per_workspace_max": {OWNER: 1}})
    assert capped.status_code == 200, capped.text
    assert capped.json()["account"]["per_workspace_max"] == {OWNER: 1}

    broker.db.collection(COLLECTION).document(account_id).update({"observed_at": NOW})
    assert acquire_hold(broker.db, account_id, tenant_id=OWNER, now=NOW) is not None
    # The owner is at its cap; its second agent is refused and nothing counted.
    assert acquire_hold(broker.db, account_id, tenant_id=OWNER, now=NOW) is None
    # A borrower is not: the account's own `max_agents` bounds everyone together.
    assert acquire_hold(broker.db, account_id, tenant_id=BORROWER, now=NOW) is not None
    assert len(accounts.get(account_id).holds) == 2
    assert accounts.get(account_id).is_full_for(OWNER, NOW)


# -- monthly_spend_cap_usd -------------------------------------------------------------


def test_a_keys_spend_cap_is_set_and_cleared_and_a_plan_has_none(client, accounts):
    assert _add_key(client).status_code == 201
    key_id = f"{OWNER}:team-key"
    client.identity.as_tenant(OWNER)
    capped = client.put(f"/v1/accounts/{key_id}/spend-cap", json={"monthly_spend_cap_usd": 150})
    assert capped.status_code == 200, capped.text
    assert accounts.get(key_id).monthly_spend_cap_usd == 150.0
    for bad in (0, -5, 2_000_000):
        refused = client.put(f"/v1/accounts/{key_id}/spend-cap", json={"monthly_spend_cap_usd": bad})
        assert refused.status_code == 422, refused.text
    cleared = client.put(f"/v1/accounts/{key_id}/spend-cap", json={"monthly_spend_cap_usd": None})
    assert cleared.status_code == 200 and accounts.get(key_id).monthly_spend_cap_usd is None
    plan_id = _plan(client, accounts)
    plan = client.put(f"/v1/accounts/{plan_id}/spend-cap", json={"monthly_spend_cap_usd": 5})
    assert plan.status_code == 422, plan.text


# -- exhausted_at -----------------------------------------------------------------------


def test_an_exhausted_report_marks_a_key_and_never_a_plan(accounts):
    key = accounts.register(OWNER, "k", provider="anthropic-api")
    plan = accounts.register(OWNER, "team")
    assert accounts.record_health(key.account_id, "exhausted", tenant_id=OWNER)
    assert accounts.record_health(plan.account_id, "exhausted", tenant_id=OWNER)
    assert accounts.get(key.account_id).exhausted_at is not None
    assert accounts.get(plan.account_id).exhausted_at is None


# -- the import of the workspaces' own keys (Q7) ------------------------------------


def test_each_workspace_key_is_imported_as_a_pool_key_and_the_rest_are_listed(
    client, accounts, broker, caplog
):
    from quota_broker import api_key

    good = _anthropic_key()
    refused = _openai_key()
    client.secrets.latest.update({
        "swarm-tenant-eng-anthropic": good,
        # A subscription token, not a key: skipped, never registered.
        "swarm-tenant-research-anthropic": "at-" + secrets.token_hex(16),
        "swarm-tenant-ops-openai": refused,
    })
    client.secrets.workspace_keys = [
        ("eng", "anthropic"), ("ops", "openai"), ("research", "anthropic")]
    client.validator.answers[refused] = api_key.KeyCheck(api_key.CHECK_REFUSED)

    client.identity.as_tenant(OWNER)
    body = {"added_by": ADMIN}
    assert client.post("/v1/accounts/import-workspace-keys", json=body).status_code == 403
    client.identity.as_platform()
    with caplog.at_level("DEBUG"):
        answer = client.post("/v1/accounts/import-workspace-keys", json=body)
    assert answer.status_code == 200, answer.text
    assert good not in answer.text and good not in caplog.text and refused not in answer.text
    report = answer.json()
    assert [i["account_id"] for i in report["imported"]] == ["eng:workspace-anthropic"]
    assert sorted((s["tenant_id"], s["reason"]) for s in report["skipped"]) == [
        ("ops", "the provider refused this key; nothing was stored"),
        ("research", "not an API key"),
    ]

    account = accounts.get("eng:workspace-anthropic")
    assert account.provider == "anthropic-api" and account.owner_tenant == "eng"
    assert account.lends == {} and account.voice_dedicated is False
    secret = accounts.secret_for(account)
    assert secret == "swarm-account-eng--key-workspace-anthropic"
    assert good in client.secrets.latest[secret]
    # A copy: the workspace's own secret is untouched, still the outage path.
    assert client.secrets.latest["swarm-tenant-eng-anthropic"] == good
    assert _doc(broker, account.account_id)["key_imported_from"] == "swarm-tenant-eng-anthropic"
    assert accounts.get("ops:workspace-openai") is None
    assert accounts.get("research:workspace-anthropic") is None

    again = client.post("/v1/accounts/import-workspace-keys", json=body).json()
    assert again["imported"] == []
    assert ("eng", "already imported") in [(s["tenant_id"], s["reason"]) for s in again["skipped"]]
