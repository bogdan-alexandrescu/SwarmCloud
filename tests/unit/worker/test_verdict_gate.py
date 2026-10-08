"""The worker half of the review shape (#264): a verdict gate, and `builds_on`.

    implement ──> review ──> fix (when review's verdict is NOT_YET)

swarm-api stores two things in `metadata.dispatch` for this shape
(tests/unit/control_plane/test_review_gate_workflow.py):

  * `verdict_gate: {"task_id": <review task>, "verdict_in": [...]}` -- run
    this step's agent only when the verdict file it staged from that task says
    one of those verdicts. When it does not, the step still FINISHES, and still
    publishes: it is the workflow's final step, and a MERGE verdict must end in
    the one pull request exactly as a fixed NOT_YET does.
  * `builds_on: <implement task>` -- clone the branch that task pushed, not
    the default branch, so the fix agent sees the code it is fixing.

What is pinned here, each against the way it would most likely go wrong:

  * a shut gate never starts the runner, and the task still SUCCEEDS;
  * an open gate runs the agent exactly as an ungated step does;
  * a verdict file that is not a verdict fails the attempt WITHOUT running the
    agent and without publishing -- an unreadable review is not a MERGE;
  * the pull request carries the verdict and its findings, fenced, because
    they are an agent's untrusted text on a public page;
  * a step that builds on another starts from that step's pushed tree, and the
    one pull request holds both steps' work.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import verdict as verdict_mod
from agent_worker.errors import ExitCode, InputUnavailable, WorkerError
from swarm_common.states import EventType, TaskState

from worker_seeds import TENANT, seed_attempt
from test_input_from import run_upstream
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)


def _verdict_text(verdict: str, findings: Any = None) -> str:
    return json.dumps({"verdict": verdict, "findings": findings or []})


def seed_gated(db: Any, *, verdict_in: list[str], stage: bool = True, **gate: Any) -> None:
    """task_2 stages `verdict.json` from task_up and is gated on it."""
    seed_attempt(
        db,
        task_id="task_2",
        attempt_id="att_2",
        lease_id="lease_2",
        task_input={"prompt": "fix the findings", "steps": 1, "sleep_seconds": 0.01},
    )
    db.doc("tasks/task_2")["metadata"] = {
        "input_from": {"task_up": "verdict.json"} if stage else {},
        "dispatch": {
            "strategy": "collect",
            "carrier": "checkpoints",
            "verdict_gate": {"task_id": gate.get("task_id", "task_up"), "verdict_in": verdict_in},
        },
    }


def run_gated(worker_factory: Any, monkeypatch: Any, *, runner_allowed: bool) -> int:
    worker, _config, _exporter = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2"
    )
    if not runner_allowed:
        def refuse(*_args, **_kwargs):
            raise AssertionError("the runner was started behind a shut verdict gate")

        monkeypatch.setattr(worker, "_run_child_supervised", refuse)
    return worker.run()


def upstream_verdict(db, worker_factory, text: str) -> None:
    run_upstream(db, worker_factory, artifact_name="verdict.json", artifact_text=text)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_a_merge_verdict_skips_the_fix_agent_and_the_step_still_succeeds(
    db, worker_factory, monkeypatch
):
    upstream_verdict(db, worker_factory, _verdict_text("MERGE", ["nothing blocks"]))
    seed_gated(db, verdict_in=["NOT_YET"])

    assert run_gated(worker_factory, monkeypatch, runner_allowed=False) == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    gate = task["result_summary"]["verdict_gate"]
    assert gate["verdict"] == "MERGE"
    assert gate["agent_ran"] is False
    assert gate["task_id"] == "task_up"
    assert gate["file"] == "verdict.json"
    assert gate["verdict_in"] == ["NOT_YET"]
    assert gate["findings"] == ["nothing blocks"]
    # Capacity came back: a skipped agent is still an attempt that ends.
    assert db.doc("leases/lease_2")["released_at"] is not None
    assert db.doc("pools/global")["active"] == 0


def test_a_not_yet_verdict_runs_the_fix_agent(db, worker_factory, monkeypatch, runner_inputs):
    upstream_verdict(
        db, worker_factory,
        _verdict_text("NOT_YET", [{"summary": "the guard checks the wrong field"}]),
    )
    seed_gated(db, verdict_in=["NOT_YET"])

    assert run_gated(worker_factory, monkeypatch, runner_allowed=True) == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    gate = task["result_summary"]["verdict_gate"]
    assert gate["verdict"] == "NOT_YET"
    assert gate["agent_ran"] is True
    assert gate["findings"] == ["the guard checks the wrong field"]
    # The agent really ran: the mock's own result is there.
    assert task["result_summary"]["runner"]["status"] != "skipped"
    assert runner_inputs[-1]["task_id"] == "task_2"


def test_the_verdict_is_read_whatever_its_case_and_spacing(db, worker_factory, monkeypatch):
    upstream_verdict(db, worker_factory, json.dumps({"verdict": "  not_yet "}))
    seed_gated(db, verdict_in=["NOT_YET"])

    assert run_gated(worker_factory, monkeypatch, runner_allowed=True) == ExitCode.OK
    assert db.doc("tasks/task_2")["result_summary"]["verdict_gate"]["verdict"] == "NOT_YET"


@pytest.mark.parametrize(
    "text",
    [
        "MERGE",                                   # not JSON
        json.dumps(["MERGE"]),                     # not an object
        json.dumps({"findings": []}),              # no verdict
        json.dumps({"verdict": "LGTM"}),           # not a verdict this platform knows
        json.dumps({"verdict": 1}),
    ],
)
def test_a_file_that_is_not_a_verdict_fails_without_running_the_agent(
    db, worker_factory, monkeypatch, text
):
    """An unreadable review is not a MERGE. Reading it as one would publish
    unreviewed work, which is the thing the gate exists to stop."""
    upstream_verdict(db, worker_factory, text)
    seed_gated(db, verdict_in=["NOT_YET"])

    assert run_gated(worker_factory, monkeypatch, runner_allowed=False) == ExitCode.FAILED

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.FAILED.value
    assert "verdict.json" in task["last_error"]
    assert "task_up" in task["last_error"]
    assert "MERGE" in task["last_error"] and "NOT_YET" in task["last_error"]
    assert db.doc("leases/lease_2")["released_at"] is not None


def test_a_gate_on_a_task_this_step_stages_nothing_from_fails(db, worker_factory, monkeypatch):
    upstream_verdict(db, worker_factory, _verdict_text("MERGE"))
    seed_gated(db, verdict_in=["NOT_YET"], stage=False)

    assert run_gated(worker_factory, monkeypatch, runner_allowed=False) == ExitCode.FAILED
    assert "task_up" in db.doc("tasks/task_2")["last_error"]


def test_a_step_with_no_gate_is_unchanged(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "ordinary", "steps": 1, "sleep_seconds": 0.01})
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.OK
    assert "verdict_gate" not in db.doc("tasks/task_1")["result_summary"]


# ---------------------------------------------------------------------------
# reading the dispatch block and the verdict file
# ---------------------------------------------------------------------------


def test_no_gate_in_the_block_is_none():
    assert verdict_mod.gate_from_dispatch({}) is None
    assert verdict_mod.gate_from_dispatch({"strategy": "integrate"}) is None


@pytest.mark.parametrize(
    "gate",
    [
        "task_up",
        {"verdict_in": ["NOT_YET"]},
        {"task_id": "", "verdict_in": ["NOT_YET"]},
        {"task_id": "task_up"},
        {"task_id": "task_up", "verdict_in": []},
        {"task_id": "task_up", "verdict_in": ["SHIP_IT"]},
        {"task_id": "task_up", "verdict_in": "NOT_YET"},
    ],
)
def test_a_malformed_gate_is_refused_rather_than_read_as_open(gate):
    """Read as absent, a malformed gate would run the agent on every verdict;
    read as shut, it would publish without the fix. Neither is the caller's."""
    with pytest.raises(InputUnavailable):
        verdict_mod.gate_from_dispatch({"verdict_gate": gate})


