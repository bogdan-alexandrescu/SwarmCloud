"""The SwarmCloud GitHub App's user authorisation: authorise, exchange,
refresh sweep, disconnect (docs/onboarding.md §3.1-§3.2, §3.4 items 5-6;
#780, lane OB3).

What is held here, every case offline against forge fakes:

  * the OAuth `state` is bound to the caller who asked for it and works
    once: another member, another tenant, a replay, an unknown or a
    ten-minute-old state are each AUTHORISATION_EXPIRED with §2.3's copy,
    and none of them reaches GitHub;
  * the exchange stores the user access token in the user's slot and the
    refresh token in its `-refresh` twin -- creating both at onboarding when
    absent (D3) -- and answers the connection, never a token;
  * the sweep refreshes a connection near expiry and leaves the rest; a
    refusal marks the connection `refresh_failed` with REFRESH_FAILED's
    copy; a forge that did not answer changes nothing; one refresher at a
    time holds a connection's refresh lease;
  * disconnect revokes the authorisation at GitHub, disables the slots'
    versions and marks the connection and its git token record revoked;
  * no token, refresh token, client secret or code appears in any response,
    Firestore document or log line -- and a line that did carry one while it
    was in hand was masked by the redaction literal;
  * every route answers "GitHub App not configured" while the client id or
    the client secret is absent;
  * the `app_user` token kind.

Every token-shaped value is built at runtime, never written as a literal.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgeapp
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.gittokens import (
    COLLECTION,
    TOKEN_KINDS,
    GitTokenRecord,
    TokenState,
    expiry_status,
    provider_suffix,
    token_kind,
)
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.onboarding import recovery_copy
from swarm_api.redaction import MASK
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
CLIENT_ID = "Iv23" + "abcdefgh12"
transport_log = logging.getLogger("tests.forgeapp.transport")


# -- forge fakes -------------------------------------------------------------------


def _user_token() -> str:
    return "ghu" + "_" + secrets.token_hex(18)


def _refresh_token() -> str:
    return "ghr" + "_" + secrets.token_hex(30)


class FakeGitHub:
    """github.com's token endpoint and api.github.com's /user and grant
    revocation, as `forgeapp.HttpSend` sees them. Every value it mints is
    remembered in `issued`, so a test can look for each one everywhere."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.issued: list[str] = []
        #: "ok", "refuse" (GitHub's 200 with an `error`), "down" (a 502),
        #: "network" (no answer at all).
        self.token_endpoint = "ok"
        self.revoke_status = 204
        self.login = "alice-gh"
        self.user_id = 4242
        self.valid_refresh: set[str] = set()
        self.valid_codes: set[str] = set()

    def code(self) -> str:
        value = secrets.token_hex(10)
        self.valid_codes.add(value)
        self.issued.append(value)
        return value

    def mint(self) -> dict[str, Any]:
        access, refresh = _user_token(), _refresh_token()
        self.issued += [access, refresh]
        self.valid_refresh.add(refresh)
        return {"access_token": access, "expires_in": 28800, "refresh_token": refresh,
                "refresh_token_expires_in": 15897600, "token_type": "bearer", "scope": ""}

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> forgeapp.HttpAnswer:
        sent = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": sent})
        # A transport that logs what it sends: the redaction literal must mask it.
        transport_log.warning("sending %s %s headers=%s body=%s", method, url, headers, sent)
        if url == forgeapp.TOKEN_URL:
            if self.token_endpoint == "network":
                raise OSError("connection reset")
            if self.token_endpoint == "down":
                return forgeapp.HttpAnswer(502, {}, b"bad gateway")
            if self.token_endpoint == "refuse":
                return _json(200, {"error": "bad_refresh_token",
                                   "error_description": "The refresh token passed is incorrect"})
            if sent.get("grant_type") == "refresh_token":
                if sent.get("refresh_token") not in self.valid_refresh:
                    return _json(200, {"error": "bad_refresh_token"})
                self.valid_refresh.discard(sent["refresh_token"])  # works once
                return _json(200, self.mint())
            if sent.get("code") not in self.valid_codes:
                return _json(200, {"error": "bad_verification_code"})
            self.valid_codes.discard(sent["code"])
            return _json(200, self.mint())
        if url == "https://api.github.com/user":
            return _json(200, {"login": self.login, "id": self.user_id})
        if url == f"https://api.github.com/applications/{CLIENT_ID}/grant" and method == "DELETE":
            return forgeapp.HttpAnswer(self.revoke_status, {}, b"")
        return forgeapp.HttpAnswer(404, {}, b"{}")

    def to(self, url: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["url"] == url]


