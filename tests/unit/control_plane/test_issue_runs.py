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

import hashlib
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
    # Absent: the run takes the platform's `merge_by_default` (contract request 47).
    assert body.auto_merge is None
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


def test_auto_merge_is_accepted_and_recorded_on_the_run(client, db):
    """Contract request 47 enabled the merge step: the "refused until #295"
    state is gone, and the run records the choice its workflow will carry."""
    response = _create(client, auto_merge=True)
    assert response.status_code == 201, response.text
    assert response.json()["run"]["auto_merge"] is True


@pytest.mark.parametrize("default", [False, True])
def test_a_run_that_does_not_say_takes_the_platform_default(client, default):
    put = client.put("/v1/admin/settings", headers=auth_header("root"),
                     json={"merge_by_default": default})
    assert put.status_code == 200, put.text
    response = _create(client)
    assert response.status_code == 201, response.text
    assert response.json()["run"]["auto_merge"] is default
    # A run that says overrides the default either way.
    assert _create(client, auto_merge=not default).json()["run"]["auto_merge"] is (not default)


def test_auto_merge_availability_says_what_the_refusal_does(monkeypatch):
    # The console reads availability; POST /v1/runs enforces the refusal.
    # They must never disagree: unavailable exactly while the refusal raises.
    availability = issueruns.auto_merge_availability()
    assert availability["available"] is True and availability["reason"] is None
    issueruns.refuse_auto_merge(True)

    from dataclasses import replace

    from swarm_common.profiles import RUNNER_PROFILES

    monkeypatch.setitem(RUNNER_PROFILES, "merge", replace(
        RUNNER_PROFILES["merge"], available=False, disabled_reason="switched off for a drill"))
    availability = issueruns.auto_merge_availability(default=True)
    assert availability["available"] is False and availability["default"] is False
    assert availability["requires"] == "#295"
    with pytest.raises(issueruns.AutoMergeUnavailable) as refused:
        issueruns.refuse_auto_merge(True)
    assert availability["reason"] == refused.value.message
    assert "switched off for a drill" in refused.value.message


def test_a_caller_cannot_pick_the_planners_image_or_profile(client):
    response = _create(client, runner_profile="generic")
    assert response.status_code == 422


# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------

def test_the_machine_is_the_one_454_names():
    # NOT_READY: the planner's verdict instead of a plan (the issue sweeper,
    # owner decisions 2026-10-08).
    assert RUN_TRANSITIONS[RunState.PLANNING] == {
        RunState.PLANNED, RunState.NOT_READY, RunState.FAILED, RunState.CANCELLED,
    }
    # FAILED: an `auto` run whose creator left the tenant before the tick approved it.
    assert RUN_TRANSITIONS[RunState.PLANNED] == {
        RunState.PLANNED, RunState.APPROVED, RunState.REJECTED, RunState.CANCELLED, RunState.FAILED,
    }
    assert RUN_TRANSITIONS[RunState.APPROVED] == {RunState.RUNNING, RunState.FAILED}
    # The workflow succeeding opens a pull request; CI decides DONE (issueci).
    # DONE from RUNNING: the build changed nothing, already on main (#646).
    assert RUN_TRANSITIONS[RunState.RUNNING] == {
        RunState.CHECKING, RunState.DONE, RunState.FAILED, RunState.CANCELLED,
    }
    assert RUN_TRANSITIONS[RunState.CHECKING] == {
        RunState.FIXING, RunState.DONE, RunState.FAILED, RunState.CANCELLED,
    }
    assert RUN_TRANSITIONS[RunState.FIXING] == {RunState.CHECKING, RunState.FAILED, RunState.CANCELLED}
    assert TERMINAL_RUN_STATES == {
        RunState.DONE, RunState.FAILED, RunState.REJECTED, RunState.CANCELLED, RunState.NOT_READY,
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


@pytest.mark.parametrize("auto_merge", [True, False])
def test_the_compiled_workflow_always_says_merge_off(auto_merge):
    """Stated, so the platform default cannot append a merge inside the
    compiled workflow: that merge would run before the CI loop wrote the
    `Closes #N` block or fixed a red check. An `auto_merge` run merges from
    the CI loop instead (test_issue_run_auto_merge.py)."""
    spec = compile_plan(_stored_run(auto_merge=auto_merge))
    assert spec.metadata["merge"] == "off"
    assert [s.step_id for s in spec.steps] == ["impl-sort-key", "impl-ui", "review", "fix"]


def test_an_auto_merge_run_submits_its_workflow_with_no_merge_step(client):
    client.put("/v1/admin/settings", headers=auth_header("root"), json={"merge_by_default": True})
    spec = compile_plan(_stored_run(auto_merge=True))
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=spec.model_dump(exclude_none=True))
    assert response.status_code == 201, response.text
    assert not [s for s in response.json()["workflow"]["steps"] if s["runner_profile"] == "merge"]


