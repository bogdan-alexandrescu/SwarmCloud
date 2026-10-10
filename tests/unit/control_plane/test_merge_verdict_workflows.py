"""Workflows end in a merge: `metadata.merge` "on_merge_verdict" and the repository's `merge_policy`.

WF-MERGE-API (2026-10-10, part of #295). 30 of the 40 SwarmCloud PRs left
unmerged on 2026-10-09 had no merge step at all: the implement -> review ->
fix workflows ended at the pull request. What these tests hold:

  1. "on_merge_verdict" on implement -> review -> fix appends a re-review of
     the fix's pushed head and ONE merge step that depends on the integrator
     and the re-review and reads the re-review's verdict -- the latest one.
     The integrator is still the fix, and integrates only the implementer.
  2. The re-review runs the review's own profile and input, by name, with the
     re-review preamble before its prompt; it builds on the integrator and
     stages the earlier verdict and the review's inputs by parent.
  3. A repository's registered `merge_policy` applies when `metadata.merge`
     says nothing, before the platform default, and only the tenant's own
     registration counts. A policy that cannot apply appends nothing.
  4. "on_merge_verdict" is refused under `collect`, under `direct-pr`, on an
     integrator with no review gate, and beside a spec's own merge step.
  5. A caller still cannot hand the re-review an image, a command or a
     backend: the steps the API derives carry only what the caller already
     sent for its review (invariant 10).

The merge step's run on the derived target, MERGE and NOT_YET, is
tests/unit/worker/test_merge_reads_the_rereview.py. No credentials, no
network, no emulator: every agent step is `mock`.
"""

from __future__ import annotations

import pytest

from swarm_api import repositories
from swarm_api.validation import (
    INPUT_LAYOUT_BY_PARENT,
    MERGE_POLICIES,
    REREVIEW_PREAMBLE,
    REREVIEW_ROUNDS,
)

from .conftest import auth_header

REPO = "https://github.com/octo-org/widget-shop.git"
REVIEW_PROMPT = "review; write verdict.json"


def _review_shape(**overrides) -> dict:
    """implement -> review -> fix (gated, the integrator), under `integrate`."""
    spec = {
        "strategy": "integrate",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
             "timeout_seconds": 900,
             "input": {"prompt": REVIEW_PROMPT}},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
             "builds_on": "implement", "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": "fix every finding"}},
        ],
    }
    spec.update(overrides)
    return spec


def _post(client, spec):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _set_default(client, value: bool) -> None:
    response = client.put("/v1/admin/settings", headers=auth_header("root"),
                          json={"merge_by_default": value})
    assert response.status_code == 200, response.text


def _register(db, tenant_id: str, merge_policy: str | None,
              repository: str = "octo-org/widget-shop") -> None:
    """A registration as `repositories.register` stores it, written directly."""
    owner, repo = repository.split("/")
    repo_id = repositories.repo_id_for(tenant_id, owner, repo)
    db.collection(repositories.COLLECTION).document(repo_id).set({
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github",
        "owner": owner, "repo": repo, "repository": repository,
        "repository_url": repositories.repository_url(owner, repo),
        "default_branch": "main", "default_branch_source": "forge",
        "merge_policy": merge_policy,
    })


def _by_id(body: dict) -> dict[str, dict]:
    return {s["step_id"]: s for s in body["workflow"]["steps"]}


def _merge_steps(body: dict) -> list[dict]:
    return [s for s in body["workflow"]["steps"] if s["runner_profile"] == "merge"]


def _task(db, task_id: str) -> dict:
    return db.docs[f"tasks/{task_id}"]


# --------------------------------------------------------------------------
# 1, 2: on_merge_verdict appends a re-review and a merge that reads it
# --------------------------------------------------------------------------

