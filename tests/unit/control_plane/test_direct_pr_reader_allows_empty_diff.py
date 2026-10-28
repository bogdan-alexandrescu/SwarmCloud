"""A `direct-pr` step that only feeds a dependant may change nothing (#2662).

Workflow wf_ea38b900 (strategy `direct-pr`) had a read-only review step and a
fix step that `depends_on` it and staged its `review.md`. The review wrote its
findings, committed nothing, and FAILED `published_nothing:` -- under
`direct-pr` every step is held to opening a pull request, and only
`allow_empty_diff` (or `verification.md`) exempts one -- and the workflow swept
the fix behind it.

So swarm-api, which every submitter (console, swarm-mcp, API) goes through,
stores `allow_empty_diff` in the signed dispatch block of a `direct-pr` step
that some agent step depends on and that no step `builds_on`: a reader, whose
output is artifacts. A step another builds_on keeps its branch obligation
(#1225), as does a step nothing depends on, and every other strategy stores
exactly the block it stored before.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api.validation import StepSpec, reader_step_ids

from .conftest import auth_header

REPO = "https://github.com/example-org/example.git"


def _post(client: Any, spec: dict[str, Any]) -> Any:
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _dispatch_blocks(client: Any, db: Any, spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    steps = {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}
    return {
        step_id: db.docs[f"tasks/{task_id}"]["metadata"]["dispatch"]
        for step_id, task_id in steps.items()
    }


def _review_then_fix(strategy: str = "direct-pr", *, builds_on: bool = False) -> dict[str, Any]:
    fix: dict[str, Any] = {
        "step_id": "fix", "runner_profile": "mock",
        "depends_on": ["review"],
        "input_from": {"review": "review.md"},
        "input": {"prompt": "fix every finding in review.md"},
    }
    if builds_on:
        fix["builds_on"] = "review"
    return {
        "strategy": strategy,
        "repository_url": REPO,
        "steps": [
            {"step_id": "review", "runner_profile": "mock",
             "input": {"prompt": "review the change; write review.md; change nothing"}},
            fix,
        ],
    }


# --------------------------------------------------------------------------
# submission: what is stored in each step's signed dispatch block
# --------------------------------------------------------------------------

def test_a_direct_pr_review_that_only_feeds_a_dependant_stores_allow_empty_diff(client, db):
    blocks = _dispatch_blocks(client, db, _review_then_fix())
    assert blocks["review"]["allow_empty_diff"] is True


def test_the_leaf_step_keeps_its_pull_request_obligation(client, db):
    blocks = _dispatch_blocks(client, db, _review_then_fix())
    assert "allow_empty_diff" not in blocks["fix"]


def test_a_step_another_builds_on_keeps_its_branch_obligation(client, db):
    blocks = _dispatch_blocks(client, db, _review_then_fix(builds_on=True))
    assert "allow_empty_diff" not in blocks["review"]
    assert "allow_empty_diff" not in blocks["fix"]


def test_a_single_direct_pr_step_with_no_dependants_stores_no_key(client, db):
    spec = _review_then_fix()
    spec["steps"] = spec["steps"][:1]
    blocks = _dispatch_blocks(client, db, spec)
    assert "allow_empty_diff" not in blocks["review"]


@pytest.mark.parametrize("strategy", ["integrate", "collect"])
def test_other_strategies_store_the_block_they_stored_before(client, db, strategy):
    blocks = _dispatch_blocks(client, db, _review_then_fix(strategy))
    assert "allow_empty_diff" not in blocks["review"]
    assert "allow_empty_diff" not in blocks["fix"]


def test_a_step_that_asked_keeps_its_flag(client, db):
    spec = _review_then_fix()
    spec["steps"][1]["allow_empty_diff"] = True
    blocks = _dispatch_blocks(client, db, spec)
    assert blocks["review"]["allow_empty_diff"] is True
    assert blocks["fix"]["allow_empty_diff"] is True


def test_the_flag_is_covered_by_the_spec_signature():
    from swarm_common.specsign import SIGNED_METADATA_KEYS

    from swarm_api.validation import DispatchOptions

    block = DispatchOptions(strategy="direct-pr").with_routing(
        builds_on=None, gate_task_id=None, allow_empty_diff=True,
    ).to_metadata()
    assert block["allow_empty_diff"] is True
    # It lives in `dispatch`, which the signature covers whole.
    assert "dispatch" in SIGNED_METADATA_KEYS


# --------------------------------------------------------------------------
# the pure rule
# --------------------------------------------------------------------------

def _step(step_id: str, *, depends_on: tuple[str, ...] = (), input_from: dict[str, str] | None = None,
          builds_on: str | None = None, runner_profile: str = "mock") -> StepSpec:
    return StepSpec(
        step_id=step_id, depends_on=depends_on, input_from=input_from or {},
        builds_on=builds_on, runner_profile=runner_profile,
    )


def test_the_rule_names_a_reader_under_direct_pr_only():
    steps = [
        _step("review"),
        _step("fix", depends_on=("review",), input_from={"review": "review.md"}),
    ]
    assert reader_step_ids(steps, strategy="direct-pr") == frozenset({"review"})
    for strategy in ("collect", "integrate", "single-pr"):
        assert reader_step_ids(steps, strategy=strategy) == frozenset()


def test_the_rule_counts_depends_on_alone():
    steps = [_step("plan"), _step("build", depends_on=("plan",))]
    assert reader_step_ids(steps, strategy="direct-pr") == frozenset({"plan"})


def test_the_rule_leaves_out_a_step_any_other_builds_on():
    steps = [
        _step("implement"),
        _step("review", depends_on=("implement",), input_from={"implement": "swarm-work.patch"}),
        _step("fix", depends_on=("review", "implement"), builds_on="implement",
              input_from={"review": "review.md"}),
    ]
    # `implement` is built on; `review` only feeds `fix`; `fix` is a leaf.
    assert reader_step_ids(steps, strategy="direct-pr") == frozenset({"review"})


def test_the_rule_leaves_out_a_lone_step():
    assert reader_step_ids([_step("only")], strategy="direct-pr") == frozenset()
