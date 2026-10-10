"""A step that may change nothing, and the steps that needed its change (owner, 2026-10-05).

`allow_empty_diff: true` on a workflow step reaches the worker as
`metadata.dispatch.allow_empty_diff`, inside the block the spec signature
covers. What is pinned here, each against the way it would most likely go
wrong:

  * WITH the flag, an empty diff is a result: the step ends SUCCEEDED with
    `result_summary.no_change: true`, and every other artifact it wrote (the
    verification it ran) is still uploaded. A patch over its cap, or another
    expected output never written, still fails the step: the flag covers an
    empty diff and nothing else.
  * WITHOUT the flag, today's FAILED for `empty_diff` is unchanged.
  * A dependant that NEEDS the change -- it stages the step's
    `swarm-work.patch`, starts from its branch (`builds_on`), or integrates
    it and nothing else -- runs no agent and clones nothing, and ends
    SUCCEEDED with `result_summary.skipped.reason == "nothing to change"`.
    Not CANCELLED, which the workflow would read as a stop.
  * A dependant that stages anything from a SKIPPED step is skipped too: a
    skipped step wrote nothing at all.
  * A dependant that stages only the verification files of a no-change step
    still runs: those files exist.
  * A step is skipped only when NONE of the changes it reads exists (#978).
    Staging two patches, one from a step that changed something, it runs and
    stages the one that exists; staging only no-change patches, it is
    skipped. Building on a no-change step with another changed input, it
    starts from the nearest ancestor along `builds_on` that pushed -- read
    through the tenant-checked upstream read, its signed spec verified as
    this workflow's -- or from the default branch, never from a branch that
    was never pushed; an ancestor whose spec does not verify is refused by
    name.
"""

from __future__ import annotations

from typing import Any

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import expected_outputs as expected_mod
from agent_worker import lifecycle
from agent_worker import workspace as workspace_mod
from agent_worker.errors import ExitCode, WorkerError
from agent_worker.lifecycle import PATCH_NAME
from swarm_common.states import TaskState

import spec_keys
from worker_seeds import TENANT, seed_attempt
from test_input_from import run_upstream
from test_strategy_end_to_end import local_urls, origin  # noqa: F401 -- fixtures

MISSING_CAUSES_KEY = "expected_outputs_missing_causes"

#: What `_harvest_git` reports for a clone the agent left exactly as it found it.
EMPTY_DIFF = {
    "base": "a" * 40, "head": "a" * 40, "patch": None, "patch_bytes": 0,
    "patch_omitted": False, "commit_count": 0, "dirty_count": 0,
}


def _seed_step(db: Any, *, expected: list[str], dispatch: dict[str, Any] | None = None,
               artifact_name: str = "checks.md") -> None:
    seed_attempt(
        db,
        task_input={
            "prompt": "fix it if it is broken; run the checks either way",
            "steps": 1,
            "sleep_seconds": 0.01,
            "artifact_name": artifact_name,
            "artifact_text": "every check passed\n",
        },
    )
    db.doc("tasks/task_1")["metadata"] = {
        "expected_outputs": expected,
        "dispatch": {"strategy": "collect", "carrier": "checkpoints", **(dispatch or {})},
    }


def _harvest_reporting(worker: Any, git: dict[str, Any], published: list[bool]) -> None:
    """Stand in for a repository the agent changed nothing in.

    As the real harvest does for an empty tree (`work.is_empty`), it returns
    before deferring a publish, so `_publish_git` never runs.
    """

    def harvest(**_kwargs: Any) -> dict[str, Any]:
        return {**git, "published": False,
                "publish_reason": "the agent changed nothing in the repository"}

    def publish_git(*, publish: bool, **_kwargs: Any) -> dict[str, Any]:
        published.append(publish)
        return {"published": publish}

    worker._harvest_git = harvest  # type: ignore[method-assign]
    worker._publish_git = publish_git  # type: ignore[method-assign]


def _names(summary: dict[str, Any]) -> list[str]:
    return [entry["name"] for entry in summary.get("artifacts") or []]


# ---------------------------------------------------------------------------
# the step that changed nothing
# ---------------------------------------------------------------------------


