"""A contributor that published nothing by design is not a missing branch (#760).

Under `integrate`, every step but the integrator is dispatched as a
contributor, a review step included. A review that applies the change it was
handed, writes `verdict.json`, reverses the patch and commits nothing ends
SUCCEEDED with `result_summary.git.published == false` and `commit_count == 0`.
It never had a branch to push. Merging it anyway made the integrator fetch
`swarm/<review>`, fail, and print it under "NOT included -- these branches were
not found on the remote", which release acceptance (rightly) reads as a
partial integration.

What is pinned here, each against the way it would most likely go wrong:

  * such a contributor is not fetched and not listed `- missing:`; the pull
    request may name it on a neutral line, never under the NOT included
    heading;
  * a FAILED contributor is still `- missing:` -- it should have pushed;
  * a SUCCEEDED contributor that DID publish, whose branch is nevertheless
    absent, is still `- missing:` -- the reason `missing` exists;
  * the implement -> review -> fix shape of
    `scripts/acceptance/groups/workflow.sh` yields a body with no `- missing:`
    line, and the implement branch merged.
"""

from __future__ import annotations

import shutil
from typing import Any

import pytest

from agent_worker import lifecycle
from swarm_common.states import TaskState

from worker_seeds import TENANT
from test_strategy_end_to_end import (  # noqa: F401  (pytest fixtures)
    _the_agent_titles_its_pull_request,
    contributor_edit,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: What a review step that reversed its patch and committed nothing records.
READ_ONLY_GIT = {
    "base": "a" * 40, "head": "a" * 40, "patch": None, "patch_bytes": 0,
    "commit_count": 0, "dirty_count": 0, "published": False,
    "publish_reason": "the agent changed nothing, so there is nothing to publish",
}


def _seed(db: Any, task_id: str, *, state: TaskState, git: dict[str, Any] | None) -> None:
    summary: dict[str, Any] = {"artifacts": [{"name": "verdict.json"}]}
    if git is not None:
        summary["git"] = dict(git)
    db.seed(f"tasks/{task_id}", {
        "id": task_id, "tenant_id": TENANT, "state": state.value, "result_summary": summary,
    })


def _record_merges(monkeypatch) -> list[list[str]]:
    """Wrap the real merge so the branches it was asked to fetch are visible."""
    asked: list[list[str]] = []
    real = lifecycle.merge_branches

    def recording(**kwargs: Any):
        asked.append(list(kwargs["branches"]))
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "merge_branches", recording)
    return asked


def _integrate(worker_factory, monkeypatch, origin, *, task_id: str, integrates: list[str]):
    return run_attempt(
        worker_factory, monkeypatch, origin,
        task_id=task_id,
        dispatch={
            "strategy": "integrate", "carrier": "branches", "role": "integrator",
            "integrates": integrates,
        },
        edit=contributor_edit(task_id),
    )


def _publish_contributor(worker_factory, monkeypatch, origin, db, task_id: str) -> None:
    """Run one contributor for real (it pushes `swarm/<task_id>`) and record
    the SUCCEEDED result it would have finished with."""
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id=task_id,
        dispatch={"strategy": "integrate", "carrier": "branches", "role": "contributor"},
        edit=contributor_edit(task_id),
    )
    assert out["published"] is True
    _seed(db, task_id, state=TaskState.SUCCEEDED, git=out)


# -- the pure decision ------------------------------------------------------


def test_published_nothing_reads_only_a_clean_unpublished_success_summary():
    assert lifecycle.published_nothing({"git": READ_ONLY_GIT}) is True
    # The controls: each of these could have pushed, so none is read-only.
    assert lifecycle.published_nothing({"git": {**READ_ONLY_GIT, "published": True}}) is False
    assert lifecycle.published_nothing({"git": {**READ_ONLY_GIT, "commit_count": 2}}) is False
    assert lifecycle.published_nothing({"git": {**READ_ONLY_GIT, "dirty_count": 1}}) is False
    assert lifecycle.published_nothing(
        {"git": {k: v for k, v in READ_ONLY_GIT.items() if k != "commit_count"}}
    ) is False
    assert lifecycle.published_nothing({"git": {**READ_ONLY_GIT, "error": "harvest"}}) is False
    assert lifecycle.published_nothing({}) is False
    assert lifecycle.published_nothing(None) is False


