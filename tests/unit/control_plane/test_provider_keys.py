"""TypeSafe provider keys: a person's own key, a tenant key, and no read path (KEYS-API).

Owner request 2026-10-14: a way to set the TypeSafe API key safely. Scope is
BOTH -- a person's own key in their personal tenant `u-<user>`, else a
tenant-wide key an admin set, else none -- and the key is set only through the
console form or a terminal hidden prompt, never through Claude.

The attackers these tests stand in for (docs/provider-keys.md, threat model):

  * ANOTHER TENANT MEMBER -- may not set, delete or learn anything about a
    tenant key unless they are an admin, and can never reach a person's own
    key at all: no route takes a user or a tenant id.
  * A LEAKED LINK -- the one-time token is single use, expires in ten
    minutes, is bound to the person who minted it, and grants nothing the
    person's own console session could not already do.
  * A LOG READER -- the key never appears in a log line, a response, or a
    Firestore document.

Every fake key is built at runtime (CLAUDE.md: nothing added may look like a
credential). Offline: FakeFirestore and the in-memory credential writer.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import refusals
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.provider_keys import (
    KEY_LINK_TTL,
    LINKS_COLLECTION,
    PROVIDER_KEY_PROVIDERS,
    TYPESAFE,
    resolve_provider_key,
)
from swarm_api.validation import (
    APP_CREDENTIAL_PROVIDERS,
    FORGE_PROVIDER,
    known_providers,
)
from swarm_api.waker import NullWaker

from .conftest import (
    ADMIN_GROUP,
    ENG_GROUP,
    RESEARCH_GROUP,
    NoForgeTokens,
    api_settings,
    auth_header,
    seed_tenant,
)
from .fakes import FakeFirestore

CONSOLE = "https://console.example.test"
BASE = "/v1/provider-keys/typesafe"


def fake_key(fill: str = "x") -> str:
    """Shaped like a key, built at runtime so no literal looks like one."""
    return "ts_" + fill * 40


def seed_personal(db: FakeFirestore, user: str, *, principal: str | None = None,
                  state: str = "ready") -> None:
    """`tenants/u-<user>` and `workspaces/u-<user>`, as the workspace job leaves them."""
    tenant_id = f"u-{user}"
    owner = principal or f"{user}@saga.xyz"
    seed_tenant(db, tenant_id)
    db.docs[f"tenants/{tenant_id}"].update(kind="user", principal=owner, display_name=owner)
    db.docs[f"workspaces/{tenant_id}"] = {
        "tenant_id": tenant_id, "workspace_id": f"w-{user[:6]:0<6}",
        "principal": owner, "state": state,
    }


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """alice and dave in eng, bob in research, root an admin in eng; carol in
    no group."""
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "carol@saga.xyz": (),
        "dave@saga.xyz": (ENG_GROUP,),
        "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def credentials() -> InMemoryCredentials:
    return InMemoryCredentials()


@pytest.fixture
def api_context(db, tokens, group_map, objects, credentials):
    return build_context(
        settings=api_settings(console_url=CONSOLE),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=credentials,
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


@pytest.fixture
def seeded(db: FakeFirestore) -> FakeFirestore:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_personal(db, "alice")
    seed_personal(db, "bob")
    return db


def as_(who: str, tenant: str | None = None) -> dict[str, str]:
    headers = auth_header(who)
    if tenant is not None:
        headers[TENANT_HEADER] = tenant
    return headers


def put_user(client, who: str, key: str, **extra):
    return client.put(f"{BASE}/user", headers=as_(who), json={"api_key": key, **extra})


def put_tenant(client, who: str, key: str, **extra):
    return client.put(f"{BASE}/tenant", headers=as_(who), json={"api_key": key, **extra})


def status_of(client, who: str, tenant: str | None = None) -> dict:
    """`tenant` selects a group: a person's ready personal workspace is their
    DEFAULT tenant (#1200), so a group's key is read with the header."""
    response = client.get(BASE, headers=as_(who, tenant))
    assert response.status_code == 200, response.text
    return response.json()


def audits(db: FakeFirestore) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith(f"{AUDIT_COLLECTION}/")]


