"""Workspace roles on schedules, repositories, access and accounts, and on a
schedule's firing (docs/design/workspace-roles.md §4.1, §4.2.3, §4.2.4, §4.8,
Q13; lane ROLES-GATES-AUTOMATE, part of #1518).

Who is who, in eng (`eng@saga.xyz`):

* `alice` -- a member with no grant: `member`, the migration default;
* `vera` -- a member a grant narrowed to `viewer`;
* `wanda` -- a member a platform admin named the workspace's `admin`; she is
  NOT a platform admin;
* `root` -- a platform admin who is also a member, narrowed to `viewer`;
* `ops` -- a platform admin in no tenant group: reaches eng only through
  `platform_admin_reach`.

What is held:

* with `role_member_required` on, a `viewer` is 403 on every schedule,
  repository, access and account write, and nothing is written; a `member`
  is not refused by it; with it off the write goes through and the log
  names the refusal it would have made;
* a repository's `context` is the workspace `admin`'s (Q13): a `member` is
  403 `role_admin_required` with that switch on, a workspace `admin` sets it
  whatever the switch says, and `platform` and `merge_policy` stay refused to
  a workspace admin who is not a platform admin;
* a firing whose owner was narrowed to `viewer` is skipped and the schedule
  auto-paused `OWNER_ROLE_NARROWED`, with a `schedule_audit` row, when the
  switch is on, and fires and logs when it is off;
* a firing whose owner is a non-member platform admin fires, with a
  `platform_admin_acted` row written before the work, and pauses
  `OWNER_NOT_MEMBER` once they lose the platform role.

No cloud and no emulator: the in-memory Firestore and the real app.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import codeintel, refusals, roles, schedulefire
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, seed_tenant
from .fakes import FakeFirestore
from .test_roles_store import grant
from .test_schedule_tick import NOW, FakeExecutor, firings, seed_schedule, tasks, ticker

MEMBER_SWITCH = refusals.env_name("role_member_required")
ADMIN_SWITCH = refusals.env_name("role_admin_required")
OWNER_SWITCH = refusals.env_name(codeintel.OwnerRequired.code)

ALICE, VERA, WANDA = "alice@saga.xyz", "vera@saga.xyz", "wanda@saga.xyz"
ROOT, OPS = "root@saga.xyz", "ops@saga.xyz"
GROUPS = {
    ALICE: (ENG_GROUP,),
    VERA: (ENG_GROUP,),
    WANDA: (ENG_GROUP,),
    ROOT: (ADMIN_GROUP, ENG_GROUP),
    OPS: (ADMIN_GROUP,),
}
SCHEDULE = "sch_000000000001"
REPO = repo_id_for("eng", "saga-xyz", "widgets")
ACCOUNT = "acct-eng-1"


class LiveGroups:
    """`StaticGroups` over a mapping a test may change: someone leaving the
    admin group is a change of directory, not of code."""

    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = {k: tuple(v) for k, v in mapping.items()}

    def groups_for(self, member_email: str, candidate_groups: tuple[str, ...], *,
                   fresh: bool = False) -> tuple[str, ...]:
        mine = {g.lower() for g in self.mapping.get(member_email.lower(), ())}
        return tuple(g for g in candidate_groups if g.lower() in mine)


class Recorder:
    """Any service a route hands its work to: records the call, answers {}."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> dict:
            self.calls.append(name)
            return {}
        return call


class Pool(Recorder):
    """The broker's account pool: one account, owned by eng."""

    def list_accounts(self, tenant_id: str) -> dict:
        self.calls.append("list_accounts")
        return {"accounts": [{"account_id": ACCOUNT, "owner_tenant": "eng", "lend_to": []}]}


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def directory() -> LiveGroups:
    return LiveGroups(GROUPS)


