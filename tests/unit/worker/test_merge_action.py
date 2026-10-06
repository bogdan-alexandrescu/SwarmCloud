"""The merge action on the tenant's `-git` token, in any repository (contract request 47).

Owner decisions of 2026-10-04 (#295): merging moves off the GitHub-side App
into a workflow `merge` step that uses the tenant's existing `-git` token,
works in any repository a workflow runs on, pins the head the workflow
pushed, needs every required check green there and the review's verdict
MERGE, squash-merges with the pull request's title, records which task
merged it, and closes every issue the pull request closes that is still
open (#569).

`merge_world.MergeWorld` is green in every respect, in a repository that is
not SwarmCloud's. The control, `test_a_green_merge_verdict_pull_request_is_
squash_merged_at_the_pinned_head`, is what makes each refusal mean
something: without it every case could be an action that refuses
everything. Each refusal asserts its code, its reason, and that NO merge call
was made -- delete the check it names and its case merges instead.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from agent_worker import forge as forge_mod
from agent_worker import merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import (
    API, CHECK, MERGED, NAME, NUMBER, OPENER, OTHER, OWNER, PINNED, PR, TASK, TITLE,
    MergeWorld,
)


def test_the_merge_target_key_and_the_forge_hosts_are_swarm_apis():
    """The worker restates what swarm-api writes and what it admits at submission."""
    from swarm_api import validation

    assert merge.MERGE_TARGET_FIELD == validation.MERGE_TARGET_FIELD
    assert set(merge.MERGEABLE_HOSTS) == set(validation.MERGE_FORGE_HOSTS)


def test_every_merge_target_key_swarm_api_writes_is_one_the_worker_reads():
    """MS1 writes `merge_target.base` for a registered repository; a worker that
    refused an unknown key would refuse every merge there as merge_target_invalid."""
    from swarm_api.validation import DispatchOptions

    block = DispatchOptions(strategy="integrate", carrier="patches").with_merge_target(
        pull_request=OPENER, review="task_rev", verdict_file="verdict.json", base="develop",
    ).to_metadata()
    target = merge.parse_merge_target(block)
    assert target == merge.MergeTarget(OPENER, "task_rev", "verdict.json", base="develop")
    bare = DispatchOptions(strategy="direct-pr", carrier="patches").with_merge_target(
        pull_request=OPENER,
    ).to_metadata()
    assert merge.parse_merge_target(bare) == merge.MergeTarget(OPENER)


def test_a_registered_base_in_the_target_still_merges(tmp_path):
    world = MergeWorld(tmp_path)
    world.target["base"] = "main"
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


@pytest.mark.parametrize("base", ["", 7, None, ["main"]])
def test_a_base_that_is_not_a_branch_name_is_an_invalid_target(base):
    with pytest.raises(merge.TargetInvalid, match="base"):
        merge.parse_merge_target({"merge_target": {"pull_request": OPENER, "base": base}})


def test_a_green_merge_verdict_pull_request_is_squash_merged_at_the_pinned_head(tmp_path):
    world = MergeWorld(tmp_path)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.end_cause is None
    (call,) = world.merge_calls()
    assert call.path == f"/repos/{OWNER}/{NAME}/pulls/{NUMBER}/merge"
    assert call.body["merge_method"] == "squash"
    assert call.body["sha"] == PINNED
    assert call.body["commit_title"] == f"{TITLE} (#{NUMBER})"
    assert f"SwarmCloud task {TASK}" in call.body["commit_message"]
    assert "Co-Authored-By" not in call.body["commit_message"]
    assert outcome.summary["merged_by_this_task"] is True
    assert outcome.summary["merge_commit"] == MERGED
    assert outcome.summary["repository"] == f"{OWNER}/{NAME}"
    # The record of which task merged it, on the pull request.
    (record,) = world.github.calls("POST", f"{API}/issues/{NUMBER}/comments")
    assert f"Merged by SwarmCloud task {TASK}" in record.body["body"]
    assert outcome.summary["recorded_on_pull_request"] is True
    # Every call went to api.github.com, about this repository and no other.
    for seen in world.github.seen:
        assert seen.url.startswith("https://api.github.com/"), seen.url
        assert seen.path == "/graphql" or seen.path.startswith(API), seen.path
    # The token was read once, after the credential-free checks.
    assert world.token_reads == 1


def test_an_unprotected_branch_with_every_check_green_merges(tmp_path):
    world = MergeWorld(tmp_path)
    world.rules = []
    world.branch = {"name": "main", "protected": False}
    world.runs = [{"name": "lint", "status": "completed", "conclusion": "success"},
                  {"name": "docs", "status": "completed", "conclusion": "neutral"}]
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_a_classic_protection_required_status_is_read_too(tmp_path):
    """A repository on classic branch protection, no rulesets: the required
    check comes from the branch's own read, and a red one refuses."""
    world = MergeWorld(tmp_path)
    world.rules = []
    world.branch = {"name": "main", "protected": True, "protection": {
        "required_status_checks": {"contexts": ["build"], "checks": [{"context": "build"}]}}}
    world.statuses = [{"context": "build", "state": "failure"}]
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "checks_failed", outcome.message
    assert world.merge_calls() == []