def test_with_the_flag_an_empty_diff_succeeds_and_keeps_its_verification(db, worker_factory):
    _seed_step(db, expected=[PATCH_NAME, "checks.md"], dispatch={"allow_empty_diff": True})
    worker, _, _ = worker_factory()
    published: list[bool] = []
    _harvest_reporting(worker, EMPTY_DIFF, published)

    assert worker.run() == ExitCode.OK

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["end_cause"] is None
    summary = task["result_summary"]
    assert summary["no_change"] is True
    # The verification artifact is kept: it is what a reader checks the
    # "nothing to change" against.
    assert "checks.md" in _names(summary)
    assert "expected_outputs_missing" not in summary
    assert MISSING_CAUSES_KEY not in summary
    assert db.doc("leases/lease_1")["released_at"] is not None


def test_without_the_flag_an_empty_diff_still_fails_for_good(db, worker_factory):
    _seed_step(db, expected=[PATCH_NAME, "checks.md"])
    worker, _, _ = worker_factory()
    _harvest_reporting(worker, EMPTY_DIFF, [])

    assert worker.run() == ExitCode.FAILED

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "outputs_missing"
    assert task["result_summary"][MISSING_CAUSES_KEY] == [
        {"name": PATCH_NAME, "cause": expected_mod.CAUSE_EMPTY_DIFF}
    ]
    assert "no_change" not in task["result_summary"]


def test_the_flag_does_not_excuse_a_patch_over_its_cap(db, worker_factory):
    _seed_step(db, expected=[PATCH_NAME, "checks.md"], dispatch={"allow_empty_diff": True})
    worker, _, _ = worker_factory()
    _harvest_reporting(
        worker,
        {**EMPTY_DIFF, "head": "b" * 40, "commit_count": 1, "patch_bytes": 9_000_000,
         "patch_omitted": True},
        [],
    )

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert "no_change" not in task["result_summary"]


def test_the_flag_does_not_excuse_another_missing_output(db, worker_factory):
    _seed_step(
        db, expected=[PATCH_NAME, "report.md"], dispatch={"allow_empty_diff": True},
        artifact_name="checks.md",
    )
    worker, _, _ = worker_factory()
    _harvest_reporting(worker, EMPTY_DIFF, [])

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    # `report.md` was never written, which a retry can change.
    assert task["state"] == TaskState.READY.value
    assert task["result_summary"][MISSING_CAUSES_KEY] == [
        {"name": "report.md", "cause": expected_mod.CAUSE_NOT_WRITTEN}
    ]


@pytest.mark.parametrize("allow", [True, False], ids=["flag", "no-flag"])
def test_a_pull_request_step_that_may_change_nothing_is_not_published_nothing(
    db, worker_factory, allow
):
    dispatch = {"strategy": "direct-pr"}
    if allow:
        dispatch["allow_empty_diff"] = True
    _seed_step(db, expected=["checks.md"], dispatch=dispatch)
    worker, _, _ = worker_factory()
    _harvest_reporting(worker, EMPTY_DIFF, [])

    outcome = worker.run()

    task = db.doc("tasks/task_1")
    if allow:
        assert outcome == ExitCode.OK
        assert task["state"] == TaskState.SUCCEEDED.value
        assert task["result_summary"]["no_change"] is True
    else:
        assert outcome == ExitCode.FAILED
        assert task["state"] == TaskState.FAILED.value
        assert task["last_error"].startswith("published_nothing:")


# ---------------------------------------------------------------------------
# the steps that needed its change
# ---------------------------------------------------------------------------


def _seed_upstream(db: Any, task_id: str, summary: dict[str, Any]) -> None:
    db.seed(f"tasks/{task_id}", {
        "id": task_id, "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "result_summary": summary,
    })


def _seed_dependant(db: Any, *, input_from: dict[str, str] | None = None,
                    dispatch: dict[str, Any] | None = None,
                    depends_on: list[str] | None = None) -> None:
    seed_attempt(
        db, task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        task_input={"prompt": "review the patch", "steps": 1, "sleep_seconds": 0.01},
    )
    doc = db.doc("tasks/task_2")
    doc["depends_on"] = list(depends_on or [])
    doc["metadata"] = {
        "input_from": dict(input_from or {}),
        "dispatch": {"strategy": "collect", "carrier": "checkpoints", **(dispatch or {})},
    }