def mint(client, user: str, scope: str) -> str:
    response = client.post(f"{BASE}/links", headers=auth_header(user), json={"scope": scope})
    assert response.status_code == 201, response.text
    return response.json()["url"]


def token_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["t"][0]


# -- the provider ------------------------------------------------------------

def test_typesafe_is_a_provider_key_provider_and_neither_a_forge_nor_an_app_key():
    assert TYPESAFE == "typesafe"
    assert TYPESAFE in PROVIDER_KEY_PROVIDERS
    assert TYPESAFE not in APP_CREDENTIAL_PROVIDERS
    assert TYPESAFE != FORGE_PROVIDER


def test_the_member_credential_route_does_not_take_a_typesafe_key(client, seeded, credentials):
    """`POST /v1/tenants/me/credentials` lets ANY member write the tenant's
    secret. A TypeSafe tenant key is an admin's, so that route must refuse it
    rather than become a second, unguarded way to set it."""
    assert TYPESAFE not in known_providers()
    response = client.post("/v1/tenants/me/credentials", headers=auth_header("alice"),
                           json={"provider": TYPESAFE, "api_key": fake_key()})
    assert response.status_code == 422
    assert credentials._versions == {}


def test_an_unknown_provider_is_refused_on_every_new_route(client, seeded):
    other = "/v1/provider-keys/openai"
    assert client.get(other, headers=auth_header("alice")).status_code == 422
    assert client.put(f"{other}/user", headers=auth_header("alice"),
                      json={"api_key": fake_key()}).status_code == 422
    assert client.post(f"{other}/links", headers=auth_header("alice"),
                       json={"scope": "user"}).status_code == 422


# -- resolution: a pure function over every combination -----------------------

@pytest.mark.parametrize("user_set,tenant_set,expected", [
    (True, True, "swarm-tenant-u-alice-typesafe"),
    (True, False, "swarm-tenant-u-alice-typesafe"),
    (False, True, "swarm-tenant-eng-typesafe"),
    (False, False, None),
])
def test_resolution_prefers_the_persons_own_key_then_the_tenants(user_set, tenant_set, expected):
    present = set()
    if user_set:
        present.add("swarm-tenant-u-alice-typesafe")
    if tenant_set:
        present.add("swarm-tenant-eng-typesafe")
    assert resolve_provider_key("eng", "u-alice", TYPESAFE, present) == expected


@pytest.mark.parametrize("present,expected", [
    ({"swarm-tenant-u-alice-typesafe"}, "swarm-tenant-u-alice-typesafe"),
    (set(), None),
])
def test_resolution_in_the_persons_own_tenant_is_their_own_key(present, expected):
    assert resolve_provider_key("u-alice", "u-alice", TYPESAFE, present) == expected


@pytest.mark.parametrize("submitter_tenant", [None, ""])
def test_resolution_without_a_confirmed_personal_tenant_falls_to_the_tenant_key(submitter_tenant):
    """A service identity, or a person whose `u-` tenant is unconfirmed --
    including a namesake whose slug is another person's -- has no own key."""
    present = {"swarm-tenant-eng-typesafe", "swarm-tenant-u-alice-typesafe"}
    assert resolve_provider_key("eng", submitter_tenant, TYPESAFE, present) == \
        "swarm-tenant-eng-typesafe"
    assert resolve_provider_key("eng", submitter_tenant, TYPESAFE,
                                {"swarm-tenant-u-alice-typesafe"}) is None


def test_resolution_never_picks_another_persons_key():
    present = {"swarm-tenant-u-bob-typesafe"}
    assert resolve_provider_key("eng", "u-alice", TYPESAFE, present) is None