def _set(path: str, value: Any) -> Callable[[MergeWorld], None]:
    def apply(world: MergeWorld) -> None:
        target: Any = world
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part] if isinstance(target, dict) else getattr(target, part)
        if isinstance(target, dict):
            target[parts[-1]] = value
        else:
            setattr(target, parts[-1], value)
    return apply


def _unprotected_and_silent(world: MergeWorld) -> None:
    world.rules = []
    world.branch = {"name": "main", "protected": False}
    world.runs = []
    world.statuses = []


def _unprotected_and_red(world: MergeWorld) -> None:
    world.rules = []
    world.branch = {"name": "main", "protected": False}
    world.runs = [{"name": "lint", "status": "completed", "conclusion": "success"},
                  {"name": "unit", "status": "completed", "conclusion": "failure"}]


#: (case, the one thing broken, refusal code, words the reason says, retryable)
REFUSALS: list[tuple[str, Callable[[MergeWorld], None], str, str, bool]] = [
    ("red_required_check", lambda w: w.runs[0].update(conclusion="failure"),
     "checks_failed", CHECK, False),
    ("neutral_required_check", lambda w: w.runs[0].update(conclusion="neutral"),
     "checks_failed", CHECK, False),
    ("pending_required_check", lambda w: w.runs[0].update(status="in_progress", conclusion=None),
     "checks_pending", CHECK, True),
    ("required_check_not_reported", _set("runs", []), "checks_pending", CHECK, True),
    ("required_check_by_another_app", lambda w: w.runs[0].update(app={"id": 1}),
     "checks_pending", CHECK, True),
    ("moved_head", lambda w: w.pr["head"].update(sha=OTHER), "head_moved", OTHER, False),
    ("verdict_not_yet", lambda w: w.verdict.update(verdict="NOT_YET"),
     "verdict_not_merge", "NOT_YET", False),
    ("verdict_not_staged", _set("verdict", None), "verdict_unreadable", "verdict.json", False),
    ("unprotected_branch_with_no_checks", _unprotected_and_silent, "no_checks",
     "at least one", True),
    ("unprotected_branch_with_a_red_check", _unprotected_and_red, "checks_failed", "unit", False),
    ("not_mergeable", lambda w: w.pr.update(mergeable=False), "not_mergeable",
     "does not merge cleanly", False),
    ("mergeability_never_computed", lambda w: w.pr.update(mergeable=None),
     "mergeability_unknown", "rereads", True),
    ("token_lacks_rights", lambda w: w.repository.update(permissions={"push": False}),
     "token_lacks_rights", "cannot write", False),
    ("token_cannot_read", lambda w: w.github.route("GET", API, (404, {}, {"message": "Not Found"})),
     "token_lacks_rights", "cannot read", False),
    ("pull_request_closed", lambda w: w.pr.update(state="closed"), "pull_request_closed",
     "closed", False),
    ("pull_request_from_another_branch", lambda w: w.pr["head"].update(ref="swarm/task_other"),
     "pull_request_not_this_workflows", f"swarm/{OPENER}", False),
    ("pull_request_from_a_fork", lambda w: w.pr["head"].update(repo={"full_name": "mallory/shop"}),
     "pull_request_not_this_workflows", f"swarm/{OPENER}", False),
    ("draft", lambda w: w.pr.update(draft=True), "draft", "draft", False),
    ("placeholder_title", lambda w: w.pr.update(title="[swarm] task_abc"),
     "title_placeholder", "placeholder", False),
    ("no_pull_request_recorded",
     lambda w: w.docs[OPENER]["result_summary"]["git"].pop("pull_request"),
     "pull_request_unknown", OPENER, False),
    ("no_head_recorded",
     lambda w: w.docs[OPENER]["result_summary"]["git"].pop("pushed_head"),
     "head_unknown", OPENER, False),
    ("opener_spec_unverified", lambda w: w.unverified.update({OPENER: "signature_mismatch"}),
     "spec_unverified", OPENER, False),
    ("review_spec_unverified", lambda w: w.unverified.update({"task_rev": "unsigned"}),
     "spec_unverified", "task_rev", False),
    ("worker_unprotected", _set("unprotected", "dumpable"), "worker_unprotected",
     "dumpable", False),
    ("processes_alive", _set("reaped", (4321,)), "processes_alive", "survived", False),
    ("unsupported_forge", _set("repository_url", "https://gitlab.com/octo-org/widget-shop.git"),
     "forge_unsupported", "github.com", False),
    ("no_merge_target", lambda w: w.target.clear(), "merge_target_invalid", "merge_target", False),
]


