"""A two-step issue run whose review says MERGE opens ONE pull request from the integrated branch (#978).

    impl_a ──┐
             ├──> review ──> fix (integrator; when: review verdict_in [NOT_YET])
    impl_b ──┘

swarm-api compiles an issue run with two implement steps to this shape
(`issueruns._compile_staged`): the review stages both implementers'
`swarm-work.patch` and builds on the LAST implementer in plan order, and the
gated fix step is the integrator -- `integrates` every earlier step,
`builds_on` that same last implementer, reading the review's `verdict.json`.

wf_ca1807e43ac64d6b8afd (2026-10-09): impl_b changed nothing, so the
"nothing to change" rule skipped the integrator because the step it builds
on had pushed no branch. It ended SUCCEEDED in 0.4 s with only
`result_summary.skipped`, cloned nothing and opened no pull request, and
impl_a's pushed branch was stranded. Pinned here, in the worker, against a
real bare remote:

  * both implementers pushed: no agent runs on MERGE, both files are on the
    pushed `swarm/<integrator>` branch, and one pull request is opened from it;
  * impl_b changed nothing: the integrator is NOT skipped -- it starts from
    the default branch (impl_b's own `builds_on` names nothing), merges
    impl_a, lists impl_b under `git.integrated.no_change`, and opens the pull
    request;
  * both changed nothing: the integrator is still skipped, which issueci's
    `already_on_main` reads (#646).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

import spec_keys
from worker_seeds import TENANT, seed_attempt
from test_input_from import run_upstream
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

WORKFLOW = "wf_978"
IMPL_A = "task_impl_a"
IMPL_B = "task_impl_b"
REVIEW = "task_review"
FIX = "task_fix"
LABEL = "Two step feature"


def _implement(worker_factory: Any, monkeypatch: Any, origin: Path, task_id: str,
               filename: str) -> None:
    """Run a contributor for real: it clones, writes `filename` and pushes `swarm/<task_id>`."""

    def edit(repo: Path) -> None:
        (repo / filename).write_text(f"work from {task_id}\n")

    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id=task_id,
        dispatch={"strategy": "integrate", "carrier": "branches", "role": "contributor"},
        edit=edit,
    )
    assert out["published"] is True, out


def _seed_implementer(db: Any, task_id: str, summary: dict[str, Any]) -> None:
    """The implementer's task document, signed as swarm-api signs a step of this workflow."""
    doc = {
        "id": task_id, "task_id": task_id, "tenant_id": TENANT, "workflow_id": WORKFLOW,
        "state": TaskState.SUCCEEDED.value, "runner_profile": "mock",
        "input": {"prompt": "implement one half of the issue"},
        "metadata": {"dispatch": {"strategy": "integrate", "carrier": "branches",
                                  "role": "contributor", "allow_empty_diff": True}},
        "result_summary": summary,
    }
    db.seed(f"tasks/{task_id}", spec_keys.sign_document(doc, task_id))


def _review(db: Any, worker_factory: Any, verdict: str) -> None:
    """A review that ran and wrote `verdict.json`, publishing nothing (#760)."""
    run_upstream(
        db, worker_factory, task_id=REVIEW, attempt_id="att_review", lease_id="lease_review",
        artifact_name="verdict.json",
        artifact_text=json.dumps({"verdict": verdict, "findings": ["nothing blocks"]}),
    )
    db.doc(f"tasks/{REVIEW}")["result_summary"]["git"] = {
        "published": False, "commit_count": 0, "dirty_count": 0,
    }


def _seed_fix(db: Any) -> None:
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
            "carrier": "branches",
            "role": "integrator",
            "integrates": [IMPL_A, IMPL_B, REVIEW],
            "builds_on": IMPL_B,
            "verdict_gate": {"task_id": REVIEW, "verdict_in": ["NOT_YET"]},
            "pr_label": LABEL,
        },
    }


