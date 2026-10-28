"""The no-op precheck, check (b) (#2524, BGD 2026-10-28).

A review writes the head it judged into verdict.json as `commit`, and a fix
step gated on NOT_YET does not run its agent when its checkout's HEAD already
contains a commit a review gave MERGE.

Pinned here, each against the way it would most likely go wrong:

  * the reader keeps a well-formed `commit` and drops anything else -- a
    malformed one is no commit, never a guess;
  * the decision skips only on a MERGE whose commit the head contains: a
    NOT_YET, a MERGE with no commit, a commit the head does not contain, and
    an ancestry question that fails all leave the agent to run;
  * the ancestry is real git ancestry (a temporary repository);
  * a gated step whose head holds a MERGEd commit ends without its agent,
    with `result_summary.noop_precheck`, and the MERGE is what it passes on;
  * the compiled review prompts ask for the commit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import verdict as verdict_mod
from agent_worker import workspace as workspace_mod
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import seed_attempt
from test_input_from import run_upstream

SHA = "0123456789abcdef0123456789abcdef01234567"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _read(document: dict[str, Any]) -> verdict_mod.Verdict:
    read = verdict_mod.verdict_of_document(document)
    assert read is not None
    return read


# ---------------------------------------------------------------------------
# the reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("commit", [SHA, SHA[:7], SHA[:12]])
def test_a_well_formed_commit_is_kept(commit):
    assert _read({"verdict": "MERGE", "commit": commit}).commit == commit


@pytest.mark.parametrize(
    "commit",
    [SHA.upper(), SHA[:6], SHA + "8", "g" * 40, f" {SHA}", "--all", "HEAD", 1234567, None, ""],
)
def test_a_malformed_commit_is_no_commit(commit):
    read = _read({"verdict": "MERGE", "commit": commit, "findings": []})
    assert read.commit is None
    # The verdict itself is read exactly as before.
    assert read.verdict == "MERGE"


def test_a_verdict_without_a_commit_reads_as_before():
    assert _read({"verdict": "NOT_YET", "findings": ["x"]}).commit is None


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


def test_a_merge_whose_commit_the_head_contains_is_the_reason():
    asked: list[str] = []

    def contains(commit: str) -> bool:
        asked.append(commit)
        return True

    hit = verdict_mod.merged_commit_contained(
        [("task_rr", _read({"verdict": "MERGE", "commit": SHA}))], contains
    )
    assert hit is not None
    assert asked == [SHA]
    assert hit.as_record() == {
        "check": "b",
        "reason": f"branch head already contains {SHA[:12]}, which review task_rr gave MERGE",
    }


def test_a_merge_whose_commit_the_head_does_not_contain_is_none():
    assert verdict_mod.merged_commit_contained(
        [("task_rr", _read({"verdict": "MERGE", "commit": SHA}))], lambda _c: False
    ) is None


def test_a_merge_with_no_commit_is_none_and_git_is_not_asked():
    def never(_commit: str) -> bool:
        raise AssertionError("asked about a verdict that names no commit")

    assert verdict_mod.merged_commit_contained(
        [("task_rr", _read({"verdict": "MERGE"}))], never
    ) is None


def test_a_not_yet_that_names_a_commit_is_none():
    assert verdict_mod.merged_commit_contained(
        [("task_up", _read({"verdict": "NOT_YET", "commit": SHA, "findings": ["x"]}))],
        lambda _c: True,
    ) is None


def test_an_ancestry_question_that_fails_never_skips():
    def broken(_commit: str) -> bool:
        raise OSError("git went away")

    assert verdict_mod.merged_commit_contained(
        [("task_rr", _read({"verdict": "MERGE", "commit": SHA}))], broken
    ) is None


def test_the_first_contained_merge_among_several_is_named():
    other = "f" * 40
    hit = verdict_mod.merged_commit_contained(
        [
            ("task_up", _read({"verdict": "NOT_YET", "commit": SHA, "findings": ["x"]})),
            ("task_a", _read({"verdict": "MERGE", "commit": other})),
            ("task_b", _read({"verdict": "MERGE", "commit": SHA})),
        ],
        lambda c: c == SHA,
    )
    assert hit is not None and (hit.task_id, hit.commit) == ("task_b", SHA)


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo_with_two_commits(root: Path) -> tuple[Path, str, str]:
    repo = root / "checkout"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "reviewed")
    reviewed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "not on the head")
    elsewhere = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "-m", "after the review")
    return repo, reviewed, elsewhere


@needs_git
def test_the_decision_on_real_git_ancestry(tmp_path):
    repo, reviewed, elsewhere = _repo_with_two_commits(tmp_path)

    def is_ancestor(commit: str) -> bool:
        return subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=str(repo),
            capture_output=True,
        ).returncode == 0

    hit = verdict_mod.merged_commit_contained(
        [("task_rr", _read({"verdict": "MERGE", "commit": reviewed}))], is_ancestor
    )
    assert hit is not None and hit.commit == reviewed
    assert verdict_mod.merged_commit_contained(
        [("task_rr", _read({"verdict": "MERGE", "commit": elsewhere}))], is_ancestor
    ) is None


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


def _seed_fix(db: Any) -> None:
    """task_2 stages both reviews' verdict.json, by parent, gated on task_up."""
    seed_attempt(
        db, task_id="task_2", attempt_id="att_2", lease_id="lease_2",
        task_input={"prompt": "fix the findings", "steps": 1, "sleep_seconds": 0.01},
    )
    db.doc("tasks/task_2")["metadata"] = {
        "input_from": {"task_up": "verdict.json", "task_rr": "verdict.json"},
        "dispatch": {
            "strategy": "collect",
            "carrier": "checkpoints",
            "input_parents": {"task_up": "review", "task_rr": "rereview"},
            "verdict_gate": {"task_id": "task_up", "verdict_in": ["NOT_YET"]},
        },
    }