def test_resolution_refuses_a_provider_outside_the_allow_list():
    with pytest.raises(ValueError):
        resolve_provider_key("eng", "u-alice", FORGE_PROVIDER, set())


# -- the person's own key ----------------------------------------------------

def test_a_person_stores_their_own_key_in_their_personal_tenant(client, seeded, credentials):
    response = put_user(client, "alice", fake_key())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"]["secret_id"] == "swarm-tenant-u-alice-typesafe"
    assert body["status"]["set"] is True
    assert credentials._versions == {"swarm-tenant-u-alice-typesafe": 1}
    # Bound to the PERSONAL tenant's worker account, never the group's.
    assert credentials._bindings["swarm-tenant-u-alice-typesafe"].startswith(
        "swarm-agent-worker-u-alice@")

    status = status_of(client, "alice", "eng")
    assert status["user"]["set"] is True
    assert status["user"]["set_by"] == "alice@saga.xyz"
    assert status["user"]["version"].endswith("/versions/1")
    assert status["user"]["set_at"]
    assert status["resolution"] == {
        "tenant_id": "eng", "uses": "user", "secret_id": "swarm-tenant-u-alice-typesafe",
    }


def test_a_person_without_a_ready_personal_workspace_cannot_store_one(client, seeded, credentials):
    # carol has no workspace at all; dave's is approved but not built.
    seed_personal(seeded, "dave", state="approved")
    for user in ("carol", "dave"):
        response = put_user(client, user, fake_key())
        assert response.status_code == 404, response.text
    assert credentials._versions == {}
    assert status_of(client, "carol")["user"] == {
        "available": False, "set": False, "set_at": None, "set_by": None, "version": None,
        "secret_id": None,
    }


def test_a_personal_tenant_named_for_someone_else_is_not_the_callers(client, seeded, credentials):
    """The frozen slug keeps only the local part: alice@partner.com would
    derive u-alice too. The tenant document's principal decides whose it is."""
    seed_personal(seeded, "carol", principal="carol@partner.com")
    assert put_user(client, "carol", fake_key()).status_code == 404
    assert client.delete(f"{BASE}/user", headers=as_("carol")).status_code == 404
    assert credentials._versions == {}


def test_a_namesakes_key_is_never_resolved_for_the_caller(client, seeded):
    """dave@saga.xyz is in eng; `u-dave` belongs to dave@partner.com, who has
    a key in it. The frozen slug derives `u-dave` for both, so a resolver fed
    the email would hand the partner's key to dave's tasks in eng."""
    seed_personal(seeded, "dave", principal="dave@partner.com")
    seeded.docs["provider_keys/swarm-tenant-u-dave-typesafe"] = {
        "tenant_id": "u-dave", "provider": TYPESAFE, "scope": "user", "set": True,
        "set_at": datetime.now(timezone.utc), "set_by": "dave@partner.com",
        "version": "projects/local/secrets/swarm-tenant-u-dave-typesafe/versions/1",
    }
    status = status_of(client, "dave", "eng")
    assert status["user"]["available"] is False and status["user"]["set"] is False
    assert status["resolution"] == {"tenant_id": "eng", "uses": "none", "secret_id": None}


def test_the_user_key_body_names_no_user_or_tenant(client, seeded, credentials):
    for field, value in (("user", "bob@saga.xyz"), ("tenant_id", "u-bob"), ("scope", "tenant")):
        response = put_user(client, "alice", fake_key(), **{field: value})
        assert response.status_code == 422, (field, response.text)
    assert credentials._versions == {}