def test_a_run_without_auto_merge_submits_no_merge_step_even_when_the_default_is_on(client):
    client.put("/v1/admin/settings", headers=auth_header("root"), json={"merge_by_default": True})
    spec = compile_plan(_stored_run(auto_merge=False))
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=spec.model_dump(exclude_none=True))
    assert response.status_code == 201, response.text
    assert not [s for s in response.json()["workflow"]["steps"] if s["runner_profile"] == "merge"]


def test_the_digest_is_of_the_plan_and_order_of_keys_does_not_change_it():
    reordered = {"steps": PLAN["steps"], "summary": PLAN["summary"]}
    assert plan_digest(reordered) == plan_digest(PLAN)
    assert plan_digest({**PLAN, "summary": "other"}) != plan_digest(PLAN)
    assert plan_digest(PLAN).startswith("sha256:")


# --------------------------------------------------------------------------
# parallel stages: the plan's `depends_on`
# --------------------------------------------------------------------------

#: Two independent roots, a step on one of them, and a step that joins both:
#: stages {api, ui} then {docs, wire}.
STAGED_PLAN = {
    "summary": "Expose widget sorting through the API and the UI.",
    "steps": [
        {"step_id": "api", "title": "Sort in the API", "prompt": "Add ?sort=name to GET /widgets."},
        {"step_id": "ui", "title": "Sort header", "prompt": "Add a sortable Name header."},
        {"step_id": "docs", "title": "Document it", "prompt": "Document ?sort.",
         "depends_on": ["api"]},
        {"step_id": "wire", "title": "Wire UI to API", "prompt": "Call ?sort from the header.",
         "depends_on": ["api", "ui"]},
    ],
}

