"""A step whose verdict gate skipped its agent publishes the reviewed branch, or fails saying why (PUB-SKIP).

    implement ──> review ──> fix (integrator; when: review verdict_in [NOT_YET]) ──> re-review

Measured 2026-10-12 23:30Z (workflow wf_18f0b5e64639417f83d6, fix step
task_58ecd704c73a433b9782): the review said MERGE, so the fix step skipped
its agent, and it ended SUCCEEDED, exit 0, with no push and no error. The
re-review, which `builds_on` the fix step, then failed cloning
`swarm/task_58ecd704c73a433b9782`: not found. The skip path reaches the same
publish as a step whose agent ran (`_finish_without_agent` -> `_finalise`),
but every way that publish can decline to push -- a token that may not push,
a refused git token, a forge this worker cannot publish to, a harvest that
short-circuited on "the agent changed nothing" -- was recorded only as
`git.published: false`, and the finish exempted a shut gate from the one
check (`_published_nothing`) that would have read it. Pinned here, against a
real bare remote:

  * the skip path pushes `swarm/<fix>` and opens the pull request, and says
    so with a `published` event carrying the branch and the pull request;
  * a step whose agent ran says so with the same event;
  * a skip-path publish that cannot push ends FAILED with
    `publish_failed: <reason>`, in the error and in a `publish_failed` event,
    never SUCCEEDED without a branch;
  * an integrator with no contributor left to merge still pushes the branch
    it built on when its gate skipped the agent, instead of reading "the agent
    changed nothing" as nothing to publish.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from swarm_common.states import EventType, TaskState

import spec_keys
from worker_seeds import TENANT, seed_attempt
from test_input_from import run_upstream
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_fence_holds,
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

WORKFLOW = "wf_pubskip"
IMPL = "task_impl"
REVIEW = "task_review"
FIX = "task_fix"
LABEL = "Guide page"


def _implement(db: Any, worker_factory: Any, monkeypatch: Any, origin: Path) -> None:
    """A contributor that really clones, edits apps/swarm-ui and pushes `swarm/<IMPL>`."""

    def edit(repo: Path) -> None:
        (repo / "apps" / "swarm-ui").mkdir(parents=True)
        (repo / "apps" / "swarm-ui" / "Guide.tsx").write_text("export const Guide = 1;\n")

    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id=IMPL,
        dispatch={"strategy": "integrate", "carrier": "checkpoints", "role": "contributor"},
        edit=edit,
    )
    assert out["published"] is True, out
    doc = {
        "id": IMPL, "task_id": IMPL, "tenant_id": TENANT, "workflow_id": WORKFLOW,
        "state": TaskState.SUCCEEDED.value, "runner_profile": "mock",
        "input": {"prompt": "write the guide page"},
        "metadata": {"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                  "role": "contributor"}},
        "result_summary": {"git": {"published": True}},
    }
    # The document the API wrote for the implementer, signed as it signs it.
    db.seed(f"tasks/{IMPL}", spec_keys.sign_document(doc, IMPL))


def _review(db: Any, worker_factory: Any, verdict: str) -> None:
    run_upstream(
        db, worker_factory, task_id=REVIEW, attempt_id="att_review", lease_id="lease_review",
        artifact_name="verdict.json",
        artifact_text=json.dumps({"verdict": verdict, "findings": ["nothing blocks"]}),
    )
    db.doc(f"tasks/{REVIEW}")["result_summary"]["git"] = {
        "published": False, "commit_count": 0, "dirty_count": 0,
    }


def _seed_fix(db: Any, *, integrates: list[str] | None = None) -> None:
    seed_attempt(
        db, task_id=FIX, attempt_id="att_fix", lease_id="lease_fix",
        task_input={"prompt": "fix the review's findings", "steps": 1, "sleep_seconds": 0.01},
    )
    doc = db.doc(f"tasks/{FIX}")
    doc["workflow_id"] = WORKFLOW
    doc["metadata"] = {
        "input_from": {REVIEW: "verdict.json"},
        "dispatch": {
            "strategy": "integrate",
            "carrier": "checkpoints",
            "role": "integrator",
            "integrates": [IMPL, REVIEW] if integrates is None else integrates,
            "builds_on": IMPL,
            "verdict_gate": {"task_id": REVIEW, "verdict_in": ["NOT_YET"]},
            "pr_label": LABEL,
        },
    }


def _run_fix(worker_factory: Any, monkeypatch: Any, origin: Path, *, agent: bool) -> int:
    worker, _config, _ = worker_factory(
        task_id=FIX, attempt_id="att_fix", lease_id="lease_fix",
        repository_url=f"file://{origin}",
    )
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    if agent:
        # The fix agent here is the mock runner, which writes no
        # `pr-title.txt`; the title is not what this file is about.
        monkeypatch.setattr(worker, "_title_owed", lambda _task: False)
    else:
        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("a fix agent was started after a MERGE verdict")

        monkeypatch.setattr(worker, "_run_child_supervised", refuse)
    return worker.run()


def _marks(db: Any, cause: str) -> list[dict[str, Any]]:
    return [
        event["detail"] for event in db.events(FIX)
        if event.get("type") == EventType.RUNNING.value
        and isinstance(event.get("detail"), dict)
        and event["detail"].get("cause") == cause
    ]


def test_the_skip_path_pushes_the_branch_opens_the_pr_and_says_published(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _implement(db, worker_factory, monkeypatch, origin)
    _review(db, worker_factory, "MERGE")
    _seed_fix(db)

    assert _run_fix(worker_factory, monkeypatch, origin, agent=False) == ExitCode.OK

    task = db.doc(f"tasks/{FIX}")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["result_summary"]["skipped_agent"] == "review verdict MERGE"
    branch = f"swarm/{FIX}"
    assert "apps/swarm-ui/Guide.tsx" in tree_at(origin, branch)
    assert [pull["head"] for pull in forge.pulls] == [branch]
    published = _marks(db, "published")
    assert len(published) == 1, db.events(FIX)
    assert published[0]["branch"] == branch
    assert published[0]["head"] == refs(origin)[branch]
    assert published[0]["pull_request"]["url"] == "https://github.com/acme/widgets/pull/1"
    assert published[0]["agent_ran"] is False
    assert _marks(db, "publish_failed") == []


def test_a_step_whose_agent_ran_says_published_too(
    db, worker_factory, monkeypatch, origin, local_urls, forge, runner_inputs
):
    _implement(db, worker_factory, monkeypatch, origin)
    _review(db, worker_factory, "NOT_YET")
    _seed_fix(db)

    rc = _run_fix(worker_factory, monkeypatch, origin, agent=True)
    assert rc == ExitCode.OK, db.doc(f"tasks/{FIX}").get("last_error")

    task = db.doc(f"tasks/{FIX}")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    branch = f"swarm/{FIX}"
    assert branch in refs(origin)
    published = _marks(db, "published")
    assert len(published) == 1, db.events(FIX)
    assert published[0]["branch"] == branch
    assert published[0]["pull_request"]["number"] == 1
    assert published[0]["agent_ran"] is True


def test_a_skip_path_publish_that_cannot_push_fails_with_the_reason(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _implement(db, worker_factory, monkeypatch, origin)
    _review(db, worker_factory, "MERGE")
    _seed_fix(db)
    # The tenant's token may read this repository and not push to it: the
    # publish declines, and used to record only `published: false`.
    forge.can_push = False

    assert _run_fix(worker_factory, monkeypatch, origin, agent=False) == ExitCode.FAILED

    task = db.doc(f"tasks/{FIX}")
    assert task["state"] == TaskState.FAILED.value
    assert task["last_error"].startswith("publish_failed: "), task["last_error"]
    assert "the token has pull but not push" in task["last_error"]
    assert f"swarm/{FIX}" not in refs(origin)
    failed = _marks(db, "publish_failed")
    assert len(failed) == 1, db.events(FIX)
    assert "the token has pull but not push" in failed[0]["reason"]
    assert _marks(db, "published") == []


def test_a_skip_path_with_no_contributor_to_merge_still_pushes_the_branch_it_built_on(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """No merge is pending, and the clone is the reviewed branch, unchanged:
    "the agent changed nothing" is the gate's doing, not an empty result."""
    _implement(db, worker_factory, monkeypatch, origin)
    _review(db, worker_factory, "MERGE")
    _seed_fix(db, integrates=[])

    assert _run_fix(worker_factory, monkeypatch, origin, agent=False) == ExitCode.OK

    task = db.doc(f"tasks/{FIX}")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    branch = f"swarm/{FIX}"
    assert "apps/swarm-ui/Guide.tsx" in tree_at(origin, branch)
    assert [pull["head"] for pull in forge.pulls] == [branch]
    assert len(_marks(db, "published")) == 1


