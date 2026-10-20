"""No credential, and no credential's name, is ever in `GET /v1/profile`.

docs/design/user-profile.md §4.2 ("No credential, and no credential's name").
`swarm_api.profile` builds every section from an allow-list and never spreads
a source document. This test is what holds it there: it stores a GitHub
connection, a TypeSafe key status and a Claude account whose secret-shaped
fields hold a sentinel built at runtime, then asserts that the serialised
response holds neither the sentinel nor any key named like a credential.

A source that gains a field tomorrow is covered too: every document below
also carries the sentinel under an UNKNOWN name, so a profile that spread a
document instead of picking from it would serve it.

Offline: FakeFirestore and an in-memory account pool.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forgeapp import CONNECTIONS, connection_id_for, user_hash
from swarm_api.gittokens import secret_name_for
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.provider_keys import KEYS_COLLECTION, TYPESAFE
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore

ALICE = "alice@example.com"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

#: Key names that may never appear anywhere in the response (§4.2).
FORBIDDEN_KEYS = frozenset({
    "secret", "secret_id", "secret_name", "token", "token_id", "value", "refresh_token",
})


@pytest.fixture
def sentinel() -> str:
    return "tok-" + secrets.token_hex(16)


class SentinelPool:
    def __init__(self, sentinel: str) -> None:
        self.sentinel = sentinel

    def list_accounts(self, tenant_id: str) -> dict[str, Any]:
        s = self.sentinel
        return {
            "accounts": [{
                "account_id": "u-alice:main",
                "owner_tenant": "u-alice",
                "label": "main",
                "provider": "anthropic",
                "state": "AVAILABLE",
                "lend_to": [],
                "windows": {"five_hour": {"utilization": 0.1, "resets_at": "2026-10-18T15:00:00Z",
                                          "token": s}},
                "stale": False,
                "secret_name": s,
                "secret": s,
                "credential": s,
                "refresh_token": s,
                "token": s,
                "value": s,
                "access_token": s,
                "future_field": s,
            }],
            "unreadable_document_count": 0,
            "secret": s,
        }


def seed(db: FakeFirestore, sentinel: str) -> None:
    seed_tenant(db, "eng")
    seed_tenant(db, "u-alice")
    db.docs["tenants/u-alice"].update(kind="user", principal=ALICE, display_name=ALICE)
    db.docs["workspaces/u-alice"] = {
        "tenant_id": "u-alice", "workspace_id": "w-alice0", "principal": ALICE,
        "state": "ready", "secret": sentinel,
    }
    for tenant_id in ("eng", "u-alice"):
        conn_id = connection_id_for(tenant_id, ALICE)
        db.docs[f"{CONNECTIONS}/{conn_id}"] = {
            "connection_id": conn_id,
            "tenant_id": tenant_id,
            "user": ALICE,
            "user_hash": user_hash(ALICE),
            "forge": "github",
            "method": "app_user",
            "forge_login": "alice",
            "forge_user_id": sentinel,
            "token_id": sentinel,
            "secret_name": sentinel,
            "secret_version": sentinel,
            "refresh_token": sentinel,
            "token": sentinel,
            "value": sentinel,
            "state": "refresh_failed",
            "failure": {"code": "REFRESH_FAILED", "recovery": "Reconnect GitHub.",
                        "secret": sentinel},
            "access_expires_at": NOW,
            "refreshed_at": NOW,
            "future_field": sentinel,
        }
        db.docs[f"{KEYS_COLLECTION}/{secret_name_for(tenant_id, TYPESAFE)}"] = {
            "set": True, "set_at": NOW, "set_by": ALICE, "version": "3",
            "secret_id": sentinel, "value": sentinel, "future_field": sentinel,
        }


@pytest.fixture
def client(db, tokens, group_map, objects, sentinel) -> TestClient:
    context = build_context(
        settings=api_settings(child_key=secrets.token_hex(32)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    app = create_app(context)
    app.state.account_pool = SentinelPool(sentinel)
    return TestClient(app, raise_server_exceptions=False)


def keys_of(value: Any) -> set[str]:
    if isinstance(value, dict):
        out = set(value)
        for item in value.values():
            out |= keys_of(item)
        return out
    if isinstance(value, list):
        out = set()
        for item in value:
            out |= keys_of(item)
        return out
    return set()


def test_no_credential_or_credential_name_is_in_the_profile(client, db, sentinel) -> None:
    seed(db, sentinel)
    r = client.get("/v1/profile", headers=auth_header("alice"))
    assert r.status_code == 200, r.text
    payload = r.json()
    # Every section was served, so the absence below is not an empty page.
    assert payload["unavailable"] == []
    assert len(payload["connections"]["github"]) == 2
    assert payload["connections"]["typesafe"]["personal"]["set"] is True
    assert payload["connections"]["typesafe"]["acting_workspace"]["set"] is True
    assert len(payload["claude_accounts"]["accounts"]) == 1
    text = json.dumps(payload)
    assert sentinel not in text
    assert "swarm-tenant-" not in text
    assert keys_of(payload) & FORBIDDEN_KEYS == set()
    assert "future_field" not in keys_of(payload)


def test_the_save_answers_no_credential_either(client, db, sentinel) -> None:
    seed(db, sentinel)
    r = client.put("/v1/profile/preferences", headers=auth_header("alice"), json={
        "display_name": None, "default_workspace": "eng",
        "notifications": {"muted": ["finished"], "reminder": "daily"}, "revision": 0,
    })
    assert r.status_code == 200, r.text
    assert sentinel not in r.text
    assert keys_of(r.json()) & FORBIDDEN_KEYS == set()