#: What `compile_plan` emitted for PLAN on 2026-10-03, before `depends_on`
#: existed, with the closing-keyword rule and the (empty) requirements list
#: every compiled prompt now carries. A plan with no `depends_on` anywhere must
#: compile to exactly this chain. Since #646 each build step also allows an
#: empty diff and asks, when it changes nothing, for the verification table.
VERIFICATION_ASK = (
    "\n\nIf the default branch already does everything this step asks, changing nothing "
    "is a correct outcome: change no file, and write $SWARM_ARTIFACTS_DIR/verification.md, "
    "a Markdown table with one row, numbered 1, for this step (the plan lists no numbered "
    "requirements):\n"
    "| # | Met on main | Where (file, function) | Proving test |\n"
    "|---|---|---|---|\n"
    "| 1 | yes | path/to/module.py, function_name | tests/path/test_module.py::test_name |\n"
    "Met on main is yes only when the default branch delivers that requirement in full "
    "and a test proves it; otherwise no, saying what is missing.\n"
)
CHAIN_AS_BEFORE = {
    "steps": [
        {
            "step_id": "impl-sort-key",
            "runner_profile": "claude-code",
            "input": {
                "prompt": (
                    "You are doing step 1 of 2 of the approved plan for GitHub issue "
                    "saga-xyz/widgets#42 (https://github.com/saga-xyz/widgets/issues/42); the "
                    "issue file named below holds the issue.\n\nThe plan: Make the widget list "
                    "sortable by name.\n\nThis step -- Add a sort key:\nAdd a name sort key to "
                    "WidgetList.\n\nDo this step only. " + issueruns.NO_CLOSING_KEYWORD
                    + VERIFICATION_ASK
                ),
                "issue": 42,
            },
            "allow_empty_diff": True,
        },
        {
            "step_id": "impl-ui",
            "runner_profile": "claude-code",
            "input": {
                "prompt": (
                    "You are doing step 2 of 2 of the approved plan for GitHub issue "
                    "saga-xyz/widgets#42 (https://github.com/saga-xyz/widgets/issues/42); the "
                    "issue file named below holds the issue.\n\nThe plan: Make the widget list "
                    "sortable by name.\n\nThis step -- Wire the header:\nMake the Name header "
                    "toggle the sort.\n\nThe earlier steps' work is already on this branch. "
                    "Do this step only. " + issueruns.NO_CLOSING_KEYWORD
                    + VERIFICATION_ASK
                ),
                "issue": 42,
            },
            "allow_empty_diff": True,
            "depends_on": ["impl-sort-key"],
            "builds_on": "impl-sort-key",
        },
        {
            "step_id": "review",
            "runner_profile": "claude-code",
            "input": {
                "issue": 42,
                "prompt": (
                    "Review the change on this branch for GitHub issue saga-xyz/widgets#42 "
                    "against the approved plan: Make the widget list sortable by name.\n\n"
                    "swarm-work.patch holds the last step's diff; the whole change is this "
                    "branch against the default branch. Do not edit files. Write "
                    "$SWARM_ARTIFACTS_DIR/verdict.json: {\"verdict\": \"MERGE\" or \"NOT_YET\", "
                    "\"findings\": [\"one blocker per entry\"], \"requirements\": []}. "
                    + issueruns.NO_CLOSING_KEYWORD
                ),
            },
            "depends_on": ["impl-ui"],
            "input_from": {"impl-ui": "swarm-work.patch"},
            "builds_on": "impl-ui",
        },
        {
            "step_id": "fix",
            "runner_profile": "claude-code",
            "input": {
                "issue": 42,
                "prompt": (
                    "Fix every finding in verdict.json for GitHub issue saga-xyz/widgets#42. "
                    "Change nothing else. " + issueruns.NO_CLOSING_KEYWORD
                ),
            },
            "depends_on": ["review"],
            "input_from": {"review": "verdict.json"},
            "when": {"step": "review", "verdict_in": ["NOT_YET"]},
            "builds_on": "impl-ui",
        },
    ],
    "metadata": {
        "issue_run": {
            "run_id": "run_x",
            "issue": "saga-xyz/widgets#42",
            "plan_digest": plan_digest(PLAN),
            "fix_rounds": 3,
            "review_rounds_compiled": 1,
        }
    },
    "strategy": "integrate",
    "repository_url": "https://github.com/saga-xyz/widgets",
}


def _with_steps(*steps) -> dict:
    return {"summary": "x", "steps": [
        {"step_id": sid, "title": "t", "prompt": "p",
         **({"depends_on": deps} if deps is not None else {})}
        for sid, deps in steps
    ]}


def test_a_plan_without_depends_on_compiles_to_the_chain_byte_for_byte():
    # The plan reads back exactly as written -- no `depends_on: null` appears in
    # it -- so the digest a person approved before this change is unchanged.
    assert issueruns.parse_plan(PLAN) == PLAN
    assert plan_digest(issueruns.parse_plan(PLAN)) == plan_digest(PLAN)
    spec = compile_plan(_stored_run())
    dumped = spec.model_dump(mode="json", exclude_defaults=True)
    # The one addition (contract request 47): the run's merge choice, stated.
    assert dumped["metadata"].pop("merge") == "off"
    assert dumped == CHAIN_AS_BEFORE