def test_a_well_formed_gate_is_read():
    gate = verdict_mod.gate_from_dispatch(
        {"verdict_gate": {"task_id": "task_up", "verdict_in": ["not_yet", "MERGE"]}}
    )
    assert gate == verdict_mod.VerdictGate(task_id="task_up", verdict_in=("NOT_YET", "MERGE"))


def test_a_verdict_file_over_the_bound_is_refused(tmp_path: Path):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps({"verdict": "MERGE", "pad": "x" * verdict_mod.MAX_VERDICT_BYTES}))
    with pytest.raises(InputUnavailable, match="bytes"):
        verdict_mod.read_verdict(path, task_id="task_up", filename="verdict.json")


def test_findings_are_bounded_in_number_and_length(tmp_path: Path):
    path = tmp_path / "verdict.json"
    many = ["y" * 2000] * (verdict_mod.MAX_FINDINGS + 5)
    path.write_text(json.dumps({"verdict": "NOT_YET", "findings": many}))
    read = verdict_mod.read_verdict(path, task_id="task_up", filename="verdict.json")
    assert len(read.findings) == verdict_mod.MAX_FINDINGS
    assert all(len(f) <= verdict_mod.MAX_FINDING_CHARS for f in read.findings)
    assert read.findings_dropped == 5


