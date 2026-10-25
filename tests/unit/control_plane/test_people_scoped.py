"""Admin › People and roles, scoped by who reads it (ADMIN-PEOPLE-ROLES; part
of #2157, proposal E frames S5c and S5f).

`GET /v1/admin/people` is the person × workspace grid. A PLATFORM admin reads
every person and every registered group workspace, as before, with each
person's role in each. A WORKSPACE admin -- a person whose stored role is
`admin` in a group workspace, and who is not a platform admin -- reads only the
workspaces they administer and only the people in them: their columns, those
people's roles there, and nothing of anyone's personal workspace, Claude
account, loans, the platform's admins or its audit. A member or a viewer is
refused as before. Every write on this router stays a platform admin's: a
workspace admin's attempt is 403 and writes nothing.

Who is who (`eng@example.com` and `research@example.com` are the groups):

* `ops` -- a platform admin in no group;
* `alice` -- in eng, named eng's admin;
* `bob` -- in eng, no grant: `member`, the migration default;
* `dave` -- in eng AND research, named admin of eng only: the attacker who
  administers one workspace and wants another's people;
* `erin` -- in research, named research's admin;
* `carol` -- in no group, with a personal workspace request open;
* `gail` -- never signed in, given `viewer` in eng.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like a
credential).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import workspaces as ws
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, seed_tenant
from .test_people_admin import FakePool, FakePublisher
from .test_workspace_secrets import RecordingFirestore, seed_personal

GROUPS = {
    "ops@example.com": (ADMIN_GROUP,),
    "alice@example.com": (ENG_GROUP,),
    "bob@example.com": (ENG_GROUP,),
    "dave@example.com": (ENG_GROUP, RESEARCH_GROUP),
    "erin@example.com": (RESEARCH_GROUP,),
    "carol@example.com": (),
}
CAROL_WS = "w-ca401e"
ROUTE = "/v1/admin/people"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seen(db, user: str, teams: list[str], minutes: int = 5) -> None:
    """A sighting as `Workspaces.touch_person` writes it."""
    db.docs[f"{ws.PEOPLE}/u-{user}"] = {
        "principal": f"{user}@example.com", "teams": teams,
        "first_seen": _now() - timedelta(days=3), "last_seen": _now() - timedelta(minutes=minutes)}


@pytest.fixture
def db() -> RecordingFirestore:
    db = RecordingFirestore()
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    for user in ("alice", "bob", "dave", "erin"):
        seed_personal(db, user)
    _seen(db, "alice", ["eng"])
    _seen(db, "bob", ["eng"], 30)
    _seen(db, "dave", ["eng", "research"], 60)
    _seen(db, "erin", ["research"], 90)
    _seen(db, "carol", [], 40)
    db.docs["workspaces/u-carol"] = {
        "tenant_id": "u-carol", "workspace_id": CAROL_WS, "principal": "carol@example.com",
        "kind": "personal", "state": "requested", "request_id": str(uuid.uuid4()),
        "requested_at": _now() - timedelta(minutes=40), "requested_via": "console",
        "decision": None, "history": [], "limits": dict(ws.DEFAULT_LIMITS), "run": None,
        "steps": {}, "failure": None, "ready_at": None, "migrated": False}
    db.docs[f"workspace_ids/{CAROL_WS}"] = {"tenant_id": "u-carol"}
    return db


@pytest.fixture
def client(db) -> TestClient:
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    ctx = build_context(
        settings=api_settings(settings_key=secrets.token_hex(32)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(),
    )
    app = create_app(ctx)
    app.state.workspace_publisher = FakePublisher()
    app.state.account_pool = FakePool()
    client = TestClient(app, raise_server_exceptions=False)
    for who, tenant, role in (("alice", "eng", "admin"), ("dave", "eng", "admin"),
                              ("erin", "research", "admin"), ("gail", "eng", "viewer")):
        name_role(client, f"{who}@example.com", role, tenant)
    return client


def h(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if tenant:
        headers[TENANT_HEADER] = tenant
    return headers


def name_role(client, who: str, role: str, tenant: str) -> None:
    response = client.put(f"/v1/admin/workspaces/{tenant}/roles/person", headers=h("ops"),
                          json={"email": who, "role": role})
    assert response.status_code == 200, response.text


def grid(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["email"]: row["roles"] for row in body["people"]}


def columns(body: dict[str, Any]) -> list[str]:
    return [c["tenant_id"] for c in body["workspaces"]]


def written(db) -> dict[str, Any]:
    """Everything a write here could change. `people/` is left out: every
    request's own sighting (`touch_person`) may refresh the caller's."""
    return {k: dict(v) for k, v in db.docs.items() if not k.startswith(f"{ws.PEOPLE}/")}


# -- a workspace admin reads only their workspaces -------------------------------------------

def test_a_workspace_admin_reads_only_their_workspaces_columns_and_people(client) -> None:
    response = client.get(ROUTE, headers=h("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "workspaces"
    # MUTATION: list every registered group to a workspace admin -> research is a column.
    assert columns(body) == ["eng"]
    # Named as /v1/tenants/mine and the Admin panel name it: the group email.
    assert body["workspaces"][0]["display_name"] == "eng@example.com"
    # MUTATION: list everyone who has signed in -> erin and carol are rows.
    assert grid(body) == {
        "alice@example.com": {"eng": "admin"},
        "bob@example.com": {"eng": "member"},
        "dave@example.com": {"eng": "admin"},
        "gail@example.com": {"eng": "viewer"},
    }


def test_a_person_in_two_workspaces_shows_only_the_readers_one(client) -> None:
    # dave is in eng and research and administers eng only.
    body = client.get(ROUTE, headers=h("dave")).json()
    assert columns(body) == ["eng"]
    assert grid(body)["dave@example.com"] == {"eng": "admin"}
    assert "erin@example.com" not in grid(body)
    # erin administers research and sees dave there, never eng.
    body = client.get(ROUTE, headers=h("erin")).json()
    assert columns(body) == ["research"]
    assert grid(body) == {"dave@example.com": {"research": "member"},
                          "erin@example.com": {"research": "admin"}}


def test_a_workspace_admins_rows_carry_nothing_of_a_personal_workspace_or_the_platform(client) -> None:
    body = client.get(ROUTE, headers=h("dave")).json()
    # MUTATION: serve `everyone()`'s rows filtered -> `teams` names research,
    # and the personal workspace, Claude account and loans are served.
    for row in body["people"]:
        assert set(row) <= {"email", "display_name", "roles", "last_active"}, row
    assert body["pending"] == 0
    for leaked in ("audit", "admins", "lendable_accounts", "provisioning"):
        assert not body.get(leaked), leaked
    assert "research" not in str(body)


def test_the_tenant_header_does_not_widen_a_workspace_admins_read(client) -> None:
    # dave may select research (he is in it); selecting it is not administering it.
    body = client.get(ROUTE, headers=h("dave", "research")).json()
    assert columns(body) == ["eng"]


# -- and may act nowhere else ----------------------------------------------------------------

@pytest.mark.parametrize(("method", "path", "json"), [
    ("POST", f"/v1/admin/workspaces/{CAROL_WS}/approve", None),
    ("POST", f"/v1/admin/workspaces/{CAROL_WS}/deny", {"reason": "not here"}),
    ("POST", f"/v1/admin/workspaces/{CAROL_WS}/retry", None),
    ("PUT", f"/v1/admin/workspaces/{CAROL_WS}/limits", {"max_active": 30}),
    ("PUT", f"/v1/admin/people/{CAROL_WS}/loan", {"account_id": "acct-1", "lend": True}),
    ("POST", f"/v1/admin/people/{CAROL_WS}/loan/decline", {}),
    ("POST", "/v1/admin/workspaces/sweep", None),
    ("PUT", "/v1/admin/workspaces/research/roles/person", {"email": "erin@example.com", "role": "viewer"}),
    ("PUT", "/v1/admin/workspaces/research/roles/person", {"email": "dave@example.com", "role": "admin"}),
    ("PUT", "/v1/admin/workspaces/eng/roles/person", {"email": "bob@example.com", "role": "admin"}),
])
def test_a_workspace_admins_action_outside_their_workspace_is_refused_and_writes_nothing(
        client, db, method, path, json) -> None:
    before = written(db)
    response = client.request(method, path, headers=h("dave"), json=json)
    assert response.status_code == 403, response.text
    assert written(db) == before
    assert db.docs["workspaces/u-carol"]["state"] == "requested"


def test_a_workspace_admin_whose_role_is_taken_away_is_refused_at_once(client) -> None:
    name_role(client, "alice@example.com", "member", "eng")
    # MUTATION: read the role from a cache -> the old admin still reads eng's people.
    assert client.get(ROUTE, headers=h("alice")).status_code == 403


# -- the platform admin, the member and the viewer -------------------------------------------

def test_a_platform_admin_still_reads_everyone_with_every_workspaces_column(client) -> None:
    response = client.get(ROUTE, headers=h("ops"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "platform"
    assert columns(body) == ["eng", "research"]
    rows = {row["email"]: row for row in body["people"]}
    # Everyone who signed in, carol's open request first, as before.
    assert body["people"][0]["email"] == "carol@example.com"
    assert body["people"][0]["workspace"]["state"] == "requested"
    assert body["pending"] == 1
    assert {"alice@example.com", "bob@example.com", "dave@example.com", "erin@example.com",
            "carol@example.com"} <= set(rows)
    assert rows["dave@example.com"]["roles"] == {"eng": "admin", "research": "member"}
    assert rows["carol@example.com"]["roles"] == {"eng": "none", "research": "none"}
    assert rows["erin@example.com"]["roles"] == {"eng": "none", "research": "admin"}
    # The platform's own sections are still served to a platform admin.
    assert "audit" in body and "lendable_accounts" in body and "admins" in body


@pytest.mark.parametrize("user", ["bob", "carol"])
def test_a_member_or_someone_in_no_group_is_refused(client, user) -> None:
    response = client.get(ROUTE, headers=h(user))
    assert response.status_code == 403, response.text
    assert "people" not in response.json()


def test_a_viewer_is_refused(client) -> None:
    name_role(client, "bob@example.com", "viewer", "eng")
    assert client.get(ROUTE, headers=h("bob")).status_code == 403


def test_no_audit_row_is_written_by_a_workspace_admins_read(client, db) -> None:
    before = sorted(k for k in db.docs if k.startswith(f"{AUDIT_COLLECTION}/"))
    client.get(ROUTE, headers=h("alice"))
    assert sorted(k for k in db.docs if k.startswith(f"{AUDIT_COLLECTION}/")) == before