def _json(status: int, data: dict[str, Any]) -> forgeapp.HttpAnswer:
    return forgeapp.HttpAnswer(status, {"content-type": "application/json"},
                               json.dumps(data).encode())


class FakeSlots:
    """Secret Manager's user slots: created secrets with labels, and versions."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}

    def _name(self, tenant_id: str, suffix: str) -> str:
        return f"swarm-tenant-{tenant_id}-{suffix}"

    def ensure(self, tenant_id: str, suffix: str) -> bool:
        name = self._name(tenant_id, suffix)
        if name in self.secrets:
            return False
        self.secrets[name] = {"labels": forgeapp.slot_labels(tenant_id, suffix), "versions": []}
        return True

    def add_version(self, tenant_id: str, suffix: str, value: str) -> str:
        versions = self.secrets[self._name(tenant_id, suffix)]["versions"]
        versions.append({"value": value, "enabled": True})
        return str(len(versions))

    def read_refresh(self, tenant_id: str, suffix: str) -> str:
        assert suffix.endswith("-refresh"), "swarm-api reads the -refresh twin and nothing else"
        enabled = [v for v in self.secrets[self._name(tenant_id, suffix)]["versions"]
                   if v["enabled"]]
        if not enabled:
            raise forgeapp.SlotUnreadable(f"{self._name(tenant_id, suffix)} has no version")
        return enabled[-1]["value"]

    def disable(self, tenant_id: str, suffix: str) -> int:
        secret = self.secrets.get(self._name(tenant_id, suffix))
        if secret is None:
            return 0
        count = 0
        for version in secret["versions"]:
            if version["enabled"]:
                version["enabled"] = False
                count += 1
        return count

    def latest(self, tenant_id: str, suffix: str) -> str | None:
        secret = self.secrets.get(self._name(tenant_id, suffix))
        enabled = [v for v in (secret or {}).get("versions", []) if v["enabled"]]
        return enabled[-1]["value"] if enabled else None


class FakeAppSecret:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.reads = 0

    def client_secret(self) -> str:
        self.reads += 1
        if not self.value:
            raise forgeapp.AppNotConfigured("the client secret has no version")
        return self.value


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


# -- fixtures ----------------------------------------------------------------------


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def slots() -> FakeSlots:
    return FakeSlots()


@pytest.fixture
def app_secret() -> FakeAppSecret:
    return FakeAppSecret(secrets.token_hex(20))


def _app(db, tokens, group_map, objects, clock, github, slots, app_secret, *,
         client_id: str = CLIENT_ID) -> TestClient:
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    seed_tenant(db, "eng", credentials=("git",))
    seed_tenant(db, "research", credentials=("git",))
    ctx = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
        now=clock,
    )
    service = forgeapp.ForgeApp(
        db,
        config=forgeapp.AppConfig(client_id=client_id, app_id="12345" if client_id else "",
                                  slug="swarmcloud" if client_id else ""),
        secrets=app_secret,
        slots=slots,
        send=github,
        now=clock,
    )
    return TestClient(create_app(ctx, forge_app=service), raise_server_exceptions=False)


@pytest.fixture
def api(db, tokens, group_map, objects, clock, github, slots, app_secret) -> TestClient:
    return _app(db, tokens, group_map, objects, clock, github, slots, app_secret)


SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}
AUTHORIZE = "/v1/onboarding/github/authorize"
EXCHANGE = "/v1/onboarding/github/exchange"
DISCONNECT = "/v1/onboarding/github"
REFRESH = "/v1/admin/forge/refresh"


def _state(api: TestClient, user: str = "alice", surface: str = "console") -> str:
    answer = api.post(AUTHORIZE, json={"surface": surface}, headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return parse_qs(urlparse(answer.json()["authorize_url"]).query)["state"][0]


def _connect(api: TestClient, github: FakeGitHub, user: str = "alice") -> dict[str, Any]:
    state = _state(api, user)
    answer = api.post(EXCHANGE, json={"state": state, "code": github.code()},
                      headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return answer.json()


def _suffix(email: str = "alice@saga.xyz") -> str:
    return provider_suffix("user", user=email)


def _connection_doc(db, email: str = "alice@saga.xyz", tenant: str = "eng") -> dict[str, Any]:
    return db.docs[f"forge_connections/{forgeapp.connection_id_for(tenant, email)}"]


def _assert_no_value_anywhere(db, texts: list[str], values: list[str]) -> None:
    dumped = json.dumps(db.dump(), default=str)
    for value in values:
        assert value not in dumped, "a forge value reached Firestore"
        for text in texts:
            assert value not in text, "a forge value reached a response or a log line"


# -- authorise ---------------------------------------------------------------------


def test_authorize_answers_githubs_url_with_a_state_stored_only_as_its_hash(api, db):
    answer = api.post(AUTHORIZE, json={"surface": "plugin"}, headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    url = urlparse(body["authorize_url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "github.com", "/login/oauth/authorize")
    query = parse_qs(url.query)
    assert query["client_id"] == [CLIENT_ID]
    state = query["state"][0]
    assert len(state) >= 32
    assert body["expires_in_seconds"] == 600
    pending = db.dump("forge_authorizations/")
    assert list(pending) == [f"forge_authorizations/{forgeapp.state_hash(state)}"]
    doc = next(iter(pending.values()))
    assert doc["tenant_id"] == "eng" and doc["user"] == "alice@saga.xyz"
    assert doc["surface"] == "plugin" and doc["used_at"] is None
    assert doc["expires_at"] == T0 + timedelta(minutes=10)
    assert state not in json.dumps(db.dump(), default=str)


def test_authorize_refuses_an_unknown_surface(api):
    answer = api.post(AUTHORIZE, json={"surface": "email"}, headers=auth_header("alice"))
    assert answer.status_code == 422


# -- the state: bound to the caller, single-use -------------------------------------


def _refusal(answer, code: str) -> None:
    assert answer.status_code == 400, answer.text
    body = answer.json()
    assert body["code"] == "authorisation_refused"
    assert body["detail"]["failure_code"] == code
    assert body["detail"]["recovery"] == recovery_copy(code)


def test_a_replayed_state_is_refused_and_github_is_asked_once(api, github, db):
    state = _state(api)
    code = github.code()
    first = api.post(EXCHANGE, json={"state": state, "code": code}, headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    second = api.post(EXCHANGE, json={"state": state, "code": code}, headers=auth_header("alice"))
    _refusal(second, "AUTHORISATION_EXPIRED")
    assert len(github.to(forgeapp.TOKEN_URL)) == 1


@pytest.mark.parametrize("thief", ["root", "bob"])
def test_a_state_is_bound_to_the_caller_who_asked_for_it(api, github, thief):
    """root is another member of eng; bob is research. Neither may spend
    alice's state, nothing reaches GitHub, and alice still can."""
    state = _state(api, "alice")
    stolen = api.post(EXCHANGE, json={"state": state, "code": github.code()},
                      headers=auth_header(thief))
    _refusal(stolen, "AUTHORISATION_EXPIRED")
    assert github.to(forgeapp.TOKEN_URL) == []
    mine = api.post(EXCHANGE, json={"state": state, "code": github.code()},
                    headers=auth_header("alice"))
    assert mine.status_code == 200, mine.text


