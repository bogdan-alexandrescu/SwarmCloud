"""The join-request routes: a person asks to join a group workspace through
its join link, and that workspace's admins or a platform admin decide
(docs/design/join-requests.md §4.2, §4.3, §4.6, §4.8; lane JOIN-API, part of
#2325; owner's answers to Q1-Q6, 2026-10-25).

Read with an attacker's eye. The attacks are:

* a code-holder PROBING codes: an unknown, a rotated and a switched-off
  code answer byte-identical 404s, and a working one shows the display name
  and nothing else;
* a workspace admin DECIDING ANOTHER WORKSPACE'S REQUEST: 403
  `join_decider_required`, the same answer an unknown ref gets, and nothing
  written;
* a reader HARVESTING what an overwrite needs: no response carries a
  document id, a code hash, or the code after the one answer that made it;
* a half-applied approval: the decision, the membership, the lowered role
  and their audit rows land in ONE transaction, or none do.

Who is who (`eng@example.com` and `research@example.com` are the groups):

* `ops` -- a platform admin in no group;
* `alice` -- in eng, named eng's admin;
* `bob` -- in eng, no grant: `member`;
* `erin` -- in research, named research's admin;
* `lee`, `kim` -- in no group: the people asking.

Every code, hash and key is made at runtime (CLAUDE.md: nothing added may
look like a credential).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import joinrequests, memberships, roles
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.joinrequests import JoinStore
from swarm_api.main import create_app
from swarm_api.memberships import MembershipStore
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, seed_tenant
from .fakes import FakeFirestore
from .test_people_admin import FakePool, FakePublisher

GROUPS = {
    "ops@example.com": (ADMIN_GROUP,),
    "alice@example.com": (ENG_GROUP,),
    "bob@example.com": (ENG_GROUP,),
    "erin@example.com": (RESEARCH_GROUP,),
    "lee@example.com": (),
    "kim@example.com": (),
}
LEE = "lee@example.com"
KIM = "kim@example.com"
CONSOLE = "https://console.example.com"


def h(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if tenant:
        headers[TENANT_HEADER] = tenant
    return headers


@pytest.fixture
def db() -> FakeFirestore:
    db = FakeFirestore()
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    db.docs["tenants/eng"]["display_name"] = "Example Labs"
    db.docs["tenants/research"]["display_name"] = "Research"
    return db


@pytest.fixture
def ctx(db):
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    return build_context(
        settings=api_settings(settings_key=secrets.token_hex(32), console_url=CONSOLE,
                              rate_limit_burst=10_000),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(),
    )


@pytest.fixture
def client(ctx) -> TestClient:
    app = create_app(ctx)
    app.state.workspace_publisher = FakePublisher()
    app.state.account_pool = FakePool()
    client = TestClient(app, raise_server_exceptions=False)
    for who, tenant in (("alice", "eng"), ("erin", "research")):
        response = client.put(f"/v1/admin/workspaces/{tenant}/roles/person", headers=h("ops"),
                              json={"email": f"{who}@example.com", "role": "admin"})
        assert response.status_code == 200, response.text
    return client


def switch_on(ctx) -> None:
    """`platform_memberships` on (lane JOIN-RESOLVE owns the setting; it is
    off by default). Set on the frozen settings object the app reads."""
    object.__setattr__(ctx.settings, "platform_memberships", True)


def link_on(client, admin: str = "alice", tenant: str = "eng", *, rotate: bool = False) -> str:
    response = client.put("/v1/workspace/join-link", headers=h(admin, tenant),
                          json={"on": True, "rotate": rotate})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["on"] is True
    link = body.get("link")
    assert link and link.startswith(f"{CONSOLE}/join#"), body
    return link.split("#", 1)[1]


def ask(client, user: str, code: str, message: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code}
    if message is not None:
        body["message"] = message
    response = client.post("/v1/join-requests", headers=h(user), json=body)
    assert response.status_code == 202, response.text
    return response.json()


def approve(client, user: str, ref: str, role: str | None = None):
    body = {} if role is None else {"role": role}
    return client.post(f"/v1/admin/join-requests/{ref}/approve", headers=h(user), json=body)


def audit(db, action: str) -> list[dict]:
    return [r for r in db.collection_docs(AUDIT_COLLECTION) if r.get("action") == action]


def membership_docs(db) -> list[dict]:
    return list(db.dump(f"{memberships.COLLECTION}/").values())


def role_grants_for(db, subject: str) -> list[dict]:
    return [d for d in db.dump(f"{roles.GRANTS_COLLECTION}/").values()
            if d.get("subject") == subject]


def decisions(db) -> list[dict]:
    return [d for p, d in db.dump(f"{joinrequests.REQUESTS}/").items()
            if f"/{joinrequests.DECISIONS}/" in p]


def asked(client) -> tuple[str, str]:
    """eng's link on, and lee's request to join eng: (ref, code)."""
    code = link_on(client)
    return ask(client, "lee", code)["ref"], code


def written(db) -> dict[str, Any]:
    """Everything a decision could change. `people/` is left out: every
    request's own sighting (`touch_person`) may refresh the caller's."""
    return {k: v for k, v in db.dump().items() if not k.startswith("people/")}


# -- the link and a code (§4.2) ----------------------------------------------------

def test_unknown_rotated_and_switched_off_codes_answer_byte_identical_404s(client) -> None:
    first = link_on(client)
    current = link_on(client, rotate=True)
    off = link_on(client, "erin", "research")
    response = client.put("/v1/workspace/join-link", headers=h("erin", "research"),
                          json={"on": False})
    assert response.status_code == 200, response.text
    assert "link" not in response.json()
    unknown = secrets.token_urlsafe(16)

    answers = []
    for code in (unknown, first, off, "", "x" * 300):
        for path, body in (("/v1/join/lookup", {"code": code}),
                           ("/v1/join-requests", {"code": code, "message": "hello"})):
            response = client.post(path, headers=h("lee"), json=body)
            answers.append((response.status_code, response.content))
    # MUTATION: answer a rotated code "rotated" or an off link "off" -> two bodies.
    assert {status for status, _ in answers} == {404}
    assert len({content for _, content in answers}) == 1, answers
    assert b"join_link_unknown" in answers[0][1]

    # The working code shows the display name and nothing else (no id, no group).
    response = client.post("/v1/join/lookup", headers=h("lee"), json={"code": current})
    assert response.status_code == 200, response.text
    assert response.json() == {"display_name": "Example Labs"}


def test_the_code_is_shown_once_and_never_again(client, db) -> None:
    code = link_on(client)
    response = client.get("/v1/workspace/join-link", headers=h("alice", "eng"))
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"on", "set_by", "set_at"}
    # Turning it on again keeps the code and does not show it.
    again = client.put("/v1/workspace/join-link", headers=h("alice", "eng"), json={"on": True})
    assert again.status_code == 200 and "link" not in again.json()
    for text in (response.text, again.text):
        assert code not in text
        assert joinrequests.code_hash(code) not in text


def test_only_the_workspaces_admin_or_a_platform_admin_sets_its_link(client, db) -> None:
    for method in ("GET", "PUT"):
        response = client.request(method, "/v1/workspace/join-link", headers=h("bob", "eng"),
                                  json={"on": True} if method == "PUT" else None)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "role_admin_required"
    assert not db.dump(f"{joinrequests.LINKS}/")
    # A personal workspace takes no requests.
    response = client.put("/v1/workspace/join-link", headers=h("lee"), json={"on": True})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "join_not_offered"
    assert not db.dump(f"{joinrequests.LINKS}/")
    # A platform admin reaches it.
    assert link_on(client, "ops", "eng")


# -- asking (§4.1) ---------------------------------------------------------------------

def test_a_request_is_202_and_asking_again_is_200_with_the_same_one(client, db) -> None:
    code = link_on(client)
    made = ask(client, "lee", code, "I'm on the labs rota from Monday")
    assert made["state"] == "requested" and made["display_name"] == "Example Labs"
    assert made["ref"].startswith("j-")
    assert set(made) == {"ref", "state", "display_name", "requested_at", "expires_at"}
    again = client.post("/v1/join-requests", headers=h("lee"), json={"code": code})
    assert again.status_code == 200, again.text
    assert again.json()["ref"] == made["ref"]
    assert len(db.dump(f"{joinrequests.REQUESTS}/")) == 1

    mine = client.get("/v1/join-requests/mine", headers=h("lee"))
    assert mine.status_code == 200, mine.text
    (row,) = mine.json()["requests"]
    assert row["ref"] == made["ref"] and row["state"] == "requested"
    assert row["display_name"] == "Example Labs"
    assert "tenant_id" not in row and "group" not in row


def test_a_member_and_a_platform_admin_are_refused_and_nothing_is_written(client, db) -> None:
    code = link_on(client)
    response = client.post("/v1/join-requests", headers=h("bob"), json={"code": code})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "join_already_member"
    response = client.post("/v1/join-requests", headers=h("ops"), json={"code": code})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "join_already_reaches"
    assert not db.dump(f"{joinrequests.REQUESTS}/")


def test_a_platform_member_is_already_a_member_once_the_switch_is_on(client, ctx, db) -> None:
    switch_on(ctx)
    ref, code = asked(client)
    assert approve(client, "alice", ref).status_code == 200
    response = client.post("/v1/join-requests", headers=h("lee"), json={"code": code})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "join_already_member"


def test_a_requester_withdraws_their_own_request_and_nobody_elses(client) -> None:
    code = link_on(client)
    ref = ask(client, "lee", code)["ref"]
    response = client.post(f"/v1/join-requests/{ref}/withdraw", headers=h("kim"))
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "join_request_unknown"
    response = client.post(f"/v1/join-requests/{ref}/withdraw", headers=h("lee"))
    assert response.status_code == 200, response.text
    assert response.json() == {"ref": ref, "state": "withdrawn"}
    (row,) = client.get("/v1/join-requests/mine", headers=h("lee")).json()["requests"]
    assert row["state"] == "withdrawn"


# -- who decides (§4.6) ---------------------------------------------------------------

def test_a_workspace_admin_decides_only_their_workspaces_request(client, db) -> None:
    lee_ref = ask(client, "lee", link_on(client))["ref"]
    kim_ref = ask(client, "kim", link_on(client, "erin", "research"))["ref"]
    before = written(db)

    # MUTATION: let any workspace admin decide any request -> alice approves kim's.
    for path, body in ((f"/v1/admin/join-requests/{kim_ref}/approve", {}),
                       (f"/v1/admin/join-requests/{kim_ref}/deny", {"reason": "no"})):
        response = client.post(path, headers=h("alice"), json=body)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "join_decider_required"
    # An unknown ref reads exactly like another workspace's: a workspace admin
    # learns nothing of the refs outside their workspaces.
    unknown = client.post("/v1/admin/join-requests/j-000000000000/approve",
                          headers=h("alice"), json={})
    assert unknown.status_code == 403
    assert unknown.json()["code"] == "join_decider_required"
    # A member who administers nothing is refused too.
    assert approve(client, "bob", lee_ref).status_code == 403
    assert written(db) == before, "a refused decision still wrote"

    response = approve(client, "alice", lee_ref)
    assert response.status_code == 200, response.text
    response = approve(client, "erin", kim_ref)
    assert response.status_code == 200, response.text
    vias = sorted(d["via"] for d in decisions(db))
    assert vias == ["workspace_admin", "workspace_admin"]


def test_a_platform_admin_decides_any_request(client, db) -> None:
    lee_ref = ask(client, "lee", link_on(client))["ref"]
    kim_ref = ask(client, "kim", link_on(client, "erin", "research"))["ref"]
    assert approve(client, "ops", lee_ref).status_code == 200
    response = client.post(f"/v1/admin/join-requests/{kim_ref}/deny", headers=h("ops"),
                           json={"reason": "research is full this term"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "denied"
    assert sorted(d["via"] for d in decisions(db)) == ["platform_admin", "platform_admin"]
    unknown = client.post("/v1/admin/join-requests/j-000000000000/approve",
                          headers=h("ops"), json={})
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "join_request_unknown"


def test_a_denial_needs_a_reason_and_the_person_is_shown_it(client, db) -> None:
    ref = ask(client, "lee", link_on(client))["ref"]
    response = client.post(f"/v1/admin/join-requests/{ref}/deny", headers=h("alice"), json={})
    assert response.status_code == 422, response.text
    assert not decisions(db)
    response = client.post(f"/v1/admin/join-requests/{ref}/deny", headers=h("alice"),
                           json={"reason": "labs is invite-only"})
    assert response.status_code == 200, response.text
    (row,) = client.get("/v1/join-requests/mine", headers=h("lee")).json()["requests"]
    assert row["state"] == "denied" and row["reason"] == "labs is invite-only"
    assert approve(client, "alice", ref).json()["code"] == "join_request_decided"


def test_an_approval_may_lower_the_default_to_viewer_and_never_raise_it(client, db) -> None:
    ref = ask(client, "lee", link_on(client))["ref"]
    response = approve(client, "alice", ref, "admin")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "join_role_not_allowed"
    # eng's default becomes viewer: member would RAISE it, and is refused.
    response = client.put("/v1/workspace/roles/default", headers=h("alice", "eng"),
                          json={"role": "viewer"})
    assert response.status_code == 200, response.text
    response = approve(client, "alice", ref, "member")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "join_role_not_allowed"
    assert not decisions(db)
    response = approve(client, "alice", ref)
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "viewer"


def test_an_approval_whose_workspace_changed_writes_nothing(client, ctx, db, monkeypatch) -> None:
    ref = ask(client, "lee", link_on(client))["ref"]
    before = written(db)
    monkeypatch.setattr(type(ctx.authenticator), "registered_group_workspaces",
                        lambda self: {"eng": "eng-new@example.com",
                                      "research": RESEARCH_GROUP})
    response = approve(client, "ops", ref)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "join_workspace_changed"
    assert written(db) == before


# -- what approval does (§4.3) -------------------------------------------------------

def test_with_the_switch_off_approval_admits_nobody_and_names_the_group_command(client, db) -> None:
    ref = ask(client, "lee", link_on(client))["ref"]
    response = approve(client, "alice", ref, "viewer")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admitted"] is False
    assert body["group_change"] == {
        "group": ENG_GROUP,
        "command": f"gcloud identity groups memberships add --group-email={ENG_GROUP} "
                   f"--member-email={LEE}",
    }
    assert not membership_docs(db)
    assert not role_grants_for(db, LEE)
    (row,) = audit(db, joinrequests.AUDIT_APPROVED)
    assert row["detail"]["admitted"] is False and row["detail"]["role"] == "viewer"


def test_with_the_switch_on_approval_writes_the_decision_membership_role_and_audit_rows(
        client, ctx, db) -> None:
    switch_on(ctx)
    ref = ask(client, "lee", link_on(client))["ref"]
    committed = db.transactions_committed
    response = approve(client, "alice", ref, "viewer")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admitted"] is True and body["role"] == "viewer"
    assert body.get("group_change") is None
    # One transaction carried all of it.
    assert db.transactions_committed == committed + 1

    (grant,) = membership_docs(db)
    assert (grant["tenant_id"], grant["group"], grant["subject"], grant["state"]) == (
        "eng", ENG_GROUP, LEE, memberships.MEMBER)
    keys = ctx.roles._keys()
    assert memberships.membership_verifies(keys, grant)
    (role,) = role_grants_for(db, LEE)
    assert role["role"] == "viewer" and roles.grant_verifies(keys, role)
    (approved,) = audit(db, joinrequests.AUDIT_APPROVED)
    assert approved["detail"] == {"ref": ref, "role": "viewer", "admitted": True,
                                  "via": "workspace_admin"}
    (added,) = audit(db, memberships.AUDIT_ADDED)
    assert added["detail"] == {"ref": ref, "via": "join_request"}
    (granted,) = audit(db, roles.AUDIT_GRANTED)
    assert granted["target_email"] == LEE and granted["detail"]["to"] == "viewer"


def test_an_approval_at_the_default_writes_no_role_override(client, ctx, db) -> None:
    switch_on(ctx)
    ref = ask(client, "lee", link_on(client))["ref"]
    response = approve(client, "alice", ref)
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "member"
    assert len(membership_docs(db)) == 1
    assert not role_grants_for(db, LEE)


@pytest.mark.parametrize("seam", [MembershipStore, roles.RoleStore], ids=["membership", "role"])
def test_an_injected_failure_in_the_approval_leaves_none_of_it(client, ctx, db, monkeypatch,
                                                               seam) -> None:
    switch_on(ctx)
    ref = ask(client, "lee", link_on(client))["ref"]
    before = written(db)

    def fail(row):
        raise RuntimeError("injected")

    monkeypatch.setattr(seam, "audit_row", staticmethod(fail))
    # MUTATION: let RoleStore.write commit its own batch -> the lowered role
    # survives a membership write that failed after it.
    response = approve(client, "alice", ref, "viewer")
    assert response.status_code == 500, response.text
    assert written(db) == before, "a failed approval left part of itself behind"
    (row,) = client.get("/v1/join-requests/mine", headers=h("lee")).json()["requests"]
    assert row["state"] == "requested"


def test_role_store_write_with_a_writer_commits_nothing_itself(ctx, db) -> None:
    batch = db.batch()
    store = ctx.roles
    store.write(tenant_id="eng", subject=LEE, role="viewer", by="alice@example.com",
                via="console", reason="", previous=None, action=roles.AUDIT_GRANTED,
                detail={}, writer=batch)
    assert not role_grants_for(db, LEE) and not audit(db, roles.AUDIT_GRANTED)
    batch.commit()
    assert len(role_grants_for(db, LEE)) == 1 and len(audit(db, roles.AUDIT_GRANTED)) == 1


# -- the joined, and removing them (§4.4) ---------------------------------------------

def test_a_workspace_admin_lists_and_removes_a_platform_membership(client, ctx, db) -> None:
    switch_on(ctx)
    ref = ask(client, "lee", link_on(client))["ref"]
    assert approve(client, "alice", ref).status_code == 200
    response = client.get("/v1/workspace/members", headers=h("alice", "eng"))
    assert response.status_code == 200, response.text
    (member,) = response.json()["members"]
    assert member["email"] == LEE and member["still_in_group"] is False
    assert member["by"] == "alice@example.com" and member["group_changed"] is False

    for user in ("bob",):
        refused = client.request("DELETE", "/v1/workspace/members/person",
                                 headers=h(user, "eng"), json={"email": LEE})
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "join_decider_required"
        listed = client.get("/v1/workspace/members", headers=h(user, "eng"))
        assert listed.status_code == 403
    response = client.request("DELETE", "/v1/workspace/members/person", headers=h("alice", "eng"),
                              json={"email": LEE, "reason": "left the rota"})
    assert response.status_code == 200, response.text
    assert response.json() == {"email": LEE, "state": "removed", "still_in_group": False}
    (removed,) = audit(db, memberships.AUDIT_REMOVED)
    assert removed["detail"] == {"still_in_group": False, "via": "workspace_admin"}
    assert client.get("/v1/workspace/members", headers=h("alice", "eng")).json()["members"] == []
    again = client.request("DELETE", "/v1/workspace/members/person", headers=h("alice", "eng"),
                           json={"email": LEE})
    assert again.status_code == 404


# -- Admin › People and roles (§4.6) --------------------------------------------------

def test_the_people_read_carries_the_open_requests_pending_and_the_requested_cell(client) -> None:
    lee_ref = ask(client, "lee", link_on(client), "on the labs rota")["ref"]
    kim_ref = ask(client, "kim", link_on(client, "erin", "research"))["ref"]

    body = client.get("/v1/admin/people", headers=h("alice")).json()
    # MUTATION: serve every open request to a workspace admin -> kim's is here.
    (row,) = body["join_requests"]
    assert row["ref"] == lee_ref and row["requester"] == LEE and row["tenant_id"] == "eng"
    assert row["display_name"] == "Example Labs" and row["message"] == "on the labs rota"
    assert body["pending"] == 1
    grid = {r["email"]: r["roles"] for r in body["people"]}
    assert grid[LEE] == {"eng": "requested"}
    assert KIM not in grid
    assert "research" not in str(body)

    body = client.get("/v1/admin/people", headers=h("ops")).json()
    assert sorted(r["ref"] for r in body["join_requests"]) == sorted([lee_ref, kim_ref])
    assert body["pending"] == 2

    # Decided, it is no longer waiting for anyone.
    assert approve(client, "alice", lee_ref).status_code == 200
    body = client.get("/v1/admin/people", headers=h("alice")).json()
    assert body["join_requests"] == [] and body["pending"] == 0


def test_no_response_carries_a_document_id_or_a_code_hash(client, ctx, db) -> None:
    switch_on(ctx)
    code = link_on(client)
    texts: list[str] = []

    def keep(response):
        assert response.status_code < 500, response.text
        texts.append(response.text)
        return response

    keep(client.post("/v1/join/lookup", headers=h("lee"), json={"code": code}))
    ref = keep(client.post("/v1/join-requests", headers=h("lee"), json={"code": code})).json()["ref"]
    kim_ref = keep(client.post("/v1/join-requests", headers=h("kim"),
                               json={"code": code})).json()["ref"]
    keep(client.get("/v1/join-requests/mine", headers=h("lee")))
    keep(client.get("/v1/workspace/join-link", headers=h("alice", "eng")))
    keep(client.get("/v1/admin/people", headers=h("alice")))
    keep(client.get("/v1/admin/people", headers=h("ops")))
    keep(approve(client, "alice", ref, "viewer"))
    keep(client.post(f"/v1/admin/join-requests/{kim_ref}/deny", headers=h("alice"),
                     json={"reason": "not this term"}))
    keep(client.get("/v1/workspace/members", headers=h("alice", "eng")))
    keep(client.get("/v1/join-requests/mine", headers=h("kim")))
    keep(client.request("DELETE", "/v1/workspace/members/person", headers=h("alice", "eng"),
                        json={"email": LEE}))

    secret_ids: set[str] = set()
    for prefix in (joinrequests.REQUESTS, memberships.COLLECTION, roles.GRANTS_COLLECTION):
        for path in db.dump(f"{prefix}/"):
            # Every segment after a collection name is a document id.
            secret_ids.update(path.split("/")[1::2])
    hashes = {d["code_sha256"] for d in db.dump(f"{joinrequests.LINKS}/").values()}
    assert secret_ids and hashes, "the flow wrote nothing, so the sweep would read nothing"
    for text in texts:
        for doc_id in secret_ids:
            assert doc_id not in text, f"a response carries document id {doc_id}"
        for digest in hashes:
            assert digest not in text, "a response carries a join code's hash"
        assert code not in text, "a response after the link's creation carries its code"


def test_the_sweep_prunes_requests_decided_more_than_90_days_ago(client, ctx, db) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=joinrequests.KEEP_DAYS + 20)
    then = JoinStore(db, ctx.settings, now=lambda: old)
    view, _ = then.ask(tenant_id="eng", group=ENG_GROUP, requester=KIM)
    then.decide(view.request.ref, state=joinrequests.DENIED, by="alice@example.com",
                via=joinrequests.VIA_WORKSPACE_ADMIN, reason="old")
    fresh = ask(client, "lee", link_on(client))["ref"]
    response = client.post("/v1/admin/workspaces/sweep", headers=h("ops"))
    assert response.status_code == 200, response.text
    assert response.json()["join_requests_pruned"] == 1
    refs = {d.get("ref") for d in db.dump(f"{joinrequests.REQUESTS}/").values()}
    assert refs == {fresh}
