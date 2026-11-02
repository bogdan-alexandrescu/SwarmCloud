"""ChatGPT-plan pool accounts, the broker's half (#2688).

The owner chose (BGD, 2026-11-01) that codex runs on ChatGPT plans from a
shared pool, the way claude-code runs on Claude subscriptions. What has to
hold:

  * THE ROTATED REFRESH TOKEN IS STORED BEFORE THE ACCESS TOKEN LEAVES. Every
    plan refresh replaces the refresh token; publishing first and dying would
    strand the plan with a refresh token nobody holds.
  * ONE EXCHANGE PER ACCOUNT AT A TIME, because two exchanges of one refresh
    token lock the plan out.
  * A REFUSED REFRESH IS `reauth_required`, and nothing is written.
  * THE WORKER IS HANDED THE ACCESS TOKEN AND THE ACCOUNT ID, NEVER THE
    REFRESH TOKEN: assign names `{base}`, which holds exactly those two.

Every token here is built at runtime: nothing credential-shaped is a literal.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker import openai_plan
from quota_broker.accounts import AccountState, secret_name
from quota_broker.accountstore import AccountStore
from quota_broker.credentials import REFRESH_SUFFIX, CredentialRefresher
from quota_broker.oauth import ReauthRequired

PROJECT = "test-project"
TENANT = "eng"
NOW = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def fake_jwt(claims: dict) -> str:
    """A JWT-shaped string with these claims and a random signature."""
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.{secrets.token_hex(8)}"


def id_token(account_id: str, user: str = "user-1", plan: str = "plus") -> str:
    return fake_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user,
                "chatgpt_plan_type": plan,
            }
        }
    )


class _Log:
    def __init__(self):
        self.records = []

    def _add(self, level, msg, *a, extra=None, **k):
        self.records.append((level, msg, extra or {}))

    def info(self, msg, *a, **k):
        self._add("info", msg, *a, **k)

    def warning(self, msg, *a, **k):
        self._add("warning", msg, *a, **k)

    def error(self, msg, *a, **k):
        self._add("error", msg, *a, **k)

    def text(self) -> str:
        return json.dumps(self.records, default=str)


class _Store:
    """Secret Manager without a network, recording every write IN ORDER."""

    def __init__(self, initial: dict[str, str]):
        self.latest = dict(initial)
        self.writes: list[tuple[str, str]] = []

    def access(self, name):
        if name not in self.latest:
            raise KeyError(name)
        return self.latest[name]

    def add_version(self, name, payload):
        self.writes.append((name, payload))
        self.latest[name] = payload


class _PlanEndpoint:
    def __init__(self, *, rotate: bool = True, error: Exception | None = None, delay: float = 0.0):
        self.presented: list[str] = []
        self.rotate = rotate
        self.error = error
        self.delay = delay
        self.active = 0
        self.overlapped = False
        self.issued: list[dict] = []

    def exchange(self, refresh_token):
        self.active += 1
        self.overlapped = self.overlapped or self.active > 1
        try:
            if self.delay:
                time.sleep(self.delay)
            self.presented.append(refresh_token)
            if self.error is not None:
                raise self.error
            answer = {"access_token": secrets.token_hex(16), "expires_in": 3600}
            if self.rotate:
                answer["refresh_token"] = secrets.token_hex(16)
            self.issued.append(answer)
            return answer
        finally:
            self.active -= 1

    def redeem(self, *, code, verifier, redirect_uri, state=""):
        raise AssertionError("not a sign-in")


BASE = "swarm-account-eng-plan"


def _stored_pair(*, expires_at: datetime, account_id: str | None = None) -> dict:
    return {
        "kind": openai_plan.KIND,
        "access_token": secrets.token_hex(16),
        "refresh_token": secrets.token_hex(16),
        "account_id": account_id or secrets.token_hex(6),
        "expires_at": int(expires_at.timestamp() * 1000),
    }


def _refresher(store, endpoint, *, now=NOW):
    return CredentialRefresher(
        store,
        endpoint=None,  # the Claude endpoint: a plan never reaches it
        plan_endpoint=endpoint,
        logger=_Log(),
        now=lambda: now,
    )


# -- refresh rotation ------------------------------------------------------------


def test_the_rotated_refresh_token_is_stored_before_the_access_token_is_published():
    pair = _stored_pair(expires_at=NOW + timedelta(minutes=5))
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": json.dumps(pair)})
    endpoint = _PlanEndpoint()

    outcome = _refresher(store, endpoint).refresh_secret(BASE, label="eng:plan")

    assert outcome.reason == "refreshed", outcome
    # The OLD refresh token was presented, exactly once.
    assert endpoint.presented == [pair["refresh_token"]]
    issued = endpoint.issued[0]
    names = [name for name, _ in store.writes]
    # MUTATION: publishing `{base}` first leaves a window where the rotated
    # refresh token exists only in this process.
    assert names == [f"{BASE}{REFRESH_SUFFIX}", BASE], names
    kept = json.loads(store.writes[0][1])
    assert kept["refresh_token"] == issued["refresh_token"]
    assert kept["kind"] == openai_plan.KIND
    assert kept["account_id"] == pair["account_id"]
    published = json.loads(store.writes[1][1])
    assert published == {"access_token": issued["access_token"], "account_id": pair["account_id"]}


def test_the_worker_facing_secret_never_holds_a_refresh_token():
    pair = _stored_pair(expires_at=NOW + timedelta(minutes=5))
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": json.dumps(pair)})
    endpoint = _PlanEndpoint()
    _refresher(store, endpoint).refresh_secret(BASE, label="eng:plan")
    published = store.latest[BASE]
    assert pair["refresh_token"] not in published
    assert endpoint.issued[0]["refresh_token"] not in published
    assert "refresh" not in published


def test_an_unrotated_refresh_token_is_carried_forward():
    pair = _stored_pair(expires_at=NOW + timedelta(minutes=5))
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": json.dumps(pair)})
    _refresher(store, _PlanEndpoint(rotate=False)).refresh_secret(BASE)
    assert json.loads(store.latest[f"{BASE}{REFRESH_SUFFIX}"])["refresh_token"] == pair["refresh_token"]


def test_a_plan_token_with_most_of_its_hour_left_is_not_exchanged():
    """The Claude window (three hours) would exchange an hour-long plan token
    on every sweep; a plan's own window is half its life."""
    pair = _stored_pair(expires_at=NOW + timedelta(minutes=50))
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": json.dumps(pair)})
    endpoint = _PlanEndpoint()
    outcome = _refresher(store, endpoint).refresh_secret(BASE)
    assert endpoint.presented == []
    assert outcome.reason in ("published", "published_unverified", "still_valid"), outcome
    # Published as the plan's pair of access token and account id.
    assert json.loads(store.latest[BASE]) == {
        "access_token": pair["access_token"],
        "account_id": pair["account_id"],
    }


