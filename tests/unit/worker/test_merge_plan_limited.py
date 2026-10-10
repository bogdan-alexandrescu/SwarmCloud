"""The merge step on a private repository whose plan has no rulesets.

Measured 2026-10-10T02:18Z: the merge step for sagaxyz/ai-studio PR 244
(workflow wf_8340fb3c) failed `token_lacks_rights` because GitHub answered
`GET /repos/{o}/{r}/rules/branches/main` with 403 "Upgrade to GitHub Pro or
make this repository public to enable this feature." That is the plan, not a
missing right: it reads as "no rules", as a 404 does, and the step then needs
every check at the head green and at least one reported -- the same path an
unprotected branch takes, never a merge on nothing. Any other 403 still
refuses `token_lacks_rights`.

`MergeWorld` is green with one required check from a ruleset; each case here
breaks the rules read the way such a plan does and changes one more thing.
"""

from __future__ import annotations

import pytest

from agent_worker import forge as forge_mod
from agent_worker import merge
from swarm_common.states import TaskState

from merge_world import API, PINNED, MergeWorld

#: GitHub's answer on a plan without the feature, as measured.
PLAN_MESSAGE = "Upgrade to GitHub Pro or make this repository public to enable this feature."
PLAN_DOCS = "https://docs.github.com/rest/repos/rules#get-rules-for-a-branch"
RULES = f"{API}/rules/branches/main"
BRANCH = f"{API}/branches/main"


def _plan_limited(world: MergeWorld, *, message: str = PLAN_MESSAGE,
                  docs: str | None = PLAN_DOCS) -> None:
    body = {"message": message}
    if docs is not None:
        body["documentation_url"] = docs
    world.github.route("GET", RULES, (403, {}, body))
    world.branch = {"name": "main", "protected": False}
    world.runs = [{"name": "lint", "status": "completed", "conclusion": "success"},
                  {"name": "unit", "status": "completed", "conclusion": "success"}]


def test_a_plan_limited_403_merges_when_every_head_check_is_green(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PINNED
    assert outcome.summary["required_checks"] == []
    assert outcome.summary["required_checks_source"] == "none_plan_limited_all_checks"


def test_githubs_plan_message_is_matched_in_any_case_and_without_a_link(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world, message=PLAN_MESSAGE.upper(), docs=None)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["required_checks_source"] == "none_plan_limited_all_checks"


def test_a_plan_limited_403_with_a_red_check_refuses_and_never_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.runs[1].update(conclusion="failure")
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed", outcome.summary
    assert "unit" in outcome.summary["refusal"]["message"]
    assert outcome.summary["required_checks_source"] == "none_plan_limited_all_checks"
    assert world.merge_calls() == []


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required"])
def test_a_plan_limited_403_with_any_unsuccessful_conclusion_never_merges(tmp_path, conclusion):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.runs[0].update(conclusion=conclusion)
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "checks_failed", outcome.summary
    assert conclusion in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_a_plan_limited_403_with_a_red_commit_status_never_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.statuses = [{"context": "ci/legacy", "state": "failure"}]
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "checks_failed", outcome.summary
    assert world.merge_calls() == []


def test_a_plan_limited_403_with_a_pending_check_waits_and_never_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.runs[1].update(status="in_progress", conclusion=None)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.summary["wait"]["code"] == "checks_pending", outcome.summary
    assert "unit" in outcome.summary["wait"]["message"]
    assert world.merge_calls() == []


def test_a_plan_limited_403_with_a_pending_commit_status_waits(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.statuses = [{"context": "ci/legacy", "state": "pending"}]
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.summary["wait"]["code"] == "checks_pending"
    assert world.merge_calls() == []


def test_a_plan_limited_403_with_no_check_reported_does_not_merge(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.runs = []
    world.statuses = []
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.summary["wait"]["code"] == "no_checks", outcome.summary
    assert outcome.summary["required_checks_source"] == "none_plan_limited_all_checks"
    assert world.merge_calls() == []


def test_a_plan_limited_403_on_a_pull_request_that_does_not_merge_cleanly_refuses(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.pr.update(mergeable=False, mergeable_state="dirty")
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "merge_conflict", outcome.summary
    assert world.merge_calls() == []


def test_a_plan_limited_403_on_the_classic_protection_read_reads_as_none_too(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.github.route("GET", BRANCH, (403, {}, {"message": PLAN_MESSAGE,
                                                 "documentation_url": PLAN_DOCS}))
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["required_checks_source"] == "none_plan_limited_all_checks"


@pytest.mark.parametrize("body", [
    {"message": "Resource not accessible by personal access token",
     "documentation_url": "https://docs.github.com/rest/repos/rules#get-rules-for-a-branch"},
    {"message": "Must have admin rights to Repository."},
    {},
    # GitHub's words, but a link that is not GitHub's: not GitHub's plan answer.
    {"message": PLAN_MESSAGE, "documentation_url": "https://example.invalid/upgrade"},
], ids=["pat_not_permitted", "admin_rights", "no_body", "foreign_link"])
def test_any_other_403_on_the_rules_read_is_still_token_lacks_rights(tmp_path, body):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.github.route("GET", RULES, (403, {}, body))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "token_lacks_rights", outcome.summary
    assert world.merge_calls() == []


def test_any_other_403_on_the_classic_protection_read_is_still_token_lacks_rights(tmp_path):
    world = MergeWorld(tmp_path)
    world.github.route("GET", BRANCH, (403, {}, {"message": "Must have admin rights"}))
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "token_lacks_rights", outcome.summary
    assert world.merge_calls() == []


def test_a_404_on_the_rules_read_is_unchanged(tmp_path):
    world = MergeWorld(tmp_path)
    _plan_limited(world)
    world.github.route("GET", RULES, (404, {}, {"message": "Not Found"}))
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["required_checks_source"] == "none_all_checks"


def test_the_source_names_rulesets_and_classic_protection(tmp_path):
    ruled = MergeWorld(tmp_path / "rules")
    outcome = merge.run_merge(ruled.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["required_checks_source"] == "rulesets"

    classic = MergeWorld(tmp_path / "classic")
    classic.rules = []
    classic.branch = {"name": "main", "protected": True, "protection": {
        "required_status_checks": {"contexts": ["ci / unit"]}}}
    outcome = merge.run_merge(classic.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["required_checks_source"] == "classic"


def test_plan_lacks_feature_reads_only_a_403():
    def answered(status: int) -> forge_mod.ForgeAnswered:
        return forge_mod.ForgeAnswered(status, RULES, PLAN_MESSAGE, documentation_url=PLAN_DOCS)

    assert merge.plan_lacks_feature(answered(403))
    for status in (401, 404, 422, 500):
        assert not merge.plan_lacks_feature(answered(status)), status