@pytest.fixture
def ctx(db, directory, monkeypatch):
    for name in (MEMBER_SWITCH, ADMIN_SWITCH, "SCHEDULES_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    for code in ("BUDGET_EXHAUSTED", "RUN_OVER_BUDGET", "CONSECUTIVE_FAILURES"):
        monkeypatch.delenv(refusals.env_name(code), raising=False)
    seed_tenant(db, "eng")
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "saga.xyz"}
              for e in GROUPS}
    context = build_context(
        settings=api_settings(platform_admin_reach=True, settings_key=secrets.token_hex(32),
                              tenant_groups=(ENG_GROUP, RESEARCH_GROUP)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=directory,
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(),
    )
    context.now = lambda: NOW
    grant(context.roles, VERA, roles.VIEWER)
    grant(context.roles, WANDA, roles.ADMIN)
    grant(context.roles, ROOT, roles.VIEWER)
    return context


@pytest.fixture
def services() -> dict[str, Recorder]:
    return {"access": Recorder(), "pool": Pool()}


@pytest.fixture
def client(ctx, services) -> TestClient:
    app = create_app(ctx)
    app.state.access = services["access"]
    app.state.account_pool = services["pool"]
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def seeded(db):
    seed_schedule(db)
    db.docs[f"repositories/{REPO}"] = {
        "repo_id": REPO, "tenant_id": "eng", "owner": "saga-xyz", "repo": "widgets",
        "default_branch": "main", "created_at": NOW, "index": {},
    }
    return db


def h(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if tenant:
        headers["X-Swarm-Tenant"] = tenant
    return headers


def state(db) -> dict[str, Any]:
    """Every document but `people/`, which any authenticated request records
    on first sight (the People page's roster), refused or not."""
    return {k: v for k, v in db.dump().items() if not k.startswith("people/")}


def credential() -> str:
    # Built at runtime: nothing in this file may look like a credential.
    return secrets.token_hex(16)


#: Every write this lane gates, with a body each route's model accepts, so a
#: refusal is the role's and never the body's.
WRITES: list[tuple[str, str, str, Any]] = [
    ("schedule create", "POST", "/v1/schedules", {"type": "issue-sweep"}),
    ("schedule edit", "PATCH", f"/v1/schedules/{SCHEDULE}", {"revision": 1}),
    ("schedule delete", "DELETE", f"/v1/schedules/{SCHEDULE}?confirm={SCHEDULE}", None),
    ("schedule pause", "POST", f"/v1/schedules/{SCHEDULE}:pause", None),
    ("schedule pause-all", "POST", "/v1/schedules:pause-all", {"confirm": "eng"}),
    ("schedule resume", "POST", f"/v1/schedules/{SCHEDULE}:resume", None),
    ("schedule take-ownership", "POST", f"/v1/schedules/{SCHEDULE}:take-ownership", None),
    ("schedule run", "POST", f"/v1/schedules/{SCHEDULE}:run", None),
    ("repository register", "POST", "/v1/repositories", {"repository": "saga-xyz/gadgets"}),
    ("repository patch", "PATCH", f"/v1/repositories/{REPO}", {"paused": True}),
    ("repository delete", "DELETE", f"/v1/repositories/{REPO}", None),
    ("repository index", "POST", f"/v1/repositories/{REPO}/index", None),
    ("repository index:run", "POST", f"/v1/repositories/{REPO}/index:run", None),
    ("access enable owner", "POST", "/v1/access/orgs", {"owner": "saga-xyz"}),
    ("access disable owner", "DELETE", "/v1/access/orgs/saga-xyz", None),
    ("access install request", "POST", "/v1/access/orgs/saga-xyz/install-request", None),
    ("access grant", "PUT", f"/v1/access/grants/{REPO}",
     {"repository": "saga-xyz/widgets", "mode": "read"}),
    ("access revoke", "DELETE", f"/v1/access/grants/{REPO}", None),
    ("access verify", "POST", f"/v1/access/grants/{REPO}/verify", None),
    ("account register", "POST", "/v1/accounts", "REGISTER"),
    ("account sign-in start", "POST", "/v1/accounts/authorize", {"label": "team"}),
    ("account sign-in finish", "POST", "/v1/accounts/exchange", "EXCHANGE"),
    ("account refresh", "POST", f"/v1/accounts/{ACCOUNT}/refresh", None),
    ("account lending", "PUT", f"/v1/accounts/{ACCOUNT}/lending", {"lend_to": []}),
    ("account model classes", "PUT", f"/v1/accounts/{ACCOUNT}/model-classes",
     {"model_classes": []}),
    ("account state", "PUT", f"/v1/accounts/{ACCOUNT}/state", {"state": "PAUSED"}),
    ("account delete", "DELETE", f"/v1/accounts/{ACCOUNT}", None),
]


def send(client, method: str, path: str, body: Any, user: str):
    if body == "REGISTER":
        body = {"label": "team", "credential": credential()}
    elif body == "EXCHANGE":
        body = {"state": secrets.token_hex(8), "code": secrets.token_hex(4)}
    kwargs: dict[str, Any] = {"headers": h(user)}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


def code_of(response) -> str | None:
    try:
        return response.json().get("code")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The member gate on every write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,method,path,body", WRITES, ids=[w[0] for w in WRITES])
def test_a_viewer_is_refused_every_write_and_nothing_is_written(
        client, db, seeded, services, monkeypatch, name, method, path, body):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    before = state(db)
    response = send(client, method, path, body, "vera")
    assert response.status_code == 403, response.text
    assert code_of(response) == "role_member_required"
    detail = response.json()["detail"]
    assert (detail["role"], detail["required"], detail["tenant_id"]) == ("viewer", "member", "eng")
    assert detail["admins"] == [WANDA]
    assert state(db) == before
    # No service behind the route was asked anything, so no forge call, no
    # token use and no broker call -- the pasted credential went nowhere.
    assert services["access"].calls == [] and services["pool"].calls == []


@pytest.mark.parametrize("name,method,path,body", WRITES, ids=[w[0] for w in WRITES])
def test_a_member_is_not_refused_by_the_role(client, seeded, monkeypatch, name, method, path, body):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    response = send(client, method, path, body, "alice")
    # Whatever else the route says of this body, it is not the role.
    assert code_of(response) != "role_member_required", response.text
    assert response.status_code != 403, response.text


@pytest.mark.parametrize("name,method,path,body", WRITES, ids=[w[0] for w in WRITES])
def test_with_the_switch_off_a_viewer_goes_through_and_the_refusal_is_logged(
        client, seeded, caplog, name, method, path, body):
    with caplog.at_level(logging.WARNING):
        response = send(client, method, path, body, "vera")
    assert code_of(response) != "role_member_required", response.text
    assert "refusal report-only code=role_member_required" in caplog.text


def test_with_the_switch_off_a_viewers_writes_land(client, db, seeded, services):
    # The control for "nothing is written": the same requests change things.
    paused = client.post(f"/v1/schedules/{SCHEDULE}:pause", headers=h("vera"))
    assert paused.status_code == 200, paused.text
    assert db.docs[f"schedules/{SCHEDULE}"]["state"] == "paused"
    removed = client.delete(f"/v1/accounts/{ACCOUNT}", headers=h("vera"))
    assert removed.status_code == 200, removed.text
    assert services["pool"].calls == ["list_accounts", "remove"]


def test_a_member_pauses_and_deletes_a_schedule_with_the_switch_on(client, db, seeded, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    paused = client.post(f"/v1/schedules/{SCHEDULE}:pause", headers=h("alice"))
    assert paused.status_code == 200, paused.text
    deleted = client.delete(f"/v1/schedules/{SCHEDULE}?confirm={SCHEDULE}", headers=h("alice"))
    assert deleted.status_code == 200, deleted.text
    assert f"schedules/{SCHEDULE}" not in db.docs


def test_a_viewer_still_reads(client, seeded, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    for path in ("/v1/schedules", f"/v1/schedules/{SCHEDULE}", "/v1/repositories",
                 f"/v1/repositories/{REPO}"):
        response = client.get(path, headers=h("vera"))
        assert response.status_code == 200, (path, response.text)
    preview = client.post("/v1/schedules:preview", headers=h("vera"),
                          json={"cron": "0 9 * * *", "timezone": "UTC"})
    assert code_of(preview) != "role_member_required", preview.text


def test_a_role_that_cannot_be_read_is_503_with_the_switch_on_and_open_with_it_off(
        client, db, seeded, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("firestore is down")

    monkeypatch.setattr(db, "collection", lambda path, _real=db.collection: (
        type("Broken", (), {"where": broken})() if path == roles.GRANTS_COLLECTION else _real(path)))
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    refused = client.post(f"/v1/schedules/{SCHEDULE}:pause", headers=h("alice"))
    assert refused.status_code == 503 and code_of(refused) == "role_unavailable"
    assert db.docs[f"schedules/{SCHEDULE}"]["state"] == "enabled"
    monkeypatch.setenv(MEMBER_SWITCH, "off")
    with caplog.at_level(logging.WARNING):
        let_through = client.post(f"/v1/schedules/{SCHEDULE}:pause", headers=h("alice"))
    assert let_through.status_code == 200, let_through.text
    assert "role unavailable report-only code=role_member_required" in caplog.text


# ---------------------------------------------------------------------------
# A repository's settings (Q13)
# ---------------------------------------------------------------------------


def _patch(client, body: dict, user: str):
    return client.patch(f"/v1/repositories/{REPO}", json=body, headers=h(user))


def test_a_member_setting_context_is_403_role_admin_required(client, db, seeded, monkeypatch):
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    response = _patch(client, {"context": "off"}, "alice")
    assert response.status_code == 403, response.text
    assert code_of(response) == "role_admin_required"
    assert response.json()["detail"]["role"] == "member"
    assert "context" not in db.docs[f"repositories/{REPO}"]


def test_with_the_admin_switch_off_a_member_is_still_held_to_todays_rule(client, db, seeded, monkeypatch):
    # Off, the role refusal is report-only -- and the established owner rule
    # (on by default) still refuses: turning roles on never loosens it.
    monkeypatch.delenv(OWNER_SWITCH, raising=False)
    response = _patch(client, {"context": "off"}, "alice")
    assert response.status_code == 403, response.text
    assert code_of(response) == codeintel.OwnerRequired.code
    assert "context" not in db.docs[f"repositories/{REPO}"]


@pytest.mark.parametrize("switch", ["on", "off"])
def test_a_workspace_admin_sets_context(client, db, seeded, monkeypatch, switch):
    monkeypatch.setenv(ADMIN_SWITCH, switch)
    monkeypatch.delenv(OWNER_SWITCH, raising=False)
    response = _patch(client, {"context": "off"}, "wanda")
    assert response.status_code == 200, response.text
    assert db.docs[f"repositories/{REPO}"]["context"] == "off"
    (entry,) = [d for p, d in db.docs.items()
                if p.startswith("admin_audit/") and d.get("target_repo_id") == REPO]
    assert entry["by"] == WANDA


def test_a_non_member_platform_admin_sets_context_audited(client, db, seeded, monkeypatch):
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    # ops selects eng through the reach: no membership, a named tenant.
    response = client.patch(f"/v1/repositories/{REPO}", json={"context": "on"},
                            headers=h("ops", "eng"))
    assert response.status_code == 200, response.text
    assert db.docs[f"repositories/{REPO}"]["context"] == "on"
    acted = [d for d in db.dump("admin_audit/").values() if d["action"] == "platform_admin_acted"]
    assert [a["detail"]["route"] for a in acted] == ["/v1/repositories/{repo_id}"]


@pytest.mark.parametrize("field,value", [("platform", True), ("merge_policy", "on_merge_verdict")])
def test_platform_fields_stay_refused_to_a_workspace_admin(client, db, seeded, monkeypatch, field, value):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    before = state(db)
    response = _patch(client, {field: value}, "wanda")
    assert response.status_code == 403, response.text
    assert code_of(response) not in ("role_member_required", "role_admin_required")
    assert state(db) == before
    created = client.post("/v1/repositories", headers=h("wanda"),
                          json={"repository": "saga-xyz/gadgets", field: value})
    assert created.status_code == 403, created.text
    assert state(db) == before


# ---------------------------------------------------------------------------
# A firing, as its owner (§4.2.3, §4.8)
# ---------------------------------------------------------------------------


def schedule_audit(db) -> list[dict]:
    return [d for p, d in db.docs.items() if p.startswith("schedule_audit/")]


def platform_rows(db) -> list[dict]:
    return [d for d in db.dump("admin_audit/").values() if d["action"].startswith("platform_admin_")]


def test_a_firing_whose_owner_was_narrowed_is_skipped_and_paused(db, ctx, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    seed_schedule(db, owner=VERA)
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    (firing,) = firings(db).values()
    assert firing["state"] == "skipped"
    assert firing["skip"]["code"] == "OWNER_ROLE_NARROWED"
    assert firing["skip"]["detail"] == {"owner": VERA, "role": "viewer"}
    schedule = db.docs[f"schedules/{SCHEDULE}"]
    assert schedule["state"] == "auto_paused"
    assert schedule["pause"]["code"] == "OWNER_ROLE_NARROWED"
    assert "a member can take ownership to resume" in schedule["pause"]["reason"]
    (entry,) = schedule_audit(db)
    assert entry["action"] == schedulefire.AUTO_PAUSED
    assert (entry["schedule_id"], entry["tenant_id"], entry["by"]) == (SCHEDULE, "eng", "schedule-tick")
    assert entry["detail"]["code"] == "OWNER_ROLE_NARROWED"
    assert entry["detail"]["owner"] == VERA and entry["detail"]["role"] == "viewer"
    assert executor.calls == 0 and tasks(db) == []


def test_with_the_switch_off_a_narrowed_owners_firing_fires_and_logs(db, ctx, caplog):
    seed_schedule(db, owner=VERA)
    executor = FakeExecutor()
    with caplog.at_level(logging.WARNING):
        ticker(ctx, executor).tick()
    assert executor.calls == 1
    (task,) = tasks(db)
    assert task["submitted_by"] == VERA
    assert db.docs[f"schedules/{SCHEDULE}"]["state"] == "enabled"
    assert schedule_audit(db) == []
    assert "refusal report-only code=role_member_required" in caplog.text
    assert VERA in caplog.text


def test_a_member_owners_firing_fires_with_the_switch_on(db, ctx, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    seed_schedule(db, owner=ALICE)
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    assert executor.calls == 1
    assert db.docs[f"schedules/{SCHEDULE}"]["state"] == "enabled"
    assert platform_rows(db) == []


def test_the_firing_reads_the_role_fresh(db, ctx, monkeypatch):
    # Primed with `member`, then narrowed: the next firing must see it.
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    assert ctx.roles.person_role("eng", ALICE).role == roles.MEMBER
    grant(ctx.roles, ALICE, roles.VIEWER)
    ctx.roles._cache[("eng", ALICE)] = (float("inf"), None)
    seed_schedule(db, owner=ALICE)
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    assert executor.calls == 0
    assert db.docs[f"schedules/{SCHEDULE}"]["pause"]["code"] == "OWNER_ROLE_NARROWED"


def test_a_non_member_platform_admins_firing_fires_audited(db, ctx, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    seed_schedule(db, owner=OPS)
    seen: list[list[dict]] = []

    class Watching(FakeExecutor):
        def create(self, firing):
            seen.append(platform_rows(db))
            return super().create(firing)

    executor = Watching()
    ticker(ctx, executor).tick()
    assert executor.calls == 1
    (task,) = tasks(db)
    assert (task["tenant_id"], task["submitted_by"]) == ("eng", OPS)
    (row,) = platform_rows(db)
    assert row["action"] == "platform_admin_acted"
    assert (row["by"], row["target_tenant_id"]) == (OPS, "eng")
    assert row["detail"]["route"] == "schedule_firing"
    assert row["detail"]["path_params"]["schedule_id"] == SCHEDULE
    assert row["detail"]["via"] == "platform_admin"
    # Written BEFORE the work was created.
    assert [r["action"] for r in seen[0]] == ["platform_admin_acted"]
    owner = executor.seen[0].owner
    assert owner.tenant_via == "platform_admin" and owner.is_admin is False


def test_a_platform_admin_firing_whose_audit_cannot_be_written_creates_nothing(db, ctx, monkeypatch):
    seed_schedule(db, owner=OPS)
    real = db.collection

    class Refusing:
        def document(self, *args):
            raise RuntimeError("firestore is down")

    monkeypatch.setattr(db, "collection",
                        lambda path: Refusing() if path == "admin_audit" else real(path))
    executor = FakeExecutor()
    report = ticker(ctx, executor).tick()
    assert executor.calls == 0 and tasks(db) == []
    assert report.errors == 1
    (firing,) = firings(db).values()
    assert firing["state"] == "claimed"


def test_a_narrowed_member_platform_admins_firing_fires_audited(db, ctx, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    seed_schedule(db, owner=ROOT)
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    assert executor.calls == 1
    (row,) = platform_rows(db)
    assert row["action"] == "platform_admin_acted" and row["by"] == ROOT
    assert row["detail"]["route"] == "schedule_firing"
    assert row["detail"]["reach"] == "role_below"


def test_a_platform_admin_who_loses_the_role_pauses_owner_not_member(db, ctx, directory, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    seed_schedule(db, owner=OPS)
    directory.mapping[OPS] = ()
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    assert executor.calls == 0
    (firing,) = firings(db).values()
    assert firing["skip"]["code"] == "OWNER_NOT_MEMBER"
    assert db.docs[f"schedules/{SCHEDULE}"]["pause"]["code"] == "OWNER_NOT_MEMBER"
    assert platform_rows(db) == []


def test_reach_off_a_non_member_platform_admin_owner_is_not_a_member(db, ctx, monkeypatch):
    # The reach ships off (§4.7): off, `may_act_in` is membership alone.
    monkeypatch.setattr(ctx.authenticator, "_reach_enabled", lambda: False)
    seed_schedule(db, owner=OPS)
    executor = FakeExecutor()
    ticker(ctx, executor).tick()
    assert executor.calls == 0
    assert db.docs[f"schedules/{SCHEDULE}"]["pause"]["code"] == "OWNER_NOT_MEMBER"


def test_member_auth_without_reach_is_unchanged(ctx):
    # `ghapprove` and anything else calling `member_auth` without opting in
    # still asks membership alone: a non-member platform admin is refused.
    with pytest.raises(schedulefire.ScheduleOwnerNotMember):
        schedulefire.member_auth(ctx, tenant_id="eng", email=OPS, subject="x",
                                 not_member=schedulefire.ScheduleOwnerNotMember)
    auth = schedulefire.schedule_owner_auth(ctx, {"schedule_id": SCHEDULE, "tenant_id": "eng",
                                                  "owner": OPS})
    assert auth.tenant_via == "platform_admin" and auth.is_admin is False


def test_run_now_by_a_member_of_a_narrowed_owners_schedule_pauses_it(client, db, seeded, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    db.docs[f"schedules/{SCHEDULE}"]["owner"] = VERA
    response = client.post(f"/v1/schedules/{SCHEDULE}:run", headers=h("alice"))
    assert response.status_code == 200, response.text
    assert response.json()["firing"]["skip"]["code"] == "OWNER_ROLE_NARROWED"
    assert db.docs[f"schedules/{SCHEDULE}"]["pause"]["code"] == "OWNER_ROLE_NARROWED"
    assert tasks(db) == []


def test_the_pause_copy_names_the_new_code():
    assert schedulefire.PAUSE_COPY[schedulefire.OWNER_ROLE_NARROWED].startswith(
        "Paused: its owner's role in this workspace is viewer")