@pytest.mark.parametrize(("case", "mutate", "code", "says", "retryable"), REFUSALS,
                         ids=[r[0] for r in REFUSALS])
def test_each_refusal_fails_with_its_reason_and_makes_no_merge_call(
    tmp_path, case, mutate, code, says, retryable
):
    world = MergeWorld(tmp_path)
    mutate(world)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, (case, outcome.message)
    assert outcome.summary["refusal"]["code"] == code, (case, outcome.summary)
    assert says in outcome.summary["refusal"]["message"], (case, outcome.summary)
    assert outcome.message.startswith(code)
    assert outcome.retryable is retryable, case
    if not retryable:
        assert outcome.end_cause is EndCause.MERGE_REFUSED, case
    assert world.merge_calls() == [], f"{case}: a refusal made the merge call"
    assert world.closed_issues() == []


@pytest.mark.parametrize("case", ["verdict_not_yet", "moved_head_is_after", "worker_unprotected",
                                  "unsupported_forge"])
def test_a_credential_free_refusal_never_reads_the_token(tmp_path, case):
    """#219's order: what needs no credential is refused before the token is read."""
    world = MergeWorld(tmp_path)
    if case == "verdict_not_yet":
        world.verdict = {"verdict": "NOT_YET", "findings": ["a blocker"]}
    elif case == "worker_unprotected":
        world.unprotected = "dumpable"
    elif case == "unsupported_forge":
        world.repository_url = "https://gitlab.com/octo-org/widget-shop.git"
    else:
        # The control: a refusal that needs the forge DOES read it, once.
        world.pr["head"]["sha"] = OTHER
    merge.run_merge(world.context())
    assert world.token_reads == (1 if case == "moved_head_is_after" else 0)


def test_a_409_from_the_merge_call_is_head_moved_and_is_not_resent(tmp_path):
    world = MergeWorld(tmp_path)
    world.merge_answer = (409, {}, {"message": "Head branch was modified"})
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved"
    assert outcome.retryable is False
    assert len(world.merge_calls()) == 1
    assert world.closed_issues() == []


def test_a_405_from_the_merge_call_is_not_mergeable_with_githubs_reason(tmp_path):
    world = MergeWorld(tmp_path)
    world.merge_answer = (405, {}, {"message": "Required status check is expected"})
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "not_mergeable"
    assert "Required status check is expected" in outcome.summary["refusal"]["message"]
    assert len(world.merge_calls()) == 1