def test_dependencies_compile_to_parallel_stages():
    spec = compile_plan(_stored_run(plan=STAGED_PLAN, plan_digest=plan_digest(STAGED_PLAN)))
    by_id = {s.step_id: s for s in spec.steps}
    assert list(by_id) == ["impl-api", "impl-ui", "impl-docs", "impl-wire", "review", "fix"]
    # The roots start at once, from the default branch.
    for root in ("impl-api", "impl-ui"):
        assert by_id[root].depends_on == [] and by_id[root].builds_on is None
        assert "already on this branch" not in by_id[root].input["prompt"]
    # One dependency: built on it, as in the chain.
    assert by_id["impl-docs"].depends_on == ["impl-api"]
    assert by_id["impl-docs"].builds_on == "impl-api"
    # A join: built on its last dependency, the other's diff staged by parent.
    assert by_id["impl-wire"].depends_on == ["impl-api", "impl-ui"]
    assert by_id["impl-wire"].builds_on == "impl-ui"
    assert by_id["impl-wire"].input_from == {"impl-api": "swarm-work.patch"}
    assert by_id["impl-wire"].metadata == {"input_layout": "by_parent"}
    assert "impl-api/swarm-work.patch" in by_id["impl-wire"].input["prompt"]
    # Signed fields that do not depend on the shape are unchanged.
    assert spec.strategy == "integrate"
    assert all(s.runner_profile == "claude-code" for s in spec.steps)
    assert spec.metadata["issue_run"]["plan_digest"] == plan_digest(STAGED_PLAN)
    assert issueruns.plan_stages(STAGED_PLAN) == [["api", "ui"], ["docs", "wire"]]


def test_the_review_depends_on_every_implementation_step_and_the_fix_stays_gated():
    spec = compile_plan(_stored_run(plan=STAGED_PLAN, plan_digest=plan_digest(STAGED_PLAN)))
    by_id = {s.step_id: s for s in spec.steps}
    impl = ["impl-api", "impl-ui", "impl-docs", "impl-wire"]
    assert by_id["review"].depends_on == impl
    assert by_id["review"].input_from == {sid: "swarm-work.patch" for sid in impl}
    assert by_id["review"].metadata == {"input_layout": "by_parent"}
    assert by_id["review"].builds_on == "impl-wire"
    for sid in impl:
        assert f"{sid}/swarm-work.patch" in by_id["review"].input["prompt"]
    assert by_id["fix"].depends_on == ["review"]
    assert by_id["fix"].when.step == "review" and by_id["fix"].when.verdict_in == ["NOT_YET"]
    assert by_id["fix"].input_from == {"review": "verdict.json"}
    assert by_id["fix"].builds_on == "impl-wire"


def test_a_staged_plan_is_accepted_by_the_workflow_validator_on_approval(client, db, objects):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, STAGED_PLAN)
    read = _run(client, run["id"]).json()["run"]
    assert read["state"] == "PLANNED", read
    assert read["plan_shape"] == "4 steps in 2 stages (2 → 2), then review and fix"
    approved = _approve(client, run["id"], read["plan_digest"])
    assert approved.status_code == 200, approved.text
    workflow = _docs(db, "workflows")[f"workflows/{approved.json()['run']['workflow_id']}"]
    assert [s["step_id"] for s in workflow["steps"]] == [
        "impl-api", "impl-ui", "impl-docs", "impl-wire", "review", "fix",
    ]


def test_the_shape_names_steps_and_stages():
    assert issueruns.plan_shape(PLAN) == "2 steps in 2 stages, then review and fix"
    assert issueruns.plan_shape(STAGED_PLAN) == "4 steps in 2 stages (2 → 2), then review and fix"
    assert issueruns.plan_shape(_with_steps(("a", None))) == "1 step in 1 stage, then review and fix"
    assert issueruns.plan_shape(None) is None
    assert issueruns.plan_shape({"summary": "x"}) is None
    assert _stored_run(plan=STAGED_PLAN).to_api()["plan_shape"] == (
        "4 steps in 2 stages (2 → 2), then review and fix"
    )


def test_an_empty_depends_on_starts_at_once():
    plan = _with_steps(("a", []), ("b", None))
    assert issueruns.plan_stages(plan) == [["a", "b"]]
    spec = compile_plan(_stored_run(plan=plan, plan_digest=plan_digest(plan)))
    assert [s.depends_on for s in spec.steps[:2]] == [[], []]


