"""A review or re-review step owes its `verdict.json` (#2837).

Reported 2026-10-30, workflow `wf_a1efae5b3f2f470496e5`: the step
`dod-data-rereview` (`task_f3bcb12e927f484287a9`) ended SUCCEEDED with only
`handoff.md`, `prepublish-advisory.md` and `pr-title.txt`; its NOT_YET was in
`handoff.md` alone. No later step staged its `verdict.json`, so nothing put
the file in `metadata.expected_outputs`, and the worker never asked for it.

Now the worker knows a review by its step id too (`metadata.workflow_step`,
which swarm-api writes on every workflow step): a step id whose last word is
`review` or `rereview` owes `verdict.json`, and a clean finish without it
fails the attempt retryably, the #149 path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_worker import expected_outputs as expected_mod
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import seed_attempt

#: `result_summary` key naming what the attempt did not produce.
MISSING_KEY = "expected_outputs_missing"

VALID_VERDICT = json.dumps({"verdict": "NOT_YET", "findings": ["docs/ops.md:78 is still MISSING"]})


@pytest.mark.parametrize(
    "step_id",
    ["review", "re-review", "re-review-2", "dod-data-review", "dod-data-rereview", "lane_ReReview"],
)
def test_a_step_named_for_a_review_is_one(step_id):
    assert expected_mod.is_review_step(step_id)


@pytest.mark.parametrize(
    "step_id",
    ["dod-data", "dod-data-fix", "review-ui", "reviewer", "preview", "", None, 3],
)
def test_any_other_step_is_not(step_id):
    assert not expected_mod.is_review_step(step_id)


def test_a_review_step_with_no_dependant_owes_its_verdict():
    """The reported case: a leaf re-review. MUTATION: drop the step-id rule
    from `declared_outputs` and this is `()`."""
    declared = expected_mod.declared_outputs({"workflow_step": "dod-data-rereview"})
    assert declared.names == ("verdict.json",)


def test_a_review_step_keeps_what_its_dependants_declared_and_names_the_verdict_once():
    declared = expected_mod.declared_outputs(
        {"workflow_step": "review", "expected_outputs": ["verdict.json", "notes.md"]}
    )
    assert declared.names == ("notes.md", "verdict.json")


def test_a_step_that_is_not_a_review_owes_only_what_was_declared():
    declared = expected_mod.declared_outputs(
        {"workflow_step": "dod-data-fix", "expected_outputs": ["swarm-work.patch"]}
    )
    assert declared.names == ("swarm-work.patch",)


# -- the verdict file's own check -------------------------------------------------


def test_a_verdict_file_that_reads_has_no_problem(tmp_path):
    (tmp_path / "verdict.json").write_text(VALID_VERDICT)
    assert expected_mod.verdict_problem(tmp_path) is None


@pytest.mark.parametrize(
    "body, said",
    [
        (None, "was not written"),
        ("Verdict: NOT_YET", "is not JSON"),
        ("[]", "is not a JSON object"),
        (json.dumps({"verdict": "LGTM"}), "'LGTM'"),
    ],
)
def test_a_verdict_file_that_does_not_read_says_why(tmp_path, body, said):
    if body is not None:
        (tmp_path / "verdict.json").write_text(body)
    problem = expected_mod.verdict_problem(tmp_path)
    assert problem is not None and said in problem, problem


# -- the attempt -----------------------------------------------------------------


def _seed(db: Any, *, step_id: str, artifact_name: str, artifact_text: str) -> None:
    seed_attempt(
        db,
        task_input={
            "prompt": "re-review the fix",
            "steps": 1,
            "sleep_seconds": 0.01,
            "artifact_name": artifact_name,
            "artifact_text": artifact_text,
        },
    )
    db.doc("tasks/task_1")["metadata"] = {"workflow_step": step_id}


def test_a_rereview_that_wrote_only_its_handoff_fails_retryably(db, worker_factory):
    """MUTATION: drop the step-id rule and this attempt SUCCEEDS, as
    `task_f3bcb12e927f484287a9` did."""
    _seed(db, step_id="dod-data-rereview", artifact_name="handoff.md",
          artifact_text="**Verdict: NOT_YET**\n")
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task["state"]
    assert "verdict.json" in task["last_error"]
    assert task["result_summary"].get(MISSING_KEY) == ["verdict.json"]
    retrying = [e for e in db.events("task_1") if e["type"] == "retrying"]
    assert len(retrying) == 1, db.event_types("task_1")
    assert retrying[0]["detail"]["cause"] == "expected_outputs_missing"
    assert "succeeded" not in db.event_types("task_1")


def test_a_rereview_that_wrote_its_verdict_succeeds(db, worker_factory):
    _seed(db, step_id="dod-data-rereview", artifact_name="verdict.json",
          artifact_text=VALID_VERDICT)
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task["state"]
    assert MISSING_KEY not in task["result_summary"]


def test_a_step_that_is_not_a_review_is_not_asked_for_a_verdict(db, worker_factory):
    _seed(db, step_id="dod-data-fix", artifact_name="handoff.md", artifact_text="done\n")
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value