# ---------------------------------------------------------------------------
# the pull request carries the verdict
# ---------------------------------------------------------------------------


def test_the_pull_request_body_carries_the_verdict_and_fenced_findings(worker_factory):
    worker, _config, _ = worker_factory()
    worker._verdict = {
        "task_id": "task_review",
        "file": "verdict.json",
        "verdict": "NOT_YET",
        "verdict_in": ["NOT_YET"],
        "agent_ran": True,
        "findings": ["the guard lets `ssh://tok@host` through ``` @everyone"],
        "findings_dropped": 0,
    }
    body = worker._pull_request_body(branch="swarm/task_1", auto_committed=False)

    assert "Review verdict: **NOT_YET**" in body
    assert "task_review" in body
    assert "fix step ran" in body
    # The finding is there, and it cannot close the fence it sits in.
    assert "the guard lets" in body
    fence_lines = [line for line in body.splitlines() if line.startswith("```")]
    assert len(fence_lines) == 2, body
    assert "``` @everyone" not in body


def test_a_merge_verdict_says_the_fix_agent_did_not_run(worker_factory):
    worker, _config, _ = worker_factory()
    worker._verdict = {
        "task_id": "task_review",
        "file": "verdict.json",
        "verdict": "MERGE",
        "verdict_in": ["NOT_YET"],
        "agent_ran": False,
        "findings": [],
        "findings_dropped": 0,
    }
    body = worker._pull_request_body(branch="swarm/task_1", auto_committed=False)
    assert "Review verdict: **MERGE**" in body
    assert "did not run" in body


def test_a_pull_request_with_no_gate_says_nothing_about_a_verdict(worker_factory):
    worker, _config, _ = worker_factory()
    body = worker._pull_request_body(branch="swarm/task_1", auto_committed=False)
    assert "verdict" not in body.lower()


# ---------------------------------------------------------------------------
# builds_on: the fix starts from the implementer's branch
# ---------------------------------------------------------------------------


def test_builds_on_names_a_branch_under_the_workers_own_prefix(worker_factory):
    worker, config, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": {"strategy": "integrate", "builds_on": "task_impl"}}}
    assert worker._dispatch_builds_on() == "task_impl"
    worker._task = {"metadata": {"dispatch": {"strategy": "integrate"}}}
    assert worker._dispatch_builds_on() == ""


@pytest.mark.parametrize("value", ["../main", "a/b", " ", "-x", 7, ["t"]])
def test_builds_on_that_is_not_a_task_id_is_refused(worker_factory, value):
    """The branch is DERIVED from a task id with the worker's prefix, as the
    integrator's are, so nothing in a task document can point the clone at an
    arbitrary ref."""
    worker, _config, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": {"strategy": "integrate", "builds_on": value}}}
    if isinstance(value, str) and not value.strip():
        assert worker._dispatch_builds_on() == ""
        return
    with pytest.raises(WorkerError):
        worker._dispatch_builds_on()


pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytestmark_git
def test_the_review_shape_ends_in_one_pull_request_holding_both_steps_work(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    # implement: a contributor that writes the feature and pushes its branch.
    def implement(repo: Path) -> None:
        (repo / "feature.py").write_text("def feature():\n    return 1\n")

    _, _, impl = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="task_impl",
        dispatch={"strategy": "integrate", "role": "contributor"},
        edit=implement,
    )
    assert impl["published"] is True
    assert "feature.py" in tree_at(origin, "swarm/task_impl")

    # review: builds on the implementer's branch, reads the code, changes
    # nothing in the repository.
    seen_by_review: dict[str, bool] = {}

    def review(repo: Path) -> None:
        seen_by_review["feature"] = (repo / "feature.py").exists()

    _, _, rev = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="task_review",
        dispatch={"strategy": "integrate", "role": "contributor", "builds_on": "task_impl"},
        edit=review,
    )
    assert seen_by_review["feature"], "the review did not start from the implementer's branch"
    # It changed nothing, so it pushed nothing -- which is why swarm-api leaves
    # it out of the integrator's `integrates` (the review's output is its
    # verdict file, not a branch).
    assert rev["published"] is False
    assert "swarm/task_review" not in refs(origin)

    # fix: the integrator, gated (the gate opened), building on the implementer.
    seen_by_fix: dict[str, bool] = {}

    def fix(repo: Path) -> None:
        seen_by_fix["feature"] = (repo / "feature.py").exists()
        (repo / "feature.py").write_text("def feature():\n    return 2\n")

    worker, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="task_fix",
        dispatch={
            "strategy": "integrate",
            "role": "integrator",
            "integrates": ["task_impl"],
            "builds_on": "task_impl",
        },
        edit=fix,
    )
    assert seen_by_fix["feature"], "the fix did not start from the implementer's branch"
    assert out["published"] is True
    # The implementer's branch is already in the fix's history, so the merge
    # takes it without conflict and the integration is complete.
    assert out["integrated"]["complete"] is True
    assert out["integrated"]["merged"] == ["swarm/task_impl"]
    assert out["integrated"]["conflicted"] == []
    assert out["integrated"]["missing"] == []

    # ONE pull request, from the fix step.
    assert len(forge.pulls) == 1
    assert forge.pulls[0]["head"] == "swarm/task_fix"
    tips = refs(origin)
    assert "swarm/task_fix" in tips
    shown = subprocess.run(
        ["git", "show", "swarm/task_fix:feature.py"],
        cwd=str(origin), check=True, capture_output=True, text=True,
    ).stdout
    assert "return 2" in shown