def _run_fix(db, worker_factory, monkeypatch, tmp_path, *, contained: bool, agent: bool):
    """Two reviews ran: task_up said NOT_YET, task_rr said MERGE naming a
    commit that the fix's head does (`contained`) or does not hold."""
    # The repository first, so the MERGE can name one of its real commits.
    repo, reviewed, elsewhere = _repo_with_two_commits(tmp_path / "git")
    commit = reviewed if contained else elsewhere
    run_upstream(
        db, worker_factory, artifact_name="verdict.json",
        artifact_text=json.dumps({"verdict": "NOT_YET", "findings": ["the guard is wrong"]}),
    )
    merge_text = json.dumps({"verdict": "MERGE", "commit": commit, "findings": []})
    run_upstream(
        db, worker_factory, task_id="task_rr", attempt_id="att_rr", lease_id="lease_rr",
        artifact_name="verdict.json", artifact_text=merge_text,
    )
    _seed_fix(db)
    # The step's checkout is this repository: a fresh clone whose HEAD is
    # known, without a forge (patched only after the upstreams ran).
    monkeypatch.setattr(workspace_mod.Workspace, "checkout", lambda _self: repo)
    worker, _config, _exporter = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2"
    )
    if not agent:
        def refuse(*_args, **_kwargs):
            raise AssertionError("the fix agent ran on a head a review already gave MERGE")

        monkeypatch.setattr(worker, "_run_child_supervised", refuse)
    return worker, commit, merge_text


@needs_git
def test_a_head_holding_a_merged_commit_runs_no_fix_agent(
    db, worker_factory, monkeypatch, tmp_path
):
    worker, commit, merge_text = _run_fix(
        db, worker_factory, monkeypatch, tmp_path, contained=True, agent=False
    )
    assert worker.run() == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    summary = task["result_summary"]
    reason = f"branch head already contains {commit[:12]}, which review task_rr gave MERGE"
    assert summary["noop_precheck"] == {"check": "b", "reason": reason}
    gate = summary["verdict_gate"]
    assert gate["verdict"] == "NOT_YET"
    assert gate["agent_ran"] is False
    assert gate["noop_precheck"] == {"check": "b", "reason": reason}
    assert summary["skipped_agent"] == f"no-op precheck (b): {reason}"
    # The MERGE that decided is what the step passes on, byte for byte.
    uploaded = next(
        entry["uri"] for entry in summary["artifacts"] if entry.get("name") == "verdict.json"
    )
    assert uploaded.startswith("file://"), uploaded
    assert Path(uploaded[len("file://"):]).read_text() == merge_text
    assert db.doc("leases/lease_2")["released_at"] is not None


@needs_git
def test_a_head_without_the_merged_commit_runs_the_fix_agent(
    db, worker_factory, monkeypatch, tmp_path, runner_inputs
):
    worker, _commit, _text = _run_fix(
        db, worker_factory, monkeypatch, tmp_path, contained=False, agent=True
    )
    assert worker.run() == ExitCode.OK

    summary = db.doc("tasks/task_2")["result_summary"]
    assert "noop_precheck" not in summary
    assert summary["verdict_gate"]["agent_ran"] is True
    assert "noop_precheck" not in summary["verdict_gate"]
    assert runner_inputs[-1]["task_id"] == "task_2"


# ---------------------------------------------------------------------------
# the prompts
# ---------------------------------------------------------------------------


def test_the_compiled_review_prompts_ask_for_the_commit():
    from swarm_api import issueruns
    from swarm_api.validation import REREVIEW_PREAMBLE

    assert '"commit": "<the full SHA of HEAD you reviewed, from git rev-parse HEAD>"' \
        in issueruns.COMMIT_SHAPE
    assert '"commit": "<the full SHA of HEAD you reviewed, from git rev-parse HEAD>"' \
        in REREVIEW_PREAMBLE.format(verdict_path="v", verdict_file="verdict.json", staged="s")