def test_two_refreshes_of_one_plan_never_overlap_and_only_one_exchanges():
    """The sweep and an operator's Refresh racing one plan: each would present
    the same refresh token, and the loser would be refused."""
    pair = _stored_pair(expires_at=NOW + timedelta(minutes=5))
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": json.dumps(pair)})
    endpoint = _PlanEndpoint(delay=0.2)
    refresher = _refresher(store, endpoint)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(refresher.refresh_secret(BASE)))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not endpoint.overlapped
    # The second waited, then found a fresh token and exchanged nothing.
    assert endpoint.presented == [pair["refresh_token"]]
    assert sorted(r.reason for r in results) == ["refreshed", "still_valid"]


# -- a refused refresh -----------------------------------------------------------


def test_a_refused_refresh_is_reauth_required_and_writes_nothing():
    pair = _stored_pair(expires_at=NOW + timedelta(minutes=5))
    store = _Store({f"{BASE}{REFRESH_SUFFIX}": json.dumps(pair)})
    refresher = _refresher(store, _PlanEndpoint(error=ReauthRequired("refused (400)")))
    outcome = refresher.refresh_secret(BASE, label="eng:plan")
    assert outcome.reason == "reauth_required"
    assert store.writes == []


def test_the_http_endpoint_reads_a_400_as_a_dead_grant_and_quotes_nothing(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    refresh_token = secrets.token_hex(16)
    sent = {}

    def refuse(request, timeout=None):
        sent["body"] = request.data.decode()
        sent["type"] = request.headers.get("Content-type")
        body = io.BytesIO(json.dumps({"error": "invalid_grant", "echo": refresh_token}).encode())
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, body)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(ReauthRequired) as refused:
        openai_plan.HttpPlanTokenEndpoint().exchange(refresh_token)
    assert refresh_token not in str(refused.value)
    # swarm-voice's approach: a FORM POST with grant_type, client_id, refresh_token.
    assert sent["type"] == "application/x-www-form-urlencoded"
    assert "grant_type=refresh_token" in sent["body"]
    assert f"client_id={openai_plan.CODEX_CLIENT_ID}" in sent["body"]


