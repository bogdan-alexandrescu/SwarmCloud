"""The agent's commits name the dispatcher's GitHub account, never an email of theirs.

Owner decision 2026-10-06 (P37): commits name the person who dispatched the
task. Owner hard rule 2026-11-07: no commit carries an email address that
identifies a person or a company -- GitHub refused every push that exposed a
private address (GH007). So the worker sets GIT_AUTHOR_* and GIT_COMMITTER_*
from the signed `metadata.dispatch.git_identity` record ONLY when its email is
a GitHub no-reply address (swarm-api writes `<login>
<id+login@users.noreply.github.com>` for a person with a connected GitHub
account), and otherwise from `BOT_GIT_IDENTITY`. `submitted_by` is never a
commit email. Nothing in `input` reaches them (invariant 10).

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.config import ConfigError
from agent_worker.gitidentity import (
    BOT_GIT_IDENTITY,
    GIT_IDENTITY_ENV,
    commit_identity,
    git_identity_env,
    is_noreply,
)
from swarm_api import gitidentity as api_gitidentity

from worker_seeds import seed_attempt, seed_tenant

ALICE = "alice@example.com"
FIXER = "swarm-ci-fix@example-project.iam.gserviceaccount.com"
LOGIN = "octo-alice"
NOREPLY = f"583231+{LOGIN}@users.noreply.github.com"
RECORD = {"name": LOGIN, "email": NOREPLY}


def _task(submitted_by: str, record: dict | None = None, **input_: str) -> dict:
    dispatch = {"strategy": "direct-pr", "carrier": "checkpoints"}
    if record is not None:
        dispatch["git_identity"] = record
    return {"submitted_by": submitted_by, "metadata": {"dispatch": dispatch}, "input": dict(input_)}


# ---------------------------------------------------------------------------
# Who the document names
# ---------------------------------------------------------------------------

def test_a_person_with_a_connected_github_account_commits_as_their_login():
    task = _task(ALICE, RECORD)
    assert commit_identity(task) == (LOGIN, NOREPLY)
    assert git_identity_env(task) == {
        "GIT_AUTHOR_NAME": LOGIN,
        "GIT_AUTHOR_EMAIL": NOREPLY,
        "GIT_COMMITTER_NAME": LOGIN,
        "GIT_COMMITTER_EMAIL": NOREPLY,
    }


def test_a_person_without_a_connected_github_account_commits_as_the_bot():
    # No record: the signed submitter's address is never a commit email.
    assert commit_identity(_task(ALICE)) == BOT_GIT_IDENTITY


def test_a_record_naming_a_persons_own_address_is_never_used():
    # A record written before 2026-11-07 named the submitter's own address.
    assert commit_identity(_task(ALICE, {"name": "Alice Example", "email": ALICE})) == BOT_GIT_IDENTITY
    assert commit_identity(_task(ALICE, {"name": "M", "email": "m@example.com"})) == BOT_GIT_IDENTITY


@pytest.mark.parametrize("email", [
    "alice@users.noreply.github.com.example.com",
    "alice+users.noreply.github.com@example.com",
    "<x>@users.noreply.github.com",
    "a b@users.noreply.github.com",
    "@users.noreply.github.com",
    "583231+octo_alice@users.noreply.github.com",
    "users.noreply.github.com",
    "",
])
def test_an_address_that_only_looks_like_a_no_reply_is_not_one(email):
    assert not is_noreply(email)
    assert commit_identity(_task(ALICE, {"name": LOGIN, "email": email})) == BOT_GIT_IDENTITY


def test_a_service_account_submission_commits_as_the_github_account_it_records():
    assert commit_identity(_task(FIXER, RECORD)) == (LOGIN, NOREPLY)


def test_a_service_account_submission_with_no_person_commits_as_the_bot():
    assert commit_identity(_task(FIXER)) == BOT_GIT_IDENTITY
    assert commit_identity(None) == BOT_GIT_IDENTITY
    assert BOT_GIT_IDENTITY == ("SwarmCloud", "swarmcloud@users.noreply.github.com")


def test_the_bot_identity_is_the_one_swarm_api_records():
    assert BOT_GIT_IDENTITY == (api_gitidentity.BOT_NAME, api_gitidentity.BOT_EMAIL)
    assert api_gitidentity.GIT_IDENTITY_FIELD == "git_identity"
    assert api_gitidentity.NOREPLY_SUFFIX == "@users.noreply.github.com"


def test_a_name_git_would_refuse_is_cleaned():
    task = _task(ALICE, {"name": "Alice <evil>\nX", "email": NOREPLY})
    name, email = commit_identity(task)
    assert "<" not in name and ">" not in name and "\n" not in name
    assert email == NOREPLY


def test_a_record_with_no_name_is_named_by_the_login_in_its_address():
    assert commit_identity(_task(ALICE, {"email": NOREPLY})) == (LOGIN, NOREPLY)


def test_input_or_caller_metadata_naming_an_identity_is_ignored():
    task = _task(
        ALICE, RECORD,
        GIT_AUTHOR_NAME="Mallory", git_author_email="mallory@example.com",
    )
    # A caller's own top-level metadata key: swarm-api stores it, nothing reads it.
    task["metadata"]["git_identity"] = {"name": "mallory", "email": "1+mallory@users.noreply.github.com"}
    assert commit_identity(task) == (LOGIN, NOREPLY)
    del task["metadata"]["dispatch"]["git_identity"]
    assert commit_identity(task) == BOT_GIT_IDENTITY


@pytest.mark.parametrize("task", [
    None,
    _task(ALICE),
    _task(FIXER),
    _task(ALICE, RECORD),
    _task(ALICE, {"name": "Alice", "email": ALICE}),
    _task(FIXER, {"name": "Alice", "email": ALICE}),
    {"submitted_by": ALICE},
    {"submitted_by": ALICE, "metadata": {"dispatch": {"git_identity": "alice@example.com"}}},
])
def test_no_path_yields_an_address_that_is_not_a_github_no_reply(task):
    _, email = commit_identity(task)
    assert email.endswith("@users.noreply.github.com") and is_noreply(email)
    assert email != ALICE


# ---------------------------------------------------------------------------
# The worker's own commits
# ---------------------------------------------------------------------------

def test_the_workers_own_identity_is_a_github_no_reply(db, worker_factory):
    seed_attempt(db, runner_profile="mock")
    seed_tenant(db)
    worker, _, _ = worker_factory(runner_profile="mock")
    assert is_noreply(worker.cfg.git_author_email)
    # A task naming nobody publishes as the worker's own identity: a no-reply.
    worker._task = _task(ALICE)
    assert is_noreply(worker._publish_identity()[1])
    worker._task = _task(ALICE, RECORD)
    assert worker._publish_identity() == (LOGIN, NOREPLY)


@pytest.mark.parametrize("email", ["agent@example.com", "swarm@localhost"])
def test_a_worker_configured_with_an_address_that_is_not_a_no_reply_refuses_to_start(
    db, worker_factory, email
):
    import dataclasses

    seed_attempt(db, runner_profile="mock")
    seed_tenant(db)
    worker, _, _ = worker_factory(runner_profile="mock")
    with pytest.raises(ConfigError, match="no-reply"):
        dataclasses.replace(worker.cfg, git_author_email=email)


# ---------------------------------------------------------------------------
# Into the runner's environment, and on to the agent's
# ---------------------------------------------------------------------------

def test_the_worker_sets_the_identity_in_the_runners_environment(db, worker_factory, tmp_path):
    seed_attempt(db, runner_profile="mock")
    seed_tenant(db)
    worker, _, _ = worker_factory(runner_profile="mock")
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._task = _task(ALICE, RECORD)

    env = worker._build_child_env()

    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        assert env[name] == LOGIN
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        assert env[name] == NOREPLY


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
    for name, value in git_identity_env(_task(ALICE, RECORD)).items():
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
        "GIT_AUTHOR_NAME": LOGIN,
        "GIT_AUTHOR_EMAIL": NOREPLY,
        "GIT_COMMITTER_NAME": LOGIN,
        "GIT_COMMITTER_EMAIL": NOREPLY,
    }