def test_the_integrator_leaves_out_a_contributor_that_published_nothing(db, worker_factory):
    _seed(db, "task_impl", state=TaskState.SUCCEEDED, git={**READ_ONLY_GIT, "published": True,
                                                            "commit_count": 1})
    _seed(db, "task_review", state=TaskState.SUCCEEDED, git=READ_ONLY_GIT)
    _seed(db, "task_broken", state=TaskState.FAILED, git=READ_ONLY_GIT)
    worker, _config, _ = worker_factory(task_id="task_fix")
    worker._task = {"metadata": {"dispatch": {
        "strategy": "integrate", "role": "integrator",
        "integrates": ["task_impl", "task_review", "task_broken"],
    }}}

    # A FAILED contributor is merged as before, so the merge names it missing.
    assert worker._integrates_with_changes() == ["task_impl", "task_broken"]
    assert worker._read_only_contributors == ["task_review"]
    assert worker._no_change_contributors == []


# -- end to end, against a real remote --------------------------------------


def test_a_read_only_contributor_is_not_fetched_and_not_listed_missing(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _publish_contributor(worker_factory, monkeypatch, origin, db, "t-impl")
    _seed(db, "t-review", state=TaskState.SUCCEEDED, git=READ_ONLY_GIT)
    asked = _record_merges(monkeypatch)

    _, _, out = _integrate(worker_factory, monkeypatch, origin,
                           task_id="t-fix", integrates=["t-impl", "t-review"])

    assert asked == [["swarm/t-impl"]], "the integrator fetched a branch nobody pushed"
    assert out["integrated"]["merged"] == ["swarm/t-impl"]
    assert out["integrated"]["missing"] == []
    assert out["integrated"]["complete"] is True
    assert out["integrated"]["read_only"] == ["swarm/t-review"]
    body = forge.pulls[0]["body"]
    assert "- missing:" not in body
    assert "NOT included" not in body
    assert "Integrates 1 contributor branch(es):" in body
    assert "read-only, nothing to merge: `swarm/t-review`" in body


def test_a_failed_contributor_is_still_listed_missing(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _publish_contributor(worker_factory, monkeypatch, origin, db, "t-impl")
    _seed(db, "t-broken", state=TaskState.FAILED, git=READ_ONLY_GIT)

    _, _, out = _integrate(worker_factory, monkeypatch, origin,
                           task_id="t-fix", integrates=["t-impl", "t-broken"])

    assert out["integrated"]["missing"] == ["swarm/t-broken"]
    assert "read_only" not in out["integrated"]
    body = forge.pulls[0]["body"]
    assert "- missing: `swarm/t-broken`" in body
    assert "read-only" not in body


def test_a_published_contributor_whose_branch_is_absent_is_still_listed_missing(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _publish_contributor(worker_factory, monkeypatch, origin, db, "t-impl")
    # It says it pushed; the remote has no such branch (deleted, renamed).
    _seed(db, "t-gone", state=TaskState.SUCCEEDED,
          git={**READ_ONLY_GIT, "published": True, "commit_count": 1})
    assert "swarm/t-gone" not in refs(origin)

    _, _, out = _integrate(worker_factory, monkeypatch, origin,
                           task_id="t-fix", integrates=["t-impl", "t-gone"])

    assert out["integrated"]["missing"] == ["swarm/t-gone"]
    assert "- missing: `swarm/t-gone`" in forge.pulls[0]["body"]


def test_the_acceptance_implement_review_fix_chain_has_no_missing_line(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """scripts/acceptance/groups/workflow.sh's "workflow: integrate chain":
    implement changes a line, review applies it, writes verdict.json and
    reverses it, fix integrates both. Its github-verify assertion fails on any
    `^- (conflicted|missing): ` line."""
    _publish_contributor(worker_factory, monkeypatch, origin, db, "t-implement")
    # review: run for real as a contributor that leaves the clone as cloned.
    _, _, review = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-review",
        dispatch={"strategy": "integrate", "carrier": "branches", "role": "contributor"},
        edit=lambda repo: None,
    )
    assert review["published"] is False
    assert "swarm/t-review" not in refs(origin)
    _seed(db, "t-review", state=TaskState.SUCCEEDED, git=review)

    _, _, out = _integrate(worker_factory, monkeypatch, origin,
                           task_id="t-fix", integrates=["t-implement", "t-review"])

    body = forge.pulls[0]["body"]
    lines = body.splitlines()
    assert not [line for line in lines if line.startswith(("- missing: ", "- conflicted: "))], body
    assert "- merged: `swarm/t-implement`" in lines
    assert out["integrated"]["complete"] is True