def test_a_person_deletes_their_own_key(client, seeded, credentials):
    put_user(client, "alice", fake_key())
    response = client.delete(f"{BASE}/user", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert response.json()["status"]["set"] is False
    assert credentials._destroyed == {"swarm-tenant-u-alice-typesafe": 1}
    status = status_of(client, "alice")
    assert status["user"]["set"] is False
    assert status["resolution"]["uses"] == "none"


def test_a_person_whose_workspace_left_ready_can_still_delete_their_key(client, seeded,
                                                                       credentials):
    put_user(client, "alice", fake_key())
    seeded.docs["workspaces/u-alice"]["state"] = "suspended"
    assert put_user(client, "alice", fake_key("y")).status_code == 404
    response = client.delete(f"{BASE}/user", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert credentials._destroyed == {"swarm-tenant-u-alice-typesafe": 1}


def test_a_tenant_key_is_never_written_into_a_personal_workspace(client, seeded, credentials):
    """In an admin's own workspace the "tenant" key would be the admin's own
    key under the wrong scope; that is the user route's."""
    seed_personal(seeded, "root")
    response = client.put(f"{BASE}/tenant", headers=as_("root", "u-root"),
                          json={"api_key": fake_key()})
    assert response.status_code == 422, response.text
    assert client.delete(f"{BASE}/tenant", headers=as_("root", "u-root")).status_code == 422
    assert credentials._versions == {}


def test_deleting_a_key_that_was_never_set_is_a_404(client, seeded):
    assert client.delete(f"{BASE}/user", headers=auth_header("alice")).status_code == 404


# -- the tenant key ----------------------------------------------------------

def test_only_an_admin_sets_or_deletes_the_tenant_key(client, seeded, credentials):
    assert put_tenant(client, "alice", fake_key()).status_code == 403
    assert credentials._versions == {}

    response = put_tenant(client, "root", fake_key())
    assert response.status_code == 200, response.text
    assert credentials._versions == {"swarm-tenant-eng-typesafe": 1}

    assert client.delete(f"{BASE}/tenant", headers=auth_header("alice")).status_code == 403
    assert status_of(client, "alice", "eng")["tenant"]["set"] is True


def test_the_tenant_key_is_used_until_the_person_sets_their_own(client, seeded):
    put_tenant(client, "root", fake_key())
    status = status_of(client, "alice", "eng")
    assert status["tenant"] == {
        "available": True, "set": True, "set_by": "root@saga.xyz",
        "set_at": status["tenant"]["set_at"], "version": status["tenant"]["version"],
        "secret_id": "swarm-tenant-eng-typesafe", "tenant_id": "eng",
    }
    assert status["resolution"]["uses"] == "tenant"
    put_user(client, "alice", fake_key("y"))
    assert status_of(client, "alice", "eng")["resolution"]["uses"] == "user"
    # In her own workspace the tenant key IS her own key: one secret.
    assert status_of(client, "alice")["resolution"]["secret_id"] == "swarm-tenant-u-alice-typesafe"


def test_another_tenants_member_learns_nothing_about_this_tenants_key(client, seeded):
    put_tenant(client, "root", fake_key())
    put_user(client, "alice", fake_key("y"))
    status = status_of(client, "bob", "research")
    assert status["tenant"]["tenant_id"] == "research"
    assert status["tenant"]["set"] is False
    assert status["user"]["set"] is False
    assert status["resolution"]["uses"] == "none"
    assert "eng" not in json.dumps(status) and "u-alice" not in json.dumps(status)


def test_an_admin_deletes_the_tenant_key(client, seeded, credentials):
    put_tenant(client, "root", fake_key())
    response = client.delete(f"{BASE}/tenant", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert credentials._destroyed == {"swarm-tenant-eng-typesafe": 1}
    assert status_of(client, "alice", "eng")["tenant"]["set"] is False


def test_a_malformed_key_is_refused_and_nothing_is_written(client, seeded, credentials):
    for key in ("ts_" + "x", "ts_" + "x" * 20 + "\n" + "y" * 20):
        response = put_user(client, "alice", key)
        assert response.status_code == 422, response.text
    assert credentials._versions == {}
    assert audits(seeded) == []


# -- the audit ---------------------------------------------------------------

def test_every_set_and_delete_is_audited_without_the_value(client, seeded):
    key = fake_key()
    put_user(client, "alice", key)
    client.delete(f"{BASE}/user", headers=auth_header("alice"))
    put_tenant(client, "root", key)
    client.delete(f"{BASE}/tenant", headers=auth_header("root"))

    entries = sorted(audits(seeded), key=lambda e: e["at"])
    assert [(e["action"], e["by"], e["detail"]["scope"], e["detail"]["tenant_id"])
            for e in entries] == [
        ("provider_key_set", "alice@saga.xyz", "user", "u-alice"),
        ("provider_key_deleted", "alice@saga.xyz", "user", "u-alice"),
        ("provider_key_set", "root@saga.xyz", "tenant", "eng"),
        ("provider_key_deleted", "root@saga.xyz", "tenant", "eng"),
    ]
    for entry in entries:
        assert entry["detail"]["provider"] == TYPESAFE
        assert entry["at"]
    assert key not in repr(seeded.docs)


# -- the log reader ----------------------------------------------------------

def test_the_key_never_reaches_a_log_a_response_or_a_document(client, seeded, caplog):
    key = fake_key()
    caplog.set_level(logging.DEBUG)
    bodies = []
    url = mint(client, "alice", "user")
    bodies.append(put_user(client, "alice", key, link_token=token_of(url)).text)
    bodies.append(put_tenant(client, "root", key).text)
    bodies.append(client.get(BASE, headers=as_("alice", "eng")).text)
    bodies.append(client.get("/v1/tenants/me", headers=auth_header("alice")).text)
    # A refused write, too: the error path is where a value is echoed by accident.
    bodies.append(put_user(client, "carol", key).text)
    bodies.append(put_tenant(client, "alice", key).text)
    bodies.append(put_user(client, "alice", key, extra="nope").text)

    for body in bodies:
        assert key not in body
    assert key not in caplog.text
    assert key[3:] not in caplog.text
    assert key not in repr(seeded.docs)


# -- the one-time link -------------------------------------------------------

def test_a_link_is_a_console_url_and_its_token_is_stored_only_hashed(client, seeded, api_context):
    url = mint(client, "alice", "user")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}" == CONSOLE
    assert parsed.path == "/settings/keys/typesafe"
    assert parse_qs(parsed.query)["scope"] == ["user"]
    token = token_of(url)
    assert len(token) >= 32

    links = {path: doc for path, doc in seeded.docs.items()
             if path.startswith(f"{LINKS_COLLECTION}/")}
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert list(links) == [f"{LINKS_COLLECTION}/{digest}"]
    assert token not in repr(seeded.docs)
    link = links[f"{LINKS_COLLECTION}/{digest}"]
    assert link["email"] == "alice@saga.xyz"
    assert link["scope"] == "user"
    assert link["provider"] == TYPESAFE
    assert link["tenant_id"] == "u-alice"
    assert link["used_at"] is None
    assert link["expires_at"] - link["created_at"] == KEY_LINK_TTL == timedelta(minutes=10)


def test_a_tenant_link_is_minted_only_for_an_admin(client, seeded):
    response = client.post(f"{BASE}/links", headers=auth_header("alice"), json={"scope": "tenant"})
    assert response.status_code == 403
    url = mint(client, "root", "tenant")
    assert parse_qs(urlparse(url).query)["scope"] == ["tenant"]


def test_a_user_link_needs_the_persons_own_workspace(client, seeded):
    response = client.post(f"{BASE}/links", headers=auth_header("carol"), json={"scope": "user"})
    assert response.status_code == 404


def test_a_link_is_consumed_by_the_write(client, seeded):
    token = token_of(mint(client, "alice", "user"))
    response = put_user(client, "alice", fake_key(), link_token=token)
    assert response.status_code == 200, response.text
    assert response.json()["via_link"] is True
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert seeded.docs[f"{LINKS_COLLECTION}/{digest}"]["used_at"] is not None
    entry = audits(seeded)[0]
    assert entry["detail"]["via_link"] is True


@pytest.fixture
def links_enforced(monkeypatch):
    monkeypatch.setenv(refusals.env_name("KEY_LINK_INVALID"), "on")


def _attempts(client, seeded):
    """Each a way a leaked or stale link is presented, with who presents it."""
    used = token_of(mint(client, "alice", "user"))
    put_user(client, "alice", fake_key(), link_token=used)

    expired = token_of(mint(client, "alice", "user"))
    digest = hashlib.sha256(expired.encode()).hexdigest()
    seeded.docs[f"{LINKS_COLLECTION}/{digest}"]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1))

    alices = token_of(mint(client, "alice", "user"))
    seed_personal(seeded, "root")
    tenant_link = token_of(mint(client, "root", "tenant"))
    made_up = "t" + "q" * 42
    return [
        ("alice", "user", used),             # replayed
        ("alice", "user", expired),          # past ten minutes
        ("bob", "user", alices),             # someone else's, leaked
        ("root", "user", tenant_link),       # minted for the other scope
        ("alice", "user", made_up),          # never minted
    ]


