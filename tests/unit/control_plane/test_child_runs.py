"""Child workflows and child issue runs (docs/design/agent-autonomy.md §3.6, lane AU-6).

A running agent spools `{"kind": "issue_run", ...}` or `{"kind": "workflow",
...}` beside today's agent child; its worker relays it to the same
`POST /v1/tasks/{task_id}/children`, signed with the same attempt key.

What these tests hold, each the row's own words:

  1. A child `issue_run` on ANOTHER repository is refused: an issue is a
     number in the parent's own repository.
  2. A `workflow` child naming an UNAPPROVED spec is refused: only a saved
     custom-prompt spec whose digest a platform admin approved runs.
  3. A spec BODY is refused: a body is images and commands by another name
     (invariant 10).
  4. An `after_merged` run HOLDS NO AGENT until its predecessor is DONE and
     merged: no run, no planner task, no lease (invariant 1).
  5. Both kinds count toward the 16 and the depth rule.

Drives the real routes over the in-memory Firestore with a fake GitHub. No
credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_worker import children as worker_children
from swarm_api import approvals, children, forge, forgewrite
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.schedtypes import custom_prompt
from swarm_api.schemas import WorkflowCreate
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, seed_tenant
from .test_child_tasks_api import (  # noqa: F401 -- `worker_tokens` is a fixture
    ATTEMPT,
    KEY,
    LEASE,
    PARENT,
    SIGNER,
    WORKER_TOKEN,
    Attempt,
    ready_parent,
    spec_settings,
    submit,
    worker_tokens,
)

SPEC_NAME = "nightly-docs"
SPEC = {"steps": [{"step_id": "write", "runner_profile": "mock", "input": {"prompt": "docs"}}]}


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def github():
    return forge_fakes.GitHub(
        issues=[forge_fakes.issue(42, "Sort the widgets"), forge_fakes.issue(7, "Then filter them")],
    )


def _context(db, worker_tokens, group_map, objects, github, **settings: Any):
    return build_context(
        settings=api_settings(**{"child_key": KEY, **spec_settings(), **settings}),
        db=db,
        signer=SIGNER,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
    )


@pytest.fixture
def ctx(db, worker_tokens, group_map, objects, github):
    return _context(db, worker_tokens, group_map, objects, github)


@pytest.fixture
def client(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def parent(client, db) -> Attempt:
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(client, db, attempt)
    return attempt


def _runs(db) -> dict[str, dict[str, Any]]:
    return {p: d for p, d in db.docs.items() if p.startswith("issue_runs/") and p.count("/") == 1}


def _workflows(db) -> dict[str, dict[str, Any]]:
    return {p: d for p, d in db.docs.items() if p.startswith("workflows/") and p.count("/") == 1}


def _ledger(db) -> dict[str, dict[str, Any]]:
    return {p: d for p, d in db.docs.items()
            if p.startswith(f"{children.CHILD_RUNS}/") and p.count("/") == 1}


def _tasks(db) -> dict[str, dict[str, Any]]:
    return {p: d for p, d in db.docs.items() if p.startswith("tasks/") and p.count("/") == 1}


def _seed_spec(db, *, tenant: str = "eng", name: str = SPEC_NAME, spec: dict | None = None,
               decision: str | None = approvals.APPROVED) -> dict[str, Any]:
    """A saved custom-prompt schedule, and (unless `decision` is None) its
    digest's `spec` approval in state `decision`, as the inbox leaves it."""
    schedule = {
        "schedule_id": f"sch_{name.replace('-', '')}",
        "tenant_id": tenant,
        "name": name,
        "type": custom_prompt.TYPE,
        "params": {"spec": spec or SPEC, "max_steps": 5},
        "gate": {"run": "approve", "merge": "approve"},
        "state": "active",
    }
    db.docs[f"schedules/{schedule['schedule_id']}"] = schedule
    if decision is not None:
        digest = custom_prompt.spec_digest(WorkflowCreate.model_validate(spec or SPEC))
        from datetime import datetime, timezone

        doc = approvals.request(
            db, datetime.now(timezone.utc), tenant_id=tenant, kind=approvals.SPEC,
            key=custom_prompt.spec_key(schedule["schedule_id"], digest, 0),
            subject={"schedule_id": schedule["schedule_id"], "spec_digest": digest},
            digest=digest, summary="test spec", approvers="platform_admin", ttl=72,
        )
        db.docs[f"{approvals.COLLECTION}/{doc['approval_id']}"]["state"] = decision
    return schedule


