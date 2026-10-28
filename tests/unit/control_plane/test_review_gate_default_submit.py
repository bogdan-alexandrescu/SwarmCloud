"""Every review step is gated by default (#2524, BGD 2026-10-28: "every review step").

docs/workflows.md "The review gate". A review step -- one another step's
`when` reads a verdict from -- that names no `metadata.review_gate` is
stored with the platform's default block, which the scheduler holds and
swarm-api resolves against the workspace's setting
(test_review_gate_default.py). A step's own block wins, and `false`
switches the gate off for that step. An issue run's requirements check is
never merged by the integrator: it is read-only.

No credentials, no network, no emulator: `runner_profile: "mock"`.
"""

from __future__ import annotations

from scheduler import loop
from swarm_api import validation, verdictpublish

from .conftest import auth_header

REPO = "https://github.com/example-org/example.git"


def _shape(review_metadata: dict | None = None, *, check: bool = False) -> dict:
    review = {"step_id": "review", "runner_profile": "mock",
              "depends_on": ["implement"], "builds_on": "implement",
              "input_from": {"implement": "swarm-work.patch"},
              "input": {"prompt": "review swarm-work.patch; write verdict.json"}}
    if review_metadata is not None:
        review["metadata"] = review_metadata
    steps = [{"step_id": "implement", "runner_profile": "mock",
              "input": {"prompt": "implement the change"}}]
    if check:
        steps.append({"step_id": "requirements-check", "runner_profile": "mock",
                      "depends_on": ["implement"], "builds_on": "implement",
                      "allow_empty_diff": True,
                      "metadata": {"review_gate": {"requirements_check": True}},
                      "input": {"prompt": "write requirements.json"}})
        review["depends_on"] = ["implement", "requirements-check"]
    steps += [review, {
        "step_id": "fix", "runner_profile": "mock",
        "depends_on": ["review"], "builds_on": "implement",
        "input_from": {"review": "verdict.json"},
        "when": {"step": "review", "verdict_in": ["NOT_YET"]},
        "input": {"prompt": "fix every finding in verdict.json"}}]
    return {"strategy": "integrate", "repository_url": REPO, "steps": steps}


def _submit(client, db, spec) -> dict[str, dict]:
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 201, response.text
    return {s["step_id"]: db.docs[f"tasks/{s['task_id']}"]
            for s in response.json()["workflow"]["steps"]}


def test_a_review_step_with_no_gate_is_stored_with_the_default_and_held(client, db):
    tasks = _submit(client, db, _shape())

    review = tasks["review"]["metadata"]
    # MUTATION: drop the stamp in `submit_workflow` and the review is never held.
    assert review["review_gate"] == {"default": True}
    assert loop.held_for_control_publish(review) is True
    assert verdictpublish.held_by_scheduler(review) is True
    # Only the review: the implementer and the fix read no one's verdict gate.
    assert "review_gate" not in tasks["implement"]["metadata"]
    assert "review_gate" not in tasks["fix"]["metadata"]


def test_a_steps_own_gate_wins_over_the_default(client, db):
    tasks = _submit(client, db, _shape({"review_gate": {"min_lines": 50}}))
    assert tasks["review"]["metadata"]["review_gate"] == {"min_lines": 50}


def test_false_switches_the_gate_off_for_the_step(client, db):
    tasks = _submit(client, db, _shape({"review_gate": False}))
    review = tasks["review"]["metadata"]
    assert "review_gate" not in review
    assert loop.held_for_control_publish(review) is False


def test_the_default_cannot_be_written_by_a_caller(client):
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=_shape({"review_gate": {"default": True}}))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_review_gate"


def test_the_requirements_check_is_held_and_never_integrated(client, db):
    tasks = _submit(client, db, _shape(check=True))
    check = tasks["requirements-check"]
    assert check["metadata"]["review_gate"] == {"default": True, "requirements_check": True}
    assert loop.held_for_control_publish(check["metadata"]) is True
    # MUTATION: drop `requirements_check_steps` from `not_integrated` and the
    # integrator merges the check's branch.
    assert tasks["fix"]["metadata"]["dispatch"]["integrates"] == [tasks["implement"]["id"]]


def test_review_steps_are_the_when_targets_that_name_no_gate():
    class Step:
        def __init__(self, step_id, when=None, metadata=None):
            self.step_id, self.metadata = step_id, metadata or {}
            self.when = type("When", (), {"step": when})() if when else None

    steps = [Step("implement"), Step("review"), Step("own", metadata={"review_gate": False}),
             Step("fix", when="review"), Step("fix-2", when="own")]
    assert validation.default_review_gate_steps(steps) == {"review"}
    assert validation.default_review_gate_steps(steps, {"review_gate": False}) == frozenset()
