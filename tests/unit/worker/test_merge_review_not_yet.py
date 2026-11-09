"""A pull request whose review said NOT_YET is held against `ready` (owner, 9 November 2026).

A lane's merge refused `verdict_not_merge`, which was right; then an operator
labelled the green pull request `ready`, GitHub's native auto-merge merged it
on CI alone, and the regression the review had found reached main. So:

  (a) a `verdict_not_merge` refusal labels the pull request `review-not-yet`,
      takes `ready` off and turns auto-merge off when they are on, and posts
      ONE comment of the review's findings -- summary, file and line, bounded
      and passed through the worker's redaction -- saying a later review must
      say MERGE and a CI fix does not clear it. A failure of any of these is
      logged, and the refusal is exactly what it was;
  (b) a merge step that merges on a MERGE verdict -- a fix loop's merge on its
      final MERGE included -- removes `review-not-yet` before it merges.

scripts/check-closing-references.sh refuses `ready` while the label is on
(tests/unit/scripts/test_check_closing_references.py). No credentials, no
network: GitHub is `merge_world`'s fake, the token made at runtime.
"""

from __future__ import annotations

import dataclasses
import secrets
from typing import Any

import pytest

from agent_worker import merge
from agent_worker.errors import FencedError
from agent_worker.secrets import CredentialMissing
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import API, NUMBER, PR, MergeWorld
from test_fix_loop_merge import _loop_world

LABELS = f"{API}/issues/{NUMBER}/labels"
READY = f"{LABELS}/ready"
NOT_YET = f"{LABELS}/review-not-yet"
COMMENTS = f"{API}/issues/{NUMBER}/comments"
NODE_ID = "PR_kwDOtest41"


def _not_yet_world(tmp_path, *, findings: list[Any] | None = None, ready: bool = True,
                   auto_merge: bool = True) -> MergeWorld:
    """A NOT_YET review of a pull request labelled `ready` with auto-merge armed:
    the 9 November shape. Every write the hold makes is served."""
    world = MergeWorld(tmp_path)
    world.verdict = {"verdict": "NOT_YET", "findings": findings if findings is not None else [
        {"summary": "The retry loop never ends when the forge answers 500",
         "file": "apps/widget/retry.py", "line": 42, "side": "new"},
        "The new column is not migrated",
    ]}
    world.pr["labels"] = [{"name": "ready"}] if ready else []
    world.pr["auto_merge"] = {"merge_method": "squash"} if auto_merge else None
    world.pr["node_id"] = NODE_ID
    _serve_hold(world)
    return world


def _serve_hold(world: MergeWorld, *, status: int = 200) -> None:
    gh = world.github
    gh.route("POST", LABELS, (status, {}, [{"name": "review-not-yet"}]))
    gh.route("DELETE", READY, (status, {}, []))
    gh.route("DELETE", NOT_YET, (status, {}, []))
    gh.route("GET", COMMENTS, (200, {}, []))
    closing = gh.routes[("POST", "/graphql")]

    def graphql(seen):
        if "disablePullRequestAutoMerge" in seen.body["query"]:
            if status != 200:
                return 200, {}, {"errors": [{"message": "Resource not accessible by integration"}]}
            return 200, {}, {"data": {"disablePullRequestAutoMerge": {
                "pullRequest": {"number": NUMBER}}}}
        return closing(seen)

    gh.route("POST", "/graphql", graphql)


def _disables(world: MergeWorld) -> list[Any]:
    return [s for s in world.github.calls("POST", "/graphql")
            if "disablePullRequestAutoMerge" in s.body["query"]]


def _comments(world: MergeWorld) -> list[str]:
    return [s.body["body"] for s in world.github.calls("POST", COMMENTS)]


def _baseline(tmp_path, **kwargs: Any):
    """The refusal with no hold at all: no forge routes for it, no token."""
    (tmp_path / "baseline").mkdir(exist_ok=True)
    world = _not_yet_world(tmp_path / "baseline", **kwargs)
    world.repository_url = None
    outcome = merge.run_merge(world.context())
    assert world.token_reads == 0
    return outcome


def _same_refusal(outcome, baseline) -> None:
    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.retryable is False
    assert outcome.summary["refusal"] == baseline.summary["refusal"]
    assert outcome.summary["refusal"]["code"] == "verdict_not_merge"
    assert outcome.message == baseline.message


# ---------------------------------------------------------------------------
# (a) the refusal holds the pull request
# ---------------------------------------------------------------------------

