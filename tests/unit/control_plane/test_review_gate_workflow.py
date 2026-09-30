"""A step's patch is reviewed inside the workflow before anything publishes (#264).

The shape is implement, then review, then fix-if-needed:

    implement ──> review ──> fix (when review's verdict is NOT_YET)

The review stages the implementer's patch through `input_from` and writes a
verdict file. The fix stages that verdict and runs its agent only when the
verdict names it; on MERGE it still runs, without an agent, because it is the
step that publishes. Two step fields make that expressible, and this file is
the contract for what the API accepts, refuses and stores for them:

  * `when: {"step": <upstream>, "verdict_in": [...]}` gates a step's AGENT on
    the verdict file it stages from that upstream.
  * `builds_on: <upstream>` starts a step's checkout from the branch that
    upstream pushed, so the fix sees the implementer's code.

Both reach the worker inside `metadata.dispatch`, which is already reserved,
keyed by upstream TASK id the way `integrates` is. The WORKER half is
tests/unit/worker/test_verdict_gate.py.

No credentials, no network, no emulator: `runner_profile: "mock"`.
"""

from __future__ import annotations

import pytest

from swarm_api.validation import REVIEW_VERDICTS

from .conftest import auth_header

REPO = "https://github.com/saga-xyz/example.git"


def _task_doc(db, task_id: str) -> dict:
    return db.docs[f"tasks/{task_id}"]


def _steps_by_id(workflow: dict) -> dict:
    return {s["step_id"]: s for s in workflow["steps"]}


def _review_shape(**overrides) -> dict:
    """The shape #264 asks for, under `integrate`, with every field it needs."""
    spec = {
        "strategy": "integrate",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            {"step_id": "review", "runner_profile": "mock",
             "depends_on": ["implement"],
             "builds_on": "implement",
             "input_from": {"implement": "swarm-work.patch"},
             "input": {"prompt": "review swarm-work.patch; write verdict.json"}},
            {"step_id": "fix", "runner_profile": "mock",
             "depends_on": ["review"],
             "builds_on": "implement",
             "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": "fix every finding in verdict.json"}},
        ],
    }
    spec.update(overrides)
    return spec


def _post(client, spec):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


# --------------------------------------------------------------------------
# The shape is accepted, and what the worker needs is stored
# --------------------------------------------------------------------------

def test_the_verdicts_are_merge_and_not_yet():
    assert REVIEW_VERDICTS == ("MERGE", "NOT_YET")


def test_the_review_shape_is_accepted_and_the_fix_is_the_one_publisher(client, db):
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    body = response.json()
    # The conditional step is the integrator: the only step that opens a pull
    # request, so whatever the verdict, exactly one step publishes and it is
    # the last one.
    assert body["dispatch"]["integrator_step_id"] == "fix"

    steps = _steps_by_id(body["workflow"])
    implement = _task_doc(db, steps["implement"]["task_id"])
    review = _task_doc(db, steps["review"]["task_id"])
    fix = _task_doc(db, steps["fix"]["task_id"])

    assert fix["metadata"]["dispatch"]["role"] == "integrator"
    # The implementer's branch is merged; the review's is not. A review's
    # deliverable is its verdict: it pushes no branch when it edits nothing,
    # which the integrator would report on the PR as a missing contributor,
    # and its edits, if any, are the unreviewed work the gate keeps out.
    assert fix["metadata"]["dispatch"]["integrates"] == [implement["id"]]
    # The gate names the upstream by TASK id, the way `integrates` does, and
    # the file it reads is the one this step stages from that task.
    assert fix["metadata"]["dispatch"]["verdict_gate"] == {
        "task_id": review["id"],
        "verdict_in": ["NOT_YET"],
    }
    assert fix["metadata"]["input_from"] == {review["id"]: "verdict.json"}
    # Both later steps start from the implementer's branch.
    assert fix["metadata"]["dispatch"]["builds_on"] == implement["id"]
    assert review["metadata"]["dispatch"]["builds_on"] == implement["id"]
    # The review stages the implementer's patch, so the implementer is told to
    # upload it -- the platform writes it, and the end-of-attempt check makes
    # its absence a retryable failure rather than a dead review.
    assert implement["metadata"]["expected_outputs"] == ["swarm-work.patch"]
    assert review["metadata"]["expected_outputs"] == ["verdict.json"]


def test_a_step_without_either_field_stores_the_dispatch_block_it_always_did(client, db):
    """No new key appears on a workflow that does not ask for one."""
    response = _post(client, {
        "strategy": "integrate",
        "repository_url": REPO,
        "steps": [
            {"step_id": "a", "runner_profile": "mock"},
            {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]},
        ],
    })
    assert response.status_code == 201, response.text
    for step in response.json()["workflow"]["steps"]:
        block = _task_doc(db, step["task_id"])["metadata"]["dispatch"]
        assert "verdict_gate" not in block
        assert "builds_on" not in block


