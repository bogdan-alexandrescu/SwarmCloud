"""Workspace roles on submitting, cancelling, answering, live control,
browser logins, an issue run's plan and an issue run's auto steps
(docs/design/workspace-roles.md §4.1, §4.2.3, §4.2.4, §4.8, Q8; lane
ROLES-GATES-WORK, part of #1518).

Who is who, in eng (`eng@saga.xyz`):

* `alice`, `bob` -- members with no grant: `member`, the migration default;
* `vera` -- a member a grant narrowed to `viewer`;
* `ops` -- a platform admin in no tenant group: reaches eng only through
  `platform_admin_reach`.

What is held:

* with `role_member_required` on, a `viewer` is 403 on `POST /v1/tasks`,
  `/v1/tasks/batch`, `/v1/workflows` and `/v1/runs`, and nothing is written
  (the task count before and after, and every other document);
* a `viewer` cancels nothing -- not another's task or workflow, and not their
  own (Q8); a `member` cancels another's;
* every other `member` route in this lane (answers, human-ready, "Save as
  login", revoking a login, taking, renewing and steering live control,
  opening control to members, and an issue run's plan decisions) refuses a
  `viewer` the same way, while the viewer's own routes (a viewer ticket,
  handing control back) do not;
* `run_owner_auth` fails a run whose creator a grant narrowed to `viewer`,
  and advances one whose creator is a non-member platform admin, with a
  `platform_admin_acted` row written before the workflow is submitted;
* with the switch off, all of the above go through and the log names the
  refusal that would have been made.

No cloud and no emulator: the in-memory Firestore and the real app.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, forgewrite, refusals, roles
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.issueruns import IssueRuns
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import runs as runs_routes
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, api_settings, seed_task, seed_tenant
from .test_issue_runs import PLAN, REF, _finish_planner
from .test_roles_gates_automate import LiveGroups
from .test_roles_store import grant

MEMBER_SWITCH = refusals.env_name("role_member_required")

ALICE, BOB, VERA, OPS = "alice@saga.xyz", "bob@saga.xyz", "vera@saga.xyz", "ops@saga.xyz"
SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
GROUPS = {
    ALICE: (ENG_GROUP,),
    BOB: (ENG_GROUP,),
    VERA: (ENG_GROUP,),
    OPS: (ADMIN_GROUP,),
}
VERAS_TASK = "tsk_vera_00000001"
ALICES_TASK = "tsk_alice_0000001"


@pytest.fixture
def directory() -> LiveGroups:
    return LiveGroups(GROUPS)


@pytest.fixture
def github():
    return forge_fakes.GitHub(
        issues=[forge_fakes.issue(42, "Widgets cannot be sorted")],
        pulls=[], files={},
    )


@pytest.fixture
def ctx(db, directory, objects, github, monkeypatch):
    monkeypatch.delenv(MEMBER_SWITCH, raising=False)
    seed_tenant(db, "eng")
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "saga.xyz"}
              for e in GROUPS}
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(platform_admin_reach=True, settings_key=secrets.token_hex(32),
                              tenant_groups=(ENG_GROUP, RESEARCH_GROUP),
                              rollup_sweeper_users=(SWEEPER,)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=directory,
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
    )
    grant(context.roles, VERA, roles.VIEWER)
    return context


@pytest.fixture
def client(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def seeded(db):
    for task_id, who in ((VERAS_TASK, VERA), (ALICES_TASK, ALICE)):
        doc = seed_task(db, task_id=task_id, tenant_id="eng", state="QUEUED")
        doc["submitted_by"] = who
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


def task_count(db) -> int:
    return sum(1 for k in db.docs if k.startswith("tasks/") and k.count("/") == 1)


def code_of(response) -> str | None:
    try:
        return response.json().get("code")
    except ValueError:
        return None


def platform_rows(db, action: str = roles.AUDIT_PLATFORM_ADMIN_ACTED) -> list[dict]:
    return [d for k, d in db.docs.items()
            if k.startswith(f"{AUDIT_COLLECTION}/") and d.get("action") == action]


#: The four submissions, each with a body its route accepts, so a refusal is
#: the role's and never the body's.
SUBMITS: list[tuple[str, str, Any]] = [
    ("task", "/v1/tasks", {"runner_profile": "mock"}),
    ("batch", "/v1/tasks/batch", {"tasks": [{"runner_profile": "mock"},
                                            {"runner_profile": "mock"}]}),
    ("workflow", "/v1/workflows", {"steps": [{"step_id": "a", "runner_profile": "mock"},
                                             {"step_id": "b", "runner_profile": "mock",
                                              "depends_on": ["a"]}]}),
    ("run", "/v1/runs", {"issue": REF}),
]

#: Every other `member` write this lane gates (§4.1's member row). The task
#: is the viewer's OWN, so the narrower rules (the submitter's, the owner's)
#: would let them through: the refusal is the role's.
MEMBER_WRITES: list[tuple[str, str, str, Any]] = [
    ("cancel own task", "POST", f"/v1/tasks/{VERAS_TASK}/cancel", None),
    ("cancel another's task", "POST", f"/v1/tasks/{ALICES_TASK}/cancel", None),
    ("answer", "POST", f"/v1/tasks/{VERAS_TASK}/answers", {"question_index": 0, "choice": "yes"}),
    # ANSWER-SYNC (#1644): an answer that says it came from Claude Code is
    # refused a viewer exactly as one from the console is.
    ("answer from claude code", "POST", f"/v1/tasks/{VERAS_TASK}/answers",
     {"question_index": 0, "choice": "yes", "via": "claude-code", "session": "ce05454f-claude"}),
    ("human-ready", "POST", f"/v1/tasks/{VERAS_TASK}/human-ready", None),
    ("save login", "POST", f"/v1/tasks/{VERAS_TASK}/browser-login", {"name": "site"}),
    ("revoke login", "DELETE", "/v1/browser-logins/site", None),
    ("take control", "POST", f"/v1/tasks/{VERAS_TASK}/live/control", None),
    ("renew control", "POST", f"/v1/tasks/{VERAS_TASK}/live/control/renew", None),
    ("device", "POST", f"/v1/tasks/{VERAS_TASK}/live/device", {"preset": "desktop"}),
    ("open to members", "POST", f"/v1/tasks/{VERAS_TASK}/live/open-to-members", None),
    ("close to members", "DELETE", f"/v1/tasks/{VERAS_TASK}/live/open-to-members", None),
    ("plan approve", "POST", "/v1/runs/run_missing/plan:approve", {"plan_digest": "d"}),
    ("plan edit", "POST", "/v1/runs/run_missing/plan:edit", {"plan_digest": "d", "plan": PLAN}),
    ("plan reject", "POST", "/v1/runs/run_missing/plan:reject", {}),
]


def send(client, method: str, path: str, body: Any, user: str):
    kwargs: dict[str, Any] = {"headers": h(user)}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,path,body", SUBMITS, ids=[s[0] for s in SUBMITS])
def test_a_viewer_is_refused_every_submission_and_nothing_is_written(
        client, db, monkeypatch, name, path, body):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    before, tasks_before = state(db), task_count(db)
    response = client.post(path, headers=h("vera"), json=body)
    assert response.status_code == 403, response.text
    assert code_of(response) == "role_member_required"
    detail = response.json()["detail"]
    assert (detail["role"], detail["required"], detail["tenant_id"]) == ("viewer", "member", "eng")
    assert task_count(db) == tasks_before
    assert state(db) == before


@pytest.mark.parametrize("name,path,body", SUBMITS, ids=[s[0] for s in SUBMITS])
def test_a_member_submits_with_the_switch_on(client, db, monkeypatch, name, path, body):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    tasks_before = task_count(db)
    response = client.post(path, headers=h("alice"), json=body)
    assert response.status_code == 201, response.text
    assert task_count(db) > tasks_before


@pytest.mark.parametrize("name,path,body", SUBMITS, ids=[s[0] for s in SUBMITS])
def test_with_the_switch_off_a_viewer_submits_and_the_refusal_is_logged(
        client, db, caplog, name, path, body):
    tasks_before = task_count(db)
    with caplog.at_level(logging.WARNING):
        response = client.post(path, headers=h("vera"), json=body)
    assert response.status_code == 201, response.text
    assert task_count(db) > tasks_before
    assert "refusal report-only code=role_member_required" in caplog.text


def test_a_role_that_cannot_be_read_is_503_with_the_switch_on_and_open_with_it_off(
        client, db, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("firestore is down")

    monkeypatch.setattr(db, "collection", lambda path, _real=db.collection: (
        type("Broken", (), {"where": broken})() if path == roles.GRANTS_COLLECTION else _real(path)))
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    tasks_before = task_count(db)
    refused = client.post("/v1/tasks", headers=h("alice"), json={"runner_profile": "mock"})
    assert refused.status_code == 503 and code_of(refused) == "role_unavailable"
    assert task_count(db) == tasks_before
    monkeypatch.setenv(MEMBER_SWITCH, "off")
    with caplog.at_level(logging.WARNING):
        let_through = client.post("/v1/tasks", headers=h("alice"), json={"runner_profile": "mock"})
    assert let_through.status_code == 201, let_through.text
    assert "role unavailable report-only code=role_member_required" in caplog.text


# ---------------------------------------------------------------------------
# Cancelling (Q8) and every other member write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,method,path,body", MEMBER_WRITES, ids=[w[0] for w in MEMBER_WRITES])
def test_a_viewer_is_refused_every_member_write_and_nothing_is_written(
        client, db, seeded, monkeypatch, name, method, path, body):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    before = state(db)
    response = send(client, method, path, body, "vera")
    assert response.status_code == 403, response.text
    assert code_of(response) == "role_member_required"
    assert response.json()["detail"]["role"] == "viewer"
    assert state(db) == before


@pytest.mark.parametrize("name,method,path,body", MEMBER_WRITES, ids=[w[0] for w in MEMBER_WRITES])
def test_with_the_switch_off_a_viewers_member_write_is_logged_not_refused(
        client, seeded, caplog, name, method, path, body):
    with caplog.at_level(logging.WARNING):
        response = send(client, method, path, body, "vera")
    assert code_of(response) != "role_member_required", response.text
    assert "refusal report-only code=role_member_required" in caplog.text


def test_a_viewer_cancels_neither_their_own_task_nor_anothers(client, db, seeded, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    for task_id in (VERAS_TASK, ALICES_TASK):
        response = client.post(f"/v1/tasks/{task_id}/cancel", headers=h("vera"))
        assert response.status_code == 403 and code_of(response) == "role_member_required"
        assert db.docs[f"tasks/{task_id}"]["state"] == "QUEUED"
        assert not db.docs[f"tasks/{task_id}"]["cancel_requested"]


def test_a_member_cancels_anothers_task_with_the_switch_on(client, db, seeded, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    response = client.post(f"/v1/tasks/{VERAS_TASK}/cancel", headers=h("bob"))
    assert response.status_code == 200, response.text
    assert db.docs[f"tasks/{VERAS_TASK}"]["state"] == "CANCELLED"


def test_a_viewer_cannot_cancel_a_workflow_and_a_member_can(client, db, monkeypatch):
    created = client.post("/v1/workflows", headers=h("alice"), json=SUBMITS[2][2])
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["workflow_id"]
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    before = state(db)
    refused = client.post(f"/v1/workflows/{workflow_id}/cancel", headers=h("vera"))
    assert refused.status_code == 403 and code_of(refused) == "role_member_required"
    assert state(db) == before
    cancelled = client.post(f"/v1/workflows/{workflow_id}/cancel", headers=h("bob"))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["tasks_cancelled"]


def test_with_the_switch_off_a_viewer_cancels_anothers_task(client, db, seeded, caplog):
    with caplog.at_level(logging.WARNING):
        response = client.post(f"/v1/tasks/{ALICES_TASK}/cancel", headers=h("vera"))
    assert response.status_code == 200, response.text
    assert db.docs[f"tasks/{ALICES_TASK}"]["state"] == "CANCELLED"
    assert "refusal report-only code=role_member_required" in caplog.text


def test_a_viewers_own_routes_are_not_refused_by_the_role(client, seeded, monkeypatch):
    # A viewer ticket is §4.1's viewer write, and handing control back only
    # ever narrows what a person holds: neither is a member's.
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    for method, path in (("POST", f"/v1/tasks/{VERAS_TASK}/live/ticket"),
                         ("POST", f"/v1/tasks/{VERAS_TASK}/live/control/return"),
                         ("DELETE", f"/v1/tasks/{VERAS_TASK}/live/control"),
                         ("GET", f"/v1/tasks/{VERAS_TASK}"),
                         ("GET", f"/v1/tasks/{VERAS_TASK}/questions"),
                         ("GET", "/v1/browser-logins")):
        response = client.request(method, path, headers=h("vera"))
        assert code_of(response) != "role_member_required", (path, response.text)


def test_the_questions_read_tells_a_viewer_why_they_may_not_answer(client, seeded, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    read = client.get(f"/v1/tasks/{VERAS_TASK}/questions", headers=h("vera"))
    assert read.status_code == 200, read.text
    assert read.json()["may_answer"] is False
    assert "viewer" in read.json()["may_answer_because"]
    # The control: the same read with the switch off says what it said before.
    monkeypatch.setenv(MEMBER_SWITCH, "off")
    assert client.get(f"/v1/tasks/{VERAS_TASK}/questions",
                      headers=h("vera")).json()["may_answer"] is True


# ---------------------------------------------------------------------------
# An issue run's auto steps: `run_owner_auth` (§4.2.3, §4.8)
# ---------------------------------------------------------------------------


def _tick(client: TestClient):
    response = client.post("/v1/admin/runs/advance?tenant_id=eng",
                           headers={"Authorization": "Bearer token-sweeper"})
    assert response.status_code == 200, response.text
    return response.json()["report"]


def _auto_run(client, db, objects, user: str = "alice", tenant: str | None = None) -> dict:
    created = client.post("/v1/runs", headers=h(user, tenant),
                          json={"issue": REF, "plan_approval": "auto"})
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, PLAN)
    return run


def _workflows(db) -> list[str]:
    return [k for k in db.docs if k.startswith("workflows/") and k.count("/") == 1]


def test_an_auto_run_whose_creator_was_narrowed_to_viewer_fails_and_submits_nothing(
        client, ctx, db, objects, monkeypatch):
    run = _auto_run(client, db, objects)
    grant(ctx.roles, ALICE, roles.VIEWER)
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    stored = IssueRuns(db).get("eng", run["id"])
    with pytest.raises(runs_routes.RunOwnerNotMember) as refused:
        runs_routes.run_owner_auth(ctx, stored)
    assert refused.value.code == "role_member_required"

    _tick(client)

    after = db.docs[f"issue_runs/{run['id']}"]
    assert after["state"] == "FAILED"
    assert "viewer" in after["error"]
    assert after["approved_by"] is None
    assert _workflows(db) == []


def test_with_the_switch_off_a_narrowed_creators_auto_run_advances_and_is_logged(
        client, ctx, db, objects, caplog):
    run = _auto_run(client, db, objects)
    grant(ctx.roles, ALICE, roles.VIEWER)
    with caplog.at_level(logging.WARNING):
        _tick(client)
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "RUNNING"
    assert len(_workflows(db)) == 1
    assert "refusal report-only code=role_member_required" in caplog.text


def test_an_auto_run_whose_creator_is_a_member_advances_with_the_switch_on(
        client, db, objects, monkeypatch):
    run = _auto_run(client, db, objects)
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    _tick(client)
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "RUNNING"
    assert platform_rows(db) == []


def test_an_auto_run_whose_creator_is_a_non_member_platform_admin_advances_audited(
        client, ctx, db, objects, monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    run = _auto_run(client, db, objects, user="ops", tenant="eng")
    assert run["tenant_id"] == "eng"
    stored = IssueRuns(db).get("eng", run["id"])
    owner = runs_routes.run_owner_auth(ctx, stored)
    assert (owner.email, owner.tenant_id, owner.is_admin) == (OPS, "eng", False)
    assert owner.tenant_via == roles.VIA_PLATFORM_ADMIN
    rows = [r for r in platform_rows(db) if r["detail"]["route"] == runs_routes.RUN_OWNER_ROUTE]
    assert len(rows) == 1
    row = rows[0]
    assert (row["by"], row["target_tenant_id"]) == (OPS, "eng")
    assert row["detail"]["path_params"] == {"run_id": run["id"]}

    _tick(client)

    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "RUNNING"
    assert len(_workflows(db)) == 1
    assert len([r for r in platform_rows(db)
                if r["detail"]["route"] == runs_routes.RUN_OWNER_ROUTE]) == 2


def test_a_platform_admins_auto_run_whose_audit_row_fails_submits_nothing(
        client, ctx, db, objects, monkeypatch):
    run = _auto_run(client, db, objects, user="ops", tenant="eng")

    def unwritable(row):
        raise RuntimeError("audit log is down")

    monkeypatch.setattr(ctx.roles, "_write_platform_row", unwritable)
    _tick(client)
    # Fail closed (§4.2.4): nothing is submitted unaudited, and the run waits
    # for the next tick rather than failing.
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"
    assert _workflows(db) == []


def test_an_auto_run_fails_once_its_creator_loses_the_platform_role(
        client, ctx, db, objects, directory):
    run = _auto_run(client, db, objects, user="ops", tenant="eng")
    directory.mapping[OPS] = ()
    _tick(client)
    after = db.docs[f"issue_runs/{run['id']}"]
    assert after["state"] == "FAILED"
    assert "no longer a member" in after["error"]
    assert _workflows(db) == []