def _run_fix(worker_factory: Any, monkeypatch: Any, origin: Path) -> int:
    worker, _config, _ = worker_factory(
        task_id=FIX, attempt_id="att_fix", lease_id="lease_fix",
        repository_url=f"file://{origin}",
    )
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a fix agent was started after a MERGE verdict")

    monkeypatch.setattr(worker, "_run_child_supervised", refuse)
    return worker.run()


def test_a_two_step_merge_verdict_opens_one_pr_from_the_integrated_branch(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _implement(worker_factory, monkeypatch, origin, IMPL_A, "a.txt")
    _implement(worker_factory, monkeypatch, origin, IMPL_B, "b.txt")
    _seed_implementer(db, IMPL_A, {"git": {"published": True}})
    _seed_implementer(db, IMPL_B, {"git": {"published": True}})
    _review(db, worker_factory, "MERGE")
    _seed_fix(db)

    assert _run_fix(worker_factory, monkeypatch, origin) == ExitCode.OK

    task = db.doc(f"tasks/{FIX}")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    summary = task["result_summary"]
    assert "skipped" not in summary
    assert summary["skipped_agent"] == "review verdict MERGE"
    branch = f"swarm/{FIX}"
    assert {"a.txt", "b.txt"} <= tree_at(origin, branch)
    assert len(forge.pulls) == 1
    assert forge.pulls[0]["head"] == branch
    assert summary["git"]["pull_request"]
    assert summary["git"]["integrated"]["read_only"] == [f"swarm/{REVIEW}"]


def test_an_integrator_building_on_a_no_change_step_still_merges_the_changed_one_and_opens_the_pr(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _implement(worker_factory, monkeypatch, origin, IMPL_A, "a.txt")
    _seed_implementer(db, IMPL_A, {"git": {"published": True}})
    # impl_b changed nothing and pushed nothing: the step the integrator builds on.
    _seed_implementer(db, IMPL_B, {"no_change": True, "artifacts": []})
    _review(db, worker_factory, "MERGE")
    _seed_fix(db)

    assert _run_fix(worker_factory, monkeypatch, origin) == ExitCode.OK

    task = db.doc(f"tasks/{FIX}")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    summary = task["result_summary"]
    assert "skipped" not in summary, summary.get("skipped")
    assert summary["skipped_agent"] == "review verdict MERGE"
    assert f"swarm/{IMPL_B}" not in refs(origin)
    branch = f"swarm/{FIX}"
    assert "a.txt" in tree_at(origin, branch)
    integrated = summary["git"]["integrated"]
    assert integrated["merged"] == [f"swarm/{IMPL_A}"]
    assert integrated["missing"] == []
    assert integrated["no_change"] == [f"swarm/{IMPL_B}"]
    assert len(forge.pulls) == 1
    assert forge.pulls[0]["head"] == branch
    assert summary["git"]["pull_request"]


def test_an_integrator_whose_contributors_all_changed_nothing_is_still_skipped(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    _seed_implementer(db, IMPL_A, {"no_change": True, "artifacts": []})
    _seed_implementer(db, IMPL_B, {"no_change": True, "artifacts": []})
    # The review staged two no-change patches, so it was skipped in turn.
    db.seed(f"tasks/{REVIEW}", {
        "id": REVIEW, "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "result_summary": {"skipped": {"reason": "nothing to change",
                                       "upstream": [IMPL_A, IMPL_B]}},
    })
    _seed_fix(db)
    before = refs(origin)

    worker, _config, _ = worker_factory(
        task_id=FIX, attempt_id="att_fix", lease_id="lease_fix",
        repository_url=f"file://{origin}",
    )

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a step with nothing to change ran an agent or cloned")

    monkeypatch.setattr(worker, "_run_child_supervised", refuse)
    monkeypatch.setattr(worker, "_maybe_clone", refuse)

    assert worker.run() == ExitCode.OK

    task = db.doc(f"tasks/{FIX}")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["result_summary"]["skipped"]["reason"] == "nothing to change"
    assert refs(origin) == before
    assert forge.pulls == []
