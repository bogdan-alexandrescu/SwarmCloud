"""An issue run: plan an issue, show the plan, run it only once approved (#454).

    POST /v1/runs {"issue": "owner/repo#N"}
      -> PLANNING   a signed claude-code PLANNER task, pointed at the issue
      -> PLANNED    its plan.json, validated, digested, shown
      -> APPROVED   the caller approved THE DIGEST THEY WERE SHOWN (D3)
      -> RUNNING    a NEW signed workflow compiled from that plan
      -> DONE | FAILED | CANCELLED   from the workflow
         REJECTED   the plan was turned down

What these tests hold, in the order they matter:

  1. A RUN WAITING FOR APPROVAL HOLDS NOTHING (invariant 1). The planner is
     terminal, no further task, lease or workflow exists until approval.
  2. APPROVAL IS OF A DIGEST. A plan edited after it was shown is not the plan
     that was approved, and the approval is refused.
  3. ANOTHER TENANT'S RUN IS A 404, on every route, the same 404 as no run.
  4. The reference parser, the request fields' bounds, the state machine.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import forge, forgewrite, issueruns
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_api.issueruns import (
    RUN_TRANSITIONS,
    TERMINAL_RUN_STATES,
    InvalidRunTransition,
    RunState,
    assert_run_transition,
    compile_plan,
    plan_digest,
)
from swarm_api.schemas import RunCreate
from swarm_api.validation import IssueRef, PullRequestReference, parse_issue_ref

from . import forge_fakes
from .conftest import api_settings, auth_header

REF = "saga-xyz/widgets#42"
BUCKET = "swarm-artifacts-saga-agents-staging"

PLAN = {
    "summary": "Make the widget list sortable by name.",
    "steps": [
        {"step_id": "sort-key", "title": "Add a sort key",
         "prompt": "Add a name sort key to WidgetList."},
        {"step_id": "ui", "title": "Wire the header",
         "prompt": "Make the Name header toggle the sort."},
    ],
}


# --------------------------------------------------------------------------
# fixtures: every run reads the open work, from a fake GitHub
# --------------------------------------------------------------------------

@pytest.fixture
def github():
    return forge_fakes.GitHub(
        issues=[forge_fakes.issue(42, "Widgets cannot be sorted"), forge_fakes.issue(7)],
        pulls=[forge_fakes.pull(9, "Sort helpers")],
        files={9: ["src/widgets/sort.py"]},
    )


@pytest.fixture
def forge_tokens():
    return forge_fakes.AnyTenantTokens()


@pytest.fixture
def api_context(db, tokens, group_map, objects, github, forge_tokens):
    """conftest's context, with the forge reads faked: no Secret Manager, no network."""
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens,
        forge=forge.GitHubIssues(send=github),
        # The write-back to the issue, in memory: test_issue_writeback.py
        # holds what it writes; here it only keeps the network out.
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _create(client, user="alice", **body):
    return client.post("/v1/runs", headers=auth_header(user), json={"issue": REF, **body})


def _run(client, run_id, user="alice"):
    return client.get(f"/v1/runs/{run_id}", headers=auth_header(user))


def _finish_planner(db, objects, run: dict, plan, *, state="SUCCEEDED") -> None:
    """The planner task ends, leaving plan.json exactly as a worker uploads it."""
    task_id = run["planner_task_id"]
    doc = db.docs[f"tasks/{task_id}"]
    doc["state"] = state
    if plan is None:
        doc["result_summary"] = {"artifacts": []} if state == "SUCCEEDED" else None
        return
    raw = plan if isinstance(plan, str) else json.dumps(plan)
    key = f"tenants/{doc['tenant_id']}/tasks/{task_id}/attempts/att_1/artifacts/plan.json"
    objects.put(key, raw)
    doc["result_summary"] = {
        "artifacts": [{"name": "plan.json", "bytes": len(raw.encode()), "uri": f"gs://{BUCKET}/{key}"}]
    }


def _planned(client, db, objects, user="alice", **body) -> dict:
    created = _create(client, user, **body)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, PLAN)
    read = _run(client, run["id"], user)
    assert read.status_code == 200, read.text
    return read.json()["run"]


def _docs(db, collection: str) -> dict:
    """Top-level documents only: a task's events are a subcollection."""
    return {
        k: v for k, v in db.docs.items() if k.startswith(collection + "/") and k.count("/") == 1
    }


