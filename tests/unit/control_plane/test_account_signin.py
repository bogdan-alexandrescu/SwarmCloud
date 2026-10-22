"""Adding an account from a browser, without a keychain and without a CLI.

Every constant and constraint here was MEASURED, by claudeswitch, and is
recorded in its docs/GROUND_TRUTH.md section 31. Two of them present as
something other than what they are, so they are pinned:

  * `state` must be 32 bytes. A 16-byte state is refused with "invalid request
    format", which reads like a malformed URL.
  * the token endpoint is form-encoded. JSON is answered with 403, which reads
    like an authorization failure.

WHY A CODE IS STILL PASTED. Anthropic's OAuth client accepts exactly one
redirect target -- its own callback page, which DISPLAYS a code. A third-party
application cannot register its own redirect, so the browser cannot come back
to us. One short code replaces a keychain item, which is the whole point.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from quota_broker.oauth import (
    AUTHORIZE_ENDPOINT,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
    CredentialError,
    build_authorize_url,
    challenge_for,
    new_state,
    new_verifier,
    split_pasted_code,
)

PROJECT = "test-project"
TENANT = "eng"


# -- the authorize URL -----------------------------------------------------


def test_a_short_state_is_refused_before_a_person_ever_sees_a_login_page():
    """16 bytes is rejected by the endpoint with "invalid request format",
    which reads like a malformed URL and sends you looking at the wrong thing.
    Catching it here names the actual cause."""
    with pytest.raises(CredentialError, match="32 random bytes"):
        build_authorize_url(state="tooshort", code_challenge="x")


def test_the_state_this_module_generates_is_long_enough():
    """The guard above is worthless if our own generator trips it."""
    state = new_state()
    assert len(state) >= 43
    build_authorize_url(state=state, code_challenge=challenge_for(new_verifier()))


def test_the_url_carries_every_parameter_claude_code_sends():
    """This endpoint has already proved to validate things a conforming server
    would not, so the parameter set is copied rather than reasoned about."""
    url = build_authorize_url(state=new_state(), code_challenge="abc")

    assert url.startswith(AUTHORIZE_ENDPOINT + "?")
    for required in (
        "code=true",
        "response_type=code",
        "code_challenge=abc",
        "code_challenge_method=S256",
    ):
        assert required in url, required
    assert "client_id=" in url and "state=" in url


def test_the_redirect_is_anthropics_callback_and_not_ours():
    """A third-party application cannot register its own redirect with this
    client. Pointing it at swarm.saga.xyz would fail at the login page, after
    the person had already signed in."""
    url = build_authorize_url(state=new_state(), code_challenge="abc")
    assert "platform.claude.com%2Foauth%2Fcode%2Fcallback" in url
    assert OAUTH_REDIRECT_URI.startswith("https://platform.claude.com/")


def test_the_scopes_include_inference_because_a_credential_without_it_is_useless():
    assert "user:inference" in OAUTH_SCOPES
    assert "user:sessions:claude_code" in OAUTH_SCOPES


def test_the_challenge_is_the_sha256_of_the_verifier_not_the_verifier():
    """Sending the verifier as the challenge is a PKCE flow that proves
    nothing, and it would still complete successfully."""
    import base64
    import hashlib

    verifier = new_verifier()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")

    assert challenge_for(verifier) == expected
    assert challenge_for(verifier) != verifier


# -- what a person actually pastes -----------------------------------------


def test_the_whole_thing_from_the_callback_page_is_accepted():
    """The page renders `<code>#<state>` and people paste what is on screen.
    Accepting only the bare code would reject the likeliest paste with a
    message about an invalid code."""
    assert split_pasted_code("abc123#somestate") == ("abc123", "somestate")


def test_a_bare_code_still_works():
    assert split_pasted_code("  abc123  ") == ("abc123", None)


# -- the two-step flow over HTTP -------------------------------------------


class _Secrets:
    def __init__(self):
        self.versions, self.created, self.accessors = {}, {}, {}

    def ensure_secret(self, name, *, labels, accessors, region):
        self.created[name] = dict(labels)
        self.accessors.setdefault(name, []).extend(accessors)
        return True

    def set_worker_readers(self, name, *, readers, manages, revoke=True):
        # The lending sync (SecretManagerStore.set_worker_readers), modelled
        # on the same accessor list `ensure_secret` writes.
        present = self.accessors.setdefault(name, [])
        added = [m for m in readers if m not in present]
        removed = [m for m in present if manages(m) and m not in readers] if revoke else []
        present[:] = [m for m in present if m not in removed] + added
        return added, removed

    def add_version(self, name, payload):
        if name not in self.created:
            raise KeyError(name)
        self.versions.setdefault(name, []).append(payload)


class _Endpoint:
    """The token endpoint, without a network."""

    def __init__(self, payload=None, error=None):
        self.calls = []
        self._payload = payload or {
            "access_token": "at-fresh",
            "refresh_token": "rt-fresh",
            "expires_in": 28800,
        }
        self._error = error

    def redeem(self, *, code, verifier, redirect_uri, state=""):
        self.calls.append(
            {"code": code, "verifier": verifier, "redirect_uri": redirect_uri, "state": state}
        )
        if self._error:
            raise self._error
        return dict(self._payload)


@pytest.fixture()
def client(broker, monkeypatch):
    from fastapi.testclient import TestClient

    from quota_broker.accountstore import AccountStore
    from quota_broker.main import WorkerIdentity, create_app

    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("BROKER_SERVICE_ACCOUNT", "swarm-quota-broker@p.iam.gserviceaccount.com")
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE",
        "swarm-agent-worker-{tenant}@p.iam.gserviceaccount.com",
    )

    class PlatformIdentity(WorkerIdentity):
        def resolve(self, authorization):
            return None, True

    app = create_app(
        broker,
        identity=PlatformIdentity(project_id=PROJECT),
        account_store=AccountStore(broker.db),
    )
    app.state.secret_store = _Secrets()
    # NOTE FOR ANYONE READING THIS FIXTURE. Assigning app.state.token_endpoint
    # here is why the missing production wiring was invisible for so long:
    # create_app did not set it, every request to /v1/accounts/exchange raised
    # AttributeError and answered 500, and this suite passed throughout because
    # the fixture did the wiring the application never did. A fixture that
    # supplies a collaborator the app is supposed to construct tests the
    # handler and hides the assembly. test_broker_app_state_wiring.py now
    # covers the assembly separately.
    app.state.token_endpoint = _Endpoint()
    c = TestClient(app, raise_server_exceptions=False)
    c.app_ref = app
    return c


def _begin(client, label="team"):
    return client.post(
        "/v1/accounts/authorize",
        json={"owner_tenant": TENANT, "label": label},
    )


def test_beginning_a_sign_in_returns_a_url_a_person_can_open(client):
    r = _begin(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authorize_url"].startswith(AUTHORIZE_ENDPOINT)
    assert body["state"] in body["authorize_url"]


def test_the_verifier_never_reaches_the_browser(client):
    """A PKCE verifier the client holds proves nothing. It is kept server-side
    and looked up by state."""
    body = _begin(client).json()
    assert "verifier" not in json.dumps(body)
    assert "code_verifier" not in json.dumps(body)


def test_finishing_the_sign_in_registers_the_account(client):
    state = _begin(client).json()["state"]

    r = client.post("/v1/accounts/exchange", json={"state": state, "code": "the-code", "expected_owner": TENANT})

    assert r.status_code == 201, r.text
    assert r.json()["account"]["label"] == "team"
    assert client.get("/v1/accounts").json()["accounts"][0]["label"] == "team"


def test_the_code_is_redeemed_with_the_verifier_that_started_this_sign_in(client):
    """Redeeming with a fresh verifier would fail, and redeeming with somebody
    else's would be the bug PKCE exists to prevent."""
    started = _begin(client).json()
    client.post("/v1/accounts/exchange", json={"state": started["state"], "code": "code-abc", "expected_owner": TENANT})

    call = client.app_ref.state.token_endpoint.calls[0]
    # The state reaches the token endpoint. Claude Code sends it on the token
    # request and the form-encoded version omitted it entirely, which the live
    # endpoint answered 400 invalid_request_error.
    assert call["state"], "the state was not passed through to the token endpoint"
    assert call["redirect_uri"] == OAUTH_REDIRECT_URI
    assert call["verifier"]
    assert call["code"] == "code-abc"