def test_a_stale_or_stolen_link_is_refused_when_enforced(client, seeded, credentials,
                                                         links_enforced):
    attempts = _attempts(client, seeded)
    before = dict(credentials._versions)
    for user, scope, token in attempts:
        response = client.put(f"{BASE}/{scope}", headers=auth_header(user),
                              json={"api_key": fake_key("z"), "link_token": token})
        assert response.status_code == 403, (user, response.text)
        assert response.json()["code"] == "KEY_LINK_INVALID"
        assert token not in response.text
    assert credentials._versions == before


def test_a_stolen_link_is_not_consumed_by_the_thief(client, seeded, links_enforced):
    alices = token_of(mint(client, "alice", "user"))
    assert put_user(client, "bob", fake_key(), link_token=alices).status_code == 403
    assert put_user(client, "alice", fake_key(), link_token=alices).status_code == 200


def test_the_link_refusal_ships_off_and_off_grants_nothing_new(client, seeded, credentials,
                                                               caplog, monkeypatch):
    """Off, a bad link is logged and the write goes through on the caller's own
    session -- exactly what a direct visit to the settings page does, which is
    why this refusal can ship report-only. The token is never logged."""
    monkeypatch.delenv(refusals.env_name("KEY_LINK_INVALID"), raising=False)
    switch = refusals.SWITCHES["KEY_LINK_INVALID"]
    assert switch.default is False
    attempts = _attempts(client, seeded)
    caplog.set_level(logging.DEBUG)
    bob = put_user(client, "bob", fake_key(), link_token=attempts[2][2])
    assert bob.status_code == 200, bob.text
    assert bob.json()["via_link"] is False
    # It went into BOB's own tenant: a link never moves a write elsewhere.
    assert bob.json()["status"]["secret_id"] == "swarm-tenant-u-bob-typesafe"
    assert "refusal report-only code=KEY_LINK_INVALID" in caplog.text
    for _, _, token in attempts:
        assert token not in caplog.text
    # The non-admin is still refused the tenant key whatever the link says.
    assert put_tenant(client, "alice", fake_key(), link_token=attempts[3][2]).status_code == 403


def test_a_link_without_a_console_origin_answers_the_path(db, tokens, group_map, objects, seeded):
    ctx = build_context(
        settings=api_settings(console_url=""), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=objects, forge_tokens=NoForgeTokens(),
    )
    bare = TestClient(create_app(ctx), raise_server_exceptions=False)
    response = bare.post(f"{BASE}/links", headers=auth_header("alice"), json={"scope": "user"})
    assert response.status_code == 201, response.text
    assert response.json()["url"] is None
    assert response.json()["path"].startswith("/settings/keys/typesafe?scope=user&t=")