# --------------------------------------------------------------------------
# the issue reference
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "saga-xyz/widgets#42",
        "https://github.com/saga-xyz/widgets/issues/42",
        "https://github.com/saga-xyz/widgets/issues/42/",
        "https://github.com/saga-xyz/widgets/issues/42#issuecomment-1",
        "https://www.github.com/saga-xyz/widgets/issues/42?x=1",
        "saga-xyz/widgets.git#42",
    ],
)
def test_an_issue_is_named_short_or_by_its_url(value):
    assert parse_issue_ref(value) == IssueRef(owner="saga-xyz", repo="widgets", number=42)


def test_the_parsed_reference_names_its_repository_and_canonical_url():
    ref = parse_issue_ref(REF)
    assert ref.repository_url == "https://github.com/saga-xyz/widgets"
    assert ref.url == "https://github.com/saga-xyz/widgets/issues/42"
    assert ref.short == REF


@pytest.mark.parametrize(
    "value",
    ["https://github.com/saga-xyz/widgets/pull/42", "https://github.com/saga-xyz/widgets/pulls/42"],
)
def test_a_pull_request_url_is_refused_as_a_pull_request(value):
    with pytest.raises(PullRequestReference, match="is a pull request"):
        parse_issue_ref(value)


def test_an_issue_url_carrying_a_credential_is_refused_without_echoing_it():
    secret = "ghp_" + "q" * 36
    with pytest.raises(ValueError) as refused:
        parse_issue_ref(f"https://{secret}@github.com/saga-xyz/widgets/issues/42")
    assert secret not in str(refused.value)
    assert "credential" in str(refused.value)


@pytest.mark.parametrize(
    "value",
    [
        "", "widgets#42", "saga-xyz/widgets", "saga-xyz/widgets#0", "saga-xyz/widgets#1000000",
        "http://github.com/saga-xyz/widgets/issues/42",
        "https://gitlab.com/saga-xyz/widgets/issues/42",
        "https://github.com/saga-xyz/widgets/issues/abc",
        "https://github.com/saga-xyz/widgets",
        "saga-xyz/..#4",
    ],
)
def test_anything_else_is_refused(value):
    with pytest.raises(ValueError):
        parse_issue_ref(value)


# --------------------------------------------------------------------------
# the request fields
# --------------------------------------------------------------------------

def test_the_defaults_are_required_approval_no_auto_merge_and_three_fix_rounds():
    body = RunCreate(issue=REF)
    assert body.plan_approval == "required"
    assert body.auto_merge is False
    assert body.fix_rounds == 3
    assert body.issue == REF


def test_an_issue_url_is_stored_in_its_short_form():
    assert RunCreate(issue="https://github.com/saga-xyz/widgets/issues/42").issue == REF


@pytest.mark.parametrize("rounds", [0, 6, -1])
def test_fix_rounds_outside_one_to_five_is_a_422(client, rounds):
    response = _create(client, fix_rounds=rounds)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("rounds", [1, 5])
def test_fix_rounds_at_either_bound_is_accepted(client, rounds):
    response = _create(client, fix_rounds=rounds)
    assert response.status_code == 201, response.text
    assert response.json()["run"]["fix_rounds"] == rounds


def test_plan_approval_is_required_or_auto(client):
    assert _create(client, plan_approval="sometimes").status_code == 422
    assert _create(client, plan_approval="auto").status_code == 201


def test_a_pull_request_reference_is_a_422_saying_so(client, db):
    response = client.post(
        "/v1/runs", headers=auth_header("alice"),
        json={"issue": "https://github.com/saga-xyz/widgets/pull/7"},
    )
    assert response.status_code == 422
    assert "pull request" in response.text
    assert not _docs(db, "tasks")


def test_auto_merge_is_refused_until_the_merge_chain_is_enabled(client, db):
    response = _create(client, auto_merge=True)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "auto_merge_unavailable"
    assert "#295" in response.json()["message"]
    # Refused before anything was created.
    assert not _docs(db, "tasks")
    assert not _docs(db, issueruns.RUNS_COLLECTION)


def test_a_caller_cannot_pick_the_planners_image_or_profile(client):
    response = _create(client, runner_profile="generic")
    assert response.status_code == 422


# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------