def test_a_not_yet_refusal_labels_unreadies_disarms_and_comments_once(tmp_path):
    """MUTATION: refuse as before #9-November and nothing on the pull request changes."""
    world = _not_yet_world(tmp_path)

    outcome = merge.run_merge(world.context())

    _same_refusal(outcome, _baseline(tmp_path))
    assert world.merge_calls() == []
    (label,) = world.github.calls("POST", LABELS)
    assert label.body == {"labels": ["review-not-yet"]}
    assert len(world.github.calls("DELETE", READY)) == 1
    (disable,) = _disables(world)
    assert disable.body["variables"] == {"pullRequestId": NODE_ID}
    (body,) = _comments(world)
    assert body.startswith(f"{merge.REVIEW_NOT_YET_MARKER}task_rev -->")
    assert "The retry loop never ends when the forge answers 500 (apps/widget/retry.py:42)" in body
    assert "The new column is not migrated" in body
    assert "A later review must say MERGE before this pull request can merge" in body
    assert "A CI fix does not clear it" in body
    assert "`review-not-yet`" in body
    assert outcome.summary["review_not_yet"] == {
        "pull_request": NUMBER, "label": True, "read": True, "disable_auto_merge": True,
        "remove_ready": True, "comment": True}
    # The credential is the step's own, read once; never in a log line.
    assert world.token_reads == 1
    assert world.token not in world.log.text


def test_the_label_lands_before_anything_else(tmp_path):
    world = _not_yet_world(tmp_path)
    merge.run_merge(world.context())
    writes = [(s.method, s.path) for s in world.github.seen if s.method != "GET"]
    assert writes[0] == ("POST", LABELS), writes


def test_without_ready_or_auto_merge_only_the_label_and_the_comment_are_written(tmp_path):
    world = _not_yet_world(tmp_path, ready=False, auto_merge=False)

    outcome = merge.run_merge(world.context())

    _same_refusal(outcome, _baseline(tmp_path, ready=False, auto_merge=False))
    assert len(world.github.calls("POST", LABELS)) == 1
    assert world.github.calls("DELETE", READY) == []
    assert _disables(world) == []
    assert len(_comments(world)) == 1


def test_the_comment_is_posted_once_per_review(tmp_path):
    """A refusal reached again for the same review says nothing new.
    MUTATION: post without reading the thread."""
    world = _not_yet_world(tmp_path)
    world.github.route("GET", COMMENTS, (200, {}, [
        {"body": f"{merge.REVIEW_NOT_YET_MARKER}task_rev -->\nalready said"}]))

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "verdict_not_merge"
    assert _comments(world) == []
    assert len(world.github.calls("POST", LABELS)) == 1


def test_the_comment_goes_through_the_workers_redaction(tmp_path):
    """MUTATION: post the findings without `ctx.scrub`."""
    leaked = "ghp_" + secrets.token_hex(18)
    world = _not_yet_world(tmp_path, findings=[
        {"summary": f"the fixture logs {leaked} on failure", "file": f"conf/{leaked}.env",
         "line": 3, "side": "new"}])
    ctx = dataclasses.replace(world.context(),
                              scrub=lambda text: text.replace(leaked, "[REDACTED]"))

    merge.run_merge(ctx)

    (body,) = _comments(world)
    assert leaked not in body
    assert "the fixture logs [REDACTED] on failure (conf/[REDACTED].env:3)" in body


def test_the_comment_is_bounded_and_its_fence_is_whole(tmp_path):
    """Fifty findings of a thousand characters each. MUTATION: list them all."""
    world = _not_yet_world(tmp_path, findings=[
        {"summary": f"finding {n} " + "x" * 990, "file": f"src/f{n}.py", "line": n + 1,
         "side": "new"} for n in range(50)])

    merge.run_merge(world.context())

    (body,) = _comments(world)
    assert len(body) <= merge.REVIEW_NOT_YET_COMMENT_CHARS
    assert body.count("```") % 2 == 0
    assert "finding 0 " in body and "finding 49 " not in body
    assert "more finding(s) are in the review's verdict and not shown here" in body
    assert "A later review must say MERGE" in body


def test_a_finding_cannot_close_the_fence(tmp_path):
    world = _not_yet_world(tmp_path, findings=["```\n[click](https://example.invalid) @someone"])
    merge.run_merge(world.context())
    (body,) = _comments(world)
    assert body.count("```") == 2


