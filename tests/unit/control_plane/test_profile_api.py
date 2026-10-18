"""Settings › Profile: `GET /v1/profile` and `PUT /v1/profile/preferences`.

docs/design/user-profile.md §4.1, §4.2 (lane PROFILE-API, part of #1505).

What these hold:

  * the profile is always the verified caller's own: no query parameter, no
    body field and no admin variant selects another person (Q8), and a
    service identity has none;
  * a saved default workspace is one this request may act in, or the save
    stores nothing;
  * every topic may be muted, needs-you topics included (Q3);
  * each section fails alone, and a failed read is named in `unavailable`,
    never served as an absence ("not connected", "no accounts");
  * only the caller's own connections and Claude accounts are served;
  * a workspace role comes from `workspace_role.role_in` alone, and is never
    `admin` (Q7).

Offline: FakeFirestore, StaticGroups and an in-memory account pool. The child
key is built at runtime.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import workspace_role
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.brokerclient import AccountPoolUnavailable
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forgeapp import CONNECTIONS, connection_id_for, user_hash
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.preferences import COLLECTION, TOPICS, Preferences
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

SERVICE = "ci-runner@saga-agents-staging.iam.gserviceaccount.com"
ALICE = "alice@saga.xyz"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


class FakePool:
    """The broker's `GET /v1/accounts?tenant_id=`: what that tenant may run on.

    It answers with more than the broker would -- a group's own pool and
    another person's account -- so a test can show the profile drops them."""

    def __init__(self, accounts: list[dict[str, Any]] | None = None, *, fail: bool = False):
        self.accounts = accounts or []
        self.fail = fail
        self.calls: list[str] = []

    def list_accounts(self, tenant_id: str) -> dict[str, Any]:
        self.calls.append(tenant_id)
        if self.fail:
            raise AccountPoolUnavailable("the account pool could not be reached")
        return {"accounts": list(self.accounts), "unreadable_document_count": 1}


def account(account_id: str, owner: str, *, lend_to: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "owner_tenant": owner,
        "label": account_id.split(":", 1)[1],
        "provider": "anthropic",
        "state": "AVAILABLE",
        "lend_to": list(lend_to),
        "windows": {
            "five_hour": {"utilization": 0.42, "resets_at": "2026-10-18T15:00:00+00:00",
                          "reset": False},
            "seven_day": {"utilization": 0.18, "resets_at": "2026-10-22T00:00:00+00:00",
                          "reset": False},
        },
        "stale": False,
    }


def seed_personal(db: FakeFirestore, user: str, *, state: str = "ready") -> None:
    """`tenants/u-<user>` and `workspaces/u-<user>`, as the workspace job leaves them."""
    tenant_id = f"u-{user}"
    owner = f"{user}@saga.xyz"
    seed_tenant(db, tenant_id)
    db.docs[f"tenants/{tenant_id}"].update(kind="user", principal=owner, display_name=owner)
    db.docs[f"workspaces/{tenant_id}"] = {
        "tenant_id": tenant_id, "workspace_id": f"w-{user[:6]:0<6}",
        "principal": owner, "state": state,
    }


def seed_connection(db: FakeFirestore, tenant_id: str, email: str, **over: Any) -> str:
    conn_id = connection_id_for(tenant_id, email)
    db.docs[f"{CONNECTIONS}/{conn_id}"] = {
        "connection_id": conn_id,
        "tenant_id": tenant_id,
        "user": email,
        "user_hash": user_hash(email),
        "forge": "github",
        "method": "app_user",
        "forge_login": email.split("@")[0],
        "state": "active",
        "failure": None,
        "access_expires_at": NOW,
        "refreshed_at": NOW,
        **over,
    }
    return conn_id


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        ALICE: (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "carol@saga.xyz": (),
        "dave@saga.xyz": (ENG_GROUP,),
        "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
        SERVICE: (ENG_GROUP,),
    }