def test_a_merge_whose_answer_is_lost_is_never_sent_again(tmp_path):
    world = MergeWorld(tmp_path)

    def lost(_seen):
        raise forge_mod.ForgeUnavailable("could not reach the forge: timed out")

    world.merge_answer = lost
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is EndCause.MERGE_FAILED
    assert outcome.summary["refusal"]["code"] == "merge_unanswered"
    assert outcome.retryable is False, "a merge whose answer was lost was retried blindly"
    assert len(world.merge_calls()) == 1


def test_the_closing_references_still_open_are_closed_after_the_merge(tmp_path):
    world = MergeWorld(tmp_path)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    # #7 is open and closed by the step; #9 was already closed and is left.
    assert world.closed_issues() == [7]
    (note,) = world.github.calls("POST", f"{API}/issues/7/comments")
    assert note.body["body"] == f"Closed by #{NUMBER}, merged by SwarmCloud task {TASK}"
    (patch,) = world.github.calls("PATCH", f"{API}/issues/7")
    assert patch.body == {"state": "closed", "state_reason": "completed"}
    assert outcome.summary["issues_closed"] == [7]
    assert outcome.summary["issues_already_closed"] == [9]
    # Closed only AFTER the merge call.
    order = [(s.method, s.path) for s in world.github.seen]
    assert order.index(("PUT", f"{PR}/merge")) < order.index(("PATCH", f"{API}/issues/7"))


def test_a_part_of_pull_request_closes_nothing(tmp_path):
    world = MergeWorld(tmp_path)
    world.pr["body"] = "Lists widgets by price.\n\npart of #7"
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert world.closed_issues() == []
    assert world.github.calls("POST", f"{API}/issues/7/comments") == []
    assert outcome.summary["issues_closed"] == []


def test_a_pull_request_already_merged_at_the_pinned_head_succeeds_and_closes_its_issues(tmp_path):
    """A lost attempt that merged: the retry finds it merged, merges nothing,
    and still closes the issues the lost attempt never reached."""
    world = MergeWorld(tmp_path)
    world.pr.update(merged=True, state="closed", merge_commit_sha=MERGED)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["already_merged"] is True
    assert world.merge_calls() == []
    assert world.closed_issues() == [7]


def test_a_cancel_before_the_merge_call_merges_nothing(tmp_path):
    world = MergeWorld(tmp_path)
    world.cancel = True
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.CANCELLED
    assert world.merge_calls() == []


def test_the_token_is_never_in_the_outcome_a_log_line_or_a_request_but_its_header(tmp_path):
    world = MergeWorld(tmp_path)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    token = world.token
    assert token not in json.dumps(outcome.summary, default=str)
    assert token not in outcome.message
    assert token not in world.log.text
    assert token in world.log.secrets, "the token was not registered with the log redaction"
    for seen in world.github.seen:
        assert token not in seen.url
        assert token not in json.dumps(seen.body, default=str)
        carriers = [k for k, v in seen.headers.items() if token in str(v)]
        assert carriers == ["Authorization"], (seen.method, seen.path, carriers)
    # And nothing the action wrote to its directory carries it.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(errors="replace"), path


def test_a_refused_merge_leaves_the_token_nowhere_either(tmp_path):
    world = MergeWorld(tmp_path)
    world.runs[0].update(conclusion="failure")
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert world.token not in json.dumps(outcome.summary, default=str) + outcome.message
    assert world.token not in world.log.text


def test_the_retired_title_rule_is_lifecycles():
    from agent_worker.lifecycle import _RETIRED_TITLE_RE

    assert merge.RETIRED_TITLE_RE.pattern == _RETIRED_TITLE_RE.pattern
    assert merge.RETIRED_TITLE_RE.flags == _RETIRED_TITLE_RE.flags


def test_only_github_has_a_merger(tmp_path):
    from fake_github import fresh_token

    token = fresh_token()
    with pytest.raises(merge.UnsupportedForge):
        merge.merger_for("https://gitlab.com/o/r.git", token=token)
    with pytest.raises(merge.UnsupportedForge):
        merge.merger_for("https://github.example.com/o/r.git", token=token)
    found = merge.merger_for("git@github.com:octo-org/widget-shop.git", token=token)
    assert isinstance(found, merge.GitHubMerger)
    assert found.full_name == "octo-org/widget-shop"
    assert token not in repr(found)
