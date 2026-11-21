"""`POST /v1/workspace/leave`: a person ends their OWN platform membership of
the workspace their sign-in selected (docs/design/join-requests.md §4.4;
owner decision 2026-11-20, the self-leave route the join probe ends on).

Read with an attacker's eye. The attacks are:

* LEAVING FOR SOMEONE ELSE: the body has no subject (an `email` is 422), and
  the membership ended is always the caller's in the workspace `tenant_scope`
  selected for them;
* a service account, a personal workspace and a person with no platform
  membership there writing a `removed` grant anyway: each is refused with
  its own code and nothing is written;
* the workspace's last `admin` by override walking out and leaving it with
  no admin of its own: 409 `workspace_leave_last_admin`, nothing written --
  and another person's admin override counts only while that person can
  still select the workspace (a membership that counts, or a fresh group
  check), so a kept override of someone who left is no admin; two admins
  leaving at once cannot both go.

Who is who (`eng@example.com` and `research@example.com` are the groups):
`ops` a platform admin; `alice` in eng, eng's admin; `bob` in eng, no grant;
`erin` research's admin; `lee` in no group, the person who joins and leaves;
`dan` and `kim` in no group, other admins of eng by platform membership or
by override alone; `builder` a service account the eng group lists.

Every key is made at runtime (CLAUDE.md: nothing added may look like a
credential).
"""

from __future__ import annotations

import secrets
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gexc

from swarm_api import memberships, roles
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import last_admin
from swarm_api.routes.join_requests import membership_store
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, seed_tenant
from .fakes import FakeDocumentRef, FakeFirestore, FakeTransaction
from .test_people_admin import FakePool, FakePublisher

SVC = "builder@example-project.iam.gserviceaccount.com"
GROUPS = {
    "ops@example.com": (ADMIN_GROUP,),
    "alice@example.com": (ENG_GROUP,),
    "bob@example.com": (ENG_GROUP,),
    "erin@example.com": (RESEARCH_GROUP,),
    "lee@example.com": (),
    "dan@example.com": (),
    "kim@example.com": (),
    SVC: (ENG_GROUP,),
}
LEE = "lee@example.com"
DAN = "dan@example.com"
KIM = "kim@example.com"
BOB = "bob@example.com"
ALICE = "alice@example.com"
LEAVE = "/v1/workspace/leave"


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
    return db


