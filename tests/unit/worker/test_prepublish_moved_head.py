"""A HEAD the agent moved off its branch is handed back to it once, by the gate.

REPORTED by a peer application's session on 5 November 2026 (about 06:20
Pacific): task_884a993427ce44feb7a6 FAILED with "published_nothing: HEAD moved
off this task's branch (to rebuild/pr-1233)". Its agent merged a train branch
into its own branch, then checked out another branch; the harvest's moved-HEAD
rule (#2094) rightly refused that line, and the merge was lost until a person
rebuilt it by hand.

So, while the session can still be continued (`prepublish.Gate`, the
claude-code runner's pre-publish gate), a moved HEAD is a gate finding: the
session is resumed ONCE with an instruction naming the step's branch and where
HEAD is now. Against a real local bare origin:

  * a step that ends on another branch is handed back once, and once the
    agent returns the gate goes on as usual (and the harvest publishes it,
    test_harvest_moved_head_bundle.py);
  * one that stays away gets no second turn: the finding is recorded and the
    harvest refuses the line as before;
  * a HEAD that descends from the start, on the step's own branch or on a
    branch the agent made, takes no turn for it;
  * the worker names the start branch to the runner, under one name both
    sides hold.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle, prepublish
from agent_worker.logs import build_logger

OWN = "swarm/task_own"
OTHER = "rebuild/pr-1233"
TRAIN = "swarm/task_train"

_IDENT = {"GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
          "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_IDENT}).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _world(tmp_path: Path) -> tuple[Path, str]:
    """A bare origin with main, the step's branch, a train branch and another
    pull request's branch; the step's clone on its own branch."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    base = _commit(seed, "README.md")
    _git(seed, "branch", OWN)
    _git(seed, "checkout", "-q", "-b", TRAIN)
    _commit(seed, "train.txt")
    # Another pull request's branch, cut from an older line: no history of
    # this step's start.
    _git(seed, "checkout", "-q", "--orphan", OTHER)
    _git(seed, "rm", "-rq", "--cached", ".")
    for path in ("README.md", "train.txt"):
        (seed / path).unlink()
    _commit(seed, "other.txt")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)],
                   check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "--branch", OWN, str(origin), str(repo)],
                   check=True, capture_output=True)
    for branch in (TRAIN, OTHER):
        _git(repo, "fetch", "-q", "origin", f"{branch}:refs/remotes/origin/{branch}")
    return repo, base


def _wanders(repo: Path) -> None:
    """What task_884a993427ce44feb7a6's agent did: its own commit, a merge of
    a train branch, then a checkout of another pull request's branch."""
    _commit(repo, "mine.txt")
    _git(repo, "merge", "-q", "--no-edit", f"origin/{TRAIN}")
    _git(repo, "checkout", "-q", "-b", OTHER, f"origin/{OTHER}")


class Turns:
    """The agent's session: each resume records its prompt and runs `act`."""

    def __init__(self, *acts: Any) -> None:
        self.prompts: list[str] = []
        self._acts = list(acts)

    def __call__(self, text: str) -> bool:
        self.prompts.append(text)
        if self._acts:
            self._acts.pop(0)()
        return True


def _gate(repo: Path, tmp_path: Path, base: str, turns: Turns, *,
          start: str | None = OWN) -> prepublish.Gate:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    env = {"PATH": f"{git_dir}:/usr/bin:/bin", "HOME": str(tmp_path), **_IDENT,
           prepublish.CLONE_BASE_ENV: base}
    if start is not None:
        env[prepublish.START_BRANCH_ENV] = start
    log = build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                       runner_profile="claude-code", stream=io.StringIO())
    return prepublish.Gate(repo=repo, env=env, secrets=(), resume=turns,
                           remaining=lambda: 3600.0, log=log, artifacts_dir=artifacts)


def test_a_step_that_ends_on_another_branch_is_handed_back_once_and_goes_on_when_it_returns(
    tmp_path,
):
    repo, base = _world(tmp_path)
    _wanders(repo)
    merge = _git(repo, "rev-parse", f"{OWN}")

    def returns() -> None:
        _git(repo, "checkout", "-q", OWN)

    turns = Turns(returns)
    record = _gate(repo, tmp_path, base, turns).run()

    # MUTATION: drop the finding and the turn never happens: the harvest
    # refuses the line and the merge is lost (task_884a993427ce44feb7a6).
    assert len(turns.prompts) == 1, turns.prompts
    prompt = turns.prompts[0]
    # MUTATION: name only "your branch" and the agent cannot tell where to go.
    assert f"`{OWN}`" in prompt and f"`{OTHER}`" in prompt, prompt
    assert f"git checkout {OWN}" in prompt and "cherry-pick" in prompt, prompt
    assert record["moved_head"] == {"moved": OTHER, "branch": OWN, "turn": True,
                                    "returned": True}
    assert _git(repo, "rev-parse", "HEAD") == merge
    assert record["fix_turn"] is False


def test_a_step_that_stays_away_gets_no_second_turn(tmp_path):
    repo, base = _world(tmp_path)
    _wanders(repo)

    def stays() -> None:
        _commit(repo, "still-elsewhere.txt")

    turns = Turns(stays, stays)
    record = _gate(repo, tmp_path, base, turns).run()

    # MUTATION: hand it back in a loop and a stubborn agent burns the step's
    # budget; one turn, then the harvest decides as before.
    assert len(turns.prompts) == 1, turns.prompts
    assert record["moved_head"] == {"moved": OTHER, "branch": OWN, "turn": True,
                                    "returned": False}


