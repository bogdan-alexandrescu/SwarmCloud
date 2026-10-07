"""An index run's clone holds the extractor's history window (G4-06).

QA pass of 2026-10-07 on the live index of this repository: all 50 hot spots
read `changes: 1`, `co_changed` was empty and no test map edge had
`co-change` evidence, because the worker cloned one commit deep and git shows
that one commit as adding every file. The extractor's notes said so; the
counts did not.

Pinned here: the indexer profile's clone deepens to the window by date and one
parent past it (`gitops.deepen_history`), with the credential file present
during the fetch and gone after; a deepen the forge refuses leaves the clone;
every other profile's clone is unchanged; and the extractor never counts a
shallow history's boundary commit -- a one-commit clone has NO history, said
with the reason, rather than "every file changed once".
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import repo_index_fixtures as fx
from agent_worker import gitops, indexrun, lifecycle
from agent_worker import workspace as workspace_mod
from agent_worker.gitops import CloneResult
from agent_worker.logs import build_logger

from worker_seeds import TENANT, seed_attempt

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index" / "repo_index_extract.py"
URL = "https://github.com/acme/widgets.git"
SHA = "9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304"
DAY = 86_400


@pytest.fixture(scope="module")
def tool() -> Any:
    spec = importlib.util.spec_from_file_location("repo_index_extract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_index_extract"] = module
    spec.loader.exec_module(module)
    return module


def _logger():
    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
                        runner_profile="indexer", stream=io.StringIO())


def test_only_an_index_run_asks_for_the_extractors_window(tool: Any) -> None:
    assert indexrun.HISTORY_DAYS == tool.HISTORY_DAYS == 90
    assert indexrun.clone_history_days(indexrun.INDEXER_PROFILE) == 90
    for profile in ("claude-code", "codex", "mock"):
        assert indexrun.clone_history_days(profile) is None


# ---------------------------------------------------------------------------
# gitops: the deepen, after the clone, with the credential, never fatal
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.timed_out = False
        self.duration_seconds = 0.1


def _fake_git(ws: Any, calls: list[dict], *, head_time: int, fetch_exit: int = 0,
              shallow_after: bool = True):
    def run_child(argv, **kwargs):
        argv = list(argv)
        calls.append({"argv": argv,
                      "cred": (ws.private / ".git-credentials").exists()})
        kwargs["stdout_path"].parent.mkdir(parents=True, exist_ok=True)
        out = SHA + "\n"
        if "show" in argv:
            out = f"{SHA} {head_time}\n"
        kwargs["stdout_path"].write_text(out)
        kwargs["stderr_path"].write_text("fatal: refused" if fetch_exit else "")
        if any(a.startswith("--shallow-since=") for a in argv):
            if shallow_after:
                git_dir = ws.work / "repo" / ".git"
                git_dir.mkdir(parents=True, exist_ok=True)
                (git_dir / "shallow").write_text("0" * 40 + "\n")
            return _Result(fetch_exit)
        return _Result()

    return run_child


def _clone(ws: Any, **kwargs: Any) -> CloneResult:
    return gitops.shallow_clone(
        url=URL, ref=SHA, destination=ws.work / "repo", private_dir=ws.private,
        logs_dir=ws.logs, timeout_seconds=30, logger=_logger(),
        token="-".join(["not", "a", "real", "token"]), **kwargs,
    )


def test_an_index_runs_clone_deepens_to_the_window_with_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop the `history_days` branch from `shallow_clone` and no
    fetch carries `--shallow-since`; move it after the `finally` and the
    fetch runs with the credential file already gone."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    calls: list[dict] = []
    head_time = 1_790_000_000
    monkeypatch.setattr(gitops, "run_child", _fake_git(ws, calls, head_time=head_time))

    result = _clone(ws, history_days=90)

    assert result.commit == SHA
    deepen = [c for c in calls if any(a.startswith("--shallow-since=") for a in c["argv"])]
    assert len(deepen) == 1
    since = next(a for a in deepen[0]["argv"] if a.startswith("--shallow-since="))
    assert since == "--shallow-since=" + time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(head_time - 90 * DAY))
    assert deepen[0]["argv"][-2:] == ["origin", SHA]
    assert deepen[0]["cred"] is True
    # One parent past the window, while still shallow.
    parent = [c for c in calls if "--deepen=1" in c["argv"]]
    assert len(parent) == 1 and parent[0]["cred"] is True
    assert calls.index(parent[0]) > calls.index(deepen[0])
    # The deepen comes after the checkout, and the credential is gone after it.
    checkout = next(i for i, c in enumerate(calls) if "checkout" in c["argv"])
    assert calls.index(deepen[0]) > checkout
    assert not (ws.private / ".git-credentials").exists()
    assert all("not-a-real-token" not in part for c in calls for part in c["argv"])


def test_a_window_holding_the_whole_history_fetches_no_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    calls: list[dict] = []
    monkeypatch.setattr(gitops, "run_child",
                        _fake_git(ws, calls, head_time=1_790_000_000, shallow_after=False))
    _clone(ws, history_days=90)
    assert [c for c in calls if any(a.startswith("--shallow-since=") for a in c["argv"])]
    assert not [c for c in calls if "--deepen=1" in c["argv"]]


def test_a_refused_deepen_leaves_the_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    calls: list[dict] = []
    monkeypatch.setattr(gitops, "run_child",
                        _fake_git(ws, calls, head_time=1_790_000_000, fetch_exit=128))
    result = _clone(ws, history_days=90)
    assert result.commit == SHA
    assert not (ws.private / ".git-credentials").exists()


def test_every_other_clone_stays_one_commit_deep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    calls: list[dict] = []
    monkeypatch.setattr(gitops, "run_child", _fake_git(ws, calls, head_time=1_790_000_000))
    _clone(ws)
    assert not [c for c in calls if "show" in c["argv"]
                or any(a.startswith(("--shallow-since", "--deepen")) for a in c["argv"])]


@pytest.mark.parametrize("profile, days", [("indexer", 90), ("mock", None)])
def test_the_worker_passes_the_window_for_an_index_run_only(
    db, worker_factory, monkeypatch: pytest.MonkeyPatch, profile: str, days: int | None
) -> None:
    seen: list[dict] = []

    def clone(**kwargs: Any) -> CloneResult:
        seen.append(kwargs)
        return CloneResult(path=kwargs["destination"], url=kwargs["url"], ref=kwargs["ref"],
                           commit="a" * 40, duration_seconds=0.1)

    monkeypatch.setattr(lifecycle, "shallow_clone", clone)
    seed_attempt(db, runner_profile=profile)
    worker, config, _ = worker_factory(repository_url=URL, runner_profile=profile,
                                       timeout_seconds=3600)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": "task_1"}
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    worker._deadline = time.monotonic() + 3600
    worker._clone_keeping_lease(worker._task)
    assert len(seen) == 1
    assert seen[0]["history_days"] == days


# ---------------------------------------------------------------------------
# the extractor: a shallow boundary is never counted
# ---------------------------------------------------------------------------


def _history(start: int) -> list[tuple[int, dict[str, str]]]:
    steps: list[tuple[int, dict[str, str]]] = []
    # Old work, outside the head's window.
    for i in range(3):
        steps.append((start + (10 + i) * DAY, {"src/pkg/store.py": f"# old {i}\n"}))
    # Inside it: users.py and its test together six times, web.py once.
    for i in range(6):
        steps.append((start + (100 + i) * DAY, {
            "src/pkg/users.py": fx.PYTHON_APP["src/pkg/users.py"] + f"# {i}\n",
            "tests/test_users.py": fx.PYTHON_APP["tests/test_users.py"] + f"# {i}\n",
        }))
    steps.append((start + 120 * DAY, {"src/pkg/web.py": "# head\n"}))
    return steps


def _git(args: list[str], env: dict[str, str]) -> None:
    subprocess.run(args, env=env, check=True, capture_output=True)


def test_a_one_commit_clone_has_no_history_and_a_deepened_one_counts_the_window(
    tool: Any, tmp_path: Path
) -> None:
    """MUTATION: count the boundary commit again in `_history` and the
    one-commit clone reports every file as a hot spot with `changes: 1`."""
    start = 1_780_000_000
    origin = fx.build_repo(tmp_path / "origin", fx.PYTHON_APP, history=_history(start),
                           first_commit_at=start)
    env = fx.git_env(tmp_path)
    clone = tmp_path / "clone"
    _git(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(clone)], env)

    shallow = tool.extract(clone, tool.Budget())
    window = shallow["extractor"]["history"]
    assert shallow["hot_spots"] == []
    assert window["available"] is False and "shallow" in window["reason"]
    assert window["commits"] == 0 and window["boundary_commits"] == 1
    assert window["window_covered"] is False

    head = subprocess.run(["git", "-C", str(clone), "show", "-s", "--format=%H %ct", "HEAD"],
                          env=env, check=True, capture_output=True, text=True).stdout.split()
    _git(gitops.history_fetch_argv("git", [], clone, head[0], int(head[1]), 90), env)
    _git(gitops.history_parent_argv("git", [], clone, head[0]), env)

    deep = tool.extract(clone, tool.Budget())
    full = tool.extract(origin, tool.Budget())
    window = deep["extractor"]["history"]
    assert window["available"] is True and window["window_covered"] is True
    assert window["commits"] == full["extractor"]["history"]["commits"] == 7
    assert deep["hot_spots"] == full["hot_spots"]
    spots = {h["path"]: h["changes"] for h in deep["hot_spots"]}
    assert spots == {"src/pkg/users.py": 6, "tests/test_users.py": 6, "src/pkg/web.py": 1}
    assert "co-change" in [
        e["evidence"] for e in deep["test_map"]
        if e["source"] == "src/pkg/users.py" and e["test"] == "tests/test_users.py"
    ] + [a for e in deep["test_map"]
         if e["source"] == "src/pkg/users.py" and e["test"] == "tests/test_users.py"
         for a in e["also_evidence"]]
