"""The merge action: every check in order, the merge, and each refusal's end cause (#295).

docs/merge-step.md §2.2, §5 and §6. The chain in `action_chain.Chain` is green
in every respect; each case below breaks exactly ONE thing and asserts the
code `result_summary.merge.refusal` carries and the end cause the task takes.
The control, `test_a_green_chain_merges_at_the_pinned_head`, is what makes a
refusal mean something: without it every case could be an action that
refuses everything.

MUTATIONS: each case is the mutation of the check it names -- delete that
check and its case merges instead (the merge call is asserted absent).
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from agent_worker import merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from action_chain import ACTIONS_APP, BOT_ID, IDS, NUMBER, PINNED, PR, TITLE, Chain


def _merge_calls(chain: Chain) -> list[Any]:
    return chain.github.calls("PUT", f"{PR}/merge")


def test_the_merges_keys_are_swarm_apis():
    """The worker restates `validation.MERGES_KEYS` and `PROOF_FILENAME`."""
    from swarm_api import validation

    assert merge.MERGES_KEYS == validation.MERGES_KEYS
    assert merge.PROOF_FILENAME == validation.PROOF_FILENAME


def test_a_green_chain_merges_at_the_pinned_head(tmp_path):
    chain = Chain(tmp_path)
    outcome = merge.run_merge(chain.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.end_cause is None
    (call,) = _merge_calls(chain)
    assert call.body["sha"] == PINNED
    assert call.body["merge_method"] == "squash"
    assert call.body["commit_title"] == f"{TITLE} (#{NUMBER})"
    assert f"task {IDS['review']}" in call.body["commit_message"]
    assert "Co-Authored-By" not in call.body["commit_message"]
    assert outcome.summary["merged_by_this_task"] is True
    assert outcome.summary["recorded_on_pull_request"] is True
    assert outcome.summary["token_revoked"] is True
    # Fencing and cancel were re-checked after the mint and before the call.
    assert chain.recheck_calls == 2
    # The token was minted for this repository with contents + pull requests only.
    minted = chain.github.calls("POST", "/app/installations/7/access_tokens")[0]
    assert minted.body == {"repositories": ["widgets"],
                           "permissions": {"contents": "write", "pull_requests": "write"}}
    # Every call went to the pinned host.
    assert all(s.url.startswith("https://api.github.com/") for s in chain.github.seen)


def _set(path: str, value: Any) -> Callable[[Chain], None]:
    def apply(chain: Chain) -> None:
        target: Any = chain
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part] if isinstance(target, dict) else getattr(target, part)
        if isinstance(target, dict):
            target[parts[-1]] = value
        else:
            setattr(target, parts[-1], value)
    return apply


OTHER = "e" * 40

CASES: list[tuple[str, Callable[[Chain], None], EndCause, str]] = [
    ("worker_unprotected", _set("unprotected", "dumpable"), EndCause.MERGE_REFUSED, "worker_unprotected"),
    ("processes_alive", _set("reaped", (4321,)), EndCause.MERGE_REFUSED, "processes_alive"),
    ("spec_unverified", lambda c: c.unverified.update({IDS["post-verdict"]: "signature_mismatch"}),
     EndCause.MERGE_REFUSED, "spec_unverified"),
    ("spec_unverified_fix", lambda c: c.unverified.update({IDS["fix"]: "unsigned"}),
     EndCause.MERGE_REFUSED, "spec_unverified"),
    ("verdict_missing", _set("review", None), EndCause.MERGE_REFUSED, "verdict_unreadable"),
    ("verdict_schema", lambda c: c.review.update(sha="short"), EndCause.MERGE_REFUSED, "verdict_unreadable"),
    ("proof_schema", lambda c: c.proof.update(outcome="MAYBE"), EndCause.MERGE_REFUSED, "verdict_unreadable"),
    ("not_proved", lambda c: c.proof.update(outcome="NOT_PROVED"), EndCause.MERGE_REFUSED, "not_proved"),
    ("review_not_at_head",
     lambda c: c.docs[IDS["fix"]]["result_summary"]["git"].update(pushed_head=OTHER),
     EndCause.MERGE_REFUSED, "review_not_at_head"),
    ("heads_disagree_review_json", lambda c: c.review.update(sha=OTHER), EndCause.MERGE_REFUSED, "heads_disagree"),
    ("heads_disagree_proof_clone",
     lambda c: c.docs[IDS["proof"]]["result_summary"]["git"].update(clone_commit=OTHER),
     EndCause.MERGE_REFUSED, "heads_disagree"),
    ("heads_disagree_proof_json", lambda c: c.proof.update(sha=OTHER), EndCause.MERGE_REFUSED, "heads_disagree"),
    ("heads_disagree_author_push",
     lambda c: c.docs[IDS["author"]]["result_summary"]["git"].update(pushed_head=OTHER),
     EndCause.MERGE_REFUSED, "heads_disagree"),
    ("title_placeholder_in_review", lambda c: c.review.update(title="  [Swarm] task_abc"),
     EndCause.MERGE_REFUSED, "title_placeholder"),
    ("forge_host_invalid", lambda c: c.environ.update(FORGE_HOST="github.example.invalid"),
     EndCause.CANNOT_START, "forge_host_invalid"),
    ("app_not_installed",
     lambda c: c.github.route("GET", "/repos/acme/widgets/installation", (404, {}, {})),
     EndCause.CANNOT_START, "app_rejected"),
    ("token_cannot_push", lambda c: c.repository.update(permissions={"push": False}),
     EndCause.CANNOT_START, "token_cannot_push"),
    ("pull_request_closed", lambda c: c.pr.update(state="closed"), EndCause.MERGE_REFUSED, "pull_request_closed"),
    ("pull_request_not_this_workflows_branch", lambda c: c.pr["head"].update(ref="swarm/task_other"),
     EndCause.MERGE_REFUSED, "pull_request_not_this_workflows"),
    ("pull_request_from_a_fork", lambda c: c.pr["head"].update(repo={"full_name": "mallory/widgets"}),
     EndCause.MERGE_REFUSED, "pull_request_not_this_workflows"),
    ("base_not_default", lambda c: c.pr["base"].update(ref="release"), EndCause.MERGE_REFUSED, "base_not_default"),
    ("draft", lambda c: c.pr.update(draft=True), EndCause.MERGE_REFUSED, "draft"),
    ("head_moved", lambda c: c.pr["head"].update(sha=OTHER), EndCause.MERGE_REFUSED, "head_moved"),
    ("title_placeholder_on_pr", lambda c: c.pr.update(title="[swarm] task_abc"),
     EndCause.MERGE_REFUSED, "title_placeholder"),
    ("title_changed", lambda c: c.pr.update(title="A different headline"), EndCause.MERGE_REFUSED, "title_changed"),
    ("touches_workflows", lambda c: c.files.append({"filename": ".github/workflows/release.yml"}),
     EndCause.MERGE_REFUSED, "touches_protected_paths"),
    ("touches_conftest", lambda c: c.files.append({"filename": "tests/unit/conftest.py"}),
     EndCause.MERGE_REFUSED, "touches_protected_paths"),
    ("renamed_out_of_scripts",
     lambda c: c.files.append({"filename": "tools/x.sh", "previous_filename": "scripts/x.sh"}),
     EndCause.MERGE_REFUSED, "touches_protected_paths"),
    ("too_many_files", lambda c: c.files.extend({"filename": f"apps/f{i}.py"} for i in range(3000)),
     EndCause.MERGE_REFUSED, "too_many_files"),
    ("no_required_checks", _set("rules", [{"type": "deletion"}]), EndCause.MERGE_REFUSED, "no_required_checks"),
    ("required_check_unpinned",
     lambda c: c.rules[0]["parameters"]["required_status_checks"].append({"context": "secret scan"}),
     EndCause.MERGE_REFUSED, "required_check_unpinned"),
    ("required_check_missing", lambda c: c.runs.pop(0), EndCause.MERGE_REFUSED, "checks_pending"),
    ("required_check_queued", lambda c: c.runs[0].update(status="queued", conclusion=None),
     EndCause.MERGE_REFUSED, "checks_pending"),
    ("required_check_neutral", lambda c: c.runs[0].update(conclusion="neutral"),
     EndCause.MERGE_REFUSED, "checks_failed"),
    ("required_check_by_another_app", lambda c: c.runs[0].update(app={"id": 999}),
     EndCause.MERGE_REFUSED, "checks_pending"),
    ("other_check_failed", lambda c: c.runs.append(
        {"name": "terraform", "status": "completed", "conclusion": "failure", "app": {"id": ACTIONS_APP}}),
     EndCause.MERGE_REFUSED, "checks_failed"),
    ("other_check_stale", lambda c: c.runs.append(
        {"name": "terraform", "status": "completed", "conclusion": "stale", "app": {"id": ACTIONS_APP}}),
     EndCause.MERGE_REFUSED, "checks_failed"),
    ("other_check_running", lambda c: c.runs.append(
        {"name": "terraform", "status": "in_progress", "conclusion": None, "app": {"id": ACTIONS_APP}}),
     EndCause.MERGE_REFUSED, "checks_pending"),
    ("verdict_mismatch", lambda c: c.review.update(verdict="NOT_YET"), EndCause.MERGE_REFUSED, "verdict_mismatch"),
    ("no_review_from_the_app", lambda c: c.reviews[0]["user"].update(id=1),
     EndCause.MERGE_REFUSED, "verdict_not_approved"),
    ("review_at_another_commit", lambda c: c.reviews[0].update(commit_id=OTHER),
     EndCause.MERGE_REFUSED, "verdict_not_approved"),
    ("latest_review_dismissed", lambda c: c.reviews.append(
        {"id": 100, "user": {"id": BOT_ID}, "state": "DISMISSED", "commit_id": PINNED}),
     EndCause.MERGE_REFUSED, "verdict_not_approved"),
    ("conflict", lambda c: c.pr.update(mergeable=False), EndCause.MERGE_REFUSED, "conflict"),
    ("mergeability_unknown", lambda c: c.pr.update(mergeable=None), EndCause.MERGE_REFUSED, "mergeability_unknown"),
    ("merge_409", _set("merge_answer", (409, {}, {"message": "Head branch was modified"})),
     EndCause.MERGE_REFUSED, "head_moved"),
    ("merge_405", _set("merge_answer", (405, {}, {"message": "not allowed"})),
     EndCause.MERGE_FAILED, "forge_refused"),
    ("merge_redirect", _set("merge_answer", (307, {"Location": "https://elsewhere.invalid"}, None)),
     EndCause.MERGE_FAILED, "forge_redirect_refused"),
    ("read_redirect", lambda c: c.github.route("GET", f"{PR}/files", (302, {"Location": "/x"}, None)),
     EndCause.MERGE_REFUSED, "forge_redirect_refused"),
    ("merges_block_missing_proof", lambda c: None, EndCause.MERGE_REFUSED, "merges_invalid"),
]


@pytest.mark.parametrize(("name", "mutate", "cause", "code"), CASES, ids=[c[0] for c in CASES])
def test_each_failed_check_refuses_with_its_end_cause(tmp_path, name, mutate, cause, code):
    chain = Chain(tmp_path)
    mutate(chain)
    merges = None
    if name == "merges_block_missing_proof":
        merges = {k: v for k, v in IDS.items() if k != "proof"}
    outcome = merge.run_merge(chain.context(merges=merges))
    assert outcome.state is TaskState.FAILED, (name, outcome.summary)
    assert outcome.end_cause is cause, (name, outcome.end_cause, outcome.message)
    assert outcome.summary["refusal"]["code"] == code, outcome.summary["refusal"]
    assert outcome.retryable is False
    if not name.startswith("merge_") and name != "merge_redirect":
        assert _merge_calls(chain) == [], f"{name}: the merge was called anyway"


def test_a_spec_refusal_carries_the_upstream_reason(tmp_path):
    chain = Chain(tmp_path)
    chain.unverified[IDS["review"]] = "signature_mismatch"
    outcome = merge.run_merge(chain.context())
    assert outcome.spec_check["reason"] == f"upstream:{IDS['review']}:signature_mismatch"
    # Checked before any credential: GitHub was never asked anything.
    assert chain.github.seen == []


def test_credential_free_refusals_never_read_the_secret_or_ask_github(tmp_path):
    chain = Chain(tmp_path)
    chain.proof["outcome"] = "NOT_PROVED"
    read: list[int] = []
    ctx = chain.context()
    original = ctx.read_app_key
    ctx.read_app_key = lambda: read.append(1) or original()
    merge.run_merge(ctx)
    assert read == [] and chain.github.seen == []


def test_mergeable_null_is_read_again_twice_two_seconds_apart(tmp_path):
    chain = Chain(tmp_path)
    chain.pr["mergeable"] = None
    merge.run_merge(chain.context())
    assert chain.slept == [2.0, 2.0]


def test_an_already_merged_pull_request_at_the_pinned_head_succeeds_without_merging(tmp_path):
    chain = Chain(tmp_path)
    chain.pr.update(merged=True, state="closed", merged_by={"login": "someone"})
    outcome = merge.run_merge(chain.context())
    assert outcome.state is TaskState.SUCCEEDED
    assert outcome.summary["merged_by_this_task"] is False
    assert _merge_calls(chain) == []


def test_an_already_merged_pull_request_at_another_head_is_refused(tmp_path):
    chain = Chain(tmp_path)
    chain.pr.update(merged=True, state="closed")
    chain.pr["head"]["sha"] = OTHER
    outcome = merge.run_merge(chain.context())
    assert outcome.summary["refusal"]["code"] == "merged_at_other_head"


def test_a_cancel_before_the_merge_call_cancels_and_merges_nothing(tmp_path):
    chain = Chain(tmp_path)
    chain.cancel = True
    outcome = merge.run_merge(chain.context())
    assert outcome.state is TaskState.CANCELLED
    assert outcome.end_cause is EndCause.CANCEL_REQUESTED
    assert _merge_calls(chain) == []
    assert outcome.summary["token_revoked"] is True


def test_a_retargeted_base_before_the_call_is_refused(tmp_path):
    chain = Chain(tmp_path)
    reads = {"n": 0}
    original = chain.github.routes[("GET", PR)]

    def pull(seen):
        reads["n"] += 1
        status, headers, body = original(seen)
        if reads["n"] >= 2:
            body = {**body, "base": {**body["base"], "ref": "unprotected"}}
        return status, headers, body

    chain.github.route("GET", PR, pull)
    outcome = merge.run_merge(chain.context())
    assert outcome.summary["refusal"]["code"] == "base_retargeted"
    assert _merge_calls(chain) == []


def test_a_base_that_changed_after_the_merge_is_recorded_not_undone(tmp_path):
    chain = Chain(tmp_path)
    chain.base_after_merge = "elsewhere"
    outcome = merge.run_merge(chain.context())
    assert outcome.state is TaskState.SUCCEEDED
    assert outcome.summary["base_mismatch_recorded"] == {"before": "main", "after": "elsewhere"}


def test_a_failed_comment_leaves_the_merge_standing(tmp_path):
    chain = Chain(tmp_path)
    chain.github.route("POST", "/repos/acme/widgets/issues/12/comments", (500, {}, {}))
    outcome = merge.run_merge(chain.context())
    assert outcome.state is TaskState.SUCCEEDED
    assert outcome.summary["recorded_on_pull_request"] is False


def test_a_forge_outage_is_retryable_merge_failed(tmp_path):
    chain = Chain(tmp_path)
    chain.github.route("GET", f"{PR}/reviews", (503, {"Retry-After": "600"}, {}))
    outcome = merge.run_merge(chain.context())
    assert outcome.retryable is True
    assert outcome.end_cause is EndCause.MERGE_FAILED
    assert outcome.retry_delay_seconds == 600


def test_the_human_gate_ends_succeeded_awaiting_a_person_with_nothing_merged(tmp_path):
    """B13r's hook: every check passes, then the step stops before the merge."""
    chain = Chain(tmp_path)
    chain.human_gate = True
    outcome = merge.run_merge(chain.context())
    assert outcome.state is TaskState.SUCCEEDED
    assert outcome.summary["awaiting_human"] is True
    assert outcome.summary["merged_by_this_task"] is False
    assert _merge_calls(chain) == []


def test_the_token_is_revoked_after_a_refusal_on_the_forge(tmp_path):
    chain = Chain(tmp_path)
    chain.pr["draft"] = True
    outcome = merge.run_merge(chain.context())
    assert outcome.summary["token_revoked"] is True
    assert chain.github.calls("DELETE", "/installation/token")
    assert chain.github.token not in repr(outcome.summary)


def test_the_placeholder_rule_matches_the_workers_pr_title_rule():
    """The pr-title.txt rules (#214): a title the author's worker refuses as the
    retired placeholder is one the merge refuses, and one it accepts is not
    refused here for that reason."""
    from agent_worker.lifecycle import _RETIRED_TITLE_RE

    assert merge.RETIRED_TITLE_RE.pattern == _RETIRED_TITLE_RE.pattern
    assert merge.RETIRED_TITLE_RE.flags == _RETIRED_TITLE_RE.flags
    titles = ("[swarm] task_1", "[SWARM] TASK_x", "  [swarm] task_y", "\t[Swarm] task_",
              "Fix [swarm] task_ handling", "[swarm]task_x", "[swarm] output marker",
              "s] task_x", TITLE)
    for title in titles:
        assert merge.title_is_placeholder(title) == bool(_RETIRED_TITLE_RE.search(title)), title
