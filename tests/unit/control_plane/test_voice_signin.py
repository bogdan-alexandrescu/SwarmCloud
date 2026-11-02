"""Voice it's "Sign in with ChatGPT" (swarm_api.voice.signin, routes/voice_signin.py).

The owner's pick of 2026-11-02: a sign-in button on Voice it › Preferences
stores his OpenAI plan's credential. This holds that:

* a finished sign-in stores ONE version in the caller's own personal secret
  `swarm-tenant-<personal tenant>-openai`, in exactly the JSON swarm-voice's
  credential.py parses -- the stored bytes are fed to its parser and refresh;
* another person cannot finish someone's sign-in, and anyone Voice it is not
  open to is refused before anything is read or written;
* no response, log line, Firestore document or audit row carries the refresh
  token, the code or the PKCE verifier;
* the sign-in is the account pool's: every constant and authorize parameter
  equals quota_broker.openai_plan's.

Every token is built at runtime with `secrets.token_hex`. OpenAI's token
endpoint and Secret Manager are in-memory doubles; no network, no credentials.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
import sys
import urllib.error
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import ApiError
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.provider_keys import KEYS_COLLECTION
from swarm_api.voice import signin as vsi
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, OfflineSecretMetadata, api_settings, auth_header

# swarm-voice is not a member of the root uv workspace; its package is put on
# the path the way tests/unit/voice/test_live_session.py reaches it.
_VOICE = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _VOICE not in sys.path:
    sys.path.insert(0, _VOICE)

from swarm_voice import credential as cred  # noqa: E402

PROJECT = "voice-signin-test"


class FakeWriter:
    """Config › Secrets' writer: secrets created and versions added, in memory."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.versions: dict[str, list[bytes]] = {}

    def create_secret(self, tenant_id: str, secret_id: str) -> None:
        self.created.append((tenant_id, secret_id))
        self.versions.setdefault(secret_id, [])

    def add_version(self, secret_id: str, payload: bytes) -> str:
        held = self.versions.setdefault(secret_id, [])
        held.append(payload)
        return f"projects/{PROJECT}/secrets/{secret_id}/versions/{len(held)}"

    def disable_version(self, version: str) -> None:
        raise AssertionError("a sign-in never disables a version")

    def destroy_versions(self, secret_id: str) -> int:
        raise AssertionError("a sign-in never destroys a version")


class FakeTokens:
    """auth.openai.com's token endpoint: one code, one fresh token pair."""

    def __init__(self) -> None:
        self.refresh_token = "rt-" + secrets.token_hex(24)
        self.access_token = "at-" + secrets.token_hex(24)
        self.redeemed: list[tuple[str, str]] = []
        self.refuse = False
        self.no_refresh = False

    def redeem(self, *, code: str, verifier: str) -> dict:
        self.redeemed.append((code, verifier))
        if self.refuse:
            raise vsi.SignInFailed("ChatGPT refused the sign-in code (HTTP 400); press Sign in with ChatGPT again")
        answer = {"access_token": self.access_token, "id_token": "x.y.z", "expires_in": 3600}
        if not self.no_refresh:
            answer["refresh_token"] = self.refresh_token
        return answer


@pytest.fixture
def writer() -> FakeWriter:
    return FakeWriter()


@pytest.fixture
def tokens() -> FakeTokens:
    return FakeTokens()