@pytest.mark.parametrize("status", [403, 422, 500])
def test_a_forge_error_on_every_write_leaves_the_refusal_unchanged(tmp_path, status):
    """MUTATION: let a forge error out of the hold and the step's result changes."""
    world = _not_yet_world(tmp_path)
    _serve_hold(world, status=status)
    world.github.route("POST", COMMENTS, (status, {}, {"message": "nope"}))

    outcome = merge.run_merge(world.context())

    _same_refusal(outcome, _baseline(tmp_path))
    # Each action was tried, though the one before it failed.
    assert len(world.github.calls("POST", LABELS)) == 1
    assert len(world.github.calls("DELETE", READY)) == 1
    assert len(_disables(world)) == 1
    assert outcome.summary["review_not_yet"]["label"] is False
    assert outcome.summary["review_not_yet"]["disable_auto_merge"] is False
    assert "the NOT_YET hold could not disable auto merge" in world.log.text
    assert world.token not in world.log.text


def test_an_unreadable_pull_request_still_labels_and_comments(tmp_path):
    world = _not_yet_world(tmp_path)
    world.github.route("GET", PR, (502, {}, {"message": "bad gateway"}))

    outcome = merge.run_merge(world.context())

    _same_refusal(outcome, _baseline(tmp_path))
    assert len(world.github.calls("POST", LABELS)) == 1
    # Whether `ready` is on is unknown: it is asked off anyway.
    assert len(world.github.calls("DELETE", READY)) == 1
    assert len(_comments(world)) == 1


def test_a_missing_credential_leaves_the_refusal_unchanged(tmp_path):
    world = _not_yet_world(tmp_path)
    ctx = world.context()

    def missing() -> str:
        raise CredentialMissing("swarm-tenant-eng-git")

    outcome = merge.run_merge(dataclasses.replace(ctx, read_git_token=missing))

    _same_refusal(outcome, _baseline(tmp_path))
    assert outcome.state is TaskState.FAILED
    assert world.github.calls("POST", LABELS) == []


def test_a_stale_worker_touches_nothing(tmp_path):
    """Invariant 5: a stale generation exits without touching the pull request."""
    world = _not_yet_world(tmp_path)
    world.stale_after = 0

    with pytest.raises(FencedError):
        merge.run_merge(world.context())

    assert [s for s in world.github.seen if s.method != "GET"] == []


def test_a_fix_loops_final_not_yet_holds_the_pull_request(tmp_path):
    world = _loop_world(tmp_path, verdict={"verdict": "NOT_YET", "findings": ["still broken"]})
    world.pr["labels"] = [{"name": "ready"}]
    _serve_hold(world)

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "verdict_not_merge"
    assert len(world.github.calls("POST", LABELS)) == 1
    assert len(world.github.calls("DELETE", READY)) == 1
    (body,) = _comments(world)
    assert body.startswith(f"{merge.REVIEW_NOT_YET_MARKER}task_rr2 -->")
    assert "still broken" in body


# ---------------------------------------------------------------------------
# (b) a MERGE verdict clears the label before it merges
# ---------------------------------------------------------------------------

def _merge_world(tmp_path, *, labelled: bool = True, status: int = 200) -> MergeWorld:
    world = MergeWorld(tmp_path)
    world.pr["labels"] = [{"name": "review-not-yet"}] if labelled else []
    _serve_hold(world, status=status)
    return world


def test_a_merge_verdict_removes_review_not_yet_before_it_merges(tmp_path):
    """MUTATION: merge without removing it, and the label sits on main's history."""
    world = _merge_world(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (removed,) = world.github.calls("DELETE", NOT_YET)
    (merged,) = world.merge_calls()
    assert world.github.seen.index(removed) < world.github.seen.index(merged)
    assert outcome.summary["review_not_yet_cleared"] is True


def test_a_merge_verdict_without_the_label_removes_nothing(tmp_path):
    world = _merge_world(tmp_path, labelled=False)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert world.github.calls("DELETE", NOT_YET) == []
    assert "review_not_yet_cleared" not in outcome.summary


def test_a_failed_removal_is_logged_and_the_merge_goes_on(tmp_path):
    world = _merge_world(tmp_path, status=500)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    assert outcome.summary["review_not_yet_cleared"] is False
    assert "`review-not-yet` could not be removed" in world.log.text


def test_a_fix_loops_final_merge_removes_review_not_yet(tmp_path):
    world = _loop_world(tmp_path)
    world.pr["labels"] = [{"name": "review-not-yet"}]
    _serve_hold(world)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (removed,) = world.github.calls("DELETE", NOT_YET)
    (merged,) = world.merge_calls()
    assert world.github.seen.index(removed) < world.github.seen.index(merged)
