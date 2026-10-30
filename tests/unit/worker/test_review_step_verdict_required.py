"""A review or re-review step owes its `verdict.json` (#2837).

Reported 2026-10-30, workflow `wf_a1efae5b3f2f470496e5`: the step
`dod-data-rereview` (`task_f3bcb12e927f484287a9`) ended SUCCEEDED with only
`handoff.md`, `prepublish-advisory.md` and `pr-title.txt`; its NOT_YET was in
`handoff.md` alone. No later step staged its `verdict.json`, so nothing put
the file in `metadata.expected_outputs`, and the worker never asked for it.

Now the worker knows a review by its step id too (the task's `step_id`, which
swarm-api writes on every workflow step and the step-spec signature covers): a
step id whose last word is
`review` or `rereview` owes `verdict.json`, and a clean finish without it
fails the attempt retryably, the #149 path.
"""

from __future__ import annotations

import json

import pytest

from agent_worker import expected_outputs as expected_mod

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
    declared = expected_mod.declared_outputs({}, step_id="dod-data-rereview")
    assert declared.names == ("verdict.json",)


def test_a_review_step_keeps_what_its_dependants_declared_and_names_the_verdict_once():
    declared = expected_mod.declared_outputs(
        {"expected_outputs": ["verdict.json", "notes.md"]}, step_id="review"
    )
    assert declared.names == ("notes.md", "verdict.json")


def test_a_step_that_is_not_a_review_owes_only_what_was_declared():
    declared = expected_mod.declared_outputs(
        {"expected_outputs": ["swarm-work.patch"]}, step_id="dod-data-fix"
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


def test_a_review_step_with_no_metadata_still_owes_its_verdict():
    assert expected_mod.declared_outputs(None, step_id="re-review").names == ("verdict.json",)


def test_an_unreadable_verdict_is_a_cause_a_retry_can_change():
    """The cause the worker gives a review's uploaded but unreadable verdict:
    retryable, because the agent can write a readable one next time."""
    causes = {"verdict.json": expected_mod.CAUSE_UNREADABLE_VERDICT}
    assert expected_mod.retryable(["verdict.json"], causes)
    assert "unreadable_verdict" in expected_mod.missing_error(["verdict.json"], causes=causes)