def build(db, settings=None):
    """One swarm-api instance over `db`; a second over the same `db` and
    `settings` is another instance, with its own role and membership caches."""
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    ctx = build_context(
        settings=settings or api_settings(settings_key=secrets.token_hex(32),
                                          console_url="https://console.example.com",
                                          rate_limit_burst=10_000, allowed_users=(SVC,)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(),
    )
    # `platform_memberships` on: a membership admits, so the person selects eng.
    object.__setattr__(ctx.settings, "platform_memberships", True)
    return ctx


def serve(ctx) -> TestClient:
    app = create_app(ctx)
    app.state.workspace_publisher = FakePublisher()
    app.state.account_pool = FakePool()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def ctx(db):
    return build(db)


@pytest.fixture
def client(ctx) -> TestClient:
    client = serve(ctx)
    for who, tenant in (("alice", "eng"), ("erin", "research")):
        set_role(client, f"{who}@example.com", "admin", tenant)
    return client


def set_role(client, email: str, role: str | None, tenant: str = "eng") -> None:
    path = f"/v1/admin/workspaces/{tenant}/roles/person"
    if role is None:
        response = client.request("DELETE", path, headers=h("ops"), json={"email": email})
    else:
        response = client.put(path, headers=h("ops"), json={"email": email, "role": role})
    assert response.status_code == 200, response.text


def joined(client, role: str | None = None) -> None:
    """lee asks to join eng through its link and alice approves."""
    response = client.put("/v1/workspace/join-link", headers=h("alice", "eng"), json={"on": True})
    assert response.status_code == 200, response.text
    code = response.json()["link"].split("#", 1)[1]
    response = client.post("/v1/join-requests", headers=h("lee"), json={"code": code})
    assert response.status_code == 202, response.text
    body = {} if role is None else {"role": role}
    response = client.post(f"/v1/admin/join-requests/{response.json()['ref']}/approve",
                           headers=h("alice"), json=body)
    assert response.status_code == 200, response.text
    assert response.json()["admitted"] is True


def audit(db, action: str) -> list[dict]:
    return [r for r in db.collection_docs(AUDIT_COLLECTION) if r.get("action") == action]


def role_grants_for(db, subject: str) -> list[dict]:
    return [d for d in db.dump(f"{roles.GRANTS_COLLECTION}/").values()
            if d.get("subject") == subject]


def written(db) -> dict[str, Any]:
    """Everything a leave could change; `people/` is each request's own
    sighting, and a refusal's own audit row is asserted apart (`refused`)."""
    return {k: v for k, v in db.dump().items()
            if not k.startswith("people/") and v.get("action") != last_admin.AUDIT_REFUSED}


def refused(db) -> list[dict]:
    return audit(db, last_admin.AUDIT_REFUSED)


def grant_member(ctx, subject: str) -> None:
    """A platform membership of eng written directly (a person the group lists too)."""
    membership_store(ctx).write(tenant_id="eng", group=ENG_GROUP, subject=subject,
                                state=memberships.MEMBER, by="ops@example.com",
                                via=memberships.VIA_PLATFORM_ADMIN)


def selects(client, user: str, tenant: str) -> int:
    return client.get("/v1/tenants/me", headers=h(user, tenant)).status_code


# -- the happy path ---------------------------------------------------------------------

def test_a_person_leaves_their_own_platform_membership_with_an_audit_row(client, ctx, db) -> None:
    joined(client)
    assert selects(client, "lee", "eng") == 200

    response = client.post(LEAVE, headers=h("lee", "eng"), json={"reason": "rota ended"})
    assert response.status_code == 200, response.text
    assert response.json() == {"email": LEE, "state": "removed", "still_in_group": False}

    grant = membership_store(ctx).read(LEE, fresh=True).newest["eng"]
    # MUTATION: write the grant as `workspace_admin` or by someone else -> these fail.
    assert (grant.state, grant.by, grant.via) == ("removed", LEE, memberships.VIA_SELF)
    (row,) = audit(db, memberships.AUDIT_REMOVED)
    assert row["by"] == LEE and row["target_email"] == LEE and row["target_tenant_id"] == "eng"
    assert row["detail"] == {"still_in_group": False, "via": "self"}
    # Gone: the person no longer selects eng, and no admin lists them.
    assert selects(client, "lee", "eng") == 403
    listed = client.get("/v1/workspace/members", headers=h("alice", "eng")).json()["members"]
    assert listed == []


def test_leaving_keeps_a_role_override_as_removal_does(client, ctx, db) -> None:
    """remove_member does not touch the person's role grant; leaving matches
    it, so asking and being approved again restores what an admin named."""
    joined(client, role="viewer")
    before = role_grants_for(db, LEE)
    assert [g["role"] for g in before] == ["viewer"]

    assert client.post(LEAVE, headers=h("lee", "eng")).status_code == 200
    # MUTATION: clear the override on leaving -> a second grant appears.
    assert role_grants_for(db, LEE) == before
    assert ctx.roles.newest_valid("eng", LEE, fresh=True).role == "viewer"
    assert not audit(db, roles.AUDIT_CLEARED)


def test_a_person_the_group_lists_too_is_told_they_are_still_in_it(client, ctx, db) -> None:
    grant_member(ctx, BOB)
    response = client.post(LEAVE, headers=h("bob", "eng"))
    assert response.status_code == 200, response.text
    # MUTATION: answer still_in_group False (or leave it out) -> this fails.
    assert response.json() == {"email": BOB, "state": "removed", "still_in_group": True}
    (row,) = audit(db, memberships.AUDIT_REMOVED)
    assert row["detail"] == {"still_in_group": True, "via": "self"}
    # The group still lists bob, so he still selects eng; the platform cannot change that.
    assert selects(client, "bob", "eng") == 200


# -- refusals: each its own code, nothing written ---------------------------------------

def test_a_subject_in_the_body_is_refused_and_nothing_is_written(client, db) -> None:
    joined(client)
    before = written(db)
    response = client.post(LEAVE, headers=h("lee", "eng"), json={"email": BOB})
    assert response.status_code == 422, response.text
    assert written(db) == before


def test_a_service_account_is_refused_and_nothing_is_written(client, ctx, db) -> None:
    assert selects(client, "builder", "eng") == 200
    before = written(db)
    response = client.post(LEAVE, headers=h("builder", "eng"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "workspace_leave_person_only"
    assert written(db) == before


def test_a_personal_workspace_is_refused_and_nothing_is_written(client, db) -> None:
    # lee in no group and no workspace: their sign-in selects their personal one.
    before = written(db)
    response = client.post(LEAVE, headers=h("lee"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "join_not_offered"
    assert written(db) == before


def test_a_caller_with_no_platform_membership_is_refused_and_nothing_is_written(
        client, db) -> None:
    before = written(db)
    # bob is in eng through its group alone.
    response = client.post(LEAVE, headers=h("bob", "eng"))
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "workspace_leave_not_member"
    assert written(db) == before
    assert not audit(db, memberships.AUDIT_REMOVED)


def test_leaving_twice_is_refused_the_second_time(client, ctx, db) -> None:
    grant_member(ctx, BOB)
    assert client.post(LEAVE, headers=h("bob", "eng")).status_code == 200
    before = written(db)
    again = client.post(LEAVE, headers=h("bob", "eng"))
    assert again.status_code == 404, again.text
    assert again.json()["code"] == "workspace_leave_not_member"
    assert written(db) == before


def test_a_person_leaves_only_the_workspace_their_sign_in_selected(client, ctx, db) -> None:
    joined(client)
    # lee cannot select research, so nothing about eng is touched from there.
    before = written(db)
    response = client.post(LEAVE, headers=h("lee", "research"))
    assert response.status_code == 403, response.text
    assert written(db) == before
    assert membership_store(ctx).read(LEE, fresh=True).newest["eng"].state == "member"


# -- the last admin -------------------------------------------------------------------

def test_the_last_admin_by_override_cannot_leave(client, ctx, db) -> None:
    joined(client)
    set_role(client, LEE, "admin")
    set_role(client, ALICE, None)
    before = written(db)
    response = client.post(LEAVE, headers=h("lee", "eng"))
    # MUTATION: drop the guard -> 200 and eng has no admin of its own.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workspace_leave_last_admin"
    assert written(db) == before
    # MUTATION: write no row for a refused leave -> this fails.
    (row,) = refused(db)
    assert (row["by"], row["target_email"], row["target_tenant_id"]) == (LEE, LEE, "eng")
    assert row["detail"] == {"route": "leave", "via": "self",
                             "refused": "workspace_leave_last_admin"}

    # With another admin named, they may.
    set_role(client, ALICE, "admin")
    response = client.post(LEAVE, headers=h("lee", "eng"))
    assert response.status_code == 200, response.text


def test_the_last_admin_the_group_still_holds_may_leave_the_platform_membership(
        client, ctx, db) -> None:
    grant_member(ctx, BOB)
    set_role(client, BOB, "admin")
    set_role(client, ALICE, None)
    response = client.post(LEAVE, headers=h("bob", "eng"))
    # The group keeps bob in eng, and his override with him: eng keeps its admin.
    assert response.status_code == 200, response.text
    assert response.json()["still_in_group"] is True


def test_a_membership_that_no_longer_counts_for_the_group_is_not_left(client, ctx, db) -> None:
    """A grant written for the workspace's old group admits nobody now
    (`counts_for`); leaving it is refused as resolution reads it."""
    membership_store(ctx).write(tenant_id="eng", group="old-eng@example.com", subject=BOB,
                                state=memberships.MEMBER, by="ops@example.com",
                                via=memberships.VIA_PLATFORM_ADMIN)
    before = written(db)
    response = client.post(LEAVE, headers=h("bob", "eng"))
    # MUTATION: check only `state == member` -> 200 and a removed grant is written.
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "workspace_leave_not_member"
    assert written(db) == before


def test_a_read_cut_short_before_it_answered_is_a_503_not_a_404(
        client, db, monkeypatch) -> None:
    """bob selects eng through its group; his bounded membership read ends
    before reaching an eng grant, so whether he holds one is unknown."""
    real = memberships.MembershipStore.read

    def read(self, subject, **kwargs):
        if subject == BOB:
            return memberships.MembershipRead(subject=BOB, newest={}, cut_short=True)
        return real(self, subject, **kwargs)

    monkeypatch.setattr(memberships.MembershipStore, "read", read)
    before = written(db)
    response = client.post(LEAVE, headers=h("bob", "eng"))
    # MUTATION: drop `cut_short` -> 404 workspace_leave_not_member.
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "membership_unavailable"
    assert written(db) == before


# -- who else counts as an admin ----------------------------------------------------

def test_an_admin_override_that_outlived_its_membership_is_no_admin(client, ctx, db) -> None:
    """dan and lee are eng's admins by platform membership alone. dan leaves
    (lee remains); his override is kept, by design, but he can no longer
    select eng, so lee is its last admin and may not leave. Nor does kim,
    named admin before ever joining, count."""
    joined(client)
    grant_member(ctx, DAN)
    set_role(client, LEE, "admin")
    set_role(client, DAN, "admin")
    set_role(client, KIM, "admin")
    set_role(client, ALICE, None)
    assert client.post(LEAVE, headers=h("dan", "eng")).status_code == 200
    assert ctx.roles.newest_valid("eng", DAN, fresh=True).role == "admin"

    before = written(db)
    response = client.post(LEAVE, headers=h("lee", "eng"))
    # MUTATION: count any other `admin` override -> 200 and eng has no admin left.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workspace_leave_last_admin"
    assert written(db) == before
    assert selects(client, "lee", "eng") == 200


class FreshOnlyGroups:
    """A directory whose cached answer is stale: only a fresh check finds
    bob in eng's group."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, bool]] = []

    def groups_for(self, email: str, candidates: tuple[str, ...], *,
                   fresh: bool = False) -> tuple[str, ...]:
        self.asked.append((email, fresh))
        if email == BOB and fresh:
            return tuple(g for g in candidates if g == ENG_GROUP)
        return ()


def test_another_admin_a_fresh_group_check_finds_lets_the_admin_leave(client, ctx, db) -> None:
    joined(client)
    set_role(client, LEE, "admin")
    set_role(client, BOB, "admin")
    set_role(client, ALICE, None)
    directory = FreshOnlyGroups()
    client.app.state.workspace_directory = directory
    response = client.post(LEAVE, headers=h("lee", "eng"))
    # MUTATION: count only platform memberships, or ask the group from cache -> 409.
    assert response.status_code == 200, response.text
    assert (BOB, True) in directory.asked


def test_a_stale_cached_admin_role_is_not_counted(client, ctx, db) -> None:
    """Another instance cleared dan's admin override; this instance's role
    cache still says admin. The roster the guard reads is fresh."""
    joined(client)
    grant_member(ctx, DAN)
    set_role(client, LEE, "admin")
    set_role(client, DAN, "admin")
    set_role(client, ALICE, None)
    assert dict(ctx.roles.overrides("eng"))[DAN].role == "admin"  # cached here now
    other = serve(build(db, ctx.settings))
    set_role(other, DAN, None)

    response = client.post(LEAVE, headers=h("lee", "eng"))
    # MUTATION: read the roster through the cache -> 200 on dan's cleared role.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workspace_leave_last_admin"


# -- two admins leaving at once -----------------------------------------------------

GUARD = memberships.LEAVE_GUARD_COLLECTION + "/"


class RacingTransaction(FakeTransaction):
    """Firestore's conflict on the leave guard: a transaction whose guard
    document changed between its read and its commit is aborted, and the
    REAL `firestore.transactional` runs it again. `on_commit` runs another
    whole leave just before the first commit -- the interleaving to beat."""

    def _begin(self, retry_id: bytes | None = None) -> None:
        super()._begin(retry_id)
        self._seen: dict[str, Any] = {}

    def get(self, ref: Any, **kwargs: Any) -> Any:
        if isinstance(ref, FakeDocumentRef) and ref.path.startswith(GUARD):
            self._seen[ref.path] = self._db.docs.get(ref.path)
        return super().get(ref, **kwargs)

    def _commit(self) -> list[Any]:
        hook, self._db.on_commit = self._db.on_commit, None
        if hook is not None:
            hook()
        if any(self._db.docs.get(path) != seen for path, seen in self._seen.items()):
            self._db.aborts += 1
            raise gexc.Aborted("the leave guard changed under this transaction")
        return super()._commit()


class RacingFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.on_commit: Callable[[], None] | None = None
        self.aborts = 0

    def transaction(self, **kwargs: Any) -> RacingTransaction:
        return RacingTransaction(self)


def test_the_last_two_admins_leaving_at_once_cannot_both_go() -> None:
    db = RacingFirestore()
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build(db)
    client = serve(ctx)
    set_role(client, ALICE, "admin")
    joined(client)
    grant_member(ctx, DAN)
    set_role(client, LEE, "admin")
    set_role(client, DAN, "admin")
    set_role(client, ALICE, None)

    answers: list[Any] = []
    db.on_commit = lambda: answers.append(client.post(LEAVE, headers=h("dan", "eng")))
    first = client.post(LEAVE, headers=h("lee", "eng"))
    if not answers:  # no transaction at all: dan leaves after lee
        answers.append(client.post(LEAVE, headers=h("dan", "eng")))
    (second,) = answers
    # MUTATION: check outside the transaction, or skip the guard -> both 200.
    assert sorted((first.status_code, second.status_code)) == [200, 409], (first.text,
                                                                          second.text)
    # dan, inside lee's commit, went first; lee's retry saw him gone.
    assert second.status_code == 200 and first.json()["code"] == "workspace_leave_last_admin"
    assert db.aborts == 1
    store = membership_store(ctx)
    assert store.read(LEE, fresh=True).newest["eng"].state == memberships.MEMBER
    assert store.read(DAN, fresh=True).newest["eng"].state == memberships.REMOVED