def test_no_credential_material_appears_in_the_response(client):
    state = _begin(client).json()["state"]
    text = client.post(
        "/v1/accounts/exchange", json={"state": state, "code": "code-abc", "expected_owner": TENANT}
    ).text
    for forbidden in ("at-fresh", "rt-fresh", "access_token", "refresh_token"):
        assert forbidden not in text, forbidden


def test_the_pair_is_stored_and_only_the_access_token_reaches_the_pod(client):
    state = _begin(client).json()["state"]
    client.post("/v1/accounts/exchange", json={"state": state, "code": "code-abc", "expected_owner": TENANT})

    base = f"swarm-account-{TENANT}--team"
    assert "rt-fresh" in client.app_ref.state.secret_store.versions[f"{base}-refresh"][-1]
    assert client.app_ref.state.secret_store.versions[base][-1] == "at-fresh"
    assert "rt-fresh" not in client.app_ref.state.secret_store.versions[base][-1]


def test_a_state_that_was_never_started_is_refused(client):
    r = client.post("/v1/accounts/exchange", json={"state": "never-issued", "code": "code-abc", "expected_owner": TENANT})
    assert r.status_code == 422
    assert "expired or was already completed" in r.json()["message"]


def test_a_code_cannot_be_redeemed_twice(client):
    """Codes are single-use at the endpoint too, but the pending record is
    deleted here so the second attempt gets a sentence rather than a 403 from
    Anthropic."""
    state = _begin(client).json()["state"]
    assert client.post("/v1/accounts/exchange", json={"state": state, "code": "code-abc", "expected_owner": TENANT}).status_code == 201

    again = client.post("/v1/accounts/exchange", json={"state": state, "code": "code-abc", "expected_owner": TENANT})
    assert again.status_code == 422
    assert "already completed" in again.json()["message"]