def test_a_detached_head_that_left_the_start_is_handed_back_naming_its_commit(tmp_path):
    repo, base = _world(tmp_path)
    _git(repo, "checkout", "-q", "--detach", f"origin/{OTHER}")
    head = _git(repo, "rev-parse", "HEAD")[:12]

    turns = Turns(lambda: _git(repo, "checkout", "-q", OWN))
    record = _gate(repo, tmp_path, base, turns).run()

    assert len(turns.prompts) == 1 and f"`{head}`" in turns.prompts[0], turns.prompts
    assert record["moved_head"]["moved"] == head
    assert record["moved_head"]["returned"] is True


@pytest.mark.parametrize("move", ["stays", "new_branch"])
def test_a_head_that_descends_from_the_start_takes_no_turn(tmp_path, move):
    repo, base = _world(tmp_path)
    if move == "new_branch":
        # An agent's own `checkout -b` from its start is its work (the owner,
        # 2026-10-23), not a move.
        _git(repo, "checkout", "-q", "-b", "mine")
    _commit(repo, "mine.txt")
    _git(repo, "merge", "-q", "--no-edit", f"origin/{TRAIN}")

    turns = Turns()
    record = _gate(repo, tmp_path, base, turns).run()

    # MUTATION: treat any branch change as a move and every implement agent
    # on its own new branch is sent back for nothing.
    assert turns.prompts == []
    assert record["moved_head"] is None


def test_a_fetched_branch_that_descends_from_the_start_is_still_a_move(tmp_path):
    """The harvest refuses a branch the agent fetched even when it descends
    from the start (#2094); the gate hands it back the same."""
    repo, base = _world(tmp_path)
    _git(repo, "checkout", "-q", "-b", TRAIN, f"origin/{TRAIN}")

    turns = Turns(lambda: _git(repo, "checkout", "-q", OWN))
    record = _gate(repo, tmp_path, base, turns).run()

    assert len(turns.prompts) == 1 and f"`{TRAIN}`" in turns.prompts[0]
    assert record["moved_head"]["returned"] is True


def test_without_a_start_branch_the_prompt_names_the_start_commit(tmp_path, monkeypatch):
    monkeypatch.delenv(prepublish.START_BRANCH_ENV, raising=False)
    repo, base = _world(tmp_path)
    _wanders(repo)

    turns = Turns()
    record = _gate(repo, tmp_path, base, turns, start=None).run()

    assert len(turns.prompts) == 1 and f"`{base[:12]}`" in turns.prompts[0], turns.prompts
    assert record["moved_head"]["branch"] is None


def test_the_gate_names_the_branch_through_the_runners_own_env_path(tmp_path, monkeypatch):
    """Review finding: the worker sets `START_BRANCH_ENV` on the runner
    process, but `cliagent.run_prepublish_gate` builds the gate's env from the
    agent's whitelisted env, which does not carry it. The gate reads it from
    the runner's own environment, as the runner does `PREPUBLISH_BASE_ENV`."""
    from agent_worker.gitmcp import server as gitmcp_server
    from agent_worker.runners import cliagent
    from agent_worker.runners.base import RunnerContext

    repo, base = _world(tmp_path)
    _git(repo, "checkout", "-q", "-b", TRAIN, f"origin/{TRAIN}")
    # What the worker hands the runner process (`Worker._build_child_env`).
    monkeypatch.setenv(prepublish.START_BRANCH_ENV, OWN)
    monkeypatch.setenv(prepublish.CLONE_BASE_ENV, base)
    monkeypatch.delenv(prepublish.PREPUBLISH_BASE_ENV, raising=False)
    monkeypatch.delenv(gitmcp_server.SPOOL_ENV, raising=False)
    git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    # The agent's env: what the runner's whitelist passes, no START_BRANCH_ENV.
    agent_env = {"PATH": f"{git_dir}:/usr/bin:/bin", "HOME": str(tmp_path), **_IDENT}
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    ctx = RunnerContext(work_dir=tmp_path, artifacts_dir=artifacts,
                        input_path=tmp_path / "input.json", result_path=tmp_path / "result.json",
                        quota_path=tmp_path / "quota.json")
    log = build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                       runner_profile="claude-code", stream=io.StringIO())

    turns = Turns(lambda: _git(repo, "checkout", "-q", OWN))
    record = cliagent.run_prepublish_gate(
        ctx, cwd=repo, env=agent_env, credential_env="ANTHROPIC_API_KEY", secrets=(),
        log=log, resume=turns, remaining=lambda: 3600.0)

    # MUTATION: read the branch only from the agent's env and a fetched branch
    # that descends from the start gets no turn, and every other move is told
    # to check out the start commit instead of the step's branch.
    assert len(turns.prompts) == 1, record
    assert f"git checkout {OWN}" in turns.prompts[0], turns.prompts
    assert record["moved_head"]["branch"] == OWN


def test_the_runner_and_the_worker_agree_on_the_start_branch_name():
    assert prepublish.START_BRANCH_ENV == lifecycle.START_BRANCH_ENV
