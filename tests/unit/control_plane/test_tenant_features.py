"""`GET /v1/tenants/me` says whether three features are on for the workspace (FEATURES-READ, part of #1748).

Owner decision, 2026-10-20. A caller such as Product Oracle (`u-or-9a0792`)
must not have to learn at submit or answer time whether

  * `caller_mcp`: registered MCP servers, `mcp_servers.mcp_servers_enabled()`
    (`SWARM_MCP_SERVERS`) as this process reads it;
  * `await_input`: the `await-input` runner profile's `available`, read from
    the frozen catalogue;
  * `service_answer`: the workspace is a service workspace whose own principal
    may answer its waits -- `awaitinput.service_principal`, the predicate the
    answer route itself applies.

Each value is `true`, `false` or `"unknown"`, and a state that could not be
read is NEVER served as `false`: Oracle's New run form warned its users that
agents could not read Oracle on 2026-10-20 because nothing said otherwise.

Offline: in-memory Firestore, static tokens. Nothing builds a real credential.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import awaitinput, mcp_servers
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_common.identity import tenant_id_for_user
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant

AWAIT = "await-input"
#: A service tenant's principal and its tenant, from the frozen rule.
ORACLE = "oracle-api@saga-agents-staging.iam.gserviceaccount.com"
ORACLE_TENANT = tenant_id_for_user(ORACLE)


@pytest.fixture
def tokens(tokens):
    return {**tokens, "token-oracle": {"email": ORACLE, "email_verified": True,
                                       "sub": f"sub-{ORACLE}"}}


@pytest.fixture
def client(db, tokens, group_map, objects) -> TestClient:
    seed_tenant(db, "eng")
    # The service tenant, as `Store.ensure_tenant` writes it on oracle-api's
    # first submission: `kind: "user"`, its principal its own address.
    db.docs[f"tenants/{ORACLE_TENANT}"] = asdict(Tenant(
        tenant_id=ORACLE_TENANT, kind="user", principal=ORACLE,
        created_at=datetime.now(timezone.utc), display_name=ORACLE_TENANT, max_active=4,
        capacity_units=8, enabled=True, credentials=[],
    ))
    context = build_context(
        settings=api_settings(allowed_users=(ORACLE,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def features(client: TestClient, user: str, **headers: str) -> dict[str, Any]:
    response = client.get("/v1/tenants/me", headers={**auth_header(user), **headers})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["features"]) == {"caller_mcp", "await_input", "service_answer"}, body
    return body["features"]


# --------------------------------------------------------------------------
# caller_mcp: SWARM_MCP_SERVERS, as this process reads it
# --------------------------------------------------------------------------


def test_caller_mcp_is_true_when_swarm_mcp_servers_is_on(client, monkeypatch):
    monkeypatch.setenv("SWARM_MCP_SERVERS", "on")
    # MUTATION: serving a constant, or reading anything but
    # `mcp_servers_enabled()`, leaves this False / absent and fails.
    assert features(client, "alice")["caller_mcp"] is True


def test_caller_mcp_is_false_when_swarm_mcp_servers_is_off(client, monkeypatch):
    monkeypatch.setenv("SWARM_MCP_SERVERS", "off")
    # MUTATION: a hard-coded True, or "unknown" for a flag that WAS read,
    # fails here.
    assert features(client, "alice")["caller_mcp"] is False
    monkeypatch.delenv("SWARM_MCP_SERVERS")
    # MUTATION: treating an unset flag as unread fails here; unset is the
    # documented "off" (`mcp_servers_enabled`'s docstring).
    assert features(client, "alice")["caller_mcp"] is False


def test_caller_mcp_that_cannot_be_read_is_unknown_not_false(client, monkeypatch):
    def unreadable(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("the environment could not be read")

    monkeypatch.setattr(mcp_servers, "mcp_servers_enabled", unreadable)
    # MUTATION: catching the failure as `False` -- the one answer the issue
    # forbids -- or letting it 500 the whole route, fails here.
    assert features(client, "alice")["caller_mcp"] == "unknown"


# --------------------------------------------------------------------------
# await_input: the frozen catalogue's `available`
# --------------------------------------------------------------------------


def test_await_input_is_the_catalogue_profiles_availability(client, monkeypatch):
    monkeypatch.setitem(RUNNER_PROFILES, AWAIT, replace(RUNNER_PROFILES[AWAIT], available=True))
    # MUTATION: a restated `True` / `False` instead of the catalogue's field
    # passes one of these two and fails the other.
    assert features(client, "alice")["await_input"] is True
    monkeypatch.setitem(RUNNER_PROFILES, AWAIT, replace(
        RUNNER_PROFILES[AWAIT], available=False,
        disabled_reason="await-input is switched off for this test"))
    assert features(client, "alice")["await_input"] is False


def test_await_input_absent_from_the_catalogue_is_unknown_not_false(client, monkeypatch):
    monkeypatch.delitem(RUNNER_PROFILES, AWAIT)
    # MUTATION: reading it through `awaitinput.dark()`, which folds "no such
    # profile" into "dark", serves False here and fails.
    assert features(client, "alice")["await_input"] == "unknown"


# --------------------------------------------------------------------------
# service_answer: the answer route's own predicate
# --------------------------------------------------------------------------


def test_a_service_workspace_says_its_principal_answers_its_waits(client):
    response = client.get("/v1/tenants/me", headers=auth_header("oracle"))
    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["tenant_id"] == ORACLE_TENANT, response.json()
    # MUTATION: a constant False, or a predicate other than
    # `awaitinput.service_principal`, fails here.
    assert response.json()["features"]["service_answer"] is True


def test_a_group_workspace_and_a_personal_one_say_false(client):
    # MUTATION: a constant True, or "any `kind: user` tenant", fails on the
    # group (alice, eng) or on the person's own workspace (carol) in turn.
    assert features(client, "alice")["service_answer"] is False
    assert features(client, "carol")["service_answer"] is False


def test_service_answer_that_cannot_be_read_is_unknown_not_false(client, monkeypatch):
    def unreadable(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("tenants/<id> could not be read")

    monkeypatch.setattr(awaitinput, "service_principal", unreadable)
    # MUTATION: a copy of the predicate (not a call through `awaitinput`)
    # never meets this failure and serves True; catching it as False fails too.
    assert features(client, "oracle")["service_answer"] == "unknown"


def test_one_unreadable_feature_leaves_the_others_and_the_identity_served(client, monkeypatch):
    monkeypatch.setenv("SWARM_MCP_SERVERS", "on")
    monkeypatch.setattr(awaitinput, "service_principal",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")))
    response = client.get("/v1/tenants/me", headers=auth_header("alice"))
    # MUTATION: one try around all three reads turns every feature unknown,
    # or a failure that escapes 500s the identity route.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant"]["tenant_id"] == "eng", body
    assert body["features"]["caller_mcp"] is True, body
    assert body["features"]["service_answer"] == "unknown", body