def test_the_machine_is_the_one_454_names():
    assert RUN_TRANSITIONS[RunState.PLANNING] == {RunState.PLANNED, RunState.FAILED, RunState.CANCELLED}
    assert RUN_TRANSITIONS[RunState.PLANNED] == {
        RunState.PLANNED, RunState.APPROVED, RunState.REJECTED, RunState.CANCELLED,
    }
    assert RUN_TRANSITIONS[RunState.APPROVED] == {RunState.RUNNING, RunState.FAILED}
    # The workflow succeeding opens a pull request; CI decides DONE (issueci).
    assert RUN_TRANSITIONS[RunState.RUNNING] == {RunState.CHECKING, RunState.FAILED, RunState.CANCELLED}
    assert RUN_TRANSITIONS[RunState.CHECKING] == {
        RunState.FIXING, RunState.DONE, RunState.FAILED, RunState.CANCELLED,
    }
    assert RUN_TRANSITIONS[RunState.FIXING] == {RunState.CHECKING, RunState.FAILED, RunState.CANCELLED}
    assert TERMINAL_RUN_STATES == {
        RunState.DONE, RunState.FAILED, RunState.REJECTED, RunState.CANCELLED,
    }
    for state in TERMINAL_RUN_STATES:
        assert RUN_TRANSITIONS[state] == frozenset()
    assert set(RUN_TRANSITIONS) == set(RunState)


@pytest.mark.parametrize(
    "frm,to",
    [
        (RunState.PLANNING, RunState.APPROVED),   # nothing to approve yet
        (RunState.PLANNING, RunState.RUNNING),
        (RunState.PLANNED, RunState.RUNNING),     # never without an approval
        (RunState.REJECTED, RunState.APPROVED),
        (RunState.DONE, RunState.RUNNING),
    ],
)
def test_a_transition_outside_the_machine_is_refused(frm, to):
    with pytest.raises(InvalidRunTransition):
        assert_run_transition(frm, to)


# --------------------------------------------------------------------------
# creating a run: one planner task, nothing else
# --------------------------------------------------------------------------

def test_a_run_starts_planning_with_one_signed_planner_task(client, db):
    response = _create(client)
    assert response.status_code == 201, response.text
    run = response.json()["run"]
    assert run["id"].startswith("run_")
    assert response.headers["Location"] == f"/v1/runs/{run['id']}"
    assert run["state"] == "PLANNING"
    assert run["issue"]["ref"] == REF
    assert run["plan"] is None and run["plan_digest"] is None
    assert run["workflow_id"] is None

    tasks = _docs(db, "tasks")
    assert list(tasks) == [f"tasks/{run['planner_task_id']}"]
    planner = tasks[f"tasks/{run['planner_task_id']}"]
    assert planner["runner_profile"] == "claude-code"
    assert planner["tenant_id"] == "eng"
    assert planner["input"]["issue"] == 42
    assert REF in planner["input"]["prompt"]
    assert "plan.json" in planner["input"]["prompt"]
    assert planner["repository_url"] == "https://github.com/saga-xyz/widgets"
    assert planner["metadata"]["issue_run"] == run["id"]
    assert not _docs(db, "workflows")
    # The run document is tenant-scoped like a task.
    stored = db.docs[f"{issueruns.RUNS_COLLECTION}/{run['id']}"]
    assert stored["tenant_id"] == "eng"


def test_a_run_waiting_for_approval_creates_no_task_lease_or_workflow(client, db, objects):
    run = _planned(client, db, objects)
    assert run["state"] == "PLANNED"
    assert run["plan"] == PLAN
    assert run["plan_digest"] == plan_digest(PLAN)
    # Reading it again, and again, creates nothing either.
    for _ in range(3):
        assert _run(client, run["id"]).json()["run"]["state"] == "PLANNED"
    tasks = _docs(db, "tasks")
    assert len(tasks) == 1, "only the (terminal) planner task may exist before approval"
    assert next(iter(tasks.values()))["state"] == "SUCCEEDED"
    assert not _docs(db, "leases")
    assert not _docs(db, "workflows")