def test_a_paste_from_a_different_sign_in_is_refused(client):
    """The callback shows `<code>#<state>`. If that state is not the one this
    page started, the person has two tabs open and guessing between them would
    put the credential on the wrong account."""
    state = _begin(client).json()["state"]

    r = client.post(
        "/v1/accounts/exchange",
        json={"state": state, "code": "somecode#a-different-state", "expected_owner": TENANT},
    )

    assert r.status_code == 422
    assert "different sign-in" in r.json()["message"]


def test_a_rejected_code_does_not_tell_the_person_to_re_authenticate(client):
    """A rejected REFRESH token means an account needs re-authenticating. A
    rejected CODE almost always means the person took too long, and saying
    "re-authenticate" would be unhelpful and slightly insulting."""
    client.app_ref.state.token_endpoint = _Endpoint(
        error=CredentialError("the sign-in code was not accepted. Codes are single-use")
    )
    state = _begin(client).json()["state"]

    r = client.post("/v1/accounts/exchange", json={"state": state, "code": "stale", "expected_owner": TENANT})

    assert r.status_code == 422
    assert "single-use" in r.json()["message"]


def test_a_failed_exchange_keeps_the_pending_sign_in_so_a_retry_is_one_paste(client):
    """Deleting it on failure would make a mistyped code cost the whole
    sign-in, including logging in again."""
    client.app_ref.state.token_endpoint = _Endpoint(error=CredentialError("nope"))
    state = _begin(client).json()["state"]
    client.post("/v1/accounts/exchange", json={"state": state, "code": "wrong", "expected_owner": TENANT})

    client.app_ref.state.token_endpoint = _Endpoint()
    good = client.post("/v1/accounts/exchange", json={"state": state, "code": "right", "expected_owner": TENANT})
    assert good.status_code == 201, "the pending sign-in was thrown away on a failed paste"


def test_a_token_response_without_a_refresh_token_is_refused(client):
    """A credential that cannot be exchanged cannot be kept alive, and the
    whole promise of this pool is that a person signs in once."""
    client.app_ref.state.token_endpoint = _Endpoint(
        payload={"access_token": "at-only", "expires_in": 3600}
    )
    state = _begin(client).json()["state"]

    r = client.post("/v1/accounts/exchange", json={"state": state, "code": "code-abc", "expected_owner": TENANT})

    assert r.status_code == 422
    assert "kept alive" in r.json()["message"]
    assert client.get("/v1/accounts").json()["accounts"] == []


def test_expires_in_is_read_as_seconds_from_now_not_as_a_timestamp(client):
    """Reading it as an epoch would date every credential to 1970 and make the
    refresher think each one was already expired."""
    state = _begin(client).json()["state"]
    r = client.post("/v1/accounts/exchange", json={"state": state, "code": "code-abc", "expected_owner": TENANT})

    expires = datetime.fromisoformat(r.json()["expires_at"])
    assert expires > datetime.now(timezone.utc) + timedelta(hours=7)


