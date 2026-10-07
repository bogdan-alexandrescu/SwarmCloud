"""The agent's commits name the person who dispatched the task (P37).

Owner decision 2026-10-06: all seven chunk-3 agents that committed failed their
first `git commit` for want of an identity, then committed as
`swarm <swarm@localhost>`. The worker now sets GIT_AUTHOR_* and GIT_COMMITTER_*
in the runner's environment from the task document -- the signed
`submitted_by`, and `metadata.dispatch.git_identity` where swarm-api recorded
more than that address -- and the CLI runner carries them to the
agent. Nothing in `input` reaches them (invariant 10).

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_worker import workspace as workspace_mod
from agent_worker.gitidentity import (
    BOT_GIT_IDENTITY,
    GIT_IDENTITY_ENV,
    commit_identity,
    git_identity_env,
)
from swarm_api import gitidentity as api_gitidentity

from worker_seeds import seed_attempt, seed_tenant

ALICE = "alice@saga.xyz"
FIXER = "swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"


def _task(submitted_by: str, record: dict | None = None, **input_: str) -> dict:
    dispatch = {"strategy": "direct-pr", "carrier": "checkpoints"}
    if record is not None:
        dispatch["git_identity"] = record
    return {"submitted_by": submitted_by, "metadata": {"dispatch": dispatch}, "input": dict(input_)}


# ---------------------------------------------------------------------------
# Who the document names
# ---------------------------------------------------------------------------

def test_a_task_submitted_by_a_person_commits_as_that_person():
    task = _task(ALICE, {"name": "Alice Example", "email": ALICE})
    assert commit_identity(task) == ("Alice Example", ALICE)
    assert git_identity_env(task) == {
        "GIT_AUTHOR_NAME": "Alice Example",
        "GIT_AUTHOR_EMAIL": ALICE,
        "GIT_COMMITTER_NAME": "Alice Example",
        "GIT_COMMITTER_EMAIL": ALICE,
    }


def test_a_task_with_no_recorded_name_commits_as_the_submitters_local_part():
    # A document written before the record existed: the signed submitter.
    assert commit_identity(_task(ALICE)) == ("alice", ALICE)


def test_a_record_naming_someone_other_than_the_signed_submitter_is_not_used():
    # The record is outside the signature; the submitter is inside it.
    task = _task(ALICE, {"name": "Mallory", "email": "mallory@saga.xyz"})
    assert commit_identity(task) == ("alice", ALICE)


def test_a_service_account_submission_commits_as_the_person_it_records():
    task = _task(FIXER, {"name": "Alice Example", "email": ALICE})
    assert commit_identity(task) == ("Alice Example", ALICE)


def test_a_service_account_submission_with_no_person_commits_as_the_bot():
    assert commit_identity(_task(FIXER)) == BOT_GIT_IDENTITY
    assert commit_identity(None) == BOT_GIT_IDENTITY
    assert BOT_GIT_IDENTITY == ("SwarmCloud", "swarmcloud@users.noreply.github.com")


def test_the_bot_identity_is_the_one_swarm_api_records():
    assert BOT_GIT_IDENTITY == (api_gitidentity.BOT_NAME, api_gitidentity.BOT_EMAIL)
    assert api_gitidentity.GIT_IDENTITY_FIELD == "git_identity"


def test_a_name_git_would_refuse_is_cleaned():
    task = _task(ALICE, {"name": "Alice <evil>\nX", "email": ALICE})
    name, _ = commit_identity(task)
    assert "<" not in name and ">" not in name and "\n" not in name


def test_input_or_caller_metadata_naming_an_identity_is_ignored():
    task = _task(
        ALICE, {"name": "Alice Example", "email": ALICE},
        GIT_AUTHOR_NAME="Mallory", git_author_email="mallory@saga.xyz",
    )
    # A caller's own top-level metadata key: swarm-api stores it, nothing reads it.
    task["metadata"]["git_identity"] = {"name": "Mallory", "email": "mallory@saga.xyz"}
    assert commit_identity(task) == ("Alice Example", ALICE)


# ---------------------------------------------------------------------------
# Into the runner's environment, and on to the agent's
# ---------------------------------------------------------------------------

def test_the_worker_sets_the_identity_in_the_runners_environment(db, worker_factory, tmp_path):
    seed_attempt(db, runner_profile="mock")
    seed_tenant(db)
    worker, _, _ = worker_factory(runner_profile="mock")
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._task = _task(ALICE, {"name": "Alice Example", "email": ALICE})

    env = worker._build_child_env()

    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        assert env[name] == "Alice Example"
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        assert env[name] == ALICE


def _fake_cli(tmp_path: Path) -> Path:
    binary = tmp_path / "fake-cli"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "seen = {k: v for k, v in os.environ.items() if k.startswith('GIT_')}\n"
        "open(os.path.join(os.environ['SWARM_ARTIFACTS_DIR'], 'git-env.json'), 'w')"
        ".write(json.dumps(seen))\n"
        "print(json.dumps({'result': 'ok'}))\n"
    )
    binary.chmod(0o755)
    return binary


def test_the_cli_runner_carries_the_identity_to_the_agent(tmp_path, monkeypatch):
    from agent_worker.runners.base import RunnerContext
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "fake-" + "k" * 24)
    monkeypatch.setenv("FAKE_BIN", str(_fake_cli(tmp_path)))
    for name, value in git_identity_env(_task(ALICE, {"name": "Alice Example", "email": ALICE})).items():
        monkeypatch.setenv(name, value)
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    ctx = RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload={"prompt": "commit something"},
    )
    spec = CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=(), key_env="FAKE_KEY", model_flag=None,
        transcript_name="fake-transcript.json",
    )

    run_cli_agent(ctx, spec)

    seen = json.loads((artifacts / "git-env.json").read_text())
    assert {name: seen.get(name) for name in GIT_IDENTITY_ENV} == {
        "GIT_AUTHOR_NAME": "Alice Example",
        "GIT_AUTHOR_EMAIL": ALICE,
        "GIT_COMMITTER_NAME": "Alice Example",
        "GIT_COMMITTER_EMAIL": ALICE,
    }