# --------------------------------------------------------------------------
# the plan the planner wrote
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "plan,why",
    [
        ({"summary": "x", "steps": []}, "steps"),
        ({"summary": "x", "steps": [{"step_id": "a", "title": "t", "prompt": "p",
                                      "runner_profile": "generic"}]}, "runner_profile"),
        ({"summary": "x", "steps": [{"step_id": "a", "title": "t", "prompt": "p"}],
          "image": "evil"}, "image"),
        ({"summary": "x", "steps": [{"step_id": "a", "title": "t", "prompt": "p"},
                                    {"step_id": "a", "title": "t", "prompt": "p"}]}, "unique"),
        ({"summary": "x", "steps": [{"step_id": "review", "title": "t", "prompt": "p"}]}, "review"),
        ("not json", "JSON"),
    ],
)
def test_a_plan_outside_the_schema_fails_the_run(client, db, objects, plan, why):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, plan)
    read = _run(client, run["id"]).json()["run"]
    assert read["state"] == "FAILED"
    assert why in read["error"]
    assert not _docs(db, "workflows")


def test_a_planner_that_wrote_no_plan_fails_the_run(client, db, objects):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, None)
    read = _run(client, run["id"]).json()["run"]
    assert read["state"] == "FAILED"
    assert "plan.json" in read["error"]


def test_a_failed_planner_fails_the_run_and_a_cancelled_one_cancels_it(client, db, objects):
    failed = _create(client).json()["run"]
    _finish_planner(db, objects, failed, None, state="FAILED")
    assert _run(client, failed["id"]).json()["run"]["state"] == "FAILED"

    cancelled = _create(client).json()["run"]
    _finish_planner(db, objects, cancelled, None, state="CANCELLED")
    assert _run(client, cancelled["id"]).json()["run"]["state"] == "CANCELLED"


def test_a_planner_still_running_leaves_the_run_planning(client, db):
    run = _create(client).json()["run"]
    db.docs[f"tasks/{run['planner_task_id']}"]["state"] = "RUNNING"
    assert _run(client, run["id"]).json()["run"]["state"] == "PLANNING"


# --------------------------------------------------------------------------
# approve / edit / reject
# --------------------------------------------------------------------------

def _approve(client, run_id, digest, user="alice"):
    return client.post(
        f"/v1/runs/{run_id}/plan:approve", headers=auth_header(user), json={"plan_digest": digest}
    )


def _edit(client, run_id, digest, plan, user="alice"):
    return client.post(
        f"/v1/runs/{run_id}/plan:edit", headers=auth_header(user),
        json={"plan_digest": digest, "plan": plan},
    )


def test_approving_the_digest_shown_submits_one_new_workflow(client, db, objects):
    run = _planned(client, db, objects)
    response = _approve(client, run["id"], run["plan_digest"])
    assert response.status_code == 200, response.text
    approved = response.json()["run"]
    assert approved["state"] == "RUNNING"
    assert approved["approved_by"] == "alice@saga.xyz"
    assert approved["approved_digest"] == run["plan_digest"]
    workflows = _docs(db, "workflows")
    assert list(workflows) == [f"workflows/{approved['workflow_id']}"]
    steps = [s["step_id"] for s in workflows[f"workflows/{approved['workflow_id']}"]["steps"]]
    assert steps == ["impl-sort-key", "impl-ui", "review", "fix"]
    # Every step is a claude-code task of the caller's tenant, pointed at the issue.
    step_tasks = [t for t in _docs(db, "tasks").values() if t.get("workflow_id") == approved["workflow_id"]]
    assert len(step_tasks) == 4
    assert all(t["runner_profile"] == "claude-code" and t["tenant_id"] == "eng" for t in step_tasks)
    assert all(t["input"]["issue"] == 42 for t in step_tasks)
    assert all(t["repository_url"] == "https://github.com/saga-xyz/widgets" for t in step_tasks)
    # Approving twice does not submit twice.
    again = _approve(client, run["id"], run["plan_digest"])
    assert again.status_code == 409
    assert len(_docs(db, "workflows")) == 1


def test_approving_a_stale_digest_is_refused_and_submits_nothing(client, db, objects):
    run = _planned(client, db, objects)
    stale = "sha256:" + "0" * 64
    response = _approve(client, run["id"], stale)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "plan_changed"
    assert response.json()["detail"]["plan_digest"] == run["plan_digest"]
    assert _run(client, run["id"]).json()["run"]["state"] == "PLANNED"
    assert not _docs(db, "workflows")
    assert len(_docs(db, "tasks")) == 1


