"""The worker records the branch it actually pushed (#667, observer P11).

Measured on 2026-10-06: C1A's agent named `c1a-631-failure-classes` in its
step summary, a branch that was never pushed; the real one was
`swarm/task_<id>`. Only `carrier: branches` recorded a branch at all, and by
name and head only. Now every push records `result_summary.branch` from git:
the name it pushed, the head it pushed, the base it built on and the commits
between, each a sha and a subject -- so a summary can be checked against a
branch and SHAs that exist.

Real git against a bare repository on disk, as in test_strategy_end_to_end.py.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

from agent_worker import gitops
from agent_worker.logs import build_logger
from swarm_common.states import TaskState

from worker_seeds import seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _log(bare: Path, revision: str) -> list[dict[str, str]]:
    raw = _git(bare, "log", "--format=%H%x1f%s", revision)
    return [
        {"sha": sha, "subject": subject}
        for sha, subject in (line.split("\x1f", 1) for line in raw.splitlines() if line)
    ]


def _two_commits(repo: Path) -> None:
    (repo / "one.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add one")
    (repo / "two.txt").write_text("two\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add two")


def test_a_push_records_the_branch_its_head_and_its_commits(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    worker, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-rec", dispatch={"strategy": "direct-pr"}, edit=_two_commits,
    )
    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}t-rec"
    remote = refs(origin)

    record = worker._pushed_branch
    assert record is not None
    assert record["name"] == branch
    assert record["head_sha"] == remote[branch]
    assert record["head"] == remote[branch]
    assert record["base_sha"] == remote["main"]
    assert record["commits"] == _log(origin, f"main..{branch}")
    assert record["commits"], "the push carried the agent's work"
    assert record["commits_truncated"] is False


def test_nothing_pushed_is_no_branch_recorded(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    worker, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-none", dispatch={"strategy": "collect", "carrier": "checkpoints"},
        edit=_two_commits,
    )
    assert out["published"] is False
    assert worker._pushed_branch is None


def test_the_commit_list_is_capped_and_says_so(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", ".")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for index in range(5):
        (repo / f"f{index}.txt").write_text(f"{index}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", f"commit {index}")
    (tmp_path / "logs").mkdir()
    commits, truncated = gitops.branch_commits(
        repo=repo, base=base, private_dir=tmp_path / "private", logs_dir=tmp_path / "logs",
        timeout_seconds=30,
        logger=build_logger(task_id="t", attempt_id="a", tenant_id="eng", generation=1,
                            runner_profile="mock", stream=io.StringIO()),
        cap=3,
    )
    assert [c["subject"] for c in commits] == ["commit 4", "commit 3", "commit 2"]
    assert truncated is True


def test_the_finish_writes_the_record_into_result_summary(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01})
    worker, _, _ = worker_factory()
    upload = worker._upload_outputs
    record = {
        "name": "swarm/task_1",
        "head": "c" * 40,
        "head_sha": "c" * 40,
        "base_sha": "b" * 40,
        "commits": [{"sha": "c" * 40, "subject": "the work"}],
        "commits_truncated": False,
    }

    def pushed_then_upload(**kwargs: Any) -> dict[str, Any]:
        worker._pushed_branch = dict(record)
        return upload(**kwargs)

    worker._upload_outputs = pushed_then_upload  # type: ignore[method-assign]
    assert worker.run() == 0
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["result_summary"]["branch"] == record