@pytest.fixture
def client(db, tokens, group_map, objects, writer) -> TestClient:
    # The conftest's `tokens` fixture (the ID tokens) is shadowed by the token
    # endpoint above; build the verifier's table here.
    id_tokens = {
        f"token-{email.split('@')[0]}": {"email": email, "email_verified": True,
                                         "sub": f"sub-{email}", "hd": "example.com"}
        for email in group_map
    }
    ctx = build_context(
        settings=api_settings(voice_people=("alice@example.com", "bob@example.com")),
        db=db,
        verifier=StaticTokenVerifier(id_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    app = create_app(ctx)
    app.state.secret_metadata = OfflineSecretMetadata()
    app.state.workspace_secret_writer = writer
    app.state.voice_chatgpt_tokens = tokens
    return TestClient(app, raise_server_exceptions=False)


def _begin(client: TestClient, user: str = "alice"):
    return client.post("/v1/voice/chatgpt/authorize", headers=auth_header(user))


def _callback(started: dict, code: str) -> str:
    """The address the sign-in tab ends on, as the person copies it."""
    return f"{vsi.REDIRECT_URI}?code={code}&scope=openid&state={started['state']}"


def _finish(client: TestClient, user: str, state: str, callback: str):
    return client.post("/v1/voice/chatgpt/exchange", json={"state": state, "callback": callback},
                       headers=auth_header(user))


def _signed_in(client: TestClient, user: str = "alice") -> tuple[dict, str, object]:
    started = _begin(client, user).json()
    code = "ac-" + secrets.token_hex(16)
    return started, code, _finish(client, user, started["state"], _callback(started, code))


def _secret(person: str) -> str:
    return cred.personal_secret_id(person)


def test_a_sign_in_stores_one_version_in_the_callers_own_secret_in_credential_pys_shape(
        client, writer, tokens):
    started, code, response = _signed_in(client)

    assert response.status_code == 200, response.text
    assert response.json()["stored"] is True
    own = _secret("alice@example.com")
    assert vsi.personal_secret_id("alice@example.com") == own
    assert list(writer.versions) == [own] and len(writer.versions[own]) == 1
    # The code was redeemed with the verifier whose challenge the authorize URL carried.
    ((redeemed_code, verifier),) = tokens.redeemed
    assert redeemed_code == code
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    assert query["code_challenge"] == [vsi.challenge_for(verifier)]

    # swarm-voice's own scope check takes the version written...
    version_ref = f"projects/{PROJECT}/secrets/{own}/versions/1"
    parsed = cred.check_scope(version_ref, person="alice@example.com", project=PROJECT)
    # ...and its own parser reads the stored bytes and refreshes with them.
    payload = writer.versions[own][0]

    class Store:
        async def access(self, ref: str) -> bytes:
            assert ref == version_ref
            return payload

        async def add_version(self, secret: str, data: bytes) -> str:
            raise AssertionError("no rotation in this exchange")

    sent: list[dict] = []

    def refresh(request: httpx.Request) -> httpx.Response:
        sent.append(dict(parse_qs(request.content.decode())))
        return httpx.Response(200, json={"access_token": "minted-" + secrets.token_hex(8),
                                         "expires_in": 3600})

    async def mint() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as http:
            held = cred.SubscriptionCredential(parsed, store=Store(), http=http)
            return (await held.bearer()).reveal()

    assert asyncio.run(mint()).startswith("minted-")
    assert sent[0]["refresh_token"] == [tokens.refresh_token]
    assert sent[0]["client_id"] == [vsi.CODEX_CLIENT_ID]
    assert json.loads(payload) == {"kind": cred.KIND_SUBSCRIPTION, "client_id": vsi.CODEX_CLIENT_ID,
                                   "refresh_token": tokens.refresh_token}


def test_status_says_whether_a_sign_in_is_stored_and_when_never_a_value(client, db, tokens):
    before = client.get("/v1/voice/chatgpt", headers=auth_header("alice"))
    assert before.status_code == 200, before.text
    assert before.json() == {"stored": False, "stored_at": None}

    _signed_in(client)

    after = client.get("/v1/voice/chatgpt", headers=auth_header("alice")).json()
    assert set(after) == {"stored", "stored_at"}
    assert after["stored"] is True and after["stored_at"]
    record = db.docs[f"{KEYS_COLLECTION}/{_secret('alice@example.com')}"]
    assert record["set"] is True and record["provider"] == "openai" and record["scope"] == "user"
    # Bob's status is his own: Alice's sign-in is not his.
    assert client.get("/v1/voice/chatgpt", headers=auth_header("bob")).json()["stored"] is False


def test_signing_in_again_adds_a_second_version(client, writer):
    _signed_in(client)
    _, _, again = _signed_in(client)

    assert again.status_code == 200, again.text
    assert len(writer.versions[_secret("alice@example.com")]) == 2


def test_another_person_cannot_finish_someones_sign_in(client, db, writer, tokens):
    started = _begin(client, "alice").json()
    code = "ac-" + secrets.token_hex(16)

    refused = _finish(client, "bob", started["state"], _callback(started, code))

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "forbidden"
    assert "started by someone else" in refused.json()["message"]
    assert tokens.redeemed == [] and writer.versions == {}
    # Left exactly as it was, so Alice can still finish her own.
    mine = _finish(client, "alice", started["state"], _callback(started, code))
    assert mine.status_code == 200, mine.text
    assert list(writer.versions) == [_secret("alice@example.com")]


@pytest.mark.parametrize("call", [
    lambda c: c.get("/v1/voice/chatgpt", headers=auth_header("carol")),
    lambda c: c.post("/v1/voice/chatgpt/authorize", headers=auth_header("carol")),
    lambda c: c.post("/v1/voice/chatgpt/exchange", headers=auth_header("carol"),
                     json={"state": "s" * 43, "callback": "code-" + "c" * 20}),
])
def test_anyone_voice_it_is_not_open_to_is_403_and_nothing_is_written(client, db, writer, tokens, call):
    response = call(client)

    assert response.status_code == 403, response.text
    assert not any(path.startswith(vsi.PENDING + "/") for path in db.docs)
    assert writer.versions == {} and tokens.redeemed == []


def test_no_response_log_line_or_document_carries_the_refresh_token(client, db, tokens, caplog):
    caplog.set_level(logging.DEBUG)
    started = _begin(client).json()
    code = "ac-" + secrets.token_hex(16)
    pending = [data for path, data in db.docs.items() if path.startswith(vsi.PENDING + "/")]
    (verifier,) = [p["verifier"] for p in pending]

    finished = _finish(client, "alice", started["state"], _callback(started, code))
    status = client.get("/v1/voice/chatgpt", headers=auth_header("alice"))
    replay = _finish(client, "alice", started["state"], _callback(started, code))

    assert finished.status_code == 200 and replay.status_code == 422
    seen = "\n".join([json.dumps(started), finished.text, status.text, replay.text,
                      caplog.text, repr(db.docs)])
    for value in (tokens.refresh_token, tokens.access_token, code):
        assert value not in seen
    assert verifier not in seen.replace(repr(pending), "")
    # The pending record, verifier and all, is gone once claimed.
    assert not any(path.startswith(vsi.PENDING + "/") for path in db.docs)


def test_a_refused_code_stores_nothing_and_says_so_without_the_code(client, writer, tokens):
    tokens.refuse = True
    started, code, response = _signed_in(client)

    assert response.status_code == 502, response.text
    assert code not in response.text and tokens.refresh_token not in response.text
    assert writer.versions == {}


def test_an_answer_without_a_refresh_token_stores_nothing(client, writer, tokens):
    tokens.no_refresh = True
    _, _, response = _signed_in(client)

    assert response.status_code == 502, response.text
    assert writer.versions == {}


def test_a_sign_in_is_single_use(client, writer, tokens):
    started, code, first = _signed_in(client)

    second = _finish(client, "alice", started["state"], _callback(started, code))

    assert first.status_code == 200 and second.status_code == 422
    assert len(tokens.redeemed) == 1


def test_an_expired_sign_in_is_refused_and_redeems_nothing(client, db, writer, tokens):
    started = _begin(client).json()
    (path,) = [p for p in db.docs if p.startswith(vsi.PENDING + "/")]
    db.docs[path]["created_at"] = db.docs[path]["created_at"] - vsi.PENDING_TTL - timedelta(seconds=1)

    response = _finish(client, "alice", started["state"], _callback(started, "ac-" + secrets.token_hex(8)))

    assert response.status_code == 422, response.text
    assert tokens.redeemed == [] and writer.versions == {}


def test_an_address_from_another_sign_in_is_refused_and_this_one_still_finishes(client, writer, tokens):
    started = _begin(client).json()
    other = {"state": "other-" + secrets.token_hex(16)}
    code = "ac-" + secrets.token_hex(16)

    wrong = _finish(client, "alice", started["state"], _callback(other, code))
    right = _finish(client, "alice", started["state"], _callback(started, code))

    assert wrong.status_code == 422 and right.status_code == 200
    assert len(tokens.redeemed) == 1


def test_a_body_naming_a_person_or_secret_is_422(client, writer):
    started = _begin(client).json()
    response = client.post("/v1/voice/chatgpt/exchange", headers=auth_header("alice"), json={
        "state": started["state"], "callback": "code-" + secrets.token_hex(8),
        "person": "bob@example.com"})

    assert response.status_code == 422
    assert writer.versions == {}


def test_the_exchange_body_is_never_captured():
    from swarm_api.main import UNCAPTURED_ROUTES
    from swarm_api.routes import voice_signin

    assert voice_signin.EXCHANGE_ROUTE == "/v1/voice/chatgpt/exchange"
    assert "/v1/voice/chatgpt/exchange" in UNCAPTURED_ROUTES


def test_authorize_says_where_the_browser_lands_and_what_to_paste(client):
    started = _begin(client).json()

    assert started["redirect_uri"] == vsi.REDIRECT_URI
    assert "localhost:1455" in started["instructions"] and "address bar" in started["instructions"]
    assert started["expires_in_seconds"] == int(vsi.PENDING_TTL.total_seconds())
    assert "verifier" not in json.dumps(started)


@pytest.mark.parametrize("pasted, expected", [
    ("http://localhost:1455/auth/callback?code=abc123&state=st-1", ("abc123", "st-1")),
    ("abc123#st-1", ("abc123", "st-1")),
    ("  abc123  ", ("abc123", None)),
])
def test_the_paste_may_be_the_address_the_code_and_state_or_the_code(pasted, expected):
    assert vsi.parse_callback(pasted) == expected


@pytest.mark.parametrize("pasted", [
    "http://localhost:1455/auth/callback?error=access_denied&state=st-1",
    "http://localhost:1455/auth/callback?state=st-1",
    "",
])
def test_a_paste_without_a_code_is_refused(pasted):
    with pytest.raises(ApiError):
        vsi.parse_callback(pasted)


def test_the_sign_in_is_the_account_pools_constant_for_constant():
    from quota_broker import oauth, openai_plan

    assert vsi.ISSUER == openai_plan.ISSUER
    assert vsi.AUTHORIZE_ENDPOINT == openai_plan.AUTHORIZE_ENDPOINT
    assert vsi.TOKEN_ENDPOINT == openai_plan.TOKEN_ENDPOINT
    assert vsi.CODEX_CLIENT_ID == openai_plan.CODEX_CLIENT_ID
    assert vsi.REDIRECT_URI == openai_plan.REDIRECT_URI
    assert vsi.SCOPES == openai_plan.SCOPES
    assert vsi.KIND_SUBSCRIPTION == cred.KIND_SUBSCRIPTION
    verifier = vsi.new_verifier()
    assert vsi.challenge_for(verifier) == oauth.challenge_for(verifier)
    state = vsi.new_state()
    challenge = oauth.challenge_for(verifier)
    assert vsi.authorize_url(state=state, code_challenge=challenge) == \
        openai_plan.build_authorize_url(state=state, code_challenge=challenge)


def test_the_real_endpoint_reports_a_refusal_by_status_alone(monkeypatch):
    code = "ac-" + secrets.token_hex(16)
    verifier = secrets.token_hex(16)
    echoed = f'{{"error":"invalid_grant","code":"{code}","code_verifier":"{verifier}"}}'.encode()

    def refuse(request, timeout):
        assert parse_qs(request.data.decode())["redirect_uri"] == [vsi.REDIRECT_URI]
        raise urllib.error.HTTPError(vsi.TOKEN_ENDPOINT, 400, "Bad Request", {}, io.BytesIO(echoed))

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(vsi.SignInFailed) as raised:
        vsi.HttpTokenEndpoint().redeem(code=code, verifier=verifier)

    assert "HTTP 400" in raised.value.message
    assert code not in str(raised.value) and verifier not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