def _run_without_an_agent(worker_factory: Any, monkeypatch: Any) -> int:
    worker, _config, _ = worker_factory(task_id="task_2", attempt_id="att_2", lease_id="lease_2")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an agent was started for a step with nothing to change")

    def no_clone(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a step with nothing to change cloned a repository")

    monkeypatch.setattr(worker, "_run_child_supervised", refuse)
    monkeypatch.setattr(worker, "_maybe_clone", no_clone)
    return worker.run()


def _assert_skipped(db: Any, upstream: list[str]) -> None:
    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert task["end_cause"] is None
    assert task["result_summary"]["skipped"] == {
        "reason": "nothing to change", "upstream": upstream,
    }
    # Capacity came back: a skipped step is still an attempt that ends.
    assert db.doc("leases/lease_2")["released_at"] is not None


def test_a_dependant_that_stages_the_patch_of_a_no_change_step_is_skipped(
    db, worker_factory, monkeypatch
):
    _seed_upstream(db, "task_up", {"no_change": True, "artifacts": []})
    _seed_dependant(db, input_from={"task_up": PATCH_NAME}, depends_on=["task_up"])

    assert _run_without_an_agent(worker_factory, monkeypatch) == ExitCode.OK
    _assert_skipped(db, ["task_up"])


def test_a_dependant_that_builds_on_a_no_change_step_is_skipped_without_a_clone(
    db, worker_factory, monkeypatch
):
    _seed_upstream(db, "task_impl", {"no_change": True, "artifacts": []})
    _seed_dependant(db, dispatch={"builds_on": "task_impl"}, depends_on=["task_impl"])

    assert _run_without_an_agent(worker_factory, monkeypatch) == ExitCode.OK
    _assert_skipped(db, ["task_impl"])


def test_anything_staged_from_a_skipped_step_skips_the_dependant(
    db, worker_factory, monkeypatch
):
    _seed_upstream(db, "task_review", {
        "skipped": {"reason": "nothing to change", "upstream": ["task_impl"]},
    })
    _seed_dependant(db, input_from={"task_review": "verdict.json"}, depends_on=["task_review"])

    assert _run_without_an_agent(worker_factory, monkeypatch) == ExitCode.OK
    _assert_skipped(db, ["task_review"])


def test_an_integrator_whose_contributors_all_changed_nothing_is_skipped(
    db, worker_factory, monkeypatch
):
    _seed_upstream(db, "task_a", {"no_change": True})
    _seed_upstream(db, "task_b", {"no_change": True})
    _seed_dependant(
        db,
        dispatch={"strategy": "integrate", "role": "integrator",
                  "integrates": ["task_a", "task_b"]},
        depends_on=["task_a", "task_b"],
    )

    assert _run_without_an_agent(worker_factory, monkeypatch) == ExitCode.OK
    _assert_skipped(db, ["task_a", "task_b"])


def test_an_integrator_leaves_out_only_the_contributors_with_nothing_to_merge(
    db, worker_factory
):
    _seed_upstream(db, "task_a", {"no_change": True})
    _seed_upstream(db, "task_b", {"git": {"published": True}})
    worker, _config, _ = worker_factory(task_id="task_2")
    worker._task = {"metadata": {"dispatch": {
        "strategy": "integrate", "role": "integrator", "integrates": ["task_a", "task_b"],
    }}}

    assert worker._integrates_with_changes() == ["task_b"]
    assert worker._no_change_contributors == ["task_a"]


def test_a_dependant_that_stages_only_the_verification_still_runs(
    db, worker_factory, runner_inputs
):
    run_upstream(db, worker_factory, artifact_name="checks.md", artifact_text="all green\n")
    db.doc("tasks/task_up")["result_summary"]["no_change"] = True
    _seed_dependant(db, input_from={"task_up": "checks.md"}, depends_on=["task_up"])
    worker, _config, _ = worker_factory(task_id="task_2", attempt_id="att_2", lease_id="lease_2")

    assert worker.run() == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert "skipped" not in task["result_summary"]
    assert runner_inputs[-1]["task_id"] == "task_2"


def test_a_dependant_of_a_step_that_changed_something_runs(
    db, worker_factory, runner_inputs
):
    """The control: the same staging from an upstream WITHOUT `no_change`."""
    run_upstream(db, worker_factory, artifact_name="checks.md", artifact_text="all green\n")
    _seed_dependant(db, input_from={"task_up": "checks.md"}, depends_on=["task_up"])
    worker, _config, _ = worker_factory(task_id="task_2", attempt_id="att_2", lease_id="lease_2")

    assert worker.run() == ExitCode.OK
    assert "skipped" not in db.doc("tasks/task_2")["result_summary"]
    assert runner_inputs[-1]["task_id"] == "task_2"


# ---------------------------------------------------------------------------
# a step is skipped only when NONE of the changes it reads exists (#978)
# ---------------------------------------------------------------------------
#
# wf_ca1807e43ac64d6b8afd: two implement steps, the review staging both
# patches and building on the second, the integrator building on the second.
# The second changed nothing, so the review was skipped for it and the
# integrator was skipped in 0.4 s, while the first step's pushed branch was
# never merged into anything.

WORKFLOW = "wf_978"
PATCH_TEXT = "diff --git a/a.txt b/a.txt\n+from the step that changed something\n"


def _two_patches(db: Any) -> None:
    _seed_dependant(
        db,
        input_from={"task_a": PATCH_NAME, "task_b": PATCH_NAME},
        dispatch={"input_parents": {"task_a": "impl_a", "task_b": "impl_b"}},
        depends_on=["task_a", "task_b"],
    )


def test_a_step_staging_two_patches_runs_when_one_upstream_changed(
    db, worker_factory, runner_inputs
):
    run_upstream(db, worker_factory, task_id="task_a", attempt_id="att_a", lease_id="lease_a",
                 artifact_name=PATCH_NAME, artifact_text=PATCH_TEXT)
    _seed_upstream(db, "task_b", {"no_change": True, "artifacts": []})
    _two_patches(db)
    worker, _config, _ = worker_factory(task_id="task_2", attempt_id="att_2", lease_id="lease_2")

    assert worker.run() == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    summary = task["result_summary"]
    assert "skipped" not in summary
    # Only the patch that exists was staged; the step that changed nothing is
    # named, not refused as an input that could not be staged.
    assert [(item["task_id"], item["path"]) for item in summary["staged_inputs"]] == [
        ("task_a", f"impl_a/{PATCH_NAME}")
    ]
    assert summary["staged_inputs_left_nothing"] == [
        {"task_id": "task_b", "filename": PATCH_NAME, "left": "no_change"}
    ]
    assert runner_inputs[-1]["task_id"] == "task_2"
    assert [item["task_id"] for item in runner_inputs[-1]["staged_inputs"]] == ["task_a"]


def test_a_step_staging_only_no_change_patches_is_skipped(db, worker_factory, monkeypatch):
    _seed_upstream(db, "task_a", {"no_change": True, "artifacts": []})
    _seed_upstream(db, "task_b", {"no_change": True, "artifacts": []})
    _two_patches(db)

    assert _run_without_an_agent(worker_factory, monkeypatch) == ExitCode.OK
    _assert_skipped(db, ["task_a", "task_b"])


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def _push_branch(origin: Path, tmp_path: Path, task_id: str, filename: str) -> None:
    """Push `swarm/<task_id>`, holding `filename`, as that step's publish would."""
    work = tmp_path / f"push-{task_id}"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                   capture_output=True)
    _git(work, "checkout", "-qb", f"swarm/{task_id}")
    (work / filename).write_text(f"work from {task_id}\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", f"{task_id} work")
    _git(work, "push", "-q", "origin", f"swarm/{task_id}")