def test_the_task_read_back_reports_the_gate_and_the_base(client, db):
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    steps = _steps_by_id(response.json()["workflow"])
    fix_id = steps["fix"]["task_id"]
    review_id = steps["review"]["task_id"]
    implement_id = steps["implement"]["task_id"]

    read = client.get(f"/v1/tasks/{fix_id}", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    dispatch = read.json()["task"]["dispatch"]
    assert dispatch["verdict_gate"] == {"task_id": review_id, "verdict_in": ["NOT_YET"]}
    assert dispatch["builds_on"] == implement_id

    plain = client.get(f"/v1/tasks/{implement_id}", headers=auth_header("alice"))
    assert "verdict_gate" not in plain.json()["task"]["dispatch"]
    assert "builds_on" not in plain.json()["task"]["dispatch"]


def test_a_gate_under_collect_is_accepted_with_no_repository(client, db):
    """`collect` publishes nothing, so gating an agent needs no push at all."""
    response = _post(client, {
        "steps": [
            {"step_id": "review", "runner_profile": "mock"},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
             "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET", "MERGE"]}},
        ],
    })
    assert response.status_code == 201, response.text
    steps = _steps_by_id(response.json()["workflow"])
    fix = _task_doc(db, steps["fix"]["task_id"])
    assert fix["metadata"]["dispatch"]["verdict_gate"]["verdict_in"] == ["NOT_YET", "MERGE"]


# --------------------------------------------------------------------------
# A gate that could not work is refused before anything is created
# --------------------------------------------------------------------------

def _assert_refused(client, db, spec, code: str) -> dict:
    before = {k for k in db.docs if k.startswith("tasks/")}
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == code, body
    assert {k for k in db.docs if k.startswith("tasks/")} == before, "a refusal created tasks"
    return body


def test_a_gate_on_a_step_it_does_not_stage_a_verdict_from_is_refused(client, db):
    spec = _review_shape()
    spec["steps"][2]["input_from"] = {}
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "fix"
    assert body["detail"]["when"] == "review"


def test_an_unknown_verdict_is_refused_naming_the_accepted_ones(client, db):
    spec = _review_shape()
    spec["steps"][2]["when"] = {"step": "review", "verdict_in": ["LGTM"]}
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["accepted_verdicts"] == list(REVIEW_VERDICTS)


def test_an_empty_verdict_list_is_refused(client, db):
    spec = _review_shape()
    spec["steps"][2]["when"] = {"step": "review", "verdict_in": []}
    response = _post(client, spec)
    assert response.status_code == 422, response.text


def test_a_gate_under_direct_pr_is_refused_because_the_implementer_would_publish_first(
    client, db
):
    """Under `direct-pr` every step opens its own pull request, the implementer's
    included, so the review would come after the publish it exists to prevent."""
    spec = _review_shape(strategy="direct-pr")
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "fix"
    assert body["detail"]["strategy"] == "direct-pr"


def test_a_gate_under_integrate_must_be_on_the_integrator(client, db):
    """The integrator's pull request is where the verdict is shown; a gate on
    any other step would route work that PR says nothing about."""
    spec = _review_shape()
    spec["steps"].append(
        {"step_id": "ship", "runner_profile": "mock", "depends_on": ["fix"]}
    )
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "fix"
    assert body["detail"]["integrator_step_id"] == "ship"


def test_a_gated_step_cannot_promise_a_file_to_a_later_step(client, db):
    """A gated step whose agent does not run writes nothing, so a dependant
    staging from it would fail every time the gate stays shut."""
    spec = {
        "steps": [
            {"step_id": "review", "runner_profile": "mock"},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
             "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]}},
            {"step_id": "after", "runner_profile": "mock", "depends_on": ["fix"],
             "input_from": {"fix": "notes.md"}},
        ],
    }
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "fix"
    assert body["detail"]["staged_by"] == ["after"]


# --------------------------------------------------------------------------
# `builds_on`
# --------------------------------------------------------------------------

def test_building_on_a_step_that_is_not_upstream_is_refused(client, db):
    spec = _review_shape()
    spec["steps"][0]["builds_on"] = "fix"
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["step_id"] == "implement"
    assert body["detail"]["builds_on"] == "fix"


def test_building_on_itself_is_refused(client, db):
    spec = _review_shape()
    spec["steps"][1]["builds_on"] = "review"
    body = _assert_refused(client, db, spec, "invalid_dag")
    assert body["detail"]["builds_on"] == "review"


def test_building_on_a_transitive_upstream_is_accepted(client, db):
    """fix depends on review alone and builds on implement, review's parent."""
    assert _post(client, _review_shape()).status_code == 201


@pytest.mark.parametrize("strategy", ["collect"])
def test_building_on_a_branch_nobody_pushes_is_refused(client, db, strategy):
    """`collect` pushes nothing, so there would be no branch to start from."""
    spec = {
        "strategy": strategy,
        "repository_url": REPO,
        "steps": [
            {"step_id": "a", "runner_profile": "mock"},
            {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"],
             "builds_on": "a"},
        ],
    }
    body = _assert_refused(client, db, spec, "invalid_dispatch")
    assert body["detail"]["step_id"] == "b"
    assert body["detail"]["strategy"] == strategy
