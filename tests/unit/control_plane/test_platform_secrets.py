"""`GET /v1/admin/platform-secrets`: status from Secret Manager metadata, admins only.

docs/design/secrets-scopes.md §5 and §11 Q7 (SSC-PLATFORM-STATUS, part of
#2158). These hold:

* a member, a person with no group and an unauthenticated caller are refused;
  no route writes a platform secret;
* the reader calls `get_secret` and `list_secret_versions` and NOTHING else --
  never `access_secret_version` -- driven through the shipped
  `SecretManagerMetadata` over a client that records every attribute asked of
  it, and checked in the module's syntax tree too;
* no name outside `swarm-` reaches Secret Manager: not from the catalogue, and
  not from a broker account document whose recorded secret names the other
  team's (saga-agents-staging is shared, CLAUDE.md rule 2);
* a row carries status, counts and times, never a value, a length or `last4`;
  a missing secret is `not_declared` and an unreadable one says why, never a
  500.

Offline: the real route over FakeFirestore, a fake Secret Manager client.
"""

from __future__ import annotations

import ast
import inspect
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gexc

from swarm_api import platform_secrets
from swarm_api.main import create_app
from swarm_api.platform_secrets import (
    PLATFORM_SECRETS,
    SecretManagerMetadata,
    SecretNameRefused,
    _full_name,
)

from .conftest import PROJECT, auth_header

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)

#: The methods a metadata-only reader may call, and no other.
ALLOWED_CALLS = {"get_secret", "list_secret_versions"}

#: The fields a row may carry. A value, a length or trailing characters added
#: later fails here.
ROW_FIELDS = {"secret_id", "managed_by", "written_by", "status", "reason",
              "enabled_versions", "newest_enabled_at", "rotation_in_grace", "family"}
ACCOUNT_FIELDS = ROW_FIELDS | {"account_id", "owner_tenant", "label", "half"}