def _seed_step_of_the_workflow(db: Any, task_id: str, summary: dict[str, Any], *,
                               builds_on: str | None = None, sign: bool = True) -> None:
    """A SUCCEEDED step of this workflow, signed as swarm-api signs one."""
    dispatch: dict[str, Any] = {"strategy": "integrate", "role": "contributor"}
    if builds_on:
        dispatch["builds_on"] = builds_on
    doc = {
        "id": task_id, "task_id": task_id, "tenant_id": TENANT, "workflow_id": WORKFLOW,
        "state": TaskState.SUCCEEDED.value, "runner_profile": "mock",
        "input": {"prompt": "implement it"}, "metadata": {"dispatch": dispatch},
        "result_summary": summary,
    }
    if sign:
        spec_keys.sign_document(doc, task_id)
    db.seed(f"tasks/{task_id}", doc)


def _cloning_worker(worker_factory: Any, origin: Path, monkeypatch: Any,
                    dispatch: dict[str, Any], input_from: dict[str, str]) -> tuple[Any, dict]:
    worker, config, _ = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {
        "task_id": "task_2", "workflow_id": WORKFLOW,
        "metadata": {"input_from": input_from,
                     "dispatch": {"carrier": "checkpoints", **dispatch}},
    }
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    return worker, task


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@needs_git
def test_builds_on_a_no_change_step_clones_the_nearest_ancestor_that_pushed(
    db, worker_factory, monkeypatch, origin, local_urls, tmp_path
):
    # task_root pushed; task_mid built on it and changed nothing; this step
    # builds on task_mid and integrates task_other, which changed something.
    _push_branch(origin, tmp_path, "task_root", "root.txt")
    _push_branch(origin, tmp_path, "task_other", "other.txt")
    _seed_step_of_the_workflow(db, "task_root", {"git": {"published": True}})
    _seed_step_of_the_workflow(db, "task_mid", {"no_change": True}, builds_on="task_root")
    _seed_step_of_the_workflow(db, "task_other", {"git": {"published": True}})
    worker, task = _cloning_worker(
        worker_factory, origin, monkeypatch,
        {"strategy": "integrate", "role": "integrator", "builds_on": "task_mid",
         "integrates": ["task_root", "task_mid", "task_other"]},
        {},
    )

    assert worker._nothing_to_work_on(task) == []
    info = worker._maybe_clone(task)

    assert info is not None and info["ref"] == "swarm/task_root"
    assert info["builds_on"] == "task_mid"
    assert info["builds_on_resolved"] == {"task_id": "task_root", "left_nothing": ["task_mid"]}
    assert (worker.ws.work / lifecycle.REPO_DIR_NAME / "root.txt").is_file()