@pytest.mark.parametrize(
    "plan,why",
    [
        (_with_steps(("a", None), ("b", ["nope"])), "'nope', which is not a step in this plan"),
        (_with_steps(("a", ["b"]), ("b", None)), "'b', a later step"),
        (_with_steps(("a", ["a"])), "cycle: a -> a"),
        (_with_steps(("a", ["b"]), ("b", ["a"])), "cycle: a -> b -> a"),
        (_with_steps(("a", None), ("b", ["a", "a"])), "names 'a' twice"),
    ],
)
def test_a_bad_depends_on_is_refused_with_a_plan_error(client, db, objects, plan, why):
    with pytest.raises(issueruns.InvalidPlan, match="depends_on") as caught:
        issueruns.parse_plan(plan)
    assert why in str(caught.value)
    # The run page shows it: the run fails with the same reason, nothing submitted.
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, plan)
    read = _run(client, run["id"]).json()["run"]
    assert read["state"] == "FAILED"
    assert why in read["error"]
    assert not _docs(db, "workflows")


def test_parallel_width_is_capped_at_the_workflow_validators_limit(monkeypatch):
    # The review depends on every implementation step, so its fan-in -- and any
    # stage's width -- must fit `WorkflowStepCreate.depends_on`.
    from swarm_api.schemas import WorkflowStepCreate

    limit = next(
        m.max_length for m in WorkflowStepCreate.model_fields["depends_on"].metadata
        if getattr(m, "max_length", None) is not None
    )
    assert issueruns.MAX_PARALLEL_STEPS == limit
    monkeypatch.setattr(issueruns, "MAX_PARALLEL_STEPS", 2)
    issueruns.parse_plan(_with_steps(("a", []), ("b", []), ("c", ["a"])))
    with pytest.raises(issueruns.InvalidPlan, match="3 steps in parallel"):
        issueruns.parse_plan(_with_steps(("a", []), ("b", []), ("c", [])))


def test_the_planner_prompt_asks_for_real_dependencies_and_one_line_per_file():
    prompt = issueruns.planner_prompt(parse_issue_ref(REF))
    assert '"depends_on"' in prompt
    assert "SAME file" in prompt
    assert "one dependency line" in prompt
    assert "earlier" in prompt


def test_the_planner_prompt_keeps_a_join_from_editing_what_its_dependencies_wrote():
    # A join re-applies its non-base dependencies' diffs as its own commit; an
    # edit to those lines conflicts when the integrator merges the dependency's
    # branch, so the prompt routes a step that CHANGES another's code onto its line.
    prompt = issueruns.planner_prompt(parse_issue_ref(REF))
    assert "only ADD code that uses what those other steps wrote, never change it" in prompt
    assert "must CHANGE code another step wrote lists that step as its last dependency" in prompt
    assert 'If you state \"depends_on\" on any step, state it on every step' in prompt
    assert "THE JOIN'S LIMIT" in (issueruns._compile_staged.__doc__ or "")