# -- a label the broker refuses is a refusal, not an outage (#1912) ----------
#
# `validate_label` raised `AccountError` out of the authorize route with no
# handler, so FastAPI answered 500 and swarm-api, which reads every broker
# status but 409/422 as the pool being down, told the person "a service the API
# depends on did not answer" about a label with capitals in it.

#: The sentence `validate_label` refuses with, in words a person can act on.
LABEL_RULE = "must be lowercase letters, digits and dashes"

REFUSED_LABELS = ("AI-ENG-2", "a_b", "a" * 41)


@pytest.mark.parametrize("label", REFUSED_LABELS)
def test_a_label_the_rule_refuses_is_a_422_carrying_the_rule(client, label):
    r = _begin(client, label=label)

    # MUTATION: drop the `except AccountError` around `validate_label` in
    # `begin_account_authorization` and this answers 500.
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed"
    # MUTATION: raise `BrokerValidationError("bad label")` instead of
    # `str(exc)` and the person loses the rule they have to follow.
    assert LABEL_RULE in r.json()["message"]
    assert "at most 40 characters" in r.json()["message"]


@pytest.mark.parametrize("label", REFUSED_LABELS)
def test_a_refused_label_leaves_no_pending_sign_in(client, label):
    _begin(client, label=label)

    pending = list(client.app_ref.state.broker.db.collection("account_auth").stream())
    # MUTATION: move `validate_label` below the pending-document `set` and a
    # sign-in that can never finish is left behind for every refusal.
    assert pending == []


def test_a_label_that_fits_still_starts_a_sign_in(client):
    r = _begin(client, label="ai-eng-2")

    # MUTATION: refuse every label (`raise BrokerValidationError(...)`
    # unconditionally) and this answers 422.
    assert r.status_code == 200, r.text
    assert r.json()["state"]


def _pasted_credential() -> str:
    """Claude Code's keychain shape, built at runtime so no literal in this
    file looks like a token."""
    expires = datetime.now(timezone.utc) + timedelta(hours=8)
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "at-" + secrets.token_hex(12),
                "refreshToken": "rt-" + secrets.token_hex(12),
                "expiresAt": int(expires.timestamp() * 1000),
            }
        }
    )


def test_a_pasted_credential_under_a_refused_label_is_a_422_and_files_nothing(client):
    """The paste path reaches the same rule through `secret_name` in
    `_provision_and_register`, which the exchange path shares."""
    r = client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "AI-ENG-2", "credential": _pasted_credential()},
    )

    # MUTATION: drop the `except AccountError` around `secret_name` in
    # `_provision_and_register` and this answers 500.
    assert r.status_code == 422, r.text
    assert LABEL_RULE in r.json()["message"]
    # MUTATION: create the secrets before naming them and a refused label
    # leaves a secret behind.
    assert client.app_ref.state.secret_store.created == {}
    assert client.get("/v1/accounts").json()["accounts"] == []


def test_swarm_api_hands_the_brokers_label_refusal_to_the_caller_whole(client, api_context):
    """End to end through the shipped client: swarm-api's `BrokerClient`, its
    transport bridged into the broker app above, and swarm-api's route. A 500
    from the broker became 503 `account_pool_unavailable` here."""
    from fastapi.testclient import TestClient as ApiTestClient

    from swarm_api.brokerclient import BrokerClient, StaticIdToken
    from swarm_api.main import create_app as create_api

    from .conftest import auth_header

    def bridge(method, url, *, headers, body=None, timeout=15.0):
        path = url.split("broker.example", 1)[1]
        answer = client.request(
            method, path, content=body, headers={"Content-Type": "application/json"}
        )
        # As `urllib_transport` reads it: JSON when it is JSON, else the text.
        try:
            return answer.status_code, answer.json()
        except ValueError:
            return answer.status_code, answer.text

    api = create_api(api_context)
    api.state.account_pool = BrokerClient(
        base_url="https://broker.example",
        tokens=StaticIdToken("id-" + secrets.token_hex(4)),
        transport=bridge,
    )
    caller = ApiTestClient(api, raise_server_exceptions=False)

    r = caller.post(
        "/v1/accounts/authorize",
        headers=auth_header("alice"),
        json={"label": "AI-ENG-2", "lend_to": []},
    )

    # MUTATION: leave `begin_account_authorization` without its
    # `except AccountError` and this is 503 `account_pool_unavailable`.
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed"
    # MUTATION: have `BrokerClient._result` drop `message` on a 422 and
    # the broker's sentence never reaches the person.
    assert LABEL_RULE in r.json()["message"]
