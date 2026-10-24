"""The questions sentence tells the agent how to wait for the owner (#2180).

THE MEASURED FAILURE (task_adb791a17d2e47d6985f). A ci-fix step's agent
rightly stopped on a decision that was the owner's. It wrote
`questions.json`, committed nothing and exited, and the step FAILED
`published_nothing`. The worker could have parked it: `Worker.
_await_owner_or_release` (lifecycle STEP 9b', before `_finalise` and so
before the publish check) parks HUMAN_REQUIRED when `questions.await_answers`
finds an empty `await-answers` beside a `questions.json` holding a
`"blocking": true` question. But `expected_outputs.questions_line`, the only
sentence that tells a claude-code or codex agent it may ask, named neither
the `blocking` key nor `await-answers`, and said the file was "never acted
on". So no agent could know how to wait.

What each test holds:

* the prompt the real claude-code and codex runners send a repository step
  -- the one a pull-request step gets, publish paragraph and all -- names
  `await-answers` and `"blocking": true`;
* the file names in the sentence are the module constants the worker reads,
  so a renamed marker cannot leave the prompt telling agents to write a file
  nothing looks for;
* a pull-request (`direct-pr`) step whose agent writes one blocking question
  and `await-answers`, commits nothing and exits 0 is PARKED HUMAN_REQUIRED
  with its lease released and a `question_wait` record, not FAILED
  `published_nothing`. That one may already pass before the prompt fix: it
  pins the worker behaviour the new sentence promises.

The sentence's words are spelled out rather than imported where they are
asserted: what the agent reads is the thing under test.

MUTATIONS: put the old sentence back in `questions_line` -- the first two
tests go red. Drop the STEP 9b' call in `Worker._execute` -- the third ends
FAILED `published_nothing`.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops, lifecycle
from agent_worker import questions as questions_mod
from agent_worker.errors import ExitCode
from agent_worker.expected_outputs import questions_line
from agent_worker.runners import claude_code, cliagent, codex
from agent_worker.runners.base import RunnerContext
from swarm_common.states import ParkReason, TaskState

from test_standalone_outputs import (  # noqa: F401 - agent_cli is a fixture
    _git,
    _seed,
    agent_cli,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: What the agent must be told, in the words it is told.
AWAIT_FILE = "await-answers"
BLOCKING_KEY = '"blocking": true'


# ---------------------------------------------------------------------------
# the prompt a repository step is sent
# ---------------------------------------------------------------------------

#: Records each start's stdin in `$HOME/stdin.txt`; answers as stream-json.
RECORDING_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
pathlib.Path(os.environ["HOME"], "stdin.txt").write_text(sys.stdin.read())
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "num_turns": 1}), flush=True)
'''


def _repository_ctx(tmp_path: Path) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    checkout = work / "repo"
    checkout.mkdir(parents=True)
    artifacts.mkdir()
    ctx = RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload={"prompt": "Make the failing check green."},
    )
    ctx.repo_dir = checkout
    return ctx


@pytest.mark.parametrize("runner, bin_env, key_env, key", [
    # Built at runtime: no credential-shaped literal in this file.
    (claude_code, "CLAUDE_CODE_BIN", "ANTHROPIC_API_KEY", "sk-ant-" + "api03-" + pysecrets.token_hex(16)),
    (codex, "CODEX_BIN", "OPENAI_API_KEY", "sk-" + "proj-" + pysecrets.token_hex(16)),
], ids=["claude-code", "codex"])
def test_a_repository_steps_prompt_names_await_answers_and_blocking(
    tmp_path, monkeypatch, runner, bin_env, key_env, key
):
    ctx = _repository_ctx(tmp_path)
    cli = tmp_path / "fake-cli"
    cli.write_text(RECORDING_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv(bin_env, str(cli))
    monkeypatch.setenv(key_env, key)
    for name in (
        "CLAUDE_CODE_ARGS", "CODEX_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
        "SWARM_CHILDREN", "SWARM_GIT", cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV,
        cliagent.RESUME_REASON_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    runner.body(ctx)

    prompt = (ctx.work_dir / "stdin.txt").read_text()
    # A repository step: the agent started in the checkout and was told the
    # worker publishes its committed branch. Without this the test could pass
    # on a standalone prompt and say nothing about a pull-request step.
    assert cliagent.PUBLISH_PARAGRAPH in prompt, prompt
    # MUTATION: the pre-#2180 sentence names neither, and an agent that must
    # wait for the owner fails `published_nothing` instead (task_adb791a17d2e47d6985f).
    assert AWAIT_FILE in prompt, prompt
    assert BLOCKING_KEY in prompt, prompt
    # The old promise was false for a blocking question.
    assert "never acted on" not in prompt, prompt
    # Said once, from the one definition both runners share.
    assert prompt.count(questions_line()) == 1, prompt


def test_the_prompt_names_the_markers_the_worker_reads():
    """A renamed marker must not leave agents writing a file nothing reads."""
    line = questions_line()
    assert questions_mod.NAME in line, line
    # MUTATION: rename AWAIT_ANSWERS without the sentence following and this is red.
    assert questions_mod.AWAIT_ANSWERS in line, line
    assert f"{questions_mod.AWAIT_ANSWERS} beside {questions_mod.NAME}" in line, line
    assert "blocking" in questions_mod.QUESTION_KEYS
    # A non-blocking question does not stop the step.
    assert "does not stop" in line, line
    assert "\n" not in line, "the sentence is one paragraph"
    lowered = line.lower()
    for marker in (*cliagent._RATE_LIMIT_MARKERS, *cliagent._CREDENTIAL_MARKERS):
        assert marker not in lowered, marker


# ---------------------------------------------------------------------------
# the worker: a pull-request step that only asks parks
# ---------------------------------------------------------------------------


def _origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "base")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(origin)],
                   check=True, capture_output=True)
    # `validate_repository_url` allows https and ssh only; a `file://` URL under
    # this test's own tmp_path is let through, every other URL is not.
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(
        gitops, "validate_repository_url", lambda url: url if url.startswith(allowed) else real(url)
    )
    return origin


def _blocking_question() -> dict[str, Any]:
    return {
        "question": "Pin the flaky check or drop it?",
        "options": [
            {"label": "pin", "description": "Keep it, pinned to the last good version."},
            {"label": "drop", "description": "Remove it from the required set."},
        ],
        "recommended": "pin",
        "context": "The fix needs a decision only the owner can make.",
        "blocking": True,
    }


@needs_git
def test_a_pull_request_step_with_only_a_blocking_question_parks_instead_of_published_nothing(
    db, tmp_path, monkeypatch, worker_factory, agent_cli
):
    """May pass before the prompt fix: it pins the park the new sentence promises."""
    origin = _origin(tmp_path, monkeypatch)
    asked = [_blocking_question()]
    # Commits nothing: only the two files in the artifacts folder.
    _seed(
        db,
        {"write_artifact": {questions_mod.NAME: json.dumps(asked), questions_mod.AWAIT_ANSWERS: ""}},
        metadata={"dispatch": {"strategy": "direct-pr"}},
    )
    db.doc("tasks/task_1")["repository_url"] = f"file://{origin}"
    # As in test_published_nothing: so a regression reads as `published_nothing`
    # rather than as a missing title.
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: False)
    worker, _config, _ = worker_factory(runner_profile="claude-code")
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")

    # MUTATION: drop the STEP 9b' call in `_execute` and this is FAILED.
    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value, (task["state"], task.get("last_error"))
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert "published_nothing" not in str(task.get("last_error") or "")
    # Invariants 1 and 4: no lease is held while the owner thinks.
    assert task["current_lease_id"] is None
    assert db.doc("leases/lease_1")["released_at"] is not None
    (entry,) = task["blocked_by"]
    wait = entry[questions_mod.QUESTION_WAIT_KEY]
    assert wait["questions"] == asked
    assert wait["blocking"] == [0]
