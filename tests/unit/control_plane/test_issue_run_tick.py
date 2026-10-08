"""Issue runs advance on a Cloud Scheduler tick, not only on reads (#454).

    POST /v1/admin/runs/advance?tenant_id=<t>

Owner decision on #454: "Advancing runs: swarm-api, on a Cloud Scheduler
tick." Before this route a run moved only when somebody read it, so a run
with `plan_approval: auto` that nobody was watching never left PLANNING and
nothing was written back to its issue.

What these tests hold:

  1. An auto run goes PLANNING -> PLANNED -> RUNNING with NO reader, and the
     workflow it submits is the RUN'S tenant's, submitted as the run's
     creator, approved by AUTO_APPROVER.
  2. A `required` run stops at PLANNED and holds nothing (invariant 1): the
     tick creates no task, no workflow and no lease for it.
  3. A terminal run is not touched; another tenant's run is never visited;
     the tick cannot submit into any tenant but the run document's own.
  4. One failing run does not stop the others, and every advanced run is
     written back to its issue (`sync_issue`).
  5. The route is the rollup sweeper's, and the sweeper still reaches
     nothing else.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, forgewrite, issueruns
from swarm_api.auth import Authenticator, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import NotFound, UpstreamUnavailable
from swarm_api.groups import GroupLookupError, StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import runs as runs_routes
from swarm_api.waker import NullWaker
from swarm_common.models import Tenant

from . import forge_fakes
from .conftest import api_settings
from .test_issue_runs import PLAN, _create, _docs, _finish_planner, _integrator_opened, _run

SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}


@pytest.fixture
def github():
    return forge_fakes.GitHub(
        issues=[forge_fakes.issue(42, "Widgets cannot be sorted"), forge_fakes.issue(7)],
        pulls=[forge_fakes.pull(9, "Sort helpers")],
        files={9: ["src/widgets/sort.py"]},
    )


@pytest.fixture
def api_context(db, tokens, group_map, objects, github):
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
    )


def _tick(client: TestClient, tenant_id: str = "eng", **params: Any):
    query = "&".join([f"tenant_id={tenant_id}"] + [f"{k}={v}" for k, v in params.items()])
    return client.post(f"/v1/admin/runs/advance?{query}", headers=SWEEPER_HEADERS)


def _stored(db, run_id: str) -> dict:
    return db.docs[f"issue_runs/{run_id}"]


def _workflow_tasks(db, workflow_id: str) -> list[dict]:
    return [d for d in _docs(db, "tasks").values() if d.get("workflow_id") == workflow_id]


# --------------------------------------------------------------------------
# moving runs with no reader
# --------------------------------------------------------------------------

def test_an_auto_run_moves_planning_to_planned_to_running_with_no_reader(client, db, objects):
    run = _create(client, plan_approval="auto").json()["run"]
    assert _tick(client).json()["report"]["moved"] == 0  # the planner is still running
    _finish_planner(db, objects, run, PLAN)

    response = _tick(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "eng"
    assert body["report"] == {"visited": 1, "moved": 1, "failed": 0, "truncated": False}
    stored = _stored(db, run["id"])
    assert stored["state"] == "RUNNING"
    assert [h["to"] for h in stored["history"]] == ["PLANNING", "PLANNED", "APPROVED", "RUNNING"]
    assert stored["approved_by"] == issueruns.AUTO_APPROVER
    workflow = db.docs[f"workflows/{stored['workflow_id']}"]
    # The run's tenant, submitted as the run's creator: the sweeper's own
    # identity is recorded nowhere on the work.
    assert workflow["tenant_id"] == "eng"
    tasks = _workflow_tasks(db, stored["workflow_id"])
    assert tasks and {t["tenant_id"] for t in tasks} == {"eng"}
    assert {t["submitted_by"] for t in tasks} == {"alice@saga.xyz"}
    assert SWEEPER not in str(workflow) and SWEEPER not in str(tasks)


def test_a_required_run_stops_at_planned_and_holds_nothing(client, db, objects):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, PLAN)
    tasks_before = set(_docs(db, "tasks"))
    leases_before = set(_docs(db, "leases"))

    first = _tick(client).json()["report"]
    second = _tick(client).json()["report"]

    assert first["moved"] == 1
    # Nothing but a person moves it now, so later ticks do not even read it.
    assert second == {"visited": 0, "moved": 0, "failed": 0, "truncated": False}
    assert _stored(db, run["id"])["state"] == "PLANNED"
    # Invariant 1: a PLANNED run waiting for a person creates no demand.
    assert not _docs(db, "workflows")
    assert set(_docs(db, "tasks")) == tasks_before
    assert set(_docs(db, "leases")) == leases_before


def test_a_running_run_follows_its_workflow_to_checking_on_the_tick(client, db, objects):
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, PLAN)
    _tick(client)
    workflow_id = _stored(db, run["id"])["workflow_id"]
    for doc in _workflow_tasks(db, workflow_id):
        doc["state"] = "SUCCEEDED"
    _integrator_opened(db, workflow_id)

    assert _tick(client).json()["report"]["moved"] == 1
    # Its pull request's CI is the tick's to read from here (test_issue_run_ci.py).
    assert _stored(db, run["id"])["state"] == "CHECKING"
    assert "CHECKING" in [
        s.value for s in issueruns.RunState
        if s not in issueruns.TERMINAL_RUN_STATES and s != issueruns.RunState.PLANNED
    ], "a CHECKING run is one the tick visits"


def test_a_terminal_run_is_not_touched(client, db, objects, monkeypatch):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, PLAN, state="FAILED")
    _tick(client)
    assert _stored(db, run["id"])["state"] == "FAILED"
    before = dict(_stored(db, run["id"]))
    synced: list[str] = []
    monkeypatch.setattr(runs_routes, "sync_issue", lambda ctx, r: synced.append(r.id) or r)

    report = _tick(client).json()["report"]

    assert report == {"visited": 0, "moved": 0, "failed": 0, "truncated": False}
    assert _stored(db, run["id"]) == before
    assert synced == []


def test_another_tenants_run_is_never_visited(client, db, objects, monkeypatch):
    ours = _create(client, "alice", plan_approval="auto").json()["run"]
    theirs = _create(client, "bob", plan_approval="auto").json()["run"]
    assert theirs["tenant_id"] == "research"
    _finish_planner(db, objects, ours, PLAN)
    _finish_planner(db, objects, theirs, PLAN)
    visited: list[str] = []
    real = runs_routes._advance_state

    def spy(ctx, tenant_id, run):
        visited.append(run.id)
        return real(ctx, tenant_id, run)

    monkeypatch.setattr(runs_routes, "_advance_state", spy)

    report = _tick(client, "eng").json()["report"]

    assert visited == [ours["id"]]
    assert report["visited"] == 1
    assert _stored(db, theirs["id"])["state"] == "PLANNING"
    assert {w["tenant_id"] for w in _docs(db, "workflows").values()} == {"eng"}


def test_the_tick_cannot_submit_into_a_tenant_other_than_the_runs_own(
    client, db, objects, api_context
):
    """The submitter is built from the run document; a mismatch is a 404, not a submission."""
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, PLAN)
    ctx = api_context
    stored = issueruns.IssueRuns(db).get("eng", run["id"])

    owner = runs_routes.run_owner_auth(ctx, stored)
    assert owner.tenant_id == "eng"
    assert owner.email == "alice@saga.xyz"
    assert owner.is_admin is False and owner.is_rollup_sweeper is False
    assert owner.member_scope == "" and owner.tenant_member == ""

    # Asked to advance the run under ANOTHER tenant, nothing moves and nothing
    # is submitted: the run answers as missing, exactly as a read does.
    with pytest.raises(NotFound):
        runs_routes.advance_run(ctx, "research", stored)
    assert _stored(db, run["id"])["state"] == "PLANNING"
    assert not _docs(db, "workflows")

    # And a run document whose tenant has no tenant document is refused
    # before anything is claimed.
    stored.tenant_id = "nobody"
    with pytest.raises(NotFound):
        runs_routes.run_owner_auth(ctx, stored)


def test_one_failing_run_does_not_stop_the_others(client, db, objects, monkeypatch):
    first = _create(client, plan_approval="auto").json()["run"]
    second = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, first, PLAN)
    _finish_planner(db, objects, second, PLAN)
    real = runs_routes._advance_state

    def broken(ctx, tenant_id, run):
        if run.id == first["id"]:
            raise RuntimeError("the store blinked")
        return real(ctx, tenant_id, run)

    monkeypatch.setattr(runs_routes, "_advance_state", broken)

    response = _tick(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"] == {"visited": 2, "moved": 1, "failed": 1, "truncated": False}
    assert body["failures"] == [{"run_id": first["id"], "error": "RuntimeError"}]
    assert "blinked" not in response.text
    assert _stored(db, first["id"])["state"] == "PLANNING"
    assert _stored(db, second["id"])["state"] == "RUNNING"


def test_the_tick_writes_every_visited_run_back_to_its_issue(client, db, objects, monkeypatch):
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, PLAN)
    synced: list[tuple[str, str]] = []
    real = runs_routes.sync_issue

    def spy(ctx, r):
        synced.append((r.id, r.state.value))
        return real(ctx, r)

    monkeypatch.setattr(runs_routes, "sync_issue", spy)

    _tick(client)

    assert synced == [(run["id"], "RUNNING")]


def test_the_tick_is_bounded_and_reports_truncation(client, db, objects):
    made = [_create(client).json()["run"] for _ in range(3)]
    for run in made:
        _finish_planner(db, objects, run, PLAN)

    report = _tick(client, limit=2).json()["report"]

    assert report["visited"] == 2 and report["truncated"] is True
    # Oldest first, and the two left waiting for a person at PLANNED drop out
    # of the set, so the next tick reaches the third rather than starving it.
    states = [_stored(db, r["id"])["state"] for r in made]
    assert states == ["PLANNED", "PLANNED", "PLANNING"]
    assert _tick(client, limit=2).json()["report"]["truncated"] is False
    assert _stored(db, made[2]["id"])["state"] == "PLANNED"


def test_a_reader_still_advances_a_run(client, db, objects):
    """The tick is in addition to reads, not instead of them."""
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, PLAN)
    assert _run(client, run["id"]).json()["run"]["state"] == "RUNNING"


# --------------------------------------------------------------------------
# who may call it
# --------------------------------------------------------------------------

def test_a_tenant_member_may_not_tick(client):
    response = client.post(
        "/v1/admin/runs/advance?tenant_id=eng", headers={"Authorization": "Bearer token-alice"}
    )
    assert response.status_code == 403


def test_an_admin_may_tick(client):
    response = client.post(
        "/v1/admin/runs/advance?tenant_id=eng", headers={"Authorization": "Bearer token-root"}
    )
    assert response.status_code == 200, response.text


def test_the_tick_needs_a_tenant(client):
    assert client.post("/v1/admin/runs/advance", headers=SWEEPER_HEADERS).status_code == 422


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


# --------------------------------------------------------------------------
# the creator is asked about again before anything is submitted as them
# --------------------------------------------------------------------------

def test_an_auto_run_whose_creator_left_the_tenant_fails_and_submits_nothing(
    client, db, objects, api_context, monkeypatch
):
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, PLAN)
    monkeypatch.setattr(api_context.authenticator, "is_tenant_member", lambda email, tenant: False)

    report = _tick(client).json()["report"]

    stored = _stored(db, run["id"])
    assert report["failed"] == 0 and report["moved"] == 1
    assert stored["state"] == "FAILED"
    assert "no longer a member" in stored["error"]
    assert [h["to"] for h in stored["history"]] == ["PLANNING", "PLANNED", "FAILED"]
    assert stored["approved_by"] is None
    assert not _docs(db, "workflows")


def test_membership_is_asked_of_the_directory_for_the_tenants_one_group(group_map):
    now = datetime.now(timezone.utc)
    authenticator = Authenticator(api_settings(), StaticTokenVerifier({}), StaticGroups(group_map))
    eng = Tenant(tenant_id="eng", kind="group", principal="eng@saga.xyz", created_at=now)
    personal = Tenant(tenant_id="u-carol", kind="user", principal="carol@saga.xyz", created_at=now)

    assert authenticator.is_tenant_member("alice@saga.xyz", eng)
    assert not authenticator.is_tenant_member("bob@saga.xyz", eng)
    assert not authenticator.is_tenant_member("", eng)
    assert authenticator.is_tenant_member("Carol@saga.xyz", personal)
    assert not authenticator.is_tenant_member("alice@saga.xyz", personal)

    class Unreachable:
        def groups_for(self, member_email, candidate_groups):
            raise GroupLookupError("Cloud Identity did not answer")

    down = Authenticator(api_settings(), StaticTokenVerifier({}), Unreachable())
    with pytest.raises(UpstreamUnavailable):
        down.is_tenant_member("alice@saga.xyz", eng)



@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