def test_the_staged_review_is_told_a_join_carries_its_dependencies_diffs():
    spec = compile_plan(_stored_run(plan=STAGED_PLAN, plan_digest=plan_digest(STAGED_PLAN)))
    review = next(s for s in spec.steps if s.step_id == "review")
    assert "also carries, in its own diff, the diffs of the dependencies it applied" in (
        review.input["prompt"]
    )


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
    "overlaps": [{"ref": "saga-xyz/widgets#9", "kind": "pull_request", "action": "required",
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


# --------------------------------------------------------------------------
# #587: an overlap says whether anyone must act on it, as a field
# --------------------------------------------------------------------------

#: A plan as stored before #587: its overlaps carry ref, kind and note, and no
#: `action`. Its digest is the one it was stored (and approved) with.
LEGACY_PLAN = {
    **{k: v for k, v in FULL_PLAN.items() if k != "overlaps"},
    "overlaps": [
        {"ref": "saga-xyz/widgets#9", "kind": "pull_request",
         "note": "PR #9 adds sort helpers; step 1 builds on them."},
        {"ref": "saga-xyz/widgets#7", "kind": "issue",
         "note": "Edits the list's styles. No action: different file."},
    ],
}
#: The digest the legacy plan was stored with, computed HERE from its
#: canonical bytes (sorted keys, no spaces, UTF-8) rather than by calling
#: `plan_digest`: a change to the canonicalisation, or an `action` default the
#: model dumped into an old plan, would no longer match it.
LEGACY_DIGEST = "sha256:" + hashlib.sha256(
    json.dumps(LEGACY_PLAN, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    .encode("utf-8")
).hexdigest()


@pytest.mark.parametrize("missing", [{}, {"action": None}])
def test_a_planner_plan_whose_overlap_has_no_action_is_refused_naming_it(missing):
    overlap = {"ref": "saga-xyz/widgets#9", "kind": "pull_request", "note": "n", **missing}
    with pytest.raises(issueruns.InvalidPlan) as refused:
        issueruns.parse_plan({**FULL_PLAN, "overlaps": [overlap]})
    assert "overlaps.0" in refused.value.message
    assert "saga-xyz/widgets#9" in refused.value.message
    assert "action" in refused.value.message


@pytest.mark.parametrize("action", ["maybe", "", "None"])
def test_an_overlap_action_is_none_or_required(action):
    overlap = {**FULL_PLAN["overlaps"][0], "action": action}
    with pytest.raises(issueruns.InvalidPlan) as refused:
        issueruns.parse_plan({**FULL_PLAN, "overlaps": [overlap]})
    assert "action" in refused.value.message


def test_a_planner_plan_missing_an_overlap_action_fails_the_run(client, db, objects):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, LEGACY_PLAN)
    read = _run(client, run["id"]).json()["run"]
    assert read["state"] == "FAILED"
    assert "saga-xyz/widgets#9" in read["error"] and "action" in read["error"]
    assert not _docs(db, "workflows")


def test_an_overlap_action_round_trips_and_is_in_the_digest():
    none = {**FULL_PLAN, "overlaps": [{**FULL_PLAN["overlaps"][0], "action": "none"}]}
    assert issueruns.parse_plan(none) == none
    assert plan_digest(issueruns.parse_plan(none)) != plan_digest(issueruns.parse_plan(FULL_PLAN))


def test_a_stored_legacy_plan_reads_unchanged_and_its_digest_still_matches():
    stored = issueruns.parse_plan(LEGACY_PLAN, stored=True)
    assert stored == LEGACY_PLAN
    # The key, not the word: the second note says "No action" in its prose.
    assert all("action" not in overlap for overlap in stored["overlaps"])
    assert '"action":' not in json.dumps(stored)
    assert plan_digest(stored) == plan_digest(LEGACY_PLAN) == LEGACY_DIGEST
    assert issueruns.plan_shape(LEGACY_PLAN) is not None
    spec = compile_plan(_stored_run(plan=LEGACY_PLAN, plan_digest=LEGACY_DIGEST,
                                    approved_digest=LEGACY_DIGEST))
    assert [s.step_id for s in spec.steps][-2:] == ["review", "fix"]


def test_a_stored_legacy_plan_is_served_and_approves_against_its_stored_digest(
    client, db, objects
):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, FULL_PLAN)
    assert _run(client, run["id"]).json()["run"]["state"] == "PLANNED"
    # The run as it was stored before #587: the plan and its digest, untouched.
    stored = db.docs[f"{issueruns.RUNS_COLLECTION}/{run['id']}"]
    stored["plan"] = json.loads(json.dumps(LEGACY_PLAN))
    stored["plan_digest"] = LEGACY_DIGEST
    planned = _run(client, run["id"]).json()["run"]
    assert planned["state"] == "PLANNED"
    assert planned["plan"] == LEGACY_PLAN
    assert planned["plan_digest"] == LEGACY_DIGEST == plan_digest(planned["plan"])
    assert planned["plan_shape"] is not None
    approved = _approve(client, run["id"], LEGACY_DIGEST)
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["state"] == "RUNNING"
    assert approved.json()["run"]["approved_digest"] == LEGACY_DIGEST


def _legacy_planned(client, db, objects):
    """A PLANNED run whose stored plan and digest predate `PlanOverlap.action`."""
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, FULL_PLAN)
    assert _run(client, run["id"]).json()["run"]["state"] == "PLANNED"
    stored = db.docs[f"{issueruns.RUNS_COLLECTION}/{run['id']}"]
    stored["plan"] = json.loads(json.dumps(LEGACY_PLAN))
    stored["plan_digest"] = LEGACY_DIGEST
    return run