class RecordingClient:
    """A Secret Manager client that records every attribute asked of it.

    `versions` maps a secret id to its versions' (state, create_time);
    `missing` are NotFound; `denied` are PermissionDenied. Any method other
    than the two metadata calls is recorded and raises, so a reader that
    reaches for `access_secret_version` fails both ways.
    """

    def __init__(self, versions: dict[str, list[tuple[str, datetime]]] | None = None,
                 *, missing: set[str] = frozenset(), denied: set[str] = frozenset()) -> None:
        self.versions = versions or {}
        self.missing = set(missing)
        self.denied = set(denied)
        self.calls: list[tuple[str, str]] = []

    def _id(self, name: str) -> str:
        return name.rsplit("/secrets/", 1)[1]

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_") or attr in {"versions", "missing", "denied", "calls"}:
            raise AttributeError(attr)

        def call(request: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            request = request or {}
            name = str(request.get("name") or request.get("parent") or "")
            self.calls.append((attr, name))
            if attr not in ALLOWED_CALLS:
                raise AssertionError(f"metadata reader called {attr}")
            secret_id = self._id(name)
            if secret_id in self.denied:
                raise gexc.PermissionDenied("denied")
            if secret_id in self.missing:
                raise gexc.NotFound("missing")
            if attr == "get_secret":
                return SimpleNamespace(name=name)
            return [SimpleNamespace(state=state, create_time=when)
                    for state, when in self.versions.get(secret_id, [])]

        return call


def _app(api_context: Any, client: RecordingClient) -> TestClient:
    app = create_app(api_context)
    app.state.secret_metadata = SecretManagerMetadata(PROJECT, client=client)
    return TestClient(app, raise_server_exceptions=False)


def _all_set(client: RecordingClient) -> None:
    for entry in PLATFORM_SECRETS:
        client.versions[entry.secret_id] = [("ENABLED", T0 - timedelta(days=3))]


def _account(db: Any, owner: str, label: str, *, secret: str = "") -> str:
    account_id = "acct-" + secrets.token_hex(4)
    db.docs[f"accounts/{account_id}"] = {
        "account_id": account_id, "owner_tenant": owner, "label": label,
        "provider": "anthropic", "state": "AVAILABLE", "secret": secret,
    }
    return account_id


# -- who may read -----------------------------------------------------------


@pytest.mark.parametrize("user", ["alice", "carol"])
def test_a_non_admin_is_403_and_nothing_is_read(api_context, user):
    recorder = RecordingClient()
    http = _app(api_context, recorder)

    response = http.get("/v1/admin/platform-secrets", headers=auth_header(user))

    assert response.status_code == 403, response.text
    assert recorder.calls == []


def test_an_unauthenticated_caller_is_refused(api_context):
    recorder = RecordingClient()
    http = _app(api_context, recorder)

    response = http.get("/v1/admin/platform-secrets")

    assert response.status_code in (401, 403), response.text
    assert recorder.calls == []


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_route_writes_a_platform_secret(api_context, method):
    http = _app(api_context, RecordingClient())

    response = getattr(http, method)("/v1/admin/platform-secrets", headers=auth_header("root"))

    assert response.status_code == 405, response.text


def test_an_admin_reads_every_catalogue_secret(api_context):
    recorder = RecordingClient()
    _all_set(recorder)
    http = _app(api_context, recorder)

    response = http.get("/v1/admin/platform-secrets", headers=auth_header("root"))

    assert response.status_code == 200, response.text
    body = response.json()
    served = [row["secret_id"] for row in body["secrets"] if row["family"] == "platform"]
    assert served == [entry.secret_id for entry in PLATFORM_SECRETS]
    assert all(row["status"] == "set" for row in body["secrets"])
    assert body["counts"] == {"set": len(PLATFORM_SECRETS)}


# -- metadata only: never versions.access ------------------------------------


def test_the_reader_never_calls_versions_access(api_context, db):
    recorder = RecordingClient()
    _all_set(recorder)
    _account(db, "u-alice", "main")
    http = _app(api_context, recorder)

    assert http.get("/v1/admin/platform-secrets", headers=auth_header("root")).status_code == 200

    called = {method for method, _ in recorder.calls}
    assert called == ALLOWED_CALLS
    assert "access_secret_version" not in called


def test_the_module_names_no_value_read():
    """The syntax tree, not the text: the docstring may say what it never does."""
    tree = ast.parse(inspect.getsource(platform_secrets))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for forbidden in ("access_secret_version", "access", "payload", "add_secret_version",
                      "create_secret", "delete_secret", "set_iam_policy", "list_secrets"):
        assert forbidden not in attributes | names, forbidden


# -- the swarm- prefix: nothing else is named ---------------------------------


def test_every_name_sent_to_secret_manager_is_a_swarm_secret_of_this_project(api_context, db):
    recorder = RecordingClient()
    _all_set(recorder)
    _account(db, "u-alice", "main")
    http = _app(api_context, recorder)

    http.get("/v1/admin/platform-secrets", headers=auth_header("root"))

    assert recorder.calls
    for _, name in recorder.calls:
        assert name.startswith(f"projects/{PROJECT}/secrets/swarm-"), name


def test_the_catalogue_names_only_swarm_secrets():
    assert all(entry.secret_id.startswith("swarm-") for entry in PLATFORM_SECRETS)
    # §11 Q3: the existing App writes Actions secrets; no second App's key.
    assert "swarm-github-secrets-app-private-key" not in {e.secret_id for e in PLATFORM_SECRETS}


@pytest.mark.parametrize("secret_id", [
    "agents-staging-db-password",          # the other team's family
    "promptlab-openai",                    # and its other one
    "",
    "swarm-",                              # the prefix alone
    "swarm-a/../../agents-x",              # a path smuggled into an id
    "projects/other/secrets/swarm-x",      # a full name, another project
    "Swarm-live-key",
])
def test_a_name_outside_swarm_is_refused_before_any_call(secret_id):
    recorder = RecordingClient()
    meta = SecretManagerMetadata(PROJECT, client=recorder)

    with pytest.raises(SecretNameRefused):
        _full_name(PROJECT, secret_id)
    with pytest.raises(SecretNameRefused):
        meta.enabled_versions(secret_id)
    assert recorder.calls == []


def test_an_account_naming_another_teams_secret_is_never_read(api_context, db):
    recorder = RecordingClient()
    _all_set(recorder)
    _account(db, "u-alice", "main")
    bad = _account(db, "u-mallory", "x", secret="agents-staging-db-password")
    http = _app(api_context, recorder)

    body = http.get("/v1/admin/platform-secrets", headers=auth_header("root")).json()

    assert body["accounts_refused"] == 1
    assert bad not in {row.get("account_id") for row in body["secrets"]}
    assert recorder.calls
    assert all(name.rsplit("/secrets/", 1)[1].startswith("swarm-") for _, name in recorder.calls)


def test_each_account_is_two_rows_by_its_recorded_or_derived_name(api_context, db):
    recorder = RecordingClient()
    _all_set(recorder)
    derived = _account(db, "u-alice", "main")
    recorded = _account(db, "u-bob", "old", secret="swarm-account-u-bob-old")
    recorder.versions["swarm-account-u-alice--main"] = [("ENABLED", T0)]
    recorder.versions["swarm-account-u-alice--main-refresh"] = [
        ("ENABLED", T0 - timedelta(hours=1)), ("ENABLED", T0)]
    recorder.missing.add("swarm-account-u-bob-old-refresh")
    http = _app(api_context, recorder)

    body = http.get("/v1/admin/platform-secrets", headers=auth_header("root")).json()

    accounts = {(row["account_id"], row["half"]): row
                for row in body["secrets"] if row["family"] == "account"}
    assert accounts[(derived, "access")]["secret_id"] == "swarm-account-u-alice--main"
    assert accounts[(derived, "refresh")]["rotation_in_grace"] is True
    assert accounts[(recorded, "access")]["secret_id"] == "swarm-account-u-bob-old"
    assert accounts[(recorded, "access")]["status"] == "not_set"
    assert accounts[(recorded, "refresh")]["status"] == "not_declared"
    for row in accounts.values():
        assert set(row) == ACCOUNT_FIELDS


# -- what a row says, and what it never says ----------------------------------


def test_a_row_carries_status_and_times_and_never_a_value(api_context):
    recorder = RecordingClient()
    _all_set(recorder)
    recorder.versions["swarm-live-key"] = [
        ("ENABLED", T0 - timedelta(days=30)), ("ENABLED", T0), ("DISABLED", T0)]
    recorder.versions["swarm-child-key"] = []
    recorder.missing.add("swarm-typesafe-fwd-key")
    recorder.denied.add("swarm-tailscale-oauth")
    http = _app(api_context, recorder)

    body = http.get("/v1/admin/platform-secrets", headers=auth_header("root")).json()
    rows = {row["secret_id"]: row for row in body["secrets"]}

    live = rows["swarm-live-key"]
    assert (live["status"], live["enabled_versions"], live["rotation_in_grace"]) == ("set", 2, True)
    assert live["newest_enabled_at"] == T0.isoformat()
    assert rows["swarm-child-key"]["status"] == "not_set"
    assert rows["swarm-typesafe-fwd-key"]["status"] == "not_declared"
    assert (rows["swarm-tailscale-oauth"]["status"],
            rows["swarm-tailscale-oauth"]["reason"]) == ("unreadable", "permission_denied")
    for row in rows.values():
        assert set(row) == ROW_FIELDS
    for forbidden in ("value", "length", "last4", "payload", "data"):
        assert forbidden not in response_keys(body)


def response_keys(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        return set(payload) | {k for v in payload.values() for k in response_keys(v)}
    if isinstance(payload, list):
        return {k for v in payload for k in response_keys(v)}
    return set()


def test_unreadable_account_documents_still_serve_the_catalogue(api_context, db):
    recorder = RecordingClient()
    _all_set(recorder)
    http = _app(api_context, recorder)

    def boom(name: str) -> Any:
        raise RuntimeError("firestore down")

    original = db.collection
    db.collection = lambda name: boom(name) if name == "accounts" else original(name)
    try:
        response = http.get("/v1/admin/platform-secrets", headers=auth_header("root"))
    finally:
        db.collection = original

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accounts_unread"] is True
    assert len(body["secrets"]) == len(PLATFORM_SECRETS)