def test_an_unknown_state_is_refused(api, github):
    answer = api.post(EXCHANGE, json={"state": secrets.token_urlsafe(32), "code": github.code()},
                      headers=auth_header("alice"))
    _refusal(answer, "AUTHORISATION_EXPIRED")
    assert github.to(forgeapp.TOKEN_URL) == []


def test_a_state_older_than_ten_minutes_is_refused(api, github, clock):
    state = _state(api)
    clock.at = T0 + timedelta(minutes=10, seconds=1)
    answer = api.post(EXCHANGE, json={"state": state, "code": github.code()},
                      headers=auth_header("alice"))
    _refusal(answer, "AUTHORISATION_EXPIRED")
    assert github.to(forgeapp.TOKEN_URL) == []


def test_access_denied_is_authorisation_denied_and_stores_nothing(api, github, slots, db):
    state = _state(api)
    answer = api.post(EXCHANGE, json={"state": state, "error": "access_denied"},
                      headers=auth_header("alice"))
    _refusal(answer, "AUTHORISATION_DENIED")
    assert github.calls == [] and slots.secrets == {}
    assert db.dump("forge_connections/") == {}
    # The state is spent all the same: a cancelled link does not work twice.
    again = api.post(EXCHANGE, json={"state": state, "code": github.code()},
                     headers=auth_header("alice"))
    _refusal(again, "AUTHORISATION_EXPIRED")


