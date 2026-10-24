"""The runner-key link (docs/design/secrets-scopes.md §4, §6.2, §6.3, §11 Q8;
lane SSC-LINKS, part of #2158).

BGD, 2026-10-23 (Q8): a value arrives only through the console form a
one-time link opens, or a terminal's --stdin; no route, tool or log takes or
prints one. `POST /v1/tenants/me/credentials/links` mints the link to the
runner-key form, and `POST /v1/tenants/me/credentials` consumes it.

What these tests hold:

  * the mint takes no value and answers its URL, path, provider and expiry
    and nothing else; the token is never stored, only its digest;
  * the link is ten minutes and ONE use, bound to the caller, the workspace
    and the provider: replayed, expired, someone else's, another provider's
    or never minted, it is `KEY_LINK_INVALID` with the switch on and nothing
    is written, and a link presented by the wrong person or for the wrong
    provider is not burned;
  * off (the default), a bad link is logged without the token and the write
    goes on, on the caller's own session; a write without a link (a
    terminal's --stdin) is unchanged;
  * the mint passes the write's own gates first: an unknown provider is 422.

Every key and token is built at run time, never one literal.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import refusals
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import tenants
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header

LINKS = "/v1/tenants/me/credentials/links"
WRITE = "/v1/tenants/me/credentials"
SWITCH = refusals.env_name("KEY_LINK_INVALID")


def fake_key() -> str:
    """A key made at run time, never one literal."""
    return "k-" + secrets.token_hex(20)


def mint(client, user: str = "alice", provider: str = "anthropic"):
    return client.post(LINKS, headers=auth_header(user), json={"provider": provider})


def token_of(response) -> str:
    return parse_qs(urlparse(response.json()["path"]).query)["t"][0]


def write(client, user: str = "alice", provider: str = "anthropic", link_token: str | None = None):
    body = {"provider": provider, "api_key": fake_key()}
    if link_token is not None:
        body["link_token"] = link_token
    return client.post(WRITE, headers=auth_header(user), json=body)


def link_docs(db) -> dict:
    return {p: d for p, d in db.docs.items() if p.startswith(f"{tenants.RUNNER_KEY_LINKS}/")}


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")


@pytest.fixture
def report_only(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


def test_a_link_answers_only_its_url_path_provider_and_expiry(client, db):
    response = mint(client)
    assert response.status_code == 201, response.text
    # MUTATION: return the link document from the mint and this has more keys.
    assert set(response.json()) == {"url", "path", "provider", "expires_at"}
    assert response.json()["provider"] == "anthropic"
    # This context configures no console origin: no bare path is passed off as a URL.
    assert response.json()["url"] is None
    token = token_of(response)
    assert response.json()["path"] == tenants.RUNNER_KEY_FORM.format(provider="anthropic", token=token)
    docs = link_docs(db)
    # The token is never stored: only its digest names the document.
    assert list(docs) == [f"{tenants.RUNNER_KEY_LINKS}/{hashlib.sha256(token.encode()).hexdigest()}"]
    (link,) = docs.values()
    assert token not in repr(link)
    assert (link["email"], link["provider"], link["used_at"]) == ("alice@example.com", "anthropic", None)
    assert link["tenant_id"] == "eng"
    # MUTATION: make RUNNER_KEY_LINK_TTL fifteen minutes and this fails.
    assert link["expires_at"] - link["created_at"] == timedelta(minutes=10)


def test_the_mint_takes_no_value(client, db):
    response = client.post(LINKS, headers=auth_header("alice"),
                           json={"provider": "anthropic", "api_key": fake_key()})
    # MUTATION: let RunnerKeyLinkCreate accept extra fields and this is 201.
    assert response.status_code == 422
    assert link_docs(db) == {}


def test_an_unknown_provider_gets_no_link(client, db):
    # MUTATION: skip `_provider` in the mint and this is 201.
    assert mint(client, provider="nonesuch").status_code == 422
    assert link_docs(db) == {}


def test_a_link_sets_the_key_once(client, db, enforced):
    token = token_of(mint(client))
    first = write(client, link_token=token)
    assert first.status_code == 201, first.text
    assert first.json()["via_link"] is True
    assert next(iter(link_docs(db).values()))["used_at"] is not None
    # MUTATION: drop the `used_at` check in `consume_runner_key_link` and this is 201.
    replay = write(client, link_token=token)
    assert replay.status_code == 403
    assert replay.json()["code"] == "KEY_LINK_INVALID"
    assert token not in replay.text


def test_a_link_is_bound_to_the_caller_and_not_burned_by_another(client, db, enforced):
    token = token_of(mint(client, "alice"))
    # root is in eng too, and an admin: the same workspace, another person.
    # MUTATION: drop `email` from the link comparison and this is 201.
    stolen = write(client, "root", link_token=token)
    assert stolen.status_code == 403
    assert stolen.json()["code"] == "KEY_LINK_INVALID"
    assert next(iter(link_docs(db).values()))["used_at"] is None
    assert write(client, "alice", link_token=token).status_code == 201


def test_a_link_is_bound_to_its_provider_and_not_burned_by_another(client, db, enforced):
    token = token_of(mint(client, provider="anthropic"))
    # MUTATION: drop `provider` from the link comparison and this is 201.
    other = write(client, provider="openai", link_token=token)
    assert other.status_code == 403
    assert other.json()["code"] == "KEY_LINK_INVALID"
    assert next(iter(link_docs(db).values()))["used_at"] is None
    assert write(client, provider="anthropic", link_token=token).status_code == 201


def test_a_link_expires_after_ten_minutes(db):
    start = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(16)
    db.docs[f"{tenants.RUNNER_KEY_LINKS}/{hashlib.sha256(token.encode()).hexdigest()}"] = {
        "email": "alice@example.com", "tenant_id": "eng", "provider": "anthropic",
        "created_at": start, "expires_at": start + tenants.RUNNER_KEY_LINK_TTL, "used_at": None}
    bound = {"email": "alice@example.com", "tenant_id": "eng", "provider": "anthropic"}
    # MUTATION: skip the `expires_at` check and this is True.
    assert tenants.consume_runner_key_link(db, token, now=start + timedelta(minutes=10, seconds=1),
                                           **bound) is False
    # Another workspace's link is never this one's.
    assert tenants.consume_runner_key_link(db, token, now=start, **{**bound, "tenant_id": "research"}) is False
    # CONTROL: inside its life, for its own binding, it is accepted, once.
    assert tenants.consume_runner_key_link(db, token, now=start + timedelta(minutes=9), **bound) is True
    assert tenants.consume_runner_key_link(db, token, now=start + timedelta(minutes=9), **bound) is False


def test_a_made_up_link_is_refused_when_enforced(client, db, enforced):
    made_up = secrets.token_urlsafe(32)
    response = write(client, link_token=made_up)
    assert response.status_code == 403
    assert response.json()["code"] == "KEY_LINK_INVALID"


def test_off_a_bad_link_is_logged_and_the_write_goes_on(client, db, report_only, caplog):
    """The link grants nothing the caller's own session cannot do, which is
    why its refusal is the provider-key link's switch, off at ship."""
    assert refusals.SWITCHES["KEY_LINK_INVALID"].default is False
    made_up = secrets.token_urlsafe(32)
    caplog.set_level(logging.DEBUG)
    response = write(client, link_token=made_up)
    assert response.status_code == 201, response.text
    assert response.json()["via_link"] is False
    assert "refusal report-only code=KEY_LINK_INVALID" in caplog.text
    assert made_up not in caplog.text


def test_a_write_without_a_link_is_unchanged(client, db, enforced, caplog):
    """A terminal's --stdin write sends no link, with the switch on or off."""
    caplog.set_level(logging.DEBUG)
    response = write(client)
    assert response.status_code == 201, response.text
    assert response.json()["via_link"] is False
    assert "KEY_LINK_INVALID" not in caplog.text


def test_no_token_or_key_is_logged(client, db, caplog):
    caplog.set_level(logging.DEBUG)
    response = mint(client)
    token = token_of(response)
    key = fake_key()
    client.post(WRITE, headers=auth_header("alice"),
                json={"provider": "anthropic", "api_key": key, "link_token": token})
    assert token not in caplog.text and key not in caplog.text
    assert "minted a runner-key link provider=anthropic tenant=eng" in caplog.text



def test_with_a_console_origin_the_url_is_the_form_on_it(db, tokens, group_map, objects):
    context = build_context(
        settings=api_settings(console_url="https://console.example.test/"),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    response = mint(TestClient(create_app(context), raise_server_exceptions=False))
    assert response.status_code == 201, response.text
    assert response.json()["url"] == "https://console.example.test" + response.json()["path"]