def _issue_run(issue: Any, **extra: Any) -> dict[str, Any]:
    return {"kind": "issue_run", "issue": issue, **extra}


def _listing(client, attempt: Attempt, task: str = PARENT) -> list[dict[str, Any]]:
    t = attempt.tuple
    query = f"tenant_id={t.tenant_id}&attempt_id={t.attempt_id}&lease_id={t.lease_id}&generation={t.generation}"
    path = f"/v1/tasks/{task}/children"
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", **attempt.sign("GET", f"{path}?{query}", b"")}
    response = client.get(f"{path}?{query}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["children"]


# --------------------------------------------------------------------------
# 1. an issue run is a number in the parent's own repository
# --------------------------------------------------------------------------


def test_an_issue_run_child_on_the_parents_repository_starts_a_run_as_the_parents_submitter(
        client, db, parent):
    response = submit(client, parent, "lane-a", child=_issue_run(42))
    assert response.status_code == 201, response.text
    child = response.json()["child"]
    # MUTATION: answer a run child as a task (`{"task": ...}`) and this KeyErrors.
    assert child["kind"] == "issue_run" and child["state"] == children.STARTED
    (run,) = _runs(db).values()
    assert child["run_id"] == run["id"]
    # MUTATION: start the run as the worker's service account instead of the
    # parent's submitter and `created_by` is not alice.
    assert run["tenant_id"] == "eng" and run["created_by"] == "alice@example.com"
    assert run["issue"]["owner"] == "acme" and run["issue"]["repo"] == "widgets"
    assert run["issue"]["number"] == 42
    # The ledger, swarm-api's own: the parent's link, never a frozen field.
    (row,) = _ledger(db).values()
    assert row["parent_task_id"] == PARENT and row["tenant_id"] == "eng"
    assert row["run_id"] == run["id"] and row["request_id"] == "lane-a"
    # The same request id again makes nothing new.
    again = submit(client, parent, "lane-a", child=_issue_run(42))
    assert again.status_code == 200 and again.json()["created"] is False
    # MUTATION: drop the ledger dedupe and a second run is started.
    assert len(_runs(db)) == 1


@pytest.mark.parametrize("named", [
    "other-org/other-repo#42",
    "https://github.com/other-org/widgets/issues/42",
    "acme/gadgets#42",
])
def test_an_issue_run_child_on_another_repository_is_refused(client, db, parent, named):
    response = submit(client, parent, "elsewhere", child=_issue_run(named))
    # MUTATION: skip the repository comparison and a run starts on another repository.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "child_run_other_repository"
    assert _runs(db) == {} and _ledger(db) == {}


def test_the_parents_repository_named_in_full_is_the_same_repository(client, db, parent):
    response = submit(client, parent, "spelled", child=_issue_run("ACME/widgets#42"))
    # MUTATION: compare owner/repo case-sensitively and GitHub's own spelling is refused.
    assert response.status_code == 201, response.text


def test_an_issue_run_child_names_no_repository_field(client, db, parent):
    response = submit(client, parent, "smuggled",
                      child=_issue_run(42, repository="https://github.com/other-org/x.git"))
    # MUTATION: let the issue-run model accept unknown keys and this is a 201.
    assert response.status_code == 422, response.text
    assert _runs(db) == {}


def test_a_parent_with_no_repository_cannot_start_an_issue_run(client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(client, db, attempt, repository_url=None, repository_ref=None)
    response = submit(client, attempt, "nowhere", child=_issue_run(42))
    # MUTATION: fall back to a default repository and this starts a run.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "child_run_no_repository"
    assert _runs(db) == {}


# --------------------------------------------------------------------------
# 2. and 3. a workflow child is a saved, approved spec, by name
# --------------------------------------------------------------------------


def test_a_workflow_child_naming_an_approved_spec_submits_it_with_merge_off(client, db, parent):
    _seed_spec(db)
    response = submit(client, parent, "docs", child={"kind": "workflow", "spec_name": SPEC_NAME})
    assert response.status_code == 201, response.text
    child = response.json()["child"]
    (workflow,) = _workflows(db).values()
    # MUTATION: never submit the workflow and there is no workflow id.
    assert child["kind"] == "workflow" and child["workflow_id"] == workflow["workflow_id"]
    assert workflow["tenant_id"] == "eng" and workflow["submitted_by"] == "alice@example.com"
    steps = [d for d in _tasks(db).values() if d.get("workflow_id") == workflow["workflow_id"]]
    assert [s["runner_profile"] for s in steps] == ["mock"]
    # MUTATION: submit the spec as stored, without `submitted_spec`, and merge is not "off".
    assert all(s["metadata"].get("merge") == "off" for s in steps)


@pytest.mark.parametrize("decision", [None, approvals.PENDING, approvals.REJECTED])
def test_a_workflow_child_naming_an_unapproved_spec_is_refused(client, db, parent, decision):
    _seed_spec(db, decision=decision)
    response = submit(client, parent, "docs", child={"kind": "workflow", "spec_name": SPEC_NAME})
    # MUTATION: skip `_unapproved_code` and an unapproved spec runs.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "child_workflow_unapproved"
    assert _workflows(db) == {} and _ledger(db) == {}


def test_a_workflow_child_naming_no_saved_spec_or_another_tenants_is_refused(client, db, parent):
    _seed_spec(db, tenant="research")
    for name in (SPEC_NAME, "no-such-spec"):
        response = submit(client, parent, f"docs-{len(name)}",
                          child={"kind": "workflow", "spec_name": name})
        # MUTATION: look schedules up by name without the tenant and research's spec runs here.
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "child_workflow_unapproved"
    assert _workflows(db) == {}


def test_an_approved_spec_edited_after_its_approval_is_refused(client, db, parent):
    schedule = _seed_spec(db)
    edited = {"steps": [{"step_id": "write", "runner_profile": "mock", "input": {"prompt": "rm"}}]}
    db.docs[f"schedules/{schedule['schedule_id']}"]["params"]["spec"] = edited
    response = submit(client, parent, "docs", child={"kind": "workflow", "spec_name": SPEC_NAME})
    # MUTATION: look the approval up by schedule alone, not by the digest, and this runs.
    assert response.status_code == 403, response.text
    assert _workflows(db) == {}


@pytest.mark.parametrize("child", [
    {"kind": "workflow", "spec_name": SPEC_NAME, "steps": SPEC["steps"]},
    {"kind": "workflow", "spec": SPEC},
    {"kind": "workflow", "spec_name": SPEC_NAME, "image": "docker.io/x:latest"},
    {"kind": "workflow", "steps": SPEC["steps"]},
])
def test_a_spec_body_is_refused(client, db, parent, child):
    _seed_spec(db)
    response = submit(client, parent, "body", child=child)
    # MUTATION: give the workflow child a `spec` or `steps` field and a body runs.
    assert response.status_code == 422, response.text
    assert _workflows(db) == {} and _ledger(db) == {}


# --------------------------------------------------------------------------
# 4. after_merged: held, holding nothing, until the predecessor merged
# --------------------------------------------------------------------------


def _chain(client, db, parent) -> tuple[dict, dict]:
    first = submit(client, parent, "lane-a", child=_issue_run(42))
    assert first.status_code == 201, first.text
    a = first.json()["child"]
    second = submit(client, parent, "lane-b", child=_issue_run(7, after_merged=a["child_id"]))
    assert second.status_code == 201, second.text
    return a, second.json()["child"]


def test_an_after_merged_run_holds_no_agent_until_its_predecessor_is_done_and_merged(
        client, ctx, db, parent):
    a, b = _chain(client, db, parent)
    # MUTATION: start an after_merged run at once and its state is STARTED.
    assert b["state"] == children.HELD and b["run_id"] is None
    # Nothing exists for it: one run (A's), and only A's planner task.
    assert len(_runs(db)) == 1
    planners = [d for d in _tasks(db).values() if (d.get("metadata") or {}).get("issue_run")]
    assert len(planners) == 1
    run_path = f"issue_runs/{a['run_id']}"

    for state, pull in (("RUNNING", None), ("CHECKING", {"number": 3, "merged": False})):
        db.docs[run_path].update({"state": state, "pull_request": pull})
        children.release_held_runs(ctx, "eng")
        # MUTATION: release on any state but DONE-and-merged and B starts here.
        assert _ledger(db)[f"{children.CHILD_RUNS}/{b['child_id']}"]["state"] == children.HELD
        assert len(_runs(db)) == 1

    db.docs[run_path].update({"state": "DONE", "pull_request": {"number": 3, "merged": True}})
    report = children.release_held_runs(ctx, "eng")
    row = _ledger(db)[f"{children.CHILD_RUNS}/{b['child_id']}"]
    # MUTATION: never release and B stays HELD.
    assert row["state"] == children.STARTED and report["started"] == 1
    assert len(_runs(db)) == 2 and f"issue_runs/{row['run_id']}" in db.docs
    assert db.docs[f"issue_runs/{row['run_id']}"]["issue"]["number"] == 7
    # Released once: a second tick starts nothing more.
    children.release_held_runs(ctx, "eng")
    assert len(_runs(db)) == 2


@pytest.mark.parametrize("ending", [
    {"state": "FAILED"},
    {"state": "CANCELLED"},
    {"state": "REJECTED"},
    {"state": "DONE", "pull_request": {"number": 3, "merged": False}},
])
def test_a_held_run_whose_predecessor_ended_without_merging_never_starts(
        client, ctx, db, parent, ending):
    a, b = _chain(client, db, parent)
    db.docs[f"issue_runs/{a['run_id']}"].update(ending)
    children.release_held_runs(ctx, "eng")
    row = _ledger(db)[f"{children.CHILD_RUNS}/{b['child_id']}"]
    # MUTATION: leave it HELD and the parent waits on it for ever.
    assert row["state"] == children.CANCELLED and row["run_id"] is None
    assert len(_runs(db)) == 1


def test_a_held_run_of_a_parent_that_ended_never_starts(client, ctx, db, parent):
    a, b = _chain(client, db, parent)
    db.docs[f"issue_runs/{a['run_id']}"].update(
        {"state": "DONE", "pull_request": {"number": 3, "merged": True}})
    db.docs[f"tasks/{PARENT}"]["state"] = "CANCELLED"
    children.release_held_runs(ctx, "eng")
    # MUTATION: skip the parent check and a cancelled parent's lane starts.
    assert _ledger(db)[f"{children.CHILD_RUNS}/{b['child_id']}"]["state"] == children.CANCELLED
    assert len(_runs(db)) == 1


def test_after_merged_names_an_issue_run_of_this_parent(client, db, parent):
    _seed_spec(db)
    wf = submit(client, parent, "wf", child={"kind": "workflow", "spec_name": SPEC_NAME})
    assert wf.status_code == 201, wf.text
    for named in ("crun_nosuchchild", wf.json()["child"]["child_id"]):
        response = submit(client, parent, f"after-{len(named)}",
                          child=_issue_run(7, after_merged=named))
        # MUTATION: accept any id and a run waits on nothing that can merge.
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "child_after_merged_unknown"


def test_the_release_reads_only_its_own_tenants_ledger(client, ctx, db, parent):
    a, b = _chain(client, db, parent)
    db.docs[f"issue_runs/{a['run_id']}"].update(
        {"state": "DONE", "pull_request": {"number": 3, "merged": True}})
    children.release_held_runs(ctx, "research")
    # MUTATION: query the ledger without the tenant and research's tick starts eng's run.
    assert _ledger(db)[f"{children.CHILD_RUNS}/{b['child_id']}"]["state"] == children.HELD


# --------------------------------------------------------------------------
# 5. the 16 and the depth rule
# --------------------------------------------------------------------------


def test_run_and_workflow_children_count_toward_the_fan_out_cap(
        db, worker_tokens, group_map, objects, github):
    ctx = _context(db, worker_tokens, group_map, objects, github, max_children_per_task=3)
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(client, db, attempt)
    _seed_spec(db)
    assert submit(client, attempt, "t1").status_code == 201
    assert submit(client, attempt, "r1", child=_issue_run(42)).status_code == 201
    assert submit(client, attempt, "w1",
                  child={"kind": "workflow", "spec_name": SPEC_NAME}).status_code == 201
    for request_id, child in (("t2", None), ("r2", _issue_run(7)),
                              ("w2", {"kind": "workflow", "spec_name": SPEC_NAME})):
        response = submit(client, attempt, request_id, child=child)
        # MUTATION: count only task children (or only ledger rows) and one of these is a 201.
        assert response.status_code == 409, (request_id, response.text)
        assert response.json()["code"] == "child_fan_out_exceeded"
    assert len(_runs(db)) == 1 and len(_workflows(db)) == 1


def test_a_child_task_may_not_start_a_run_or_a_workflow(client, db):
    seed_tenant(db, "eng")
    _seed_spec(db)
    attempt = Attempt()
    ready_parent(client, db, attempt, parent_task_id="task_grandparent", parent_attempt_id="att_g")
    for request_id, child in (("r", _issue_run(42)), ("w", {"kind": "workflow", "spec_name": SPEC_NAME})):
        response = submit(client, attempt, request_id, child=child)
        # MUTATION: check depth on the agent path only and a child starts a run.
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "child_depth_exceeded"
    assert _runs(db) == {} and _workflows(db) == {}


def test_a_task_of_a_child_workflow_or_child_run_may_not_have_children(client, db, parent):
    _seed_spec(db)
    wf = submit(client, parent, "wf", child={"kind": "workflow", "spec_name": SPEC_NAME}).json()["child"]
    run = submit(client, parent, "run", child=_issue_run(42)).json()["child"]
    for n, extra in enumerate(({"workflow_id": wf["workflow_id"]},
                               {"metadata": {"issue_run": run["run_id"]}},
                               {"metadata": {"issue_run": {"run_id": run["run_id"]}}})):
        step = Attempt(task=f"task_step_{n}", attempt=f"att_step_{n}", lease=f"lease_step_{n}")
        ready_parent(client, db, step, **extra)
        for request_id, child in (("t", None), ("r", _issue_run(7))):
            response = submit(client, step, request_id, child=child)
            # MUTATION: skip the ledger lookup in `_refuse_nested` and a grandchild is made.
            assert response.status_code == 409, (extra, request_id, response.text)
            assert response.json()["code"] == "child_depth_exceeded"


# --------------------------------------------------------------------------
# the listing a resumed worker reads, and the worker's half
# --------------------------------------------------------------------------


def test_the_listing_shows_run_and_workflow_children_and_whether_they_ended(client, db, parent):
    _seed_spec(db)
    run = submit(client, parent, "run", child=_issue_run(42)).json()["child"]
    wf = submit(client, parent, "wf", child={"kind": "workflow", "spec_name": SPEC_NAME}).json()["child"]
    rows = {r.get("child_id"): r for r in _listing(client, parent) if r.get("child_id")}
    # MUTATION: leave ledger rows out of the listing and the worker never waits for them.
    assert rows[run["child_id"]]["kind"] == "issue_run" and rows[run["child_id"]]["ended"] is False
    assert rows[wf["child_id"]]["kind"] == "workflow" and rows[wf["child_id"]]["ended"] is False
    db.docs[f"issue_runs/{run['run_id']}"]["state"] = "DONE"
    db.docs[f"workflows/{wf['workflow_id']}"]["state"] = "SUCCEEDED"
    rows = {r.get("child_id"): r for r in _listing(client, parent) if r.get("child_id")}
    # MUTATION: read "ended" from the ledger state alone and a DONE run is still live.
    assert rows[run["child_id"]]["ended"] is True and rows[run["child_id"]]["state"] == "DONE"
    assert rows[wf["child_id"]]["ended"] is True


class _Log:
    def info(self, *a: Any, **k: Any) -> None: ...
    def warning(self, *a: Any, **k: Any) -> None: ...


class _Api:
    """swarm-api's child route, answering a run child as the route does."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def call(self, method, path, *, body=None, headers=None):
        sent = json.loads(body)
        self.bodies.append(sent)
        child = {"child_id": "crun_" + "a" * 32, "kind": sent["child"]["kind"],
                 "request_id": sent["request_id"], "state": "STARTED", "run_id": "run_1",
                 "workflow_id": None}
        return 201, {"child": child, "created": True}


def _bare(tmp_path, api):
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        child_submit_retry_seconds=10, max_child_requests_per_tick=4,
        max_child_request_bytes=262144, tenant_id="eng", attempt_id="att_1",
        lease_id="lease_1", generation=1, task_id="task_1", swarm_api_url=None,
        swarm_api_audience=None, child_nonce="n" * 12,
    )
    clock = [0.0]
    path = worker_children.ChildPath(
        cfg, logger=_Log(), memory=None, api=api, sleep=lambda s: None,
        clock=lambda: clock[0], wall=lambda: 1_790_000_000.0,
    )
    path._key = worker_children.AttemptKey()
    path.unavailable = None
    work = tmp_path / "work"
    path.prepare(work)
    return path, work


def _spool(work, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
    root = worker_children.ChildPath.spool(work)
    (root / "requests" / f"{request_id}.json").write_text(json.dumps({"request_id": request_id, **request}))
    return root


def test_the_worker_relays_a_run_child_and_records_its_id(tmp_path):
    api = _Api()
    path, work = _bare(tmp_path, api)
    root = _spool(work, "lane-a", {"kind": "issue_run", "issue": 42})
    path.tick(work)
    answer = json.loads((root / "responses" / "lane-a.json").read_text())
    # MUTATION: refuse `kind` locally and the request never reaches the route.
    assert api.bodies and api.bodies[0]["child"] == {"kind": "issue_run", "issue": 42}
    assert answer["child_id"] == "crun_" + "a" * 32 and answer["kind"] == "issue_run"
    assert answer["run_id"] == "run_1" and answer["state"] == "STARTED"
    # MUTATION: read only `task_id` in `known_children` and the await skips run children.
    assert path.known_children(work) == ["crun_" + "a" * 32]


@pytest.mark.parametrize("request_body", [
    {"kind": "workflow", "spec_name": SPEC_NAME, "steps": SPEC["steps"]},
    {"kind": "workflow", "spec": SPEC},
    {"kind": "issue_run", "issue": 42, "runner_profile": "claude-code"},
    {"kind": "issue_run", "issue": 42, "repository": "other-org/x"},
    {"kind": "pipeline", "spec_name": SPEC_NAME},
])
def test_the_worker_refuses_a_spec_body_or_an_unknown_kind_without_a_call(tmp_path, request_body):
    api = _Api()
    path, work = _bare(tmp_path, api)
    root = _spool(work, "body", request_body)
    path.tick(work)
    refusal = json.loads((root / "responses" / "body.json").read_text())["refused"]
    # MUTATION: pass unknown keys through for the run kinds and the route is called.
    assert refusal["code"] == "request_invalid" and api.bodies == []


def test_a_live_run_child_keeps_the_parent_waiting(tmp_path, monkeypatch):
    api = _Api()
    path, work = _bare(tmp_path, api)
    _spool(work, "lane-a", {"kind": "issue_run", "issue": 42})
    path.tick(work)
    rows = [{"child_id": "crun_" + "a" * 32, "kind": "issue_run", "state": "RUNNING", "ended": False,
             "task_id": None}]
    monkeypatch.setattr(path, "list_children", lambda: rows)
    # MUTATION: judge a run child by TaskState and RUNNING-as-a-run reads as unknown, so ended.
    assert path.live_children(work) == ["crun_" + "a" * 32]
    rows[0].update({"state": "DONE", "ended": True})
    assert path.live_children(work) == []


def test_the_workers_run_child_fields_match_swarm_api():
    # MUTATION: add a field to one side only and the worker refuses (or passes) what the route does not.
    assert worker_children.ISSUE_RUN_FIELDS == set(children.ChildIssueRun.model_fields)
    assert worker_children.WORKFLOW_FIELDS == set(children.ChildWorkflow.model_fields)
    assert worker_children.CHILD_FIELDS == set(children.ChildSpec.model_fields)
    assert worker_children.CHILD_KINDS == (children.KIND_AGENT, children.KIND_ISSUE_RUN,
                                           children.KIND_WORKFLOW)


def test_the_agent_guide_states_the_run_kinds():
    guide = worker_children.AGENT_GUIDE
    # MUTATION: leave the kinds out of the guide and no agent knows they exist.
    for word in ("`kind`", "`issue_run`", "`workflow`", "`spec_name`", "`after_merged`", "`child_id`"):
        assert word in guide, word