def test_a_code_github_refuses_stores_nothing(api, github, slots, db):
    state = _state(api)
    answer = api.post(EXCHANGE, json={"state": state, "code": secrets.token_hex(10)},
                      headers=auth_header("alice"))
    _refusal(answer, "AUTHORISATION_EXPIRED")
    assert slots.secrets == {} and db.dump("forge_connections/") == {}


@pytest.mark.parametrize("failure", ["down", "network"])
def test_an_exchange_github_did_not_answer_is_forge_unreachable(api, github, slots, failure):
    state = _state(api)
    github.token_endpoint = failure
    answer = api.post(EXCHANGE, json={"state": state, "code": github.code()},
                      headers=auth_header("alice"))
    assert answer.status_code == 503, answer.text
    assert answer.json()["detail"]["failure_code"] == "FORGE_UNREACHABLE"
    assert answer.json()["detail"]["recovery"] == recovery_copy("FORGE_UNREACHABLE")
    assert slots.secrets == {}


def test_the_exchange_body_refuses_a_token_field(api, github):
    state = _state(api)
    answer = api.post(EXCHANGE, json={"state": state, "code": github.code(),
                                      "access_token": _user_token()},
                      headers=auth_header("alice"))
    assert answer.status_code == 422


# -- the exchange ------------------------------------------------------------------