def test_a_plan_edited_after_it_was_shown_cannot_be_approved_by_the_old_digest(client, db, objects):
    run = _planned(client, db, objects)
    edited_plan = {**PLAN, "summary": "Sort by name, then by date."}
    edited = _edit(client, run["id"], run["plan_digest"], edited_plan)
    assert edited.status_code == 200, edited.text
    after = edited.json()["run"]
    assert after["state"] == "PLANNED"
    assert after["plan"] == edited_plan
    assert after["plan_digest"] == plan_digest(edited_plan) != run["plan_digest"]
    assert after["plan_revision"] == 2
    assert after["plan_edited_by"] == "alice@saga.xyz"

    refused = _approve(client, run["id"], run["plan_digest"])
    assert refused.status_code == 409
    assert refused.json()["code"] == "plan_changed"
    assert not _docs(db, "workflows")

    accepted = _approve(client, run["id"], after["plan_digest"])
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["run"]["state"] == "RUNNING"


def test_an_edit_from_a_stale_digest_is_refused(client, db, objects):
    run = _planned(client, db, objects)
    first = _edit(client, run["id"], run["plan_digest"], {**PLAN, "summary": "one"})
    assert first.status_code == 200
    second = _edit(client, run["id"], run["plan_digest"], {**PLAN, "summary": "two"})
    assert second.status_code == 409
    assert second.json()["code"] == "plan_changed"
    assert _run(client, run["id"]).json()["run"]["plan"]["summary"] == "one"


def test_an_edit_outside_the_plan_schema_is_a_422(client, db, objects):
    run = _planned(client, db, objects)
    bad = {"summary": "x", "steps": [{"step_id": "a", "title": "t", "prompt": "p", "image": "x"}]}
    response = _edit(client, run["id"], run["plan_digest"], bad)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_plan"


