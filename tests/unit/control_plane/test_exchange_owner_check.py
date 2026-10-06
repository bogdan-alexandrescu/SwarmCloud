"""A sign-in is finished only by the tenant that started it.

THE HOLE THIS CLOSES (S0, owner decision 2026-10-01). `POST
/v1/accounts/exchange` filed the credential under the PENDING record's
`owner_tenant`, and nothing compared that with the CALLER. swarm-api reaches
the broker as a PLATFORM caller, and the broker's `_authorize` returns the
pending owner unexamined for one -- so a member of `research` holding a live
`state` that `eng` started would complete eng's sign-in: their own Anthropic
credential filed into eng's pool, and eng's new account's metadata handed back
to them.

WHAT IS PINNED HERE

  * swarm-api sends the caller's CHECKED tenant (`_tenant_id`, through
    `tenant_for`) as `expected_owner`, never anything from the request body;
  * the broker compares it with the pending owner BEFORE the code is redeemed,
    and a mismatch is a 403 that redeems nothing, files nothing and DELETES
    NOTHING -- the pending sign-in stays usable by its real owner, so holding a
    state is not enough to burn somebody else's sign-in either;
  * a platform caller that sends no `expected_owner` is refused the same way
    (fail closed), so a swarm-api older than this check cannot skip it;
  * swarm-api reports that 403 as the HUMAN caller's 403, not as its own
    service identity being rejected.

Both halves run for real: the end-to-end tests drive the shipped swarm-api
routes through the shipped `BrokerClient`, whose transport hands the bytes to
the shipped broker app. Nothing leaves the process.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from swarm_api.brokerclient import BrokerClient
from swarm_api.main import create_app as create_api

from .conftest import auth_header

PROJECT = "test-project"


class _Secrets:
    def __init__(self):
        self.versions, self.created = {}, {}

    def ensure_secret(self, name, *, labels, accessors, region):
        self.created[name] = dict(labels)
        return True

    def set_worker_readers(self, name, *, readers, manages, revoke=True):
        # The lending sync; this double records no IAM, so nothing changes.
        return [], []

    def add_version(self, name, payload):
        if name not in self.created:
            raise KeyError(name)
        self.versions.setdefault(name, []).append(payload)


class _Endpoint:
    """The token endpoint, without a network. Records every redemption."""

    def __init__(self):
        self.calls = []

    def redeem(self, *, code, verifier, redirect_uri, state=""):
        self.calls.append({"code": code, "state": state})
        return {
            "access_token": "at-placeholder",
            "refresh_token": "rt-placeholder",
            "expires_in": 28800,
        }


class _Identity:
    """Who the broker thinks is calling. Mutable so one test can be two callers."""

    def __init__(self):
        self.caller = (None, True)  # platform

    def resolve(self, authorization):
        return self.caller


@pytest.fixture()
def broker_app(broker, monkeypatch):
    from quota_broker.accountstore import AccountStore
    from quota_broker.main import create_app

    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv(
        "BROKER_SERVICE_ACCOUNT", "swarm-quota-broker@p.iam.gserviceaccount.com"
    )
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE",
        "swarm-agent-worker-{tenant}@p.iam.gserviceaccount.com",
    )
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=AccountStore(broker.db))
    app.state.secret_store = _Secrets()
    app.state.token_endpoint = _Endpoint()
    app.state.test_identity = identity
    return app


@pytest.fixture()
def broker_client(broker_app):
    return TestClient(broker_app, raise_server_exceptions=False)


def _begin(broker_client, owner="eng", label="team"):
    r = broker_client.post(
        "/v1/accounts/authorize", json={"owner_tenant": owner, "label": label}
    )
    assert r.status_code == 200, r.text
    return r.json()["state"]


def _pending_exists(broker, state):
    return broker.db.collection("account_auth").document(state).get().exists


def _nothing_filed(broker_app, broker_client):
    assert broker_app.state.token_endpoint.calls == [], "the code was redeemed"
    assert broker_app.state.secret_store.versions == {}, "a credential was written"
    assert broker_client.get("/v1/accounts").json()["accounts"] == []


# -- the broker ------------------------------------------------------------


def test_a_platform_call_naming_another_tenant_is_refused_before_redeeming(
    broker, broker_app, broker_client
):
    state = _begin(broker_client, owner="eng")

    r = broker_client.post(
        "/v1/accounts/exchange",
        json={"state": state, "code": "the-code", "expected_owner": "research"},
    )

    assert r.status_code == 403, r.text
    assert r.json()["code"] == "sign_in_owner_mismatch"
    _nothing_filed(broker_app, broker_client)
    # Nothing was redeemed, so nothing was spent: the record stays for its
    # real owner rather than letting a state-holder burn the sign-in.
    assert _pending_exists(broker, state)


def test_the_owner_still_finishes_after_a_refused_attempt(
    broker, broker_app, broker_client
):
    state = _begin(broker_client, owner="eng")
    refused = broker_client.post(
        "/v1/accounts/exchange",
        json={"state": state, "code": "the-code", "expected_owner": "research"},
    )
    assert refused.status_code == 403

    good = broker_client.post(
        "/v1/accounts/exchange",
        json={"state": state, "code": "the-code", "expected_owner": "eng"},
    )

    assert good.status_code == 201, good.text
    assert good.json()["account"]["owner_tenant"] == "eng"
    assert len(broker_app.state.token_endpoint.calls) == 1
    assert not _pending_exists(broker, state), "a completed sign-in stays redeemable"


def test_a_platform_call_without_expected_owner_is_refused(
    broker, broker_app, broker_client
):
    """Fail closed: a swarm-api that predates the check cannot skip it by
    simply not sending the field."""
    state = _begin(broker_client, owner="eng")

    r = broker_client.post("/v1/accounts/exchange", json={"state": state, "code": "c0de"})

    assert r.status_code == 403, r.text
    assert "expected_owner" in r.json()["message"]
    _nothing_filed(broker_app, broker_client)
    assert _pending_exists(broker, state)


def test_a_platform_call_with_a_blank_expected_owner_is_refused(
    broker, broker_app, broker_client
):
    state = _begin(broker_client, owner="eng")

    r = broker_client.post(
        "/v1/accounts/exchange",
        json={"state": state, "code": "c0de", "expected_owner": ""},
    )

    assert r.status_code == 403, r.text
    _nothing_filed(broker_app, broker_client)
    assert _pending_exists(broker, state)


def test_a_tenant_caller_of_another_tenant_is_refused_too(
    broker, broker_app, broker_client
):
    """A non-platform caller is compared by its own identity, and an
    `expected_owner` it sends cannot widen that."""
    state = _begin(broker_client, owner="eng")
    broker_app.state.test_identity.caller = ("research", False)

    for body in (
        {"state": state, "code": "c0de"},
        {"state": state, "code": "c0de", "expected_owner": "eng"},
    ):
        r = broker_client.post("/v1/accounts/exchange", json=body)
        assert r.status_code == 403, r.text

    broker_app.state.test_identity.caller = (None, True)
    _nothing_filed(broker_app, broker_client)
    assert _pending_exists(broker, state)


def test_the_mismatch_is_checked_before_the_age_of_the_sign_in(
    broker, broker_app, broker_client
):
    """A non-owner learns nothing about the record and changes nothing about
    it -- not even the expiry clean-up an owner's late paste would trigger."""
    from datetime import datetime, timedelta, timezone

    state = _begin(broker_client, owner="eng")
    ref = broker.db.collection("account_auth").document(state)
    stale = dict(ref.get().to_dict())
    stale["created_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
    ref.set(stale)

    r = broker_client.post(
        "/v1/accounts/exchange",
        json={"state": state, "code": "c0de", "expected_owner": "research"},
    )

    assert r.status_code == 403, r.text
    assert _pending_exists(broker, state)


# -- swarm-api, end to end through the real BrokerClient ---------------------


class _Bridge:
    """A BrokerClient transport that hands the request to the broker app."""

    def __init__(self, broker_client):
        self._c = broker_client
        self.bodies = []

    def __call__(self, method, url, *, headers, body=None, timeout=30.0):
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if body:
            self.bodies.append((parts.path, json.loads(body.decode())))
        r = self._c.request(method, path, content=body, headers=dict(headers))
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, data


class _Token:
    def token(self, audience: str) -> str:
        return "t"


@pytest.fixture()
def api(api_context, broker_client):
    bridge = _Bridge(broker_client)
    app = create_api(api_context)
    app.state.account_pool = BrokerClient(
        base_url="https://broker.invalid",
        audience="https://broker.invalid",
        transport=bridge,
        tokens=_Token(),
    )
    c = TestClient(app, raise_server_exceptions=False)
    c.bridge = bridge
    return c


def _api_begin(api, user):
    r = api.post("/v1/accounts/authorize", headers=auth_header(user), json={"label": "team"})
    assert r.status_code == 200, r.text
    return r.json()["state"]


def test_swarm_api_sends_the_callers_checked_tenant_as_expected_owner(api):
    state = _api_begin(api, "alice")

    r = api.post(
        "/v1/accounts/exchange",
        headers=auth_header("alice"),
        json={"state": state, "code": "the-code"},
    )

    assert r.status_code == 201, r.text
    assert r.json()["account"]["owner_tenant"] == "eng"
    sent = [b for p, b in api.bridge.bodies if p == "/v1/accounts/exchange"]
    assert sent == [{"state": state, "code": "the-code", "expected_owner": "eng"}]


def test_another_tenant_holding_a_live_state_gets_403_and_nothing_is_filed(
    api, broker, broker_app, broker_client
):
    state = _api_begin(api, "alice")  # eng started it

    r = api.post(
        "/v1/accounts/exchange",
        headers=auth_header("bob"),  # research holds the state
        json={"state": state, "code": "the-code"},
    )

    # The human caller's 403 -- not a 503 blaming swarm-api's own identity.
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "forbidden"
    sent = [b for p, b in api.bridge.bodies if p == "/v1/accounts/exchange"]
    assert sent[0]["expected_owner"] == "research"
    _nothing_filed(broker_app, broker_client)
    assert _pending_exists(broker, state)

    # And eng, the real owner, still finishes it with the same paste.
    ok = api.post(
        "/v1/accounts/exchange",
        headers=auth_header("alice"),
        json={"state": state, "code": "the-code"},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["account"]["owner_tenant"] == "eng"


def test_the_body_cannot_choose_expected_owner(api, broker, broker_app, broker_client):
    """The request model is strict: a caller cannot send the field and have
    swarm-api forward it in place of the checked tenant.

    The refusal is the request model's 422, so the property is that NOTHING
    reached the broker -- asserted as an empty list, not as `all()` over the
    forwarded bodies, which an empty list satisfies without checking anything.
    """
    state = _api_begin(api, "alice")

    r = api.post(
        "/v1/accounts/exchange",
        headers=auth_header("bob"),
        json={"state": state, "code": "the-code", "expected_owner": "eng"},
    )

    assert r.status_code == 422, r.text
    sent = [b for p, b in api.bridge.bodies if p == "/v1/accounts/exchange"]
    assert sent == [], "a body naming expected_owner was forwarded to the broker"
    _nothing_filed(broker_app, broker_client)
    assert _pending_exists(broker, state)