def test_the_exchange_stores_both_tokens_in_the_users_slots_and_returns_none(
        api, github, slots, db, app_secret, caplog):
    caplog.set_level(logging.DEBUG)
    state = _state(api)
    code = github.code()
    answer = api.post(EXCHANGE, json={"state": state, "code": code}, headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    access, refresh = github.issued[-2], github.issued[-1]

    suffix = _suffix()
    # D3: both slots created at onboarding, labelled for offboarding by label.
    assert slots.latest("eng", suffix) == access
    assert slots.latest("eng", suffix + "-refresh") == refresh
    labels = slots.secrets[f"swarm-tenant-eng-{suffix}"]["labels"]
    assert labels == {"managed-by": "swarm-api", "swarm-tenant": "eng", "tenant": "eng",
                      "provider": suffix}

    # The client secret was read at request time and sent only to GitHub's token endpoint.
    assert app_secret.reads >= 1
    sent = github.to(forgeapp.TOKEN_URL)[0]
    assert sent["method"] == "POST"
    assert sent["body"] == {"client_id": CLIENT_ID, "client_secret": app_secret.value, "code": code}

    connection = body["connection"]
    assert connection["state"] == "active"
    assert connection["method"] == "app_user"
    assert connection["forge_login"] == "alice-gh" and connection["forge_user_id"] == 4242
    assert connection["access_expires_at"] == (T0 + timedelta(hours=8)).isoformat()

    doc = _connection_doc(db)
    assert doc["tenant_id"] == "eng" and doc["user"] == "alice@saga.xyz"
    assert doc["state"] == "active" and doc["forge"] == "github"
    record = GitTokenRecord.from_firestore(db.docs[f"{COLLECTION}/{doc['token_id']}"])
    assert record.kind == "app_user" and record.scope.value == "user"
    assert record.provider_suffix == suffix and record.forge_login == "alice-gh"
    assert record.state is TokenState.ACTIVE

    # Nothing anywhere holds a value; the transport's log line carried one
    # and was masked by the redaction literal.
    _assert_no_value_anywhere(db, [answer.text, caplog.text],
                              [access, refresh, app_secret.value, code])
    assert MASK in caplog.text
    assert state not in answer.text


def test_a_reconnect_replaces_the_tokens_and_reactivates_the_connection(api, github, slots, db):
    _connect(api, github)
    db.docs[f"forge_connections/{forgeapp.connection_id_for('eng', 'alice@saga.xyz')}"][
        "state"] = "refresh_failed"
    again = _connect(api, github)
    assert again["connection"]["state"] == "active"
    assert again["connection"]["failure"] is None
    assert slots.latest("eng", _suffix()) == github.issued[-2]


# -- the refresh sweep -------------------------------------------------------------


def test_the_sweep_refreshes_a_connection_near_expiry_and_leaves_the_rest(
        api, github, slots, db, clock, caplog, app_secret):
    caplog.set_level(logging.DEBUG)
    _connect(api, github, "alice")
    github.login = "root-gh"
    _connect(api, github, "root")
    first_refresh = slots.latest("eng", _suffix() + "-refresh")
    root_refresh = slots.latest("eng", _suffix("root@saga.xyz") + "-refresh")
    # alice: 1h59m left; root: pushed out to 7h left.
    clock.at = T0 + timedelta(hours=6, minutes=1)
    db.docs[f"forge_connections/{forgeapp.connection_id_for('eng', 'root@saga.xyz')}"][
        "access_expires_at"] = clock.at + timedelta(hours=7)

    answer = api.post(REFRESH, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    report = answer.json()
    assert report["refreshed"] == 1 and report["failed"] == 0 and report["unreachable"] == 0

    spent = [c["body"]["refresh_token"] for c in github.to(forgeapp.TOKEN_URL)
             if c["body"].get("grant_type") == "refresh_token"]
    assert spent == [first_refresh]
    new_access, new_refresh = github.issued[-2], github.issued[-1]
    assert slots.latest("eng", _suffix()) == new_access
    assert slots.latest("eng", _suffix() + "-refresh") == new_refresh
    assert slots.latest("eng", _suffix("root@saga.xyz") + "-refresh") == root_refresh
    doc = _connection_doc(db)
    assert doc["access_expires_at"] == clock.at + timedelta(hours=8)
    assert doc["refreshed_at"] == clock.at and doc["refresh_lease"] is None
    record = GitTokenRecord.from_firestore(db.docs[f"{COLLECTION}/{doc['token_id']}"])
    assert record.expires_at == clock.at + timedelta(hours=8)
    _assert_no_value_anywhere(db, [answer.text, caplog.text], github.issued + [app_secret.value])


def _checklist(api: TestClient, user: str = "alice") -> dict[str, Any]:
    answer = api.get("/v1/onboarding", headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return answer.json()


def _step_state(view: dict[str, Any], name: str) -> str:
    return next(s for s in view["steps"] if s["step"] == name)["state"]


def test_a_connection_through_the_app_completes_the_github_connected_step(api, github):
    assert _step_state(_checklist(api), "github_connected") != "done"
    _connect(api, github)
    view = _checklist(api)
    assert _step_state(view, "github_connected") == "done"
    assert view["next_step"] != "github_connected"


def test_a_refresh_keeps_github_connected_done_past_the_stale_interval(api, github, clock):
    from swarm_api.onboarding import STALE_AFTER

    _connect(api, github)
    clock.at = T0 + STALE_AFTER + timedelta(hours=1)
    answer = api.post(REFRESH, headers=auth_header("root"))
    assert answer.status_code == 200 and answer.json()["refreshed"] == 1, answer.text
    view = _checklist(api)
    assert _step_state(view, "github_connected") == "done"
    assert view["next_step"] != "github_connected"


def test_a_refused_refresh_marks_the_connection_failed_with_the_recovery_copy(
        api, github, db, clock):
    _connect(api, github)
    clock.at = T0 + timedelta(hours=7)
    github.token_endpoint = "refuse"
    report = api.post(REFRESH, headers=SWEEPER_HEADERS)
    assert report.status_code == 200, report.text
    assert report.json()["failed"] == 1
    doc = _connection_doc(db)
    assert doc["state"] == "refresh_failed"
    assert doc["failure"] == {"code": "REFRESH_FAILED",
                              "recovery": recovery_copy("REFRESH_FAILED", login="alice-gh")}
    record = GitTokenRecord.from_firestore(db.docs[f"{COLLECTION}/{doc['token_id']}"])
    assert record.state is TokenState.EXPIRED
    # A failed connection is not tried again on the next tick.
    github.token_endpoint = "ok"
    again = api.post(REFRESH, headers=SWEEPER_HEADERS).json()
    assert again["refreshed"] == 0 and again["failed"] == 0


def test_a_refresh_token_past_its_six_months_fails_without_asking_github(
        api, github, db, clock):
    _connect(api, github)
    clock.at = T0 + timedelta(days=200)
    calls = len(github.calls)
    report = api.post(REFRESH, headers=SWEEPER_HEADERS).json()
    assert report["failed"] == 1 and len(github.calls) == calls
    assert _connection_doc(db)["state"] == "refresh_failed"


@pytest.mark.parametrize("failure", ["down", "network"])
def test_a_refresh_github_did_not_answer_changes_nothing(api, github, db, clock, slots, failure):
    _connect(api, github)
    before = slots.latest("eng", _suffix() + "-refresh")
    clock.at = T0 + timedelta(hours=7)
    github.token_endpoint = failure
    report = api.post(REFRESH, headers=SWEEPER_HEADERS).json()
    assert report["unreachable"] == 1 and report["failed"] == 0
    doc = _connection_doc(db)
    assert doc["state"] == "active" and doc["refresh_lease"] is None
    assert slots.latest("eng", _suffix() + "-refresh") == before


def test_a_connection_another_refresher_holds_is_left_to_it(api, github, db, clock):
    _connect(api, github)
    clock.at = T0 + timedelta(hours=7)
    _connection_doc(db)["refresh_lease"] = {"holder": "another", "until": clock.at
                                            + timedelta(minutes=2)}
    calls = len(github.calls)
    report = api.post(REFRESH, headers=SWEEPER_HEADERS).json()
    assert report["leased_elsewhere"] == 1 and report["refreshed"] == 0
    assert len(github.calls) == calls
    # An expired lease is taken over.
    clock.at += timedelta(minutes=5)
    assert api.post(REFRESH, headers=SWEEPER_HEADERS).json()["refreshed"] == 1


def test_the_sweep_is_an_admin_route(api):
    answer = api.post(REFRESH, headers=auth_header("alice"))
    assert answer.status_code == 403


# -- disconnect --------------------------------------------------------------------


def test_disconnect_revokes_at_github_and_disables_the_slots(
        api, github, slots, db, app_secret, caplog):
    caplog.set_level(logging.DEBUG)
    _connect(api, github)
    answer = api.delete(DISCONNECT, headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    revokes = github.to(f"https://api.github.com/applications/{CLIENT_ID}/grant")
    assert len(revokes) == 1 and revokes[0]["method"] == "DELETE"
    # The grant is revoked with a token the refresh minted, never one read
    # from the base slot, which swarm-api may not read.
    assert revokes[0]["body"]["access_token"] == github.issued[-2]
    assert body["github_revoked"] is True
    assert body["connection"]["state"] == "revoked"
    assert slots.latest("eng", _suffix()) is None
    assert slots.latest("eng", _suffix() + "-refresh") is None
    doc = _connection_doc(db)
    assert doc["state"] == "revoked" and doc["revoked_by"] == "alice@saga.xyz"
    record = GitTokenRecord.from_firestore(db.docs[f"{COLLECTION}/{doc['token_id']}"])
    assert record.state is TokenState.REVOKED
    _assert_no_value_anywhere(db, [answer.text, caplog.text], github.issued + [app_secret.value])


def test_disconnect_after_github_refused_the_refresh_still_disconnects(api, github, slots, db):
    _connect(api, github)
    github.token_endpoint = "refuse"
    body = api.delete(DISCONNECT, headers=auth_header("alice")).json()
    assert body["github_revoked"] is False
    assert "already" in body["github"]
    assert _connection_doc(db)["state"] == "revoked"
    assert slots.latest("eng", _suffix()) is None


def test_disconnect_deletes_the_callers_grants_only(api, github, db):
    _connect(api, github)
    mine = forgeapp.user_hash("alice@saga.xyz")
    theirs = forgeapp.user_hash("root@saga.xyz")
    db.docs[f"forge_grants/eng__{mine}__repo_1"] = {"tenant_id": "eng", "user_hash": mine}
    db.docs[f"forge_grants/eng__{theirs}__repo_1"] = {"tenant_id": "eng", "user_hash": theirs}
    db.docs[f"forge_grants/research__{mine}__repo_1"] = {"tenant_id": "research",
                                                         "user_hash": mine}
    body = api.delete(DISCONNECT, headers=auth_header("alice")).json()
    assert body["grants_deleted"] == 1
    assert sorted(db.dump("forge_grants/")) == [f"forge_grants/eng__{theirs}__repo_1",
                                                 f"forge_grants/research__{mine}__repo_1"]


def test_disconnect_waits_for_a_running_refresh(api, github, db, clock, slots):
    _connect(api, github)
    _connection_doc(db)["refresh_lease"] = {"holder": "sweep", "until": clock.at
                                            + timedelta(minutes=1)}
    answer = api.delete(DISCONNECT, headers=auth_header("alice"))
    assert answer.status_code == 409, answer.text
    assert _connection_doc(db)["state"] == "active"
    assert slots.latest("eng", _suffix()) is not None
    clock.at += timedelta(minutes=2)
    assert api.delete(DISCONNECT, headers=auth_header("alice")).status_code == 200


def test_a_slot_that_cannot_be_written_is_a_503_and_no_connection(api, github, slots, db):
    def refuse(tenant_id: str, suffix: str) -> bool:
        raise PermissionError("denied")

    slots.ensure = refuse
    state = _state(api)
    answer = api.post(EXCHANGE, json={"state": state, "code": github.code()},
                      headers=auth_header("alice"))
    assert answer.status_code == 503, answer.text
    assert answer.json()["code"] == "upstream_unavailable"
    assert db.dump("forge_connections/") == {}
    _assert_no_value_anywhere(db, [answer.text], github.issued)


def test_disconnect_with_no_connection_is_not_found(api, github):
    _connect(api, github, "alice")
    assert api.delete(DISCONNECT, headers=auth_header("root")).status_code == 404
    assert api.delete(DISCONNECT, headers=auth_header("bob")).status_code == 404


# -- the App not configured --------------------------------------------------------


def _not_configured(answer) -> None:
    assert answer.status_code == 503, answer.text
    body = answer.json()
    assert body["code"] == "github_app_not_configured"
    assert "docs/runbooks/github-app.md" in body["message"]


def test_every_route_says_not_configured_without_a_client_id(
        db, tokens, group_map, objects, clock, github, slots, app_secret):
    api = _app(db, tokens, group_map, objects, clock, github, slots, app_secret, client_id="")
    _not_configured(api.post(AUTHORIZE, json={"surface": "console"}, headers=auth_header("alice")))
    _not_configured(api.post(EXCHANGE, json={"state": "s" * 43, "code": "c"},
                             headers=auth_header("alice")))
    _not_configured(api.delete(DISCONNECT, headers=auth_header("alice")))
    _sweep_not_configured(api, db)
    assert github.calls == []


def _sweep_not_configured(api: TestClient, db) -> None:
    """With no connection to keep alive the sweep has nothing undone: 200,
    saying so. With an active one it is that user's token running out: 503."""
    idle = api.post(REFRESH, headers=SWEEPER_HEADERS)
    assert idle.status_code == 200, idle.text
    assert idle.json()["configured"] is False and idle.json()["refreshed"] == 0
    assert "not configured" in idle.json()["message"]
    assert "docs/runbooks/github-app.md" in idle.json()["message"]
    db.docs[f"forge_connections/{forgeapp.connection_id_for('eng', 'alice@saga.xyz')}"] = {
        "connection_id": forgeapp.connection_id_for("eng", "alice@saga.xyz"),
        "tenant_id": "eng", "user": "alice@saga.xyz", "state": "active"}
    _not_configured(api.post(REFRESH, headers=SWEEPER_HEADERS))


def test_every_route_says_not_configured_without_a_client_secret(
        db, tokens, group_map, objects, clock, github, slots):
    api = _app(db, tokens, group_map, objects, clock, github, slots, FakeAppSecret(None))
    _not_configured(api.post(AUTHORIZE, json={"surface": "console"}, headers=auth_header("alice")))
    _not_configured(api.post(EXCHANGE, json={"state": "s" * 43, "code": "c"},
                             headers=auth_header("alice")))
    _not_configured(api.delete(DISCONNECT, headers=auth_header("alice")))
    _sweep_not_configured(api, db)
    assert github.calls == []


def test_the_config_reads_the_apps_public_settings_from_the_environment():
    empty = forgeapp.AppConfig.from_env({})
    assert empty.missing() == ["GITHUB_APP_CLIENT_ID"]
    full = forgeapp.AppConfig.from_env({"GITHUB_APP_CLIENT_ID": CLIENT_ID,
                                        "GITHUB_APP_ID": "12345",
                                        "GITHUB_APP_SLUG": "swarmcloud"})
    assert full.missing() == [] and full.client_id == CLIENT_ID


# -- the app_user kind -------------------------------------------------------------


def test_the_app_user_kind():
    assert "app_user" in TOKEN_KINDS
    assert token_kind(_user_token()) == "app_user"
    record = GitTokenRecord.from_firestore({
        "token_id": "tok_" + "0" * 16, "tenant_id": "eng", "scope": "user",
        "provider_suffix": _suffix(), "secret_name": "x", "registered_at": T0,
        "kind": "app_user", "expires_at": T0 + timedelta(hours=3),
    })
    status = expiry_status(record, T0)
    assert status["level"] == "by_design"