@pytest.fixture
def child_key() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def api_context(db, tokens, group_map, objects, child_key):
    return build_context(
        settings=api_settings(child_key=child_key, allowed_users=(SERVICE,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


@pytest.fixture
def pool() -> FakePool:
    return FakePool([
        account("u-alice:main", "u-alice", lend_to=("research",)),
        account("eng:shared", "eng", lend_to=("u-alice",)),
        # A group's own pool, not lent to alice: never on her profile.
        account("eng:pool", "eng"),
        account("u-bob:main", "u-bob"),
    ])


@pytest.fixture
def client(api_context, pool) -> TestClient:
    app = create_app(api_context)
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def seeded(db: FakeFirestore) -> FakeFirestore:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_personal(db, "alice")
    return db


def as_(who: str, tenant: str | None = None) -> dict[str, str]:
    headers = auth_header(who)
    if tenant is not None:
        headers[TENANT_HEADER] = tenant
    return headers


def body(**over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "display_name": None,
        "default_workspace": None,
        "notifications": {"muted": [], "reminder": "as_set"},
        "revision": 0,
    }
    out.update(over)
    return out


def stored(db: FakeFirestore) -> list[str]:
    return db.paths(COLLECTION + "/")


# -- the save: whose, and what ----------------------------------------------------


def test_a_default_workspace_outside_tenant_choices_is_422_and_stores_nothing(
    client, seeded
) -> None:
    for foreign in ("research", "u-bob", "no-such-workspace"):
        r = client.put("/v1/profile/preferences", headers=as_("alice"),
                       json=body(default_workspace=foreign))
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["reason"] == "not_your_workspace"
        # The message never says whether the workspace exists.
        assert foreign not in r.json()["message"]
    assert stored(seeded) == []


def test_a_default_workspace_the_caller_can_act_in_is_saved(client, seeded) -> None:
    r = client.put("/v1/profile/preferences", headers=as_("alice"),
                   json=body(default_workspace="eng"))
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["revision"] == 1
    assert saved["default_workspace"] == {"tenant_id": "eng", "source": "profile",
                                          "ignored": None}
    assert len(stored(seeded)) == 1
    got = client.get("/v1/profile", headers=as_("alice")).json()
    assert got["revision"] == 1
    assert [w["tenant_id"] for w in got["workspaces"] if w["is_default"]] == ["eng"]


def test_an_unknown_topic_is_422_and_stores_nothing(client, seeded) -> None:
    r = client.put("/v1/profile/preferences", headers=as_("alice"),
                   json=body(notifications={"muted": ["finished", "marketing"],
                                            "reminder": "as_set"}))
    assert r.status_code == 422, r.text
    assert stored(seeded) == []


@pytest.mark.parametrize("extra", [
    {"user": ALICE},
    {"email": "bob@saga.xyz"},
    {"tenant_id": "eng"},
])
def test_an_unknown_field_is_422_and_stores_nothing(client, seeded, extra) -> None:
    r = client.put("/v1/profile/preferences", headers=as_("alice"), json={**body(), **extra})
    assert r.status_code == 422, r.text
    assert stored(seeded) == []


def test_an_unknown_notifications_field_is_422(client, seeded) -> None:
    r = client.put("/v1/profile/preferences", headers=as_("alice"),
                   json=body(notifications={"muted": [], "reminder": "as_set",
                                            "email": True}))
    assert r.status_code == 422, r.text
    assert stored(seeded) == []


def test_a_missing_field_is_422_rather_than_a_silent_reset(client, seeded) -> None:
    partial = body()
    del partial["notifications"]
    r = client.put("/v1/profile/preferences", headers=as_("alice"), json=partial)
    assert r.status_code == 422, r.text


def test_a_query_parameter_naming_a_person_is_422_on_both_routes(client, seeded) -> None:
    r = client.get("/v1/profile?user=bob@saga.xyz", headers=as_("alice"))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["parameters"] == ["user"]
    r = client.put("/v1/profile/preferences?user=bob@saga.xyz", headers=as_("alice"),
                   json=body())
    assert r.status_code == 422, r.text
    assert stored(seeded) == []


def test_a_service_identity_is_403(client, seeded) -> None:
    r = client.get("/v1/profile", headers=as_("ci-runner"))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["reason"] == "profile_people_only"
    r = client.put("/v1/profile/preferences", headers=as_("ci-runner"), json=body())
    assert r.status_code == 403, r.text
    assert stored(seeded) == []


def test_every_topic_needs_you_ones_included_can_be_muted(client, seeded) -> None:
    every = list(TOPICS)
    assert {"approval", "waiting_step", "question", "admin"} <= set(every)
    r = client.put("/v1/profile/preferences", headers=as_("alice"),
                   json=body(notifications={"muted": every, "reminder": "never"}))
    assert r.status_code == 200, r.text
    assert r.json()["notifications"]["preferences"] == {"muted": every, "reminder": "never"}
    got = client.get("/v1/profile", headers=as_("alice")).json()
    assert got["notifications"]["preferences"]["muted"] == every
    assert got["notifications"]["topics"] == every


def test_a_stale_revision_is_409(client, seeded) -> None:
    assert client.put("/v1/profile/preferences", headers=as_("alice"),
                      json=body()).status_code == 200
    r = client.put("/v1/profile/preferences", headers=as_("alice"),
                   json=body(notifications={"muted": ["finished"], "reminder": "as_set"}))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "preferences_changed"


# -- the read: shape -------------------------------------------------------------


def test_connections_has_exactly_github_and_typesafe(client, seeded) -> None:
    got = client.get("/v1/profile", headers=as_("alice"))
    assert got.status_code == 200, got.text
    assert set(got.json()["connections"]) == {"github", "typesafe"}
    assert got.json()["unavailable"] == []


def test_workspaces_are_tenant_choices_in_order_and_mark_acting_and_default(
    client, seeded
) -> None:
    got = client.get("/v1/profile", headers=as_("alice", "eng")).json()
    rows = got["workspaces"]
    assert [(w["tenant_id"], w["kind"]) for w in rows] == [("u-alice", "user"), ("eng", "group")]
    assert [w["tenant_id"] for w in rows if w["is_acting"]] == ["eng"]
    assert [w["tenant_id"] for w in rows if w["is_default"]] == ["u-alice"]
    assert rows[0]["state"] == "ready"
    assert got["default_workspace"] == {"tenant_id": "u-alice", "source": "personal",
                                        "ignored": None}


def test_a_personal_workspace_not_ready_yet_is_a_row_that_cannot_be_selected(
    client, db
) -> None:
    seed_tenant(db, "eng")
    seed_personal(db, "alice", state="requested")
    rows = client.get("/v1/profile", headers=as_("alice")).json()["workspaces"]
    assert [(w["tenant_id"], w["selectable"], w["state"]) for w in rows] == [
        ("eng", True, None), ("u-alice", False, "requested")]


def test_a_stored_default_the_caller_left_is_ignored_and_kept(
    client, seeded, api_context, child_key
) -> None:
    prefs = Preferences(seeded, [child_key])
    prefs.save(ALICE, display_name=None, default_workspace="research", muted=[],
               reminder="as_set", revision=0)
    got = client.get("/v1/profile", headers=as_("alice")).json()
    assert got["default_workspace"] == {"tenant_id": "u-alice", "source": "personal",
                                        "ignored": "not_a_member"}
    assert prefs.read(ALICE).default_workspace == "research"


# -- GitHub ------------------------------------------------------------------------


def test_github_has_one_entry_per_selectable_workspace_from_one_get_all(
    client, seeded
) -> None:
    seed_connection(seeded, "eng", ALICE)
    batches: list[list[str]] = []
    real = seeded.get_all

    def counting(refs, *a, **k):
        refs = list(refs)
        if any(ref.path.startswith(CONNECTIONS + "/") for ref in refs):
            batches.append([ref.path for ref in refs])
        return real(refs, *a, **k)

    seeded.get_all = counting  # type: ignore[method-assign]
    got = client.get("/v1/profile", headers=as_("alice")).json()
    assert len(batches) == 1 and len(batches[0]) == 2
    github = {e["tenant_id"]: e for e in got["connections"]["github"]}
    assert set(github) == {"u-alice", "eng"}
    assert github["u-alice"] == {"tenant_id": "u-alice", "state": "not_connected"}
    assert github["eng"]["state"] == "active"
    assert github["eng"]["forge_login"] == "alice"
    assert github["eng"]["method"] == "app_user"
    assert github["eng"]["access_expires_at"] == NOW.isoformat()


def test_a_failed_github_batch_is_named_never_served_as_not_connected(
    client, seeded
) -> None:
    seed_connection(seeded, "eng", ALICE)
    real = seeded.get_all

    def failing(refs, *a, **k):
        refs = list(refs)
        if any(ref.path.startswith(CONNECTIONS + "/") for ref in refs):
            raise RuntimeError("firestore down")
        return real(refs, *a, **k)

    seeded.get_all = failing  # type: ignore[method-assign]
    got = client.get("/v1/profile", headers=as_("alice"))
    assert got.status_code == 200, got.text
    payload = got.json()
    assert "github" in payload["unavailable"]
    assert "github" not in payload["connections"]
    assert "not_connected" not in got.text
    # The rest is served.
    assert "typesafe" in payload["connections"]
    assert payload["claude_accounts"]["personal_workspace"] == "u-alice"


def test_another_members_connection_is_never_served(client, seeded) -> None:
    seed_connection(seeded, "eng", "dave@saga.xyz", forge_login="dave-gh")
    # And a document at alice's own id that names dave: a collision, not hers.
    seed_connection(seeded, "eng", ALICE, user="dave@saga.xyz",
                    user_hash=user_hash("dave@saga.xyz"), forge_login="dave-gh")
    got = client.get("/v1/profile", headers=as_("alice"))
    assert "dave" not in got.text
    github = {e["tenant_id"]: e for e in got.json()["connections"]["github"]}
    assert github["eng"] == {"tenant_id": "eng", "state": "not_connected"}


# -- Claude accounts ------------------------------------------------------------------


def test_claude_accounts_are_own_and_borrowed_never_a_group_pool(client, seeded, pool) -> None:
    got = client.get("/v1/profile", headers=as_("alice", "eng"))
    accounts = got.json()["claude_accounts"]
    assert pool.calls == ["u-alice"]
    assert {a["account_id"]: a["relation"] for a in accounts["accounts"]} == {
        "u-alice:main": "own", "eng:shared": "borrowed"}
    assert "eng:pool" not in got.text and "u-bob" not in got.text
    own = next(a for a in accounts["accounts"] if a["relation"] == "own")
    assert own["lent_to_count"] == 1
    assert "research" not in str(own)
    assert own["windows"] == {
        "five_hour": {"utilization": 0.42, "resets_at": "2026-10-18T15:00:00+00:00"},
        "seven_day": {"utilization": 0.18, "resets_at": "2026-10-22T00:00:00+00:00"},
    }
    assert accounts["unreadable_document_count"] == 1


def test_no_personal_workspace_asks_the_broker_nothing(client, seeded, pool) -> None:
    got = client.get("/v1/profile", headers=as_("dave")).json()
    assert pool.calls == []
    assert got["claude_accounts"] == {"personal_workspace": None, "accounts": [],
                                      "unreadable_document_count": 0}


def test_a_broker_failure_leaves_claude_accounts_out_and_serves_the_rest(
    client, seeded, pool
) -> None:
    pool.fail = True
    got = client.get("/v1/profile", headers=as_("alice"))
    assert got.status_code == 200, got.text
    payload = got.json()
    assert "claude_accounts" not in payload
    assert payload["unavailable"] == ["claude_accounts"]
    assert payload["identity"]["email"] == ALICE
    assert set(payload["connections"]) == {"github", "typesafe"}
    assert payload["workspaces"]


def test_unreadable_preferences_are_named_and_the_page_is_still_served(
    client, seeded
) -> None:
    real = seeded.get_all

    def failing(refs, *a, **k):
        refs = list(refs)
        if any(ref.path.startswith(COLLECTION + "/") for ref in refs):
            raise RuntimeError("firestore down")
        return real(refs, *a, **k)

    seeded.get_all = failing  # type: ignore[method-assign]
    got = client.get("/v1/profile", headers=as_("alice"))
    assert got.status_code == 200, got.text
    payload = got.json()
    assert payload["unavailable"] == ["preferences"]
    assert payload["revision"] is None
    assert payload["default_workspace"]["source"] == "unavailable"
    assert "notifications" not in payload
    assert payload["workspaces"]


# -- the role seam ------------------------------------------------------------------


def test_role_comes_from_role_in_only_and_is_served_unchanged(
    client, seeded, monkeypatch
) -> None:
    assigned = {"name": "maintainer", "source": "assigned"}
    seen: list[str] = []

    def patched(ctx, tenant_id):
        seen.append(tenant_id)
        return dict(assigned)

    monkeypatch.setattr(workspace_role, "role_in", patched)
    monkeypatch.setattr("swarm_api.profile.role_in", patched)
    rows = client.get("/v1/profile", headers=as_("alice")).json()["workspaces"]
    assert [w["role"] for w in rows] == [assigned, assigned]
    assert seen == ["u-alice", "eng"]


def test_derived_roles_are_owner_and_member_with_the_group_address(client, seeded) -> None:
    rows = client.get("/v1/profile", headers=as_("alice")).json()["workspaces"]
    assert [w["role"] for w in rows] == [
        {"name": "owner", "source": "derived", "via": None},
        {"name": "member", "source": "derived", "via": ENG_GROUP},
    ]


def test_role_in_never_returns_admin_and_the_platform_role_is_shown_once(
    client, seeded
) -> None:
    got = client.get("/v1/profile", headers=as_("root")).json()
    assert got["identity"]["platform_role"] == "admin"
    assert got["identity"]["is_admin"] is True
    assert all(w["role"]["name"] != "admin" for w in got["workspaces"])
    assert "admin" not in {w["role"]["name"] for w in got["workspaces"]}


def test_role_in_is_none_outside_tenant_choices(client, seeded, api_context) -> None:
    auth = api_context.authenticator.authenticate("Bearer token-alice")
    assert workspace_role.role_in(auth, "research") is None
    for tenant_id, _ in auth.tenant_choices:
        assert workspace_role.role_in(auth, tenant_id)["name"] in {"owner", "member"}


# -- the suggested name ----------------------------------------------------------------


@pytest.fixture
def named_tokens(tokens) -> dict[str, dict[str, Any]]:
    tokens["token-alice"]["name"] = "  Alice   Liddell "
    tokens["token-bob"]["name"] = "Bob [admin] 2"
    return tokens


def test_the_suggested_name_is_the_cleaned_claim_while_nothing_is_stored(
    named_tokens, client, seeded
) -> None:
    got = client.get("/v1/profile", headers=as_("alice")).json()["identity"]
    assert got["display_name"] is None
    assert got["display_name_suggested"] == "Alice Liddell"


def test_the_suggested_name_is_null_after_a_save(named_tokens, client, seeded) -> None:
    r = client.put("/v1/profile/preferences", headers=as_("alice"),
                   json=body(display_name="Alice P Liddell"))
    assert r.status_code == 200, r.text
    assert r.json()["identity"] == {"display_name": "Alice P Liddell",
                                    "display_name_suggested": None}
    got = client.get("/v1/profile", headers=as_("alice")).json()["identity"]
    assert got["display_name"] == "Alice P Liddell"
    assert got["display_name_suggested"] is None


def test_the_suggested_name_is_null_for_a_claim_that_fails_a_rule(
    named_tokens, client, seeded
) -> None:
    got = client.get("/v1/profile", headers=as_("bob")).json()["identity"]
    assert got["display_name_suggested"] is None


def test_a_display_name_that_breaks_a_rule_is_422_with_its_code(client, seeded) -> None:
    r = client.put("/v1/profile/preferences", headers=as_("alice"),
                   json=body(display_name="Alice Admin"))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "display_name_reserved"
    assert stored(seeded) == []
