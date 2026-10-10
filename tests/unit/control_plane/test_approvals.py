"""Approvals: one record, one inbox, and the SD3 merge switch (docs/schedules.md §4.2, §4.5-§4.7, lane S5).

What these tests hold, against the real app (`create_app` mounts the router)
and the in-memory Firestore:

  * THE INBOX projects PLANNED runs (`run:<run_id>`, `kind: plan`, or
    `kind: hold` when held) beside its records, and materialises a `run`
    record for each firing awaiting approval. Another tenant's item is the
    404 a missing one is, on every route.
  * A DECISION IS ONE TRANSACTION: a stale digest is 409 (`plan_changed`,
    `approval_changed`, and `merge_changed` after a push), and a second
    decision of one item is 409 `already_decided` -- one transition.
  * EXPIRY (§4.7): a firing's run approval ends the firing `expired`; a
    scheduled plan nobody decided is REJECTED with reason "expired" and no
    task exists for it; a merge approval leaves the pull request unmerged
    and the run DONE.
  * THE MERGE POINT (§4.4-§4.5): a held run with `auto_merge` does not merge
    until a `merge` approval from the hold's approvers -- and the hold may
    first appear there, from the pull request's own changed files. A
    `merge: approve` run waits the same way, for any member.
  * THE SWITCH (SD3): the last gate editor cannot switch to `auto` in a
    group tenant and another member can; a one-person tenant types the
    schedule's name; a `platform: true` repository answers 403 to a member
    and succeeds for PLATFORM_OWNER; switching back is anyone's; each writes
    its audit entry; `PATCH` with `gate.merge: auto` is 422 `use_merge_switch`.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from swarm_api import approvals, forge, forgewrite, issueruns, refusals, scheduletypes
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import IssueRun, IssueRuns, RunState
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.validation import IssueRef
from swarm_api.waker import NullWaker
from swarm_common.identity import tenant_id_for_user

from . import forge_fakes
from .conftest import ENG_GROUP, RESEARCH_GROUP, api_settings, auth_header, seed_grant, seed_tenant
from .test_issue_run_ci import PR, SHA_A, SHA_B, Clock, _read, _workflow_ends
from .test_issue_run_keyword import _review_writes, _verdict
from .test_issue_runs import FULL_PLAN, PLAN, _approve, _create, _docs, _finish_planner, _run
from .test_schedule_tick import seed_schedule

OWNER = "root@saga.xyz"
SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWITCH = refusals.env_name("hold_approver_required")
REPO = "repo_00000000000000a1"
#: The first types' executor files, as S6 adds them (test_schedule_routes.py).
BUILT = frozenset({"issue-sweep", "issue-plan-only", "repo-index-refresh", "observer"})


def _bootstrap_plan() -> dict:
    plan = copy.deepcopy(FULL_PLAN)
    plan["steps"][0]["files"] = ["terraform/bootstrap/main.tf"]
    return plan


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def built_types(monkeypatch):
    monkeypatch.setattr(scheduletypes, "executor_present", lambda entry, root=None: entry.name in BUILT)
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)


@pytest.fixture
def group_map():
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "dave@saga.xyz": (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "carol@saga.xyz": (),
        OWNER: ("swarm-admins@saga.xyz", ENG_GROUP),
    }


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def writes():
    fake = forge_fakes.GitHubWrites()
    fake.require("unit")
    return fake


@pytest.fixture
def pr_files():
    return ["src/widgets/list.py"]


@pytest.fixture
def api_context(db, tokens, group_map, objects, writes, clock, pr_files):
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(platform_owner=OWNER, rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=forge_fakes.GitHub(issues=[forge_fakes.issue(42)],
                                                         files={PR: pr_files})),
        forge_writer=forgewrite.GitHubWriter(send=writes, locate=writes.locate, fetch=writes.fetch),
    )


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _grants(db):
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
    for email in ("dave@saga.xyz", OWNER):
        seed_grant(db, "eng", email, "saga-xyz/widgets")


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")


def _inbox(client, user="alice", **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get("/v1/approvals" + (f"?{query}" if query else ""), headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()["approvals"]


def _decide(client, item_id, verb, user="alice", **body):
    return client.post(f"/v1/approvals/{item_id}:{verb}", headers=auth_header(user), json=body)


def _tick(client, tenant_id="eng"):
    response = client.post(f"/v1/admin/runs/advance?tenant_id={tenant_id}",
                           headers={"Authorization": "Bearer token-sweeper"})
    assert response.status_code == 200, response.text


def _planned(client, db, objects, plan=PLAN, user="alice", **body) -> dict:
    created = _create(client, user, **body)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    return _run(client, run["id"], user).json()["run"]


def _audit(db, schedule_id: str) -> list[dict]:
    rows = [v for k, v in db.docs.items() if k.startswith("schedule_audit/") and v.get("schedule_id") == schedule_id]
    return sorted(rows, key=lambda r: r["at"])


def _firing(db, fid: str, *, schedule_id="sch_000000000001", digest="digest-1", tenant_id="eng") -> dict:
    doc = {
        "firing_id": fid, "schedule_id": schedule_id, "tenant_id": tenant_id, "type": "issue-sweep",
        "state": "awaiting_approval", "params_digest": digest, "approval_id": None,
        "history": [{"state": "awaiting_approval", "by": "schedule-tick"}],
    }
    db.docs[f"schedule_firings/{fid}"] = doc
    return doc


def _merge_workflows(db) -> list[dict]:
    return [w for w in _docs(db, "workflows").values()
            if any(s.get("step_id") == "merge" for s in w.get("steps") or [])]


# --------------------------------------------------------------------------
# the inbox and projected runs
# --------------------------------------------------------------------------

def test_the_router_is_mounted_and_an_empty_inbox_is_empty(client):
    assert _inbox(client) == []


def test_a_planned_run_is_projected_and_approved_from_the_inbox_once(client, db, objects):
    run = _planned(client, db, objects)
    (item,) = _inbox(client)
    assert item["approval_id"] == f"run:{run['id']}"
    assert item["kind"] == "plan" and item["digest"] == run["plan_digest"] and item["projected"] is True
    assert "saga-xyz/widgets#42" in item["summary"]

    stale = _decide(client, item["approval_id"], "approve", digest="0" * 64)
    assert stale.status_code == 409 and stale.json()["code"] == "plan_changed"
    assert not _docs(db, "workflows")

    approved = _decide(client, item["approval_id"], "approve", digest=run["plan_digest"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["state"] == "RUNNING"
    assert len(_docs(db, "workflows")) == 1

    again = _decide(client, item["approval_id"], "approve", digest=run["plan_digest"], user="dave")
    assert again.status_code == 409 and again.json()["code"] == "already_decided"
    assert len(_docs(db, "workflows")) == 1
    assert _inbox(client) == []


def test_a_held_run_is_a_hold_item_and_the_inbox_cannot_step_round_it(client, db, objects, switch_on):
    run = _planned(client, db, objects, _bootstrap_plan())
    (item,) = _inbox(client)
    assert item["kind"] == "hold" and item["approvers"] == "second_member"
    assert item["hold"]["code"] == "NEEDS_SECOND_MEMBER"
    refused = _decide(client, item["approval_id"], "approve", digest=run["plan_digest"])
    assert refused.status_code == 403 and refused.json()["code"] == "hold_approver_required"
    assert _decide(client, item["approval_id"], "approve", digest=run["plan_digest"],
                   user="dave").status_code == 200


def test_rejecting_from_the_inbox_needs_a_reason_and_rejects_the_run(client, db, objects):
    run = _planned(client, db, objects)
    assert _decide(client, f"run:{run['id']}", "reject").status_code == 422
    response = _decide(client, f"run:{run['id']}", "reject", reason="out of scope")
    assert response.status_code == 200, response.text
    assert response.json()["run"]["state"] == "REJECTED"
    assert response.json()["run"]["rejection_reason"] == "out of scope"


def test_another_tenants_items_are_404_everywhere(client, db, objects):
    run = _planned(client, db, objects)
    seed_schedule(db, gate={"run": "approve"})
    _firing(db, "sch_000000000001:1")
    (record,) = [i for i in _inbox(client) if i["kind"] == "run"]
    assert _inbox(client, user="bob") == []
    for item_id in (f"run:{run['id']}", record["approval_id"]):
        assert client.get(f"/v1/approvals/{item_id}", headers=auth_header("bob")).status_code == 404
        assert _decide(client, item_id, "approve", user="bob", digest="x").status_code == 404
        assert _decide(client, item_id, "reject", user="bob", reason="x").status_code == 404
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"


# --------------------------------------------------------------------------
# a firing's run approval
# --------------------------------------------------------------------------

def test_a_firing_awaiting_approval_gets_one_record_and_one_decision(client, db):
    seed_schedule(db, gate={"run": "approve"})
    firing = _firing(db, "sch_000000000001:1")

    (item,) = _inbox(client)
    assert item["kind"] == "run" and item["digest"] == "digest-1"
    assert item["subject"] == {"schedule_id": "sch_000000000001", "firing_id": "sch_000000000001:1"}
    assert firing["approval_id"] == item["approval_id"]
    assert len(_inbox(client)) == 1  # a second read makes no second record

    stale = _decide(client, item["approval_id"], "approve", digest="digest-0")
    assert stale.status_code == 409 and stale.json()["code"] == "approval_changed"

    approved = _decide(client, item["approval_id"], "approve", digest="digest-1")
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["state"] == "approved"
    assert firing["run_approval"]["by"] == "alice@saga.xyz"

    again = _decide(client, item["approval_id"], "approve", digest="digest-1", user="dave")
    assert again.status_code == 409 and again.json()["code"] == "already_decided"
    actions = [e["action"] for e in _audit(db, "sch_000000000001")]
    assert actions == ["approval_approved"]
    # Invariant 1: a decision moved a document; no task, workflow or lease exists.
    assert not _docs(db, "tasks") and not _docs(db, "workflows")


def test_a_rejected_firing_ends_rejected(client, db):
    seed_schedule(db, gate={"run": "approve"})
    firing = _firing(db, "sch_000000000001:2")
    (item,) = _inbox(client)
    response = _decide(client, item["approval_id"], "reject", reason="not this week")
    assert response.status_code == 200, response.text
    assert firing["state"] == "rejected" and firing["outcome"] == "rejected"
    assert _audit(db, "sch_000000000001")[-1]["detail"]["reason"] == "not this week"


def test_a_named_approvers_list_admits_only_its_members(client, db):
    seed_tenant(db, "eng")  # membership is asked of the directory, through the tenant's group
    seed_schedule(db, gate={"run": "approve", "approvers": ["dave@saga.xyz"]})
    _firing(db, "sch_000000000001:3")
    (item,) = _inbox(client)
    refused = _decide(client, item["approval_id"], "approve", digest="digest-1")
    assert refused.status_code == 403 and refused.json()["code"] == "approver_not_allowed"
    assert _decide(client, item["approval_id"], "approve", user="dave", digest="digest-1").status_code == 200


# --------------------------------------------------------------------------
# expiry (§4.7)
# --------------------------------------------------------------------------

def test_an_expired_run_approval_ends_its_firing_expired(client, db, clock):
    seed_schedule(db, gate={"run": "approve", "approval_ttl_hours": 1})
    firing = _firing(db, "sch_000000000001:4")
    (item,) = _inbox(client)
    clock.at += timedelta(hours=2)

    late = _decide(client, item["approval_id"], "approve", digest="digest-1")
    assert late.status_code == 409 and late.json()["code"] == "approval_expired"
    _tick(client)

    assert firing["state"] == "expired" and firing["outcome"] == "expired"
    assert db.docs[f"approvals/{item['approval_id']}"]["state"] == "expired"
    assert _inbox(client) == []
    assert [e["action"] for e in _audit(db, "sch_000000000001")] == ["approval_expired"]


def test_an_expired_scheduled_plan_is_rejected_expired_and_no_task_exists(client, db, objects, clock):
    seed_schedule(db)
    created = _create(client).json()["run"]
    db.docs[f"issue_runs/{created['id']}"]["metadata"] = {"schedule": {
        "schedule_id": "sch_000000000001", "firing_id": "sch_000000000001:5", "type": "issue-sweep"}}
    _finish_planner(db, objects, created, PLAN)
    assert _run(client, created["id"]).json()["run"]["state"] == "PLANNED"
    tasks_before = set(_docs(db, "tasks"))

    clock.at += timedelta(hours=71)
    _tick(client)
    assert db.docs[f"issue_runs/{created['id']}"]["state"] == "PLANNED"
    clock.at += timedelta(hours=2)
    _tick(client)

    stored = db.docs[f"issue_runs/{created['id']}"]
    assert stored["state"] == "REJECTED" and stored["rejection_reason"] == "expired"
    assert set(_docs(db, "tasks")) == tasks_before and not _docs(db, "workflows")


def test_a_run_made_by_hand_does_not_expire(client, db, objects, clock):
    run = _planned(client, db, objects)
    clock.at += timedelta(days=30)
    _tick(client)
    assert db.docs[f"issue_runs/{run['id']}"]["state"] == "PLANNED"


# --------------------------------------------------------------------------
# the merge point
# --------------------------------------------------------------------------

def _checking(client, db, objects, writes, plan, approver="alice") -> dict:
    created = _create(client, auto_merge=True)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    planned = _run(client, run["id"]).json()["run"]
    response = _approve(client, run["id"], planned["plan_digest"], user=approver)
    assert response.status_code == 200, response.text
    running = response.json()["run"]
    writes.open_pull(PR, SHA_A)
    integrator = _workflow_ends(db, running["workflow_id"])
    integrator["result_summary"]["git"]["pushed_head"] = SHA_A
    _review_writes(db, objects, running["workflow_id"], json.dumps(_verdict(True, True)))
    return running


def test_a_held_run_with_auto_merge_waits_for_a_merge_approval_from_the_holds_approvers(
    client, db, objects, writes, clock, switch_on,
):
    running = _checking(client, db, objects, writes, _bootstrap_plan(), approver="dave")
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])
    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING" and run["auto_merge"] is True
    assert _merge_workflows(db) == []
    (item,) = _inbox(client, kind="merge")
    assert item["digest"] == {"head_sha": SHA_A, "verdict": "MERGE"}
    assert item["approvers"] == "second_member"

    refused = _decide(client, item["approval_id"], "approve", digest=item["digest"])
    assert refused.status_code == 403 and refused.json()["code"] == "hold_approver_required"
    assert _merge_workflows(db) == []

    approved = _decide(client, item["approval_id"], "approve", user="dave", digest=item["digest"])
    assert approved.status_code == 200, approved.text
    (merge_wf,) = _merge_workflows(db)
    stored = db.docs[f"issue_runs/{running['id']}"]
    assert stored["merge_approved"]["head_sha"] == SHA_A and stored["merge"]["head_sha"] == SHA_A

    again = _decide(client, item["approval_id"], "approve", user="root", digest=item["digest"])
    assert again.status_code == 409 and again.json()["code"] == "already_decided"
    assert len(_merge_workflows(db)) == 1


def test_the_merge_point_reads_the_pull_requests_files_and_holds_on_them(
    client, db, objects, writes, clock, switch_on, pr_files,
):
    """The plan's files are advisory: a plan that named no protected path,
    whose pull request changed one, is held at the merge."""
    pr_files.append("terraform/bootstrap/deployer.tf")
    running = _checking(client, db, objects, writes, FULL_PLAN)
    assert db.docs[f"issue_runs/{running['id']}"]["approval_hold"] is None
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING"
    assert run["approval_hold"]["matched"] == ["terraform/bootstrap/deployer.tf"]
    assert _merge_workflows(db) == []
    assert len(_inbox(client, kind="merge")) == 1


def test_a_push_after_the_request_makes_the_merge_approval_stale(
    client, db, objects, writes, clock, switch_on,
):
    running = _checking(client, db, objects, writes, _bootstrap_plan(), approver="dave")
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge")
    writes.open_pull(PR, SHA_B)
    db.docs[f"issue_runs/{running['id']}"]["pull_request"]["head_sha"] = SHA_B

    stale = _decide(client, item["approval_id"], "approve", user="dave", digest=item["digest"])

    assert stale.status_code == 409 and stale.json()["code"] == "merge_changed"
    assert _merge_workflows(db) == []


def test_a_merge_gated_run_waits_for_any_member_then_merges(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes, FULL_PLAN)
    db.docs[f"issue_runs/{running['id']}"]["merge_approval"] = "required"
    writes.check(SHA_A, "unit", "success")

    _read(client, clock, running["id"])
    assert _merge_workflows(db) == []
    (item,) = _inbox(client, kind="merge")
    assert item["approvers"] == "members"

    approved = _decide(client, item["approval_id"], "approve", digest=f"{SHA_A}:MERGE")
    assert approved.status_code == 200, approved.text
    assert len(_merge_workflows(db)) == 1


def test_an_expired_merge_approval_leaves_the_run_done_and_unmerged(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes, FULL_PLAN)
    db.docs[f"issue_runs/{running['id']}"]["merge_approval"] = "required"
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    (item,) = _inbox(client, kind="merge")

    clock.at += timedelta(hours=73)
    _tick(client)

    stored = db.docs[f"issue_runs/{running['id']}"]
    assert stored["state"] == "DONE" and stored["green_sha"] == SHA_A
    assert db.docs[f"approvals/{item['approval_id']}"]["state"] == "expired"
    assert _merge_workflows(db) == []


def test_a_run_without_a_hold_or_a_merge_gate_merges_as_before(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes, FULL_PLAN)
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    assert len(_merge_workflows(db)) == 1
    assert _inbox(client) == []


# --------------------------------------------------------------------------
# the SD3 merge switch
# --------------------------------------------------------------------------

def _register(db, repo_id: str, tenant_id: str, *, platform: bool = False) -> None:
    db.docs[f"repositories/{repo_id}"] = {"repo_id": repo_id, "tenant_id": tenant_id, "platform": platform}


def _sweep(client, db, *, user="alice", tenant_id="eng", repo_id=REPO, platform=False, type_="issue-sweep"):
    seed_tenant(db, "eng")
    _register(db, repo_id, tenant_id, platform=platform)
    response = client.post("/v1/schedules", headers=auth_header(user), json={
        "name": "nightly sweep", "type": type_, "scope": {"mode": "repos", "repo_ids": [repo_id]},
        "cron": "0 9 * * 1-5", "timezone": "Europe/London",
    })
    assert response.status_code == 201, response.text
    return response.json()["schedule"]


def _switch(client, schedule, mode, user="alice", **extra):
    return client.post(f"/v1/schedules/{schedule['schedule_id']}:merge-mode", headers=auth_header(user),
                       json={"mode": mode, "revision": schedule["revision"], **extra})


def test_the_last_gate_editor_cannot_switch_to_auto_and_another_member_can(client, db):
    sweep = _sweep(client, db)
    assert sweep["gate"]["merge"] == "approve"

    refused = _switch(client, sweep, "auto", user="alice")
    assert refused.status_code == 403 and refused.json()["code"] == "second_person_required"

    switched = _switch(client, sweep, "auto", user="dave")
    assert switched.status_code == 200, switched.text
    doc = switched.json()["schedule"]
    assert doc["gate"]["merge"] == "auto" and doc["params"]["merge"] == "auto"
    assert doc["revision"] == sweep["revision"] + 1
    entry = _audit(db, sweep["schedule_id"])[-1]
    assert entry["action"] == "gate_merge_auto" and entry["by"] == "dave@saga.xyz"
    assert entry["detail"]["gate"]["from"]["merge"] == "approve"
    assert entry["detail"]["gate"]["to"]["merge"] == "auto"

    back = _switch(client, doc, "approve", user="dave")
    assert back.status_code == 200, back.text
    assert back.json()["schedule"]["gate"]["merge"] == "approve"
    assert _audit(db, sweep["schedule_id"])[-1]["action"] == "gate_merge_approve"


def test_switching_back_to_approval_is_anyones(client, db):
    sweep = _sweep(client, db)
    on = _switch(client, sweep, "auto", user="dave").json()["schedule"]
    back = _switch(client, on, "approve", user="alice")
    assert back.status_code == 200 and back.json()["schedule"]["gate"]["merge"] == "approve"


def test_a_stale_revision_is_409_and_changes_nothing(client, db):
    sweep = _sweep(client, db)
    stale = client.post(f"/v1/schedules/{sweep['schedule_id']}:merge-mode", headers=auth_header("dave"),
                        json={"mode": "auto", "revision": sweep["revision"] + 5})
    assert stale.status_code == 409 and stale.json()["code"] == "schedule_changed"
    assert db.docs[f"schedules/{sweep['schedule_id']}"]["gate"]["merge"] == "approve"


def test_patch_cannot_reach_auto_merge(client, db):
    sweep = _sweep(client, db)
    response = client.patch(f"/v1/schedules/{sweep['schedule_id']}", headers=auth_header("dave"),
                            json={"revision": sweep["revision"], "gate": {"merge": "auto"}})
    assert response.status_code == 422 and response.json()["code"] == "use_merge_switch"


def test_a_one_person_tenant_switches_after_typing_the_schedules_name(client, db):
    personal = tenant_id_for_user("carol@saga.xyz")
    sweep = _sweep(client, db, user="carol", tenant_id=personal, repo_id="repo_00000000000000c3")
    bare = _switch(client, sweep, "auto", user="carol")
    assert bare.status_code == 422 and bare.json()["code"] == "confirmation_required"
    typed = _switch(client, sweep, "auto", user="carol", confirm="nightly sweep")
    assert typed.status_code == 200, typed.text
    assert _audit(db, sweep["schedule_id"])[-1]["detail"]["confirmed"] is True


def test_in_a_platform_repository_only_a_platform_admin_switches(client, db):
    sweep = _sweep(client, db, platform=True, user="root")
    refused = _switch(client, sweep, "auto", user="dave")
    assert refused.status_code == 403 and refused.json()["code"] == "platform_admin_required"
    switched = _switch(client, sweep, "auto", user="root")
    assert switched.status_code == 200, switched.text
    back = _switch(client, switched.json()["schedule"], "approve", user="dave")
    assert back.status_code == 200


def test_a_type_whose_floor_forbids_auto_merge_is_refused(client, db):
    plan_only = _sweep(client, db, type_="issue-plan-only")
    response = _switch(client, plan_only, "auto", user="dave")
    assert response.status_code == 422 and response.json()["code"] == "merge_auto_not_allowed"


def test_another_tenants_schedule_is_404_to_the_switch(client, db):
    sweep = _sweep(client, db)
    assert _switch(client, sweep, "approve", user="bob").status_code == 404


# --------------------------------------------------------------------------
# the issue run's metadata.schedule and its lookup by firing id
# --------------------------------------------------------------------------

def test_a_run_is_found_by_the_firing_that_made_it_in_its_own_tenant_only(db, clock):
    runs = IssueRuns(db, now=clock)
    mark = {"schedule_id": "sch_000000000001", "firing_id": "sch_000000000001:9", "type": "issue-sweep"}
    for run_id, tenant_id in (("run_a", "eng"), ("run_b", "research")):
        runs.create(IssueRun(
            id=run_id, tenant_id=tenant_id, created_by="alice@saga.xyz", created_at=clock(),
            updated_at=clock(), state=RunState.PLANNING, issue=IssueRef("saga-xyz", "widgets", 42),
            plan_approval="required", auto_merge=False, fix_rounds=3, planner_task_id="t1",
            schedule=dict(mark),
        ))
    assert db.docs["issue_runs/run_a"]["metadata"]["schedule"]["firing_id"] == "sch_000000000001:9"
    assert [r.id for r in runs.for_firing("eng", "sch_000000000001:9")] == ["run_a"]
    assert runs.for_firing("eng", "sch_000000000001:8") == []
    assert runs.get("eng", "run_a").schedule == mark


def test_a_one_person_hold_is_not_a_group_hold():
    assert approvals.PROJECTED_PREFIX == "run:"
    hold = issueruns.plan_hold(_bootstrap_plan(), platform=False, hard_stop_paths=[])
    assert issueruns.hold_confirmation(hold) == "terraform/bootstrap/main.tf"
    assert repo_id_for("eng", "saga-xyz", "widgets").startswith("repo_")