def test_no_shut_gate_pull_request_step_ends_succeeded_without_a_branch(worker_factory):
    """The finish's own rule, read without a forge: a skip-path step that owes
    a pull request and pushed no branch has a `publish_failed` reason."""
    worker, _, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": {"strategy": "integrate", "role": "integrator",
                                              "integrates": [IMPL]}}}
    worker._verdict = {"verdict": "MERGE", "verdict_in": ["NOT_YET"], "agent_ran": False}
    unpushed = {"git": {"published": False, "publish_reason": "publishing is disabled"}}
    reason = worker._skipped_publish_missing(unpushed)
    assert reason is not None and reason.startswith("publish_failed: ")
    assert "publishing is disabled" in reason
    assert worker._skipped_publish_missing({}).startswith("publish_failed: ")
    pushed = {"git": {"published": True, "branch": f"swarm/{FIX}"}}
    assert worker._skipped_publish_missing(pushed) is None
    # A step whose agent ran keeps the rules it always had.
    worker._verdict = {"verdict": "NOT_YET", "verdict_in": ["NOT_YET"], "agent_ran": True}
    assert worker._skipped_publish_missing(unpushed) is None
    # And a step that owes no pull request publishes nothing by design.
    worker._verdict = {"verdict": "MERGE", "verdict_in": ["NOT_YET"], "agent_ran": False}
    worker._task = {"metadata": {"dispatch": {"strategy": "collect"}}}
    assert worker._skipped_publish_missing(unpushed) is None
