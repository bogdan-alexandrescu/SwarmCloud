"""A workflow that merges on its review's verdict runs the fix loop, 3 rounds, unless its spec declares its own.

The owner, 9 November 2026 (docs/design/fix-loop.md §9 Q8 (b)): a lane ran
one fix round, its re-review said NOT_YET, the workflow ended FAILED, and a
`ready` label then merged the green pull request on CI alone. So when
`metadata.merge` "on_merge_verdict" (or the repository's `merge_policy`)
applies to an `integrate` workflow whose integrator is gated on the review's
NOT_YET, and the spec declares no `fix-loop` step, swarm-api appends:

  1. a `fix-loop` step {"review", "fix", "max_rounds": 3} depending on the
     fix, checked and signed into `metadata.dispatch.fix_loop` exactly as a
     declared loop is -- and no separate re-review, the loop's `re-review:1`
     being that re-review;
  2. the merge, reading the loop: its signed `merge_target` names the loop
     (`review_loop`) in place of a review (§4.6).

Unchanged: a spec's own loop, `max_rounds` included; a workflow that does not
merge on the verdict; a fix gated on other verdicts; `direct-pr` and
`collect`. While the `fix-loop` profile ships unavailable (contract request
124) the re-review is still appended
(tests/unit/control_plane/test_merge_verdict_workflows.py holds that shape),
because an appended loop would refuse every such workflow as disabled.

No credentials, no network, no emulator: every agent step is `mock`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from swarm_api import repositories
from swarm_api.validation import (
    FIX_LOOP_MAX_ROUNDS,
    FIX_LOOP_MIN_ROUNDS,
    FIX_LOOP_PROFILE,
    FIX_LOOP_VERDICT_MERGE_ROUNDS,
)
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import auth_header

REPO = "https://github.com/octo-org/widget-shop.git"


@pytest.fixture
def loop_enabled(monkeypatch):
    """`fix-loop`, available for this test only, as it will be once its acceptance run passes."""
    monkeypatch.setitem(RUNNER_PROFILES, FIX_LOOP_PROFILE,
                        replace(RUNNER_PROFILES[FIX_LOOP_PROFILE], available=True))


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)


def _gated(verdicts=("NOT_YET",), **top) -> dict:
    """implement -> review -> fix (gated on the review, the integrator)."""
    spec = {
        "strategy": "integrate",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
             "input": {"prompt": "review; write verdict.json"}},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
             "builds_on": "implement", "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": list(verdicts)},
             "input": {"prompt": "fix every finding"}},
        ],
    }
    spec.update(top)
    return spec


def _post(client, spec):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _by_id(body: dict) -> dict[str, dict]:
    return {s["step_id"]: s for s in body["workflow"]["steps"]}


def _dispatch(db, step: dict) -> dict:
    return db.docs[f"tasks/{step['task_id']}"]["metadata"]["dispatch"]


def _register(db, tenant_id: str, merge_policy: str) -> None:
    """A registration as `repositories.register` stores it, written directly."""
    owner, repo = "octo-org", "widget-shop"
    repo_id = repositories.repo_id_for(tenant_id, owner, repo)
    db.collection(repositories.COLLECTION).document(repo_id).set({
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github",
        "owner": owner, "repo": repo, "repository": f"{owner}/{repo}",
        "repository_url": repositories.repository_url(owner, repo),
        "default_branch": "main", "default_branch_source": "forge",
        "merge_policy": merge_policy,
    })


def _assert_the_loop_and_its_merge(db, body: dict) -> None:
    steps = _by_id(body)
    assert list(steps) == ["implement", "review", "fix", "loop", "merge"]
    assert not [s for s in steps.values() if s["step_id"].startswith("re-review")]
    loop = steps["loop"]
    assert loop["runner_profile"] == FIX_LOOP_PROFILE
    assert loop["depends_on"] == ["fix"]
    # Signed as a declared loop's block is: step ids and their task ids.
    assert _dispatch(db, loop)["fix_loop"] == {
        "review": "review", "review_task_id": steps["review"]["task_id"],
        "fix": "fix", "fix_task_id": steps["fix"]["task_id"],
        "max_rounds": 3,
    }
    merge = steps["merge"]
    assert merge["depends_on"] == ["fix", "loop"]
    assert merge["input_from"] == {}
    target = _dispatch(db, merge)["merge_target"]
    assert target["pull_request"] == steps["fix"]["task_id"]
    assert target["review_loop"] == loop["task_id"]
    assert "review" not in target and "verdict_file" not in target
    # The fix is still the integrator and integrates the implementer only.
    assert body["dispatch"]["integrator_step_id"] == "fix"
    assert _dispatch(db, steps["fix"])["integrates"] == [steps["implement"]["task_id"]]


# --------------------------------------------------------------------------
# 1, 2: the loop and the merge reading it
# --------------------------------------------------------------------------

def test_the_default_is_three_rounds_the_owners_choice():
    assert FIX_LOOP_VERDICT_MERGE_ROUNDS == 3
    assert FIX_LOOP_MIN_ROUNDS <= FIX_LOOP_VERDICT_MERGE_ROUNDS <= FIX_LOOP_MAX_ROUNDS


def test_on_merge_verdict_appends_the_loop_and_a_merge_reading_it(client, db, loop_enabled):
    response = _post(client, _gated(metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 201, response.text
    _assert_the_loop_and_its_merge(db, response.json())


def test_a_repository_on_merge_verdict_policy_appends_the_loop_too(client, db, loop_enabled):
    _register(db, "eng", "on_merge_verdict")
    response = _post(client, _gated())
    assert response.status_code == 201, response.text
    _assert_the_loop_and_its_merge(db, response.json())


def test_the_merge_reads_the_loop_not_a_review(client, db, loop_enabled):
    response = _post(client, _gated(metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    target = _dispatch(db, steps["merge"])["merge_target"]
    assert target["review_loop"] == steps["loop"]["task_id"]
    assert "review" not in target and "verdict_file" not in target


def test_a_step_already_named_loop_gets_a_suffixed_id(client, db, loop_enabled):
    spec = _gated(metadata={"merge": "on_merge_verdict"})
    spec["steps"][0]["step_id"] = "loop"
    spec["steps"][1]["depends_on"] = ["loop"]
    spec["steps"][1]["builds_on"] = "loop"
    spec["steps"][1]["input_from"] = {"loop": "swarm-work.patch"}
    spec["steps"][2]["builds_on"] = "loop"
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["loop", "review", "fix", "loop-2", "merge"]
    assert steps["loop-2"]["runner_profile"] == FIX_LOOP_PROFILE
    assert _dispatch(db, steps["merge"])["merge_target"]["review_loop"] == (
        steps["loop-2"]["task_id"])


# --------------------------------------------------------------------------
# Unchanged
# --------------------------------------------------------------------------

def test_a_declared_loop_is_kept_as_declared(client, db, loop_enabled):
    spec = _gated(metadata={"merge": "on_merge_verdict"})
    spec["steps"].append({
        "step_id": "rounds", "runner_profile": FIX_LOOP_PROFILE, "depends_on": ["fix"],
        "fix_loop": {"review": "review", "fix": "fix", "max_rounds": 5},
    })
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["implement", "review", "fix", "rounds", "merge"]
    assert _dispatch(db, steps["rounds"])["fix_loop"]["max_rounds"] == 5
    assert _dispatch(db, steps["merge"])["merge_target"]["review_loop"] == (
        steps["rounds"]["task_id"])


def test_merge_on_appends_no_loop(client, db, loop_enabled):
    response = _post(client, _gated(metadata={"merge": "on"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["implement", "review", "fix", "merge"]
    assert _dispatch(db, steps["merge"])["merge_target"]["review"] == steps["review"]["task_id"]


def test_merge_off_appends_nothing(client, db, loop_enabled):
    response = _post(client, _gated(metadata={"merge": "off"}))
    assert response.status_code == 201, response.text
    assert list(_by_id(response.json())) == ["implement", "review", "fix"]


def test_no_merge_choice_appends_nothing(client, db, loop_enabled):
    response = _post(client, _gated())
    assert response.status_code == 201, response.text
    assert list(_by_id(response.json())) == ["implement", "review", "fix"]


def test_a_fix_gated_on_other_verdicts_keeps_the_rereview(client, db, loop_enabled):
    spec = _gated(verdicts=("NOT_YET", "MERGE"), metadata={"merge": "on_merge_verdict"})
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["implement", "review", "fix", "re-review", "merge"]


def test_an_ungated_integrator_is_still_refused(client, db, loop_enabled):
    spec = _gated(metadata={"merge": "on_merge_verdict"})
    spec["steps"][2].pop("when")
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert not [k for k in db.docs if k.startswith("tasks/")]


@pytest.mark.parametrize("strategy", ["direct-pr", "collect"])
def test_direct_pr_and_collect_are_refused_as_before(client, db, loop_enabled, strategy):
    spec = {"strategy": strategy, "repository_url": REPO, "metadata": {"merge": "on_merge_verdict"},
            "steps": [{"step_id": "implement", "runner_profile": "mock",
                       "input": {"prompt": "implement the change"}}]}
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert FIX_LOOP_PROFILE not in response.text


def test_while_the_profile_is_unavailable_the_rereview_is_appended(client, db):
    assert not RUNNER_PROFILES[FIX_LOOP_PROFILE].available
    response = _post(client, _gated(metadata={"merge": "on_merge_verdict"}))
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["implement", "review", "fix", "re-review", "merge"]