def test_rejecting_ends_the_run_and_nothing_can_approve_it_after(client, db, objects):
    run = _planned(client, db, objects)
    rejected = client.post(
        f"/v1/runs/{run['id']}/plan:reject", headers=auth_header("alice"),
        json={"reason": "wrong approach"},
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()["run"]
    assert body["state"] == "REJECTED"
    assert body["rejected_by"] == "alice@saga.xyz"
    assert body["rejection_reason"] == "wrong approach"
    assert _approve(client, run["id"], run["plan_digest"]).status_code == 409
    assert not _docs(db, "workflows")


def test_a_plan_still_being_written_cannot_be_approved(client, db):
    run = _create(client).json()["run"]
    response = _approve(client, run["id"], "sha256:" + "0" * 64)
    assert response.status_code == 409
    assert not _docs(db, "workflows")


def test_auto_approval_submits_the_workflow_as_soon_as_the_plan_arrives(client, db, objects):
    run = _create(client, plan_approval="auto").json()["run"]
    assert not _docs(db, "workflows")
    _finish_planner(db, objects, run, PLAN)
    read = _run(client, run["id"]).json()["run"]
    assert read["state"] == "RUNNING"
    assert read["approved_by"] == issueruns.AUTO_APPROVER
    assert read["approved_digest"] == plan_digest(PLAN)
    assert len(_docs(db, "workflows")) == 1


def _integrator_opened(db, workflow_id: str, number: int = 57) -> None:
    """The workflow's `fix` step -- its integrator -- records the PR it opened."""
    steps = db.docs[f"workflows/{workflow_id}"]["steps"]
    (task_id,) = [s["task_id"] for s in steps if s["step_id"] == issueruns.FIX_STEP]
    db.docs[f"tasks/{task_id}"]["result_summary"] = {"git": {"pull_request": {
        "number": number, "url": f"https://github.com/saga-xyz/widgets/pull/{number}",
    }}}


def test_the_run_follows_its_workflow_to_checking_its_pull_request(client, db, objects):
    """A workflow that succeeded is not CI that passed: test_issue_run_ci.py
    holds CHECKING -> DONE."""
    run = _planned(client, db, objects)
    running = _approve(client, run["id"], run["plan_digest"]).json()["run"]
    for doc in _docs(db, "tasks").values():
        if doc.get("workflow_id") == running["workflow_id"]:
            doc["state"] = "SUCCEEDED"
    _integrator_opened(db, running["workflow_id"])
    read = _run(client, run["id"]).json()["run"]
    assert read["state"] == "CHECKING"
    assert read["pull_request"]["number"] == 57


def test_the_run_follows_its_workflow_to_failed(client, db, objects):
    run = _planned(client, db, objects)
    running = _approve(client, run["id"], run["plan_digest"]).json()["run"]
    for doc in _docs(db, "tasks").values():
        if doc.get("workflow_id") == running["workflow_id"]:
            doc["state"] = "FAILED"
    assert _run(client, run["id"]).json()["run"]["state"] == "FAILED"


# --------------------------------------------------------------------------
# the compiled workflow
# --------------------------------------------------------------------------

def _stored_run(**overrides) -> issueruns.IssueRun:
    now = datetime.now(timezone.utc)
    base = dict(
        id="run_x", tenant_id="eng", created_by="alice@saga.xyz", created_at=now, updated_at=now,
        state=RunState.APPROVED, issue=parse_issue_ref(REF), plan_approval="required",
        auto_merge=False, fix_rounds=3, planner_task_id="task_p", plan=PLAN,
        plan_digest=plan_digest(PLAN),
    )
    base.update(overrides)
    return issueruns.IssueRun(**base)


def test_the_plan_compiles_to_implement_review_and_a_gated_fix():
    spec = compile_plan(_stored_run())
    assert spec.strategy == "integrate"
    assert spec.repository_url == "https://github.com/saga-xyz/widgets"
    by_id = {s.step_id: s for s in spec.steps}
    assert list(by_id) == ["impl-sort-key", "impl-ui", "review", "fix"]
    assert by_id["impl-ui"].depends_on == ["impl-sort-key"]
    assert by_id["impl-ui"].builds_on == "impl-sort-key"
    assert by_id["review"].input_from == {"impl-ui": "swarm-work.patch"}
    assert by_id["fix"].when.step == "review"
    assert by_id["fix"].when.verdict_in == ["NOT_YET"]
    assert by_id["fix"].input_from == {"review": "verdict.json"}
    # The plan's prompts reach the steps, and nothing in the plan picked a profile.
    assert "Add a name sort key" in by_id["impl-sort-key"].input["prompt"]
    assert all(s.runner_profile == "claude-code" for s in spec.steps)
    assert spec.metadata["issue_run"]["run_id"] == "run_x"
    assert spec.metadata["issue_run"]["fix_rounds"] == 3


def test_auto_merge_compilation_refuses_naming_295():
    with pytest.raises(issueruns.AutoMergeUnavailable, match="#295"):
        compile_plan(_stored_run(auto_merge=True))


def test_the_digest_is_of_the_plan_and_order_of_keys_does_not_change_it():
    reordered = {"steps": PLAN["steps"], "summary": PLAN["summary"]}
    assert plan_digest(reordered) == plan_digest(PLAN)
    assert plan_digest({**PLAN, "summary": "other"}) != plan_digest(PLAN)
    assert plan_digest(PLAN).startswith("sha256:")


# --------------------------------------------------------------------------
# the tenant boundary
# --------------------------------------------------------------------------

def test_another_tenants_run_reads_404_on_every_route(client, db, objects):
    run = _planned(client, db, objects, user="alice")
    missing = _run(client, "run_doesnotexist", user="bob")
    theirs = _run(client, run["id"], user="bob")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["message"].replace(run["id"], "X") == missing.json()["message"].replace(
        "run_doesnotexist", "X"
    )
    assert _approve(client, run["id"], run["plan_digest"], user="bob").status_code == 404
    assert _edit(client, run["id"], run["plan_digest"], PLAN, user="bob").status_code == 404
    assert client.post(
        f"/v1/runs/{run['id']}/plan:reject", headers=auth_header("bob"), json={}
    ).status_code == 404
    listed = client.get("/v1/runs", headers=auth_header("bob")).json()
    assert listed["runs"] == []
    # And none of that touched it.
    assert _run(client, run["id"]).json()["run"]["state"] == "PLANNED"
    assert not _docs(db, "workflows")


def test_the_list_is_the_callers_tenant_newest_first_and_paged(client, db):
    ids = [_create(client).json()["run"]["id"] for _ in range(3)]
    base = datetime.now(timezone.utc)
    for offset, run_id in enumerate(ids):
        db.docs[f"{issueruns.RUNS_COLLECTION}/{run_id}"]["created_at"] = base + timedelta(seconds=offset)
    _create(client, user="bob")

    first = client.get("/v1/runs?limit=2", headers=auth_header("alice")).json()
    assert [r["id"] for r in first["runs"]] == [ids[2], ids[1]]
    assert first["tenant_id"] == "eng"
    assert first["next_page_token"]
    second = client.get(
        f"/v1/runs?limit=2&page_token={first['next_page_token']}", headers=auth_header("alice")
    ).json()
    assert [r["id"] for r in second["runs"]] == [ids[0]]
    assert second["next_page_token"] is None


# --------------------------------------------------------------------------
# the plan's #454 fields: requirements, overlaps, mode, files, tests, estimate
# --------------------------------------------------------------------------

FULL_PLAN = {
    "summary": "Make the widget list sortable by name.",
    "mode": "workflow",
    "requirements": ["The Name header sorts the list", "The sort survives a reload"],
    "overlaps": [{"ref": "saga-xyz/widgets#9", "kind": "pull_request",
                  "note": "PR #9 adds sort helpers; step 1 builds on them."}],
    "estimate": "2 agent-hours",
    "steps": [
        {"step_id": "sort-key", "title": "Add a sort key",
         "prompt": "Add a name sort key to WidgetList.",
         "files": ["src/widgets/list.py"], "tests": ["test_sorts_by_name"],
         "estimate": "1h"},
        {"step_id": "ui", "title": "Wire the header",
         "prompt": "Make the Name header toggle the sort."},
    ],
}


def test_a_plan_stored_before_the_new_fields_still_parses_unchanged():
    assert issueruns.parse_plan(PLAN) == PLAN
    # So its digest is the one it was stored with.
    assert plan_digest(issueruns.parse_plan(PLAN)) == plan_digest(PLAN)


def test_the_new_fields_round_trip_through_the_schema():
    assert issueruns.parse_plan(json.dumps(FULL_PLAN)) == FULL_PLAN


def test_adding_an_overlap_changes_the_digest():
    without = {k: v for k, v in FULL_PLAN.items() if k != "overlaps"}
    assert plan_digest(issueruns.parse_plan(without)) != plan_digest(
        issueruns.parse_plan(FULL_PLAN)
    )


@pytest.mark.parametrize(
    "plan,named",
    [
        ({**FULL_PLAN, "runner_profile": "generic"}, "runner_profile"),
        ({**FULL_PLAN, "steps": [{**FULL_PLAN["steps"][0], "image": "evil"}]}, "image"),
        ({**FULL_PLAN, "overlaps": [{**FULL_PLAN["overlaps"][0], "command": "x"}]}, "command"),
    ],
)
def test_an_unknown_key_is_still_refused_naming_it(plan, named):
    with pytest.raises(issueruns.InvalidPlan) as refused:
        issueruns.parse_plan(plan)
    assert named in refused.value.message


@pytest.mark.parametrize(
    "change,where",
    [
        ({"mode": "swarm"}, "mode"),
        ({"overlaps": [{"ref": "not a ref", "kind": "issue", "note": "n"}]}, "overlaps"),
        ({"overlaps": [{"ref": "a/b#1", "kind": "commit", "note": "n"}]}, "overlaps"),
        ({"requirements": ["r"] * (issueruns.MAX_PLAN_REQUIREMENTS + 1)}, "requirements"),
        ({"requirements": ["x" * 501]}, "requirements"),
        ({"estimate": "x" * 201}, "estimate"),
        ({"steps": [{**FULL_PLAN["steps"][0],
                     "files": ["f"] * (issueruns.MAX_STEP_FILES + 1)}]}, "files"),
        ({"steps": [{**FULL_PLAN["steps"][0], "tests": [""]}]}, "tests"),
    ],
)
def test_the_new_fields_are_bounded(change, where):
    with pytest.raises(issueruns.InvalidPlan) as refused:
        issueruns.parse_plan({**FULL_PLAN, **change})
    assert where in refused.value.message


def test_a_steps_files_and_tests_reach_its_prompt_and_the_requirements_the_review():
    spec = compile_plan(_stored_run(plan=FULL_PLAN, plan_digest=plan_digest(FULL_PLAN)))
    by_id = {s.step_id: s for s in spec.steps}
    first = by_id["impl-sort-key"].input["prompt"]
    assert "src/widgets/list.py" in first
    assert "test_sorts_by_name" in first and "write them first" in first
    assert "Files this step" not in by_id["impl-ui"].input["prompt"]
    review = by_id["review"].input["prompt"]
    assert "1. The Name header sorts the list" in review
    assert "2. The sort survives a reload" in review


def test_a_single_mode_plan_compiles_to_the_same_shape_with_one_implementer():
    plan = {"summary": "One change.", "mode": "single",
            "steps": [{"step_id": "all", "title": "Do it", "prompt": "Do it all."}]}
    spec = compile_plan(_stored_run(plan=plan, plan_digest=plan_digest(plan)))
    assert [s.step_id for s in spec.steps] == ["impl-all", "review", "fix"]
    assert spec.strategy == "integrate"


def test_a_plan_with_the_new_fields_is_served_and_approvable(client, db, objects):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, FULL_PLAN)
    planned = _run(client, run["id"]).json()["run"]
    assert planned["state"] == "PLANNED"
    assert planned["plan"] == FULL_PLAN
    approved = _approve(client, run["id"], planned["plan_digest"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["state"] == "RUNNING"


# --------------------------------------------------------------------------
# the open work on the run document
# --------------------------------------------------------------------------

def test_the_open_work_is_read_with_the_tenants_token_and_stored_on_the_run(
    client, db, github, forge_tokens
):
    run = _create(client).json()["run"]
    # The open-work read, then the status comment's write-back: both the
    # caller's own tenant's secret, and no other (test_issue_writeback.py).
    assert set(forge_tokens.asked) == {"swarm-tenant-eng-git"}
    token = forge_tokens.issued["swarm-tenant-eng-git"]
    assert github.calls and all(
        headers["Authorization"] == f"Bearer {token}" for _, headers in github.calls
    )
    work = run["open_work"]
    assert work["repository"] == "saga-xyz/widgets"
    # The planned issue is not its own overlap.
    assert [i["number"] for i in work["issues"]] == [7]
    assert work["pull_requests"] == [{
        "number": 9, "title": "Sort helpers", "files": ["src/widgets/sort.py"],
        "files_truncated": False,
    }]
    assert isinstance(work["read_at"], str)
    stored = db.docs[f"{issueruns.RUNS_COLLECTION}/{run['id']}"]
    assert stored["open_work"]["pull_requests"][0]["number"] == 9
    # And it reaches the planner, between the run's own delimiters.
    prompt = db.docs[f"tasks/{run['planner_task_id']}"]["input"]["prompt"]
    marker = f"=== OPEN WORK {run['id']} ==="
    assert prompt.count(marker) == 2
    section = prompt.split(marker)[1]
    assert "pull request saga-xyz/widgets#9: Sort helpers" in section
    assert "src/widgets/sort.py" in section
    assert "issue saga-xyz/widgets#7" in section
    assert '"overlaps"' in prompt and '"requirements"' in prompt
    assert token not in json.dumps(run) and token not in prompt


@pytest.mark.parametrize(
    "status,suffix,code,http",
    [
        (403, "/issues", "no_access", 403),
        (401, "/pulls", "no_access", 403),
        (404, "/issues", "not_found", 404),
        (404, "/pulls/9/files", "not_found", 404),
        (502, "/pulls", "read_failed", 502),
    ],
)
def test_a_forge_refusal_refuses_the_run_and_creates_nothing(
    client, db, github, status, suffix, code, http
):
    github.status[suffix] = status
    response = _create(client)
    assert response.status_code == http, response.text
    assert response.json()["code"] == code
    assert not _docs(db, "tasks")
    assert not _docs(db, issueruns.RUNS_COLLECTION)


def test_a_tenant_with_no_git_secret_cannot_create_a_run(client, db, forge_tokens):
    forge_tokens.missing.add("swarm-tenant-eng-git")
    response = _create(client)
    assert response.status_code == 409
    assert response.json()["code"] == "no_forge_credential"
    assert not _docs(db, "tasks")
    assert not _docs(db, issueruns.RUNS_COLLECTION)


def test_a_run_stored_before_the_open_work_read_still_loads():
    stored = _stored_run().to_firestore()
    del stored["open_work"]
    loaded = issueruns.IssueRun.from_firestore(stored)
    assert loaded.open_work is None
    assert loaded.to_api()["open_work"] is None


def test_the_open_work_round_trips_through_the_document():
    now = datetime.now(timezone.utc)
    work = {"repository": "saga-xyz/widgets", "read_at": now,
            "issues": [{"number": 7, "title": "t"}], "issues_truncated": True,
            "pull_requests": [], "pull_requests_truncated": False}
    run = _stored_run(open_work=work)
    loaded = issueruns.IssueRun.from_firestore(run.to_firestore())
    assert loaded.open_work == work
    served = loaded.to_api()["open_work"]
    assert served["read_at"] == now.isoformat()
    assert served["issues_truncated"] is True