@pytestmark_git
def test_a_merge_verdict_still_opens_the_one_pull_request_carrying_it(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The gate stayed shut: no agent, no edit -- and the integrator still
    merges the implementer's branch and opens the pull request, because it is
    the only step that publishes."""
    from agent_worker import workspace as workspace_mod

    def implement(repo: Path) -> None:
        (repo / "feature.py").write_text("def feature():\n    return 1\n")

    run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="task_impl",
        dispatch={"strategy": "integrate", "role": "contributor"},
        edit=implement,
    )

    url = f"file://{origin}"
    worker, config, _ = worker_factory(
        task_id="task_fix", attempt_id="att-fix", lease_id="lease-fix", repository_url=url,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "task_fix", "metadata": {"dispatch": {
        "strategy": "integrate", "role": "integrator",
        "integrates": ["task_impl"], "builds_on": "task_impl",
    }}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None
    # What `_evaluate_verdict_gate` records when the verdict does not name
    # this step; the agent then never runs, so nothing is edited.
    worker._verdict = {
        "task_id": "task_review", "file": "verdict.json", "verdict": "MERGE",
        "verdict_in": ["NOT_YET"], "agent_ran": False,
        "findings": ["no blockers"], "findings_dropped": 0,
    }

    out = worker._harvest_git(publish=True)

    assert out["published"] is True, out
    assert out["integrated"]["merged"] == ["swarm/task_impl"]
    assert len(forge.pulls) == 1
    body = forge.pulls[0]["body"]
    assert "Review verdict: **MERGE**" in body
    assert "no blockers" in body
    assert "feature.py" in tree_at(origin, "swarm/task_fix")


@pytestmark_git
def test_a_generation_bumped_after_the_shut_gate_fences_the_publish(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """Invariant 5, on the no-agent path (#264).

    The gate is re-evaluated in the worker (`lifecycle.py`,
    `_evaluate_verdict_gate`), and a shut gate still ends in the step's own
    publish -- the merge of `builds_on` and the one pull request -- because it
    is the step that opens it. `self.control.validate_generation()` runs
    again right before that publish (immediately after the gate closes),
    exactly as it does before an agent starts, because the publish pushes.

    Here the attempt is superseded in the gap between reading the verdict and
    that re-check -- a reconciler bumping `current_generation` while this
    worker is mid-`_prepare`, exactly as `test_fencing.py`'s
    `test_generation_bumped_mid_run_stops_the_agent_and_keeps_the_lease` does
    for the agent path. The worker must exit FENCED without pushing the
    branch, without opening the pull request, and without touching the lease
    it no longer owns -- not silently publish another generation's work.

    The task's STATE is RUNNING, not LEASED, when that happens, and that is
    correct rather than a second fencing gap: `_prepare` walks LEASED ->
    DISPATCHED -> STARTING -> RUNNING at STEP 2 (`lifecycle.py`
    `advance_to_running`, called at line 728), long before STEP 5d's verdict
    gate (line 813) is even reached -- while this attempt's generation was
    still current. Each of those transitions is its own fenced write
    (`control.py` `transition`, line 772, shares `_fenced_task` with
    `validate_generation`), so a transition made under a stale generation
    would itself be refused; this one was not stale when it ran. The
    generation is only bumped afterwards, from inside the verdict-gate
    wrapper below, which is why RUNNING -- the state that legitimate
    transition left behind -- is exactly what invariant 5 predicts here: the
    lease and the task's terminal state are what a stale worker must leave
    untouched, and both are.
    """

    def implement(repo: Path) -> None:
        (repo / "feature.py").write_text("def feature():\n    return 1\n")

    run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="task_impl",
        dispatch={"strategy": "integrate", "role": "contributor"},
        edit=implement,
    )

    upstream_verdict(db, worker_factory, _verdict_text("MERGE", ["nothing blocks"]))
    seed_gated(db, verdict_in=["NOT_YET"])
    db.doc("tasks/task_2")["metadata"]["dispatch"] = {
        "strategy": "integrate",
        "role": "integrator",
        "integrates": ["task_impl"],
        "builds_on": "task_impl",
        "verdict_gate": {"task_id": "task_up", "verdict_in": ["NOT_YET"]},
    }

    url = f"file://{origin}"
    worker, _config, _exporter = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2", repository_url=url,
    )
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the runner was started behind a shut verdict gate")

    monkeypatch.setattr(worker, "_run_child_supervised", refuse)

    original_gate = worker._evaluate_verdict_gate

    def gate_then_supersede(staged: Any) -> bool | None:
        # The gate closes here -- MERGE is not in verdict_in, so the agent
        # will not run. Superseding the attempt in this exact gap, before the
        # worker's own re-check, is what this test exists to prove closes.
        result = original_gate(staged)
        db.doc("tasks/task_2")["current_generation"] = 99
        return result

    monkeypatch.setattr(worker, "_evaluate_verdict_gate", gate_then_supersede)

    assert worker.run() == ExitCode.GENERATION_FENCED

    assert forge.pulls == [], "a superseded worker opened a pull request"
    assert "swarm/task_2" not in refs(origin), "a superseded worker pushed its branch"
    assert db.doc("leases/lease_2")["released_at"] is None
    # RUNNING, not LEASED: `advance_to_running` (lifecycle.py:728) already
    # walked the task there, legitimately, before the verdict gate at
    # lifecycle.py:813 was reached -- see the docstring above.
    assert db.doc("tasks/task_2")["state"] == TaskState.RUNNING.value
    assert EventType.GENERATION_FENCED.value in db.event_types("task_2")


@pytestmark_git
def test_building_on_a_branch_that_was_never_pushed_fails_the_clone_by_name(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    url = f"file://{origin}"
    worker, config, _ = worker_factory(
        task_id="task_fix", attempt_id="att-fix", lease_id="lease-fix", repository_url=url,
    )
    from agent_worker import workspace as workspace_mod

    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "task_fix",
            "metadata": {"dispatch": {"strategy": "integrate", "builds_on": "task_gone"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")

    with pytest.raises(WorkerError, match="task_gone"):
        worker._maybe_clone(task)


# ---------------------------------------------------------------------------
# the two ends agree
# ---------------------------------------------------------------------------


def test_the_worker_and_the_api_know_the_same_verdicts():
    from swarm_api.validation import REVIEW_VERDICTS

    assert verdict_mod.REVIEW_VERDICTS == REVIEW_VERDICTS


def test_the_block_the_api_writes_is_the_block_the_worker_reads(worker_factory):
    from swarm_api.validation import DispatchOptions

    block = DispatchOptions(
        strategy="integrate",
        role="integrator",
        integrates=("task_impl", "task_review"),
        builds_on="task_impl",
        gate_task_id="task_review",
        gate_verdicts=("NOT_YET",),
    ).to_metadata()
    worker, _config, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": block}}
    assert worker._dispatch_builds_on() == "task_impl"
    assert verdict_mod.gate_from_dispatch(block) == verdict_mod.VerdictGate(
        task_id="task_review", verdict_in=("NOT_YET",)
    )