def test_a_5xx_is_a_transient_failure_not_a_dead_grant(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    from quota_broker.oauth import CredentialError

    def fail(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 503, "x", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(CredentialError) as failed:
        openai_plan.HttpPlanTokenEndpoint().exchange(secrets.token_hex(8))
    assert not isinstance(failed.value, ReauthRequired)


# -- the routes: sign-in, assign, the refresh button ------------------------------


class _Secrets:
    def __init__(self):
        self.versions, self.created, self.accessors = {}, {}, {}

    def ensure_secret(self, name, *, labels, accessors, region):
        self.created[name] = dict(labels)
        self.accessors.setdefault(name, []).extend(accessors)
        return True

    def set_worker_readers(self, name, *, readers, manages, revoke=True):
        present = self.accessors.setdefault(name, [])
        added = [m for m in readers if m not in present]
        present.extend(added)
        return added, []

    def add_version(self, name, payload):
        if name not in self.created:
            raise KeyError(name)
        self.versions.setdefault(name, []).append(payload)

    def access(self, name):
        if not self.versions.get(name):
            raise KeyError(name)
        return self.versions[name][-1]


class _SignInEndpoint:
    def __init__(self):
        self.calls = []
        self.account_id = secrets.token_hex(6)
        self.answer = {
            "access_token": secrets.token_hex(16),
            "refresh_token": secrets.token_hex(16),
            "id_token": id_token(self.account_id),
            "expires_in": 3600,
        }

    def redeem(self, *, code, verifier, redirect_uri, state=""):
        self.calls.append({"code": code, "verifier": verifier, "redirect_uri": redirect_uri})
        return dict(self.answer)

    def exchange(self, refresh_token):
        raise ReauthRequired("refused (401)")


class _Identity:
    def __init__(self):
        self.tenant, self.platform = TENANT, False

    def resolve(self, authorization):
        return (None, True) if self.platform else (self.tenant, False)


@pytest.fixture()
def client(broker, monkeypatch):
    from fastapi.testclient import TestClient

    from quota_broker.main import create_app

    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("BROKER_SERVICE_ACCOUNT", "swarm-quota-broker@p.iam.gserviceaccount.com")
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE", "swarm-agent-worker-{tenant}@p.iam.gserviceaccount.com"
    )
    identity = _Identity()
    endpoint = _SignInEndpoint()
    app = create_app(
        broker,
        identity=identity,
        account_store=AccountStore(broker.db),
        plan_token_endpoint=endpoint,
    )
    app.state.secret_store = _Secrets()
    c = TestClient(app, raise_server_exceptions=False)
    c.app_ref, c.identity, c.endpoint = app, identity, endpoint
    return c


def _sign_in(client, label="plan"):
    started = client.post(
        "/v1/accounts/authorize",
        json={"owner_tenant": TENANT, "label": label, "provider": "openai"},
    )
    assert started.status_code == 200, started.text
    body = started.json()
    code = secrets.token_hex(8)
    done = client.post(
        "/v1/accounts/exchange",
        json={"state": body["state"], "code": code, "expected_owner": TENANT},
    )
    return body, code, done


def test_a_plan_sign_in_opens_openais_page_with_the_measured_redirect(client):
    body, _, _ = _sign_in(client)
    url = body["authorize_url"]
    assert url.startswith(openai_plan.AUTHORIZE_ENDPOINT + "?")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback" in url
    assert f"client_id={openai_plan.CODEX_CLIENT_ID}" in url
    assert "code_challenge_method=S256" in url and "offline_access" in url
    assert body["provider"] == "openai"
    assert body["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert "verifier" not in json.dumps(body)


def test_finishing_a_plan_sign_in_files_an_openai_account_and_returns_no_token(client):
    _, code, done = _sign_in(client)
    assert done.status_code == 201, done.text
    assert done.json()["account"]["provider"] == "openai"
    call = client.endpoint.calls[0]
    assert call["code"] == code and call["verifier"]
    assert call["redirect_uri"] == openai_plan.REDIRECT_URI
    answer = client.endpoint.answer
    for material in (answer["access_token"], answer["refresh_token"], answer["id_token"]):
        assert material not in done.text
    versions = client.app_ref.state.secret_store.versions
    pair = json.loads(versions[f"{secret_name(TENANT, 'plan')}{REFRESH_SUFFIX}"][-1])
    assert pair["refresh_token"] == answer["refresh_token"]
    assert pair["account_id"] == client.endpoint.account_id
    published = json.loads(versions[secret_name(TENANT, "plan")][-1])
    assert published == {"access_token": answer["access_token"], "account_id": client.endpoint.account_id}


def test_an_unknown_account_provider_is_refused_before_a_sign_in_starts(client):
    r = client.post(
        "/v1/accounts/authorize",
        json={"owner_tenant": TENANT, "label": "x", "provider": "gemini"},
    )
    assert r.status_code == 422, r.text


def test_assign_for_openai_names_the_plans_secret_and_carries_no_key_material(client):
    _, _, done = _sign_in(client)
    assert done.status_code == 201, done.text
    assigned = client.post("/v1/accounts/assign", json={"provider": "openai"})
    assert assigned.status_code == 200, assigned.text
    body = assigned.json()
    assert body["account_id"] == f"{TENANT}:plan"
    assert body["assignment_id"]
    # A NAME: the worker reads `{base}` under its own service account.
    assert body["secret"] == secret_name(TENANT, "plan")
    answer = client.endpoint.answer
    for material in (answer["access_token"], answer["refresh_token"]):
        assert material not in assigned.text
    # What that name holds: the access token and account id, never the refresh token.
    held = json.loads(client.app_ref.state.secret_store.access(body["secret"]))
    assert set(held) == {"access_token", "account_id"}
    # And a Claude ask is not handed the plan.
    claude = client.post("/v1/accounts/assign", json={"provider": "anthropic"}).json()
    assert claude["account_id"] is None


def test_a_refused_plan_refresh_marks_the_account_reauth_needed(client):
    _, _, done = _sign_in(client)
    assert done.status_code == 201, done.text
    pair_name = f"{secret_name(TENANT, 'plan')}{REFRESH_SUFFIX}"
    secrets_store = client.app_ref.state.secret_store
    pair = json.loads(secrets_store.access(pair_name))
    pair["expires_at"] = int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp() * 1000)
    secrets_store.versions[pair_name].append(json.dumps(pair))
    client.app_ref.state.credential_refresher = CredentialRefresher(
        secrets_store, endpoint=None, plan_endpoint=client.endpoint, logger=_Log()
    )
    r = client.post(f"/v1/accounts/{TENANT}:plan/refresh")
    assert r.status_code == 200, r.text
    assert r.json()["refresh"]["reason"] == "reauth_required"
    account = AccountStore(client.app_ref.state.broker.db).get(f"{TENANT}:plan")
    assert account.state is AccountState.REAUTH_REQUIRED


def test_codexs_own_auth_json_can_be_pasted_to_register_a_plan(client):
    account_id = secrets.token_hex(6)
    auth_json = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token(account_id),
            "access_token": fake_jwt({"exp": int(time.time()) + 3600}),
            "refresh_token": secrets.token_hex(16),
            "account_id": account_id,
        },
        "last_refresh": "2026-11-01T19:00:00Z",
    }
    r = client.post(
        "/v1/accounts",
        json={
            "owner_tenant": TENANT,
            "label": "pasted",
            "provider": "openai",
            "credential": json.dumps(auth_json),
        },
    )
    assert r.status_code == 201, r.text
    assert auth_json["tokens"]["refresh_token"] not in r.text
    pair = json.loads(
        client.app_ref.state.secret_store.access(f"{secret_name(TENANT, 'pasted')}{REFRESH_SUFFIX}")
    )
    assert pair["kind"] == openai_plan.KIND and pair["account_id"] == account_id