@needs_git
def test_builds_on_a_no_change_root_with_another_changed_input_clones_the_default_branch(
    db, worker_factory, monkeypatch, origin, local_urls, tmp_path
):
    # The review of #978: it stages both implementers' patches and builds on
    # the second, a root step that changed nothing and pushed no branch.
    _push_branch(origin, tmp_path, "task_a", "a.txt")
    _seed_step_of_the_workflow(db, "task_a", {"git": {"published": True}})
    _seed_step_of_the_workflow(db, "task_b", {"no_change": True})
    worker, task = _cloning_worker(
        worker_factory, origin, monkeypatch,
        {"strategy": "collect", "builds_on": "task_b",
         "input_parents": {"task_a": "impl_a", "task_b": "impl_b"}},
        {"task_a": PATCH_NAME, "task_b": PATCH_NAME},
    )

    assert worker._nothing_to_work_on(task) == []
    info = worker._maybe_clone(task)

    assert info is not None and info["ref"] != "swarm/task_b"
    assert info["builds_on_resolved"] == {"task_id": None, "left_nothing": ["task_b"]}
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    assert (repo / "README.md").is_file()
    assert not (repo / "a.txt").exists()


@needs_git
def test_an_ancestor_whose_spec_does_not_verify_is_refused_by_name(
    db, worker_factory, monkeypatch, origin, local_urls, tmp_path
):
    _push_branch(origin, tmp_path, "task_root", "root.txt")
    _push_branch(origin, tmp_path, "task_other", "other.txt")
    _seed_step_of_the_workflow(db, "task_root", {"git": {"published": True}})
    # task_mid's dispatch block is what would name the branch to start from,
    # and nobody signed it: it is not trusted, and not silently skipped over.
    _seed_step_of_the_workflow(db, "task_mid", {"no_change": True}, builds_on="task_root",
                               sign=False)
    _seed_step_of_the_workflow(db, "task_other", {"git": {"published": True}})
    worker, task = _cloning_worker(
        worker_factory, origin, monkeypatch,
        {"strategy": "integrate", "role": "integrator", "builds_on": "task_mid",
         "integrates": ["task_mid", "task_other"]},
        {},
    )

    with pytest.raises(WorkerError) as refused:
        worker._maybe_clone(task)

    message = str(refused.value)
    assert "task_mid" in message
    assert "did not verify" in message
    assert "unsigned" in message
    assert not (worker.ws.work / lifecycle.REPO_DIR_NAME / "root.txt").exists()
