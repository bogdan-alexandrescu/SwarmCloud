"""Plans pipeline, lane P5: the reads a decision is made from (docs/design/plans-pipeline.md §2.4, §3.7-§3.10).

What these hold:

  * `plan_risk`: each step's hold categories from its own files and its own
    prompt's paths, with the paths that matched; a sibling step that touches
    none of them gets none; the union of the steps' rows is the run's
    `plan_hold` over the same plan;
  * `expected_cost_usd`: a range from estimates that read as hours, and a
    dash with `why_none` -- never 0 -- for one that does not;
  * `may_decide` on `GET /v1/runs/{id}` is the approval read's for the same
    reader, with `edit` and `comment`;
  * `proposals_7d` counts a schedule's proposals and runs over seven days
    and says `complete: false` past its bound;
  * `proposes` on each schedule type, served on `GET /v1/schedule-types`.

Each assertion names the MUTATION it catches. No credentials, no emulator.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from swarm_api import approvals, issueruns, scheduletypes
from swarm_api.issueruns import RunState

from .conftest import auth_header
from .test_approvals import (  # noqa: F401 -- fixtures, used by name
    _grants,
    _planned,
    api_context,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    writes,
)
from .test_plans_pipeline import _mark, _record, _seed_run
from .test_schedule_tick import seed_schedule

WORKFLOW_PLAN = {
    "summary": "Pin the CI image.",
    "steps": [
        {"step_id": "ci", "title": "Pin the image", "prompt": "Pin the runner image in the workflow.",
         "files": [".github/workflows/x.yml"]},
        {"step_id": "docs", "title": "Say so", "prompt": "Note the pin in docs/ci.md.",
         "files": ["docs/ci.md"]},
    ],
}

MIXED_PLAN = {
    "summary": "Grant the reader role and protect the module.",
    "steps": [
        {"step_id": "grant", "title": "Grant", "prompt": "Add the binding in terraform/infra/iam.tf.",
         "files": ["terraform/infra/iam.tf"]},
        {"step_id": "guard", "title": "Guard", "prompt": "Change src/protected/core.py and its test.",
         "files": ["src/protected/core.py"]},
        {"step_id": "ui", "title": "Wire", "prompt": "Make the header sortable.", "files": ["src/ui/list.py"]},
    ],
}


# --------------------------------------------------------------------------
# plan_risk (§3.7)
# --------------------------------------------------------------------------

def test_a_step_touching_a_workflow_gets_workflows_and_its_path_and_its_sibling_gets_none():
    steps = issueruns.step_holds(WORKFLOW_PLAN, platform=False, hard_stop_paths=(), workflow_files=True)
    # MUTATION: running the stops over the whole plan, not per step.
    assert steps["ci"] == [{"stop": "workflows", "matched": [".github/workflows/x.yml"]}]
    assert steps["docs"] == []


def test_the_union_of_the_steps_rows_is_the_runs_hold_over_the_same_plan():
    registration = {"platform": False, "hard_stop_paths": ("src/protected/**",), "workflow_files": False}
    steps = issueruns.step_holds(MIXED_PLAN, **registration)
    hold = issueruns.plan_hold(MIXED_PLAN, **registration)
    # MUTATION: a step's stop attributed to its sibling, or a stop dropped.
    assert {r["stop"] for r in steps["grant"]} == {"iam"}
    assert steps["guard"] == [{"stop": "protected_path", "matched": ["src/protected/core.py"]}]
    assert steps["ui"] == []
    union_stops = {r["stop"] for rows in steps.values() for r in rows}
    union_matched = {p for rows in steps.values() for r in rows for p in r["matched"]}
    assert union_stops == set(hold["reasons"])
    assert union_matched == set(hold["matched"])


def test_a_steps_matched_paths_are_bounded():
    files = [f"src/protected/f{i}.py" for i in range(issueruns.MAX_HOLD_MATCHED + 5)]
    plan = {"summary": "s", "steps": [{"step_id": "a", "title": "a", "prompt": "p", "files": files}]}
    (row,) = issueruns.step_holds(plan, platform=False, hard_stop_paths=("src/protected/**",))["a"]
    # MUTATION: dropping the bound (MAX_HOLD_MATCHED per step).
    assert len(row["matched"]) == issueruns.MAX_HOLD_MATCHED


# --------------------------------------------------------------------------
# expected_cost_usd (§3.8)
# --------------------------------------------------------------------------

RATES = {"opus": 6.0, "sonnet": 3.0, "haiku": 1.0}


def _plan(*estimates, model_class=None) -> dict:
    steps = []
    for i, estimate in enumerate(estimates):
        step = {"step_id": f"s{i}", "title": "t", "prompt": "p", "estimate": estimate}
        if model_class:
            step["model_class"] = model_class
        steps.append(step)
    return {"summary": "s", "steps": steps}


def test_an_estimate_in_agent_hours_yields_a_range():
    cost = issueruns.expected_cost(_plan("2 agent-hours", "1-2 hours"), RATES, prices_at="2026-10-03T12:00:00+00:00")
    # MUTATION: a figure of 0, or a single point, or the bounds swapped.
    assert cost["low"] is not None and cost["high"] is not None
    assert 0 < cost["low"] < cost["high"]
    review_low, review_high = issueruns.EXPECTED_REVIEW_FIX_HOURS
    default = issueruns.expected_step_class()
    # 3 to 4 agent-hours of steps, plus the review and fix, at the step's class.
    assert cost["low"] == pytest.approx(round((3 + review_low) * RATES[default], 2))
    assert cost["high"] == pytest.approx(round((4 + review_high) * RATES[default], 2))
    assert cost["model_classes"] == {default: 2}
    assert cost["prices_at"] == "2026-10-03T12:00:00+00:00"
    assert "2 steps" in cost["basis"]


def test_an_estimate_in_words_yields_why_none_never_zero():
    cost = issueruns.expected_cost(_plan("2 agent-hours", "a day or two"), RATES, prices_at=None)
    # MUTATION: an unparsed estimate counted as 0 hours.
    assert cost["low"] is None and cost["high"] is None
    assert cost["why_none"] and "a day or two" in cost["why_none"]


def test_a_step_without_an_estimate_yields_why_none():
    cost = issueruns.expected_cost(_plan("2 hours", None), RATES, prices_at=None)
    assert cost["low"] is None and "s1" in cost["why_none"]


def test_a_class_without_a_rate_yields_why_none():
    cost = issueruns.expected_cost(_plan("2 hours"), {}, prices_at=None)
    # MUTATION: an unpriced class priced at 0.
    assert cost["low"] is None and cost["why_none"]


# --------------------------------------------------------------------------
# the run read: plan_risk, expected_cost_usd, may_decide (§3.7-§3.9)
# --------------------------------------------------------------------------

def test_the_run_read_serves_plan_risk_and_expected_cost(client, db, objects):
    run = _planned(client, db, objects)
    # MUTATION: not serving them, or computing them for another digest.
    assert run["plan_risk"]["digest"] == run["plan_digest"]
    assert set(run["plan_risk"]["steps"]) == {s["step_id"] for s in run["plan"]["steps"]}
    assert run["plan_risk"]["run_hold"] == run["approval_hold"]
    assert "expected_cost_usd" in run
    assert ("why_none" in run["expected_cost_usd"]) == (run["expected_cost_usd"]["low"] is None)


def test_may_decide_on_the_run_read_equals_the_approval_reads(client, db, objects):
    run = _planned(client, db, objects)
    for user in ("alice", "dave"):
        from_run = client.get(f"/v1/runs/{run['id']}", headers=auth_header(user)).json()["run"]["may_decide"]
        from_approval = client.get(f"/v1/approvals/run:{run['id']}",
                                   headers=auth_header(user)).json()["approval"]["may_decide"]
        # MUTATION: a second computation on the run read that drifts from the approval's.
        assert from_run == from_approval
        assert {"approve", "reject", "why", "deciders", "edit", "comment"} <= set(from_run)
        assert from_run["edit"] is True and from_run["comment"] is True


def test_a_run_not_waiting_on_a_plan_serves_may_decide_null(client, db, clock):
    _seed_run(db, clock, "run_done", RunState.DONE)
    served = client.get("/v1/runs/run_done", headers=auth_header("alice")).json()["run"]
    # MUTATION: offering a decision on a run with nothing to decide.
    assert served["may_decide"] is None


def test_another_tenants_run_read_is_404(client, db, objects):
    run = _planned(client, db, objects)
    # MUTATION: the new reads computed before the tenant check.
    assert client.get(f"/v1/runs/{run['id']}", headers=auth_header("bob")).status_code == 404


# --------------------------------------------------------------------------
# proposals_7d (§3.10)
# --------------------------------------------------------------------------

def test_proposals_7d_counts_a_schedules_proposals_and_runs(api_context, db, clock):
    seed_schedule(db, schedule_id="sch_000000000009", type_="observer")
    seed_schedule(db, schedule_id="sch_000000000001", type_="issue-sweep")
    subject = {"schedule_id": "sch_000000000009", "firing_id": "f"}
    _record(db, clock, "p1", kind=approvals.PROPOSAL, subject=subject)
    _record(db, clock, "p2", kind=approvals.PROPOSAL, state=approvals.APPROVED, subject=subject,
            decided_ago=timedelta(days=1))
    _record(db, clock, "p3", kind=approvals.PROPOSAL, state=approvals.REJECTED, subject=subject,
            decided_ago=timedelta(days=2))
    _record(db, clock, "p4", kind=approvals.PROPOSAL, state=approvals.EXPIRED, subject=subject,
            decided_ago=timedelta(days=3))
    _record(db, clock, "old", kind=approvals.PROPOSAL, state=approvals.APPROVED, subject=subject,
            decided_ago=timedelta(days=10))
    _record(db, clock, "theirs", kind=approvals.PROPOSAL, subject=subject, tenant_id="research")
    sweep = _mark("issue-sweep")
    _seed_run(db, clock, "run_a", RunState.RUNNING, schedule=sweep, approved_by="alice@example.com")
    _seed_run(db, clock, "run_b", RunState.DONE, schedule=sweep, approved_by=issueruns.AUTO_APPROVER,
              pull_request={"number": 1, "merged": True})
    _seed_run(db, clock, "run_c", RunState.REJECTED, schedule=sweep, rejected_by="alice@example.com")
    _seed_run(db, clock, "run_old", RunState.DONE, schedule=sweep, ago=timedelta(days=9))
    _seed_run(db, clock, "run_x", RunState.DONE, schedule=sweep, tenant_id="research")
    counts = approvals.proposals_by_schedule(api_context, "eng")
    # MUTATION: counting outside the window, or another tenant's rows.
    assert counts["sch_000000000009"] == {
        "proposed": 4, "approved": 1, "auto_approved": 0, "rejected": 1, "expired": 1,
        "running": 0, "merged": 0, "window_days": 7, "complete": True,
    }
    assert counts["sch_000000000001"] == {
        "proposed": 3, "approved": 1, "auto_approved": 1, "rejected": 1, "expired": 0,
        "running": 1, "merged": 1, "window_days": 7, "complete": True,
    }


def test_proposals_7d_is_incomplete_past_its_bound(api_context, db, clock):
    subject = {"schedule_id": "sch_000000000009"}
    for i in range(4):
        _record(db, clock, f"p{i}", kind=approvals.PROPOSAL, subject=subject)
    counts = approvals.proposals_by_schedule(api_context, "eng", limit=3)
    # MUTATION: a full read reported as complete.
    assert counts["sch_000000000009"]["complete"] is False
    assert approvals.proposals_by_schedule(api_context, "eng", limit=10)["sch_000000000009"]["complete"] is True


# --------------------------------------------------------------------------
# proposes (§2.4)
# --------------------------------------------------------------------------

def test_the_schedule_types_that_propose_say_so():
    proposing = {entry.name for entry in scheduletypes.TYPES if entry.proposes}
    # MUTATION: a proposing type left out, or a non-proposing one added.
    assert proposing == {"issue-sweep", "issue-plan-only", "failure-review", "observer", "ci-flake-hunter"}
    assert scheduletypes.describe(scheduletypes.get("observer"))["proposes"] is True
    assert scheduletypes.describe(scheduletypes.get("cost-report"))["proposes"] is False


def test_schedule_types_serve_proposes(client):
    rows = client.get("/v1/schedule-types", headers=auth_header("alice")).json()["types"]
    assert rows and all(isinstance(row["proposes"], bool) for row in rows)