def test_on_merge_verdict_appends_a_rereview_and_a_merge_reading_its_verdict(client, db):
    response = _post(client, _review_shape(metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 201, response.text
    body = response.json()
    steps = _by_id(body)
    assert list(steps) == ["implement", "review", "fix", "re-review", "merge"]

    merge = steps["merge"]
    assert merge["depends_on"] == ["fix", "re-review"]
    assert merge["input_from"] == {"re-review": "verdict.json"}
    block = _task(db, merge["task_id"])["metadata"]["dispatch"]
    assert block["merge_target"] == {
        "pull_request": steps["fix"]["task_id"],
        "review": steps["re-review"]["task_id"],
        "verdict_file": "verdict.json",
    }
    # The pull request is still the fix's, integrating the implementer only:
    # the re-review comes after the integrator and is not one of its branches.
    assert body["dispatch"]["integrator_step_id"] == "fix"
    fix = _task(db, steps["fix"]["task_id"])
    assert fix["metadata"]["dispatch"]["integrates"] == [steps["implement"]["task_id"]]


def test_the_rereview_reviews_the_fixs_head_with_the_reviews_own_profile_and_input(client, db):
    response = _post(client, _review_shape(metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    rereview = steps["re-review"]
    assert rereview["runner_profile"] == steps["review"]["runner_profile"] == "mock"
    assert rereview["timeout_seconds"] == 900
    assert rereview["depends_on"] == ["fix", "implement", "review"]
    assert rereview["input_from"] == {"implement": "swarm-work.patch",
                                      "review": "verdict.json"}
    # The review's own prompt, after the preamble that says what changed.
    prompt = rereview["input"]["prompt"]
    assert prompt.endswith(REVIEW_PROMPT)
    assert prompt.startswith(REVIEW_PREAMBLE_HEAD)
    assert "review/verdict.json" in prompt
    task = _task(db, rereview["task_id"])
    dispatch = task["metadata"]["dispatch"]
    # Its checkout is the head the integrator pushed: the head the merge pins.
    assert dispatch["builds_on"] == steps["fix"]["task_id"]
    # By parent, so the earlier verdict.json is review/verdict.json and never
    # the file this step writes; its agent always runs (no gate).
    assert task["metadata"]["input_layout"] == INPUT_LAYOUT_BY_PARENT
    assert "verdict_gate" not in dispatch


REVIEW_PREAMBLE_HEAD = REREVIEW_PREAMBLE.split("{", 1)[0]


def test_one_rereview_round_is_the_bound():
    assert REREVIEW_ROUNDS == 1


def test_without_on_merge_verdict_nothing_rereviews(client):
    """The control: "on" keeps the merge reading the first review, no re-review."""
    response = _post(client, _review_shape(metadata={"merge": "on"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert "re-review" not in steps
    assert steps["merge"]["input_from"] == {"review": "verdict.json"}


def test_a_step_already_named_re_review_gets_a_suffixed_id(client):
    spec = _review_shape(metadata={"merge": "on_merge_verdict"})
    spec["steps"][0]["step_id"] = "re-review"
    for step in spec["steps"][1:]:
        step["depends_on"] = ["re-review" if d == "implement" else d for d in step["depends_on"]]
        step["input_from"] = {("re-review" if k == "implement" else k): v
                              for k, v in step["input_from"].items()}
        if step.get("builds_on") == "implement":
            step["builds_on"] = "re-review"
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert steps["merge"]["input_from"] == {"re-review-2": "verdict.json"}


# --------------------------------------------------------------------------
# 3: the repository's merge_policy, between the job and the platform
# --------------------------------------------------------------------------

def test_the_policies_are_off_and_on_merge_verdict():
    assert MERGE_POLICIES == ("off", "on_merge_verdict")


def test_a_repository_on_merge_verdict_applies_when_the_spec_says_nothing(client, db):
    _register(db, "eng", "on_merge_verdict")
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert steps["merge"]["input_from"] == {"re-review": "verdict.json"}


def test_an_unregistered_repository_and_the_platform_default_off_append_nothing(client):
    """The control for the test above: the same spec without the policy."""
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    assert _merge_steps(response.json()) == []


def test_a_repository_off_overrides_the_platform_default(client, db):
    _set_default(client, True)
    _register(db, "eng", "off")
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    assert _merge_steps(response.json()) == []


def test_a_repository_with_no_policy_defers_to_the_platform_default(client, db):
    _set_default(client, True)
    _register(db, "eng", None)
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    # The platform default is "on": a merge, no re-review.
    assert "re-review" not in steps
    assert steps["merge"]["input_from"] == {"review": "verdict.json"}


def test_the_spec_overrides_the_repository_policy(client, db):
    _register(db, "eng", "on_merge_verdict")
    response = _post(client, _review_shape(metadata={"merge": "off"}))
    assert response.status_code == 201, response.text
    assert _merge_steps(response.json()) == []


def test_another_tenants_policy_never_applies(client, db):
    _register(db, "research", "on_merge_verdict")
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    assert _merge_steps(response.json()) == []


def test_a_policy_that_cannot_apply_appends_nothing_and_refuses_nothing(client, db):
    """A one-step direct-pr workflow has no review to wait on: the policy does
    not apply, and the tenant is not refused for an admin's setting."""
    _register(db, "eng", "on_merge_verdict")
    response = _post(client, {
        "strategy": "direct-pr", "repository_url": REPO,
        "steps": [{"step_id": "only", "runner_profile": "mock", "input": {"prompt": "x"}}],
    })
    assert response.status_code == 201, response.text
    assert _merge_steps(response.json()) == []


# --------------------------------------------------------------------------
# 4, 5: refusals
# --------------------------------------------------------------------------

def test_on_merge_verdict_under_collect_is_refused_saying_why(client):
    response = _post(client, _review_shape(strategy="collect", steps=[
        {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "x"}},
        {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"], "input": {"prompt": "y"}},
    ], metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert "'collect' opens no pull request" in body["message"]
    assert "on_merge_verdict" in body["message"]


def test_on_merge_verdict_under_direct_pr_is_refused_saying_why(client):
    response = _post(client, {
        "strategy": "direct-pr", "repository_url": REPO,
        "metadata": {"merge": "on_merge_verdict"},
        "steps": [{"step_id": "only", "runner_profile": "mock", "input": {"prompt": "x"}}],
    })
    assert response.status_code == 422, response.text
    assert "under 'direct-pr'" in response.json()["message"]


def test_on_merge_verdict_on_an_ungated_integrator_is_refused(client):
    spec = _review_shape(metadata={"merge": "on_merge_verdict"})
    fix = spec["steps"][2]
    fix.pop("when")
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert "not gated on a review" in response.json()["message"]


def test_on_merge_verdict_beside_a_stated_merge_step_is_refused(client):
    spec = _review_shape(metadata={"merge": "on_merge_verdict"})
    spec["steps"].append({"step_id": "merge", "runner_profile": "merge",
                          "depends_on": ["fix", "review"],
                          "input_from": {"review": "verdict.json"}})
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert "remove the step" in response.json()["message"]


@pytest.mark.parametrize("field", ["image", "command", "backend"])
def test_a_caller_still_cannot_send_an_execution_parameter(client, field):
    spec = _review_shape(metadata={"merge": "on_merge_verdict"})
    spec["steps"][1][field] = "anything"
    response = _post(client, spec)
    assert response.status_code == 422, response.text


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