def test_a_legacy_plan_edited_only_in_its_summary_is_accepted(client, db, objects):
    # The console's editor sends the whole plan back, legacy overlaps included.
    run = _legacy_planned(client, db, objects)
    edited = {**json.loads(json.dumps(LEGACY_PLAN)), "summary": "Sort the widget list by name."}
    response = _edit(client, run["id"], LEGACY_DIGEST, edited)
    assert response.status_code == 200, response.text
    read = response.json()["run"]
    assert read["plan"] == edited
    assert read["plan_digest"] == plan_digest(edited) != LEGACY_DIGEST


@pytest.mark.parametrize("change", [
    {"note": "A note the stored plan never had."},
    {"ref": "saga-xyz/widgets#8"},
    {"kind": "issue"},
])
def test_a_legacy_plan_edit_that_changes_an_overlap_must_give_it_an_action(
    client, db, objects, change
):
    run = _legacy_planned(client, db, objects)
    edited = json.loads(json.dumps(LEGACY_PLAN))
    edited["overlaps"][0] = {**edited["overlaps"][0], **change}
    response = _edit(client, run["id"], LEGACY_DIGEST, edited)
    assert response.status_code == 422, response.text
    assert "overlaps.0" in response.text and "action" in response.text
    # With an action, the same change is accepted.
    edited["overlaps"][0]["action"] = "required"
    assert _edit(client, run["id"], LEGACY_DIGEST, edited).status_code == 200


def test_an_edit_of_a_new_plan_may_not_drop_an_overlap_action(client, db, objects):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, FULL_PLAN)
    planned = _run(client, run["id"]).json()["run"]
    edited = json.loads(json.dumps(FULL_PLAN))
    del edited["overlaps"][0]["action"]
    response = _edit(client, run["id"], planned["plan_digest"], edited)
    assert response.status_code == 422, response.text
    assert "saga-xyz/widgets#9" in response.text


def test_the_planner_is_told_every_overlap_needs_an_action():
    prompt = issueruns.planner_prompt(parse_issue_ref(REF), run_id="run_x", open_work={})
    assert '"action": "none" or "required"' in prompt
    assert '"action" is mandatory' in prompt
    assert '"none" when this plan does nothing about it' in prompt
    assert '"required" when this plan or a person must act' in prompt


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


def test_a_join_stages_every_ancestor_its_base_branch_does_not_carry(client, db, objects):
    # d joins c (its base: the later of its dependencies) and b; b built on a,
    # so both a's and b's diffs are staged, in plan order, and d depends on both.
    plan = _with_steps(("a", []), ("b", ["a"]), ("c", []), ("d", ["c", "b"]))
    spec = compile_plan(_stored_run(plan=plan, plan_digest=plan_digest(plan)))
    d = {s.step_id: s for s in spec.steps}["impl-d"]
    assert d.builds_on == "impl-c"
    assert d.input_from == {"impl-a": "swarm-work.patch", "impl-b": "swarm-work.patch"}
    assert set(d.depends_on) == {"impl-a", "impl-b", "impl-c"}
    prompt = d.input["prompt"]
    assert prompt.index("impl-a/swarm-work.patch") < prompt.index("impl-b/swarm-work.patch")
    assert issueruns.plan_shape(plan) == "4 steps in 3 stages (2 → 1 → 1), then review and fix"
    # And the workflow validator accepts the shape.
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, plan)
    read = _run(client, run["id"]).json()["run"]
    assert _approve(client, run["id"], read["plan_digest"]).status_code == 200



@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)


def test_a_plan_approval_refused_by_the_workspace_gate_leaves_the_run_planned(
        api_context, client, db, objects):
    # docs/workspaces.md §5.3 (#847 W1): the gate is asked before the claim, so
    # a person whose workspace is not ready is refused and can approve again
    # once it is -- not left with a FAILED run.
    run = _planned(client, db, objects, user="carol")
    api_context.submissions.workspaces.gate = True
    db.docs["workspaces/u-carol"] = {"tenant_id": "u-carol", "workspace_id": "w-3f9a2c",
                                     "state": "requested", "request_id": "r"}
    response = _approve(client, run["id"], run["plan_digest"], user="carol")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "WORKSPACE_NOT_READY"
    assert _run(client, run["id"], "carol").json()["run"]["state"] == "PLANNED"
    assert not _docs(db, "workflows")