# -- usage ----------------------------------------------------------------------


def test_codexs_usage_answer_becomes_the_pools_two_windows():
    now = NOW
    payload = {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 42,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 600,
                "reset_at": int((now + timedelta(minutes=10)).timestamp()),
            },
            "secondary_window": {
                "used_percent": 97,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 86400,
                "reset_at": int((now + timedelta(days=1)).timestamp()),
            },
        },
    }
    windows = openai_plan.parse_usage(payload, now=now)
    assert set(windows) == {"five_hour", "seven_day"}
    assert windows["five_hour"].utilization == pytest.approx(0.42)
    assert windows["seven_day"].utilization == pytest.approx(0.97)
    assert windows["seven_day"].resets_at == now + timedelta(days=1)


def test_an_unreadable_usage_answer_raises_rather_than_reading_as_empty():
    from quota_broker.usage import UsageUnavailable

    for payload in ({}, {"rate_limit": None}, {"rate_limit": {"primary_window": {"x": 1}}}):
        with pytest.raises(UsageUnavailable):
            openai_plan.parse_usage(payload)


def test_the_poller_reads_a_plan_through_codexs_usage_route_not_claudes():
    from quota_broker.accounts import Account, WindowReading
    from quota_broker.usagepoll import UsagePoller

    published = json.dumps({"access_token": secrets.token_hex(8), "account_id": "a"})
    seen = {"claude": [], "plan": []}
    recorded = {}

    class Accounts:
        def record_reading(self, account_id, windows, at):
            recorded[account_id] = windows

    reading = {"five_hour": WindowReading(utilization=0.5, resets_at=NOW + timedelta(hours=1))}
    poller = UsagePoller(
        _Store({"plan-secret": published}),
        Accounts(),
        logger=_Log(),
        now=lambda: NOW,
        fetch=lambda token: seen["claude"].append(token) or reading,
        fetch_plan=lambda token: seen["plan"].append(token) or reading,
    )
    plan = Account(
        account_id=f"{TENANT}:plan", owner_tenant=TENANT, label="plan",
        provider="openai", secret_ref="plan-secret",
    )
    poller.poll_round([plan])
    assert seen == {"claude": [], "plan": [published]}
    assert recorded[plan.account_id] == reading
