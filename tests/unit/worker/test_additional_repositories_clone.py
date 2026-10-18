"""The worker clones each additional repository at its signed commit, read-only (MR-WORKER).

docs/design/multi-repository-steps.md §4.4-§4.6, #1434. A task's signed
`additional_repositories` (contract request 95) are each cloned at the
entry's `commit` onto the cache volume, linked under `work/repos/<name>`,
marked read-only, and listed in `work/.swarm-context/repositories.json`,
before the agent starts. Pinned here:

* **#1434 check 1**: a step with two additional repositories sees three
  checkouts, each `git rev-parse HEAD` equal to its signed commit -- the
  pinned one, not the branch tip;
* a revoked grant ends the attempt before the agent, terminally, with no
  clone and no token read;
* a clone that fails ends the attempt retryably, naming the entry and never
  its URL;
* a total over the platform's size budget (a run-time setting, never a code
  constant) ends the attempt terminally;
* a resumed attempt clones the same commits again into its own cache;
* the credentials file is gone before the agent starts, and no token is in
  the context file, the environment or the log;
* an attempt without a cache volume of its own refuses them.

Real git against local bare repositories; only the transport is redirected
from the signed `https://github.com/...` URL to the bare repository on disk.
Every fake token is built at runtime.

MUTATIONS, each caught below: drop the STEP 5' call from `_prepare` -- the
run tests see the agent start with no checkouts. Clone the branch tip instead
of `commit` -- check 1 sees the tip. Skip the grant loop -- the revoked-grant
test sees a clone. Put git's text in the clone refusal -- the URL test fails.
Hard-code the budget -- the setting test passes the clone.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker import extrarepos, gitops, lifecycle, workspace as workspace_mod
from agent_worker.gitops import CloneResult, GitError
from swarm_api import forgeapp, gittokens, repositories
from swarm_common.states import EventType, TaskState

from fakes import FakeSecretClient
from worker_seeds import TENANT, seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    local_urls,
    origin,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PERSON = "ada@example.com"
OWNER = "acme"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_bare(tmp_path: Path, repo: str, *, commits: int = 2) -> tuple[Path, list[str]]:
    """A bare repository with `commits` commits on main; their shas, oldest first."""
    seed = tmp_path / f"seed-{repo}"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    shas: list[str] = []
    for i in range(commits):
        (seed / "README.md").write_text(f"{repo} revision {i}\n")
        (seed / "src").mkdir(exist_ok=True)
        (seed / "src" / f"mod{i}.py").write_text(f"VALUE = {i}\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "--quiet", "-m", f"{repo} {i}")
        shas.append(_git(seed, "rev-parse", "HEAD"))
    bare = tmp_path / f"{repo}.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
                   check=True, capture_output=True)
    # Let `fetch <sha>` reach a commit that is not a branch tip, as GitHub does.
    _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    return bare, shas


def user_slot(user: str = PERSON) -> str:
    return gittokens.provider_suffix(gittokens.Scope.USER, user=user)


def entry(repo: str, commit: str, *, name: str | None = None, ref: str = "main",
          forge_credential: str = "git", size_kib: int = 10) -> dict[str, Any]:
    """One `Task.additional_repositories` entry, in the contract's shape."""
    return {
        "name": name or repo,
        "repo_id": repositories.repo_id_for(TENANT, OWNER, repo),
        "repository": f"{OWNER}/{repo}",
        "repository_url": f"https://github.com/{OWNER}/{repo}",
        "ref": ref,
        "commit": commit,
        "forge_credential": forge_credential,
        "forge_credential_source": "person" if forge_credential != "git" else "tenant",
        "size_kib": size_kib,
    }


def redirect_clones(monkeypatch, bares: dict[str, Path]) -> list[dict[str, Any]]:
    """Send each entry's clone to its bare repository on disk, through the real
    `clone_at_commit`. Records each call's arguments."""
    real = gitops.clone_at_commit
    seen: list[dict[str, Any]] = []

    def clone(**kwargs: Any) -> CloneResult:
        seen.append(dict(kwargs))
        repo = kwargs["url"].rsplit("/", 1)[-1]
        return real(**{**kwargs, "url": f"file://{bares[repo]}"})

    monkeypatch.setattr(extrarepos, "clone_at_commit", clone)
    return seen


def make_worker(worker_factory, tmp_path: Path, *, task: dict[str, Any],
                attempt_id: str = "att_1", cache: bool = True, **kwargs: Any):
    worker, config, _ = worker_factory(
        attempt_id=attempt_id,
        cache_root=(tmp_path / "cache") if cache else None,
        **kwargs,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id,
                                     cache_root=config.cache_root)
    worker._task = task
    worker._deadline = time.monotonic() + 3600
    return worker, config


def head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# #1434 check 1: three checkouts, each at its signed commit
# ---------------------------------------------------------------------------


def test_two_additional_repositories_give_three_checkouts_at_their_signed_commits(
    worker_factory, monkeypatch, tmp_path, origin, local_urls
):
    """MUTATION: clone `ref` instead of `commit` and `web` lands on its tip."""
    web, web_shas = make_bare(tmp_path, "web", commits=3)
    sdk, sdk_shas = make_bare(tmp_path, "sdk", commits=2)
    redirect_clones(monkeypatch, {"web": web, "sdk": sdk})
    # `web` signed at its FIRST commit, not its tip: the pin, not the branch.
    task = {"task_id": "task_1", "additional_repositories": [
        entry("web", web_shas[0]), entry("sdk", sdk_shas[-1]),
    ]}
    worker, _ = make_worker(worker_factory, tmp_path, task=task,
                            repository_url=f"file://{origin}")
    assert worker._maybe_clone(task) is not None

    checkouts = worker._clone_additional_keeping_lease(task)

    work = worker.ws.work
    assert head(work / "repo") == _git(origin, "rev-parse", "main")
    assert head(work / "repos" / "web") == web_shas[0] != web_shas[-1]
    assert head(work / "repos" / "sdk") == sdk_shas[-1]
    assert [c.name for c in checkouts] == ["web", "sdk"]
    # Linked from `work/repos`, cloned onto the cache volume.
    for name in ("web", "sdk"):
        link = work / "repos" / name
        assert link.is_symlink()
        assert link.resolve().is_relative_to(worker.ws.cache.resolve())
    listed = json.loads((work / ".swarm-context" / "repositories.json").read_text())
    assert listed == [
        {"name": "web", "repository": "acme/web", "ref": "main", "commit": web_shas[0],
         "path": str(work / "repos" / "web")},
        {"name": "sdk", "repository": "acme/sdk", "ref": "main", "commit": sdk_shas[-1],
         "path": str(work / "repos" / "sdk")},
    ]
    # The read-only signal: no write bit on the tree, and a push URL nowhere.
    readme = work / "repos" / "web" / "README.md"
    assert not os.stat(readme).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert _git(work / "repos" / "web", "config", "remote.origin.pushurl") \
        == extrarepos.READ_ONLY_PUSH_URL
    # Every later checkpoint leaves them out.
    assert worker.checkpoints.leaves_out_extra_repos is True
    # And the teardown can still remove them.
    extrarepos.release(worker.ws)
    workspace_mod.destroy(worker.ws)
    assert not (worker.ws.cache / "repos").exists()


def test_a_task_with_none_clones_nothing_and_changes_nothing(worker_factory, tmp_path):
    worker, _ = make_worker(worker_factory, tmp_path, task={"task_id": "task_1"})

    assert worker._clone_additional_keeping_lease({"task_id": "task_1"}) == []
    assert not (worker.ws.work / "repos").exists()
    assert worker.checkpoints.leaves_out_extra_repos is False
    assert extrarepos.child_env(worker.ws, []) == {}


def test_the_agent_is_told_where_the_checkouts_are(
    worker_factory, monkeypatch, tmp_path, local_urls
):
    web, web_shas = make_bare(tmp_path, "web")
    redirect_clones(monkeypatch, {"web": web})
    task = {"task_id": "task_1", "additional_repositories": [entry("web", web_shas[-1])]}
    worker, _ = make_worker(worker_factory, tmp_path, task=task)

    checkouts = worker._clone_additional_keeping_lease(task)

    assert extrarepos.child_env(worker.ws, checkouts) == {
        "SWARM_REPOS_DIR": str(worker.ws.work / "repos")
    }


# ---------------------------------------------------------------------------
# A whole attempt: the grant, the agent, the ending
# ---------------------------------------------------------------------------


def _seed_task(db, entries: list[dict[str, Any]]) -> None:
    """A task with NO primary repository and additional ones (the owner's Q8)."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01})
    doc = db.doc("tasks/task_1")
    doc["submitted_by"] = PERSON
    doc["additional_repositories"] = entries


def _seed_grant(db, repo: str) -> None:
    rid = repositories.repo_id_for(TENANT, OWNER, repo)
    db.seed(f"forge_grants/{TENANT}__{forgeapp.user_hash(PERSON)}__{rid}", {
        "tenant_id": TENANT, "user": PERSON, "user_hash": forgeapp.user_hash(PERSON),
        "repo_id": rid, "repository": f"{OWNER}/{repo}", "owner": OWNER,
        "mode": "read", "can_push": False,
    })


def _agent_starts(worker, monkeypatch) -> list[bool]:
    started: list[bool] = []
    real = worker._run_child_supervised

    def spy(child_env):
        started.append(True)
        return real(child_env)

    monkeypatch.setattr(worker, "_run_child_supervised", spy)
    return started


def test_a_revoked_grant_ends_the_attempt_before_the_agent_and_any_clone(
    db, worker_factory, monkeypatch, tmp_path, log_stream
):
    token = "ghu_" + secrets.token_hex(18)
    client = FakeSecretClient({f"swarm-tenant-{TENANT}-{user_slot()}": token})
    _seed_task(db, [entry("web", "a" * 40, forge_credential=user_slot())])
    worker, _, _ = worker_factory(secret_client=client, cache_root=tmp_path / "cache")
    started = _agent_starts(worker, monkeypatch)
    cloned: list[bool] = []
    monkeypatch.setattr(extrarepos, "clone_at_commit", lambda **_: cloned.append(True))
    # No grant: the person removed the repository after submitting.

    assert worker.run() == lifecycle.ExitCode.FAILED

    assert started == [] and cloned == []
    assert client.accessed == [], "a token was read for a revoked grant"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "cannot_start"
    assert task["last_error"].startswith("additional_repository_not_granted:"), task["last_error"]
    assert "'web'" in task["last_error"]
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value] == []
    assert token not in log_stream.getvalue()


def test_a_standing_grant_reaches_the_clone_and_the_agent_sees_the_checkout(
    db, worker_factory, monkeypatch, tmp_path, local_urls, runner_inputs, log_stream
):
    """The control for the test above: the same task with its grant runs, and
    the runner input names the checkout the worker made."""
    web, web_shas = make_bare(tmp_path, "web")
    seen = redirect_clones(monkeypatch, {"web": web})
    token = "ghu_" + secrets.token_hex(18)
    client = FakeSecretClient({f"swarm-tenant-{TENANT}-{user_slot()}": token})
    _seed_task(db, [entry("web", web_shas[0], forge_credential=user_slot())])
    _seed_grant(db, "web")
    worker, _, _ = worker_factory(secret_client=client, cache_root=tmp_path / "cache")
    started = _agent_starts(worker, monkeypatch)

    assert worker.run() == 0

    assert started == [True]
    assert len(seen) == 1 and seen[0]["commit"] == web_shas[0]
    # The token the entry's own signed slot names, read at the clone.
    assert seen[0]["token"] == token
    assert f"swarm-tenant-{TENANT}-{user_slot()}" in client.accessed
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    given = runner_inputs[-1]["additional_repositories"]
    assert [(g["name"], g["commit"]) for g in given] == [("web", web_shas[0])]
    assert token not in log_stream.getvalue()


def test_a_clone_that_fails_ends_the_attempt_retryably_naming_the_entry_and_no_url(
    db, worker_factory, monkeypatch, tmp_path, log_stream
):
    """MUTATION: put git's message in the refusal and the URL is in the error."""
    _seed_task(db, [entry("web", "b" * 40)])

    def clone(**kwargs: Any) -> CloneResult:
        raise GitError(f"fatal: repository '{kwargs['url']}' not found")

    monkeypatch.setattr(extrarepos, "clone_at_commit", clone)
    worker, _, _ = worker_factory(cache_root=tmp_path / "cache")
    started = _agent_starts(worker, monkeypatch)

    assert worker.run() == lifecycle.ExitCode.FAILED

    assert started == []
    task = db.doc("tasks/task_1")
    # Attempt 1 of 3: back to READY, the ordinary retry policy.
    assert task["state"] == TaskState.READY.value
    assert task["last_error"].startswith("clone_failed:"), task["last_error"]
    assert "'web'" in task["last_error"]
    assert "github.com" not in task["last_error"]
    assert [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value]


def test_a_clone_failure_leaves_no_partial_set(worker_factory, monkeypatch, tmp_path,
                                               local_urls):
    web, web_shas = make_bare(tmp_path, "web")
    real = gitops.clone_at_commit

    def clone(**kwargs: Any) -> CloneResult:
        if "sdk" in kwargs["url"]:
            raise GitError("fatal: could not read from remote repository")
        return real(**{**kwargs, "url": f"file://{web}"})

    monkeypatch.setattr(extrarepos, "clone_at_commit", clone)
    task = {"task_id": "task_1", "additional_repositories": [
        entry("web", web_shas[-1]), entry("sdk", "c" * 40),
    ]}
    worker, _ = make_worker(worker_factory, tmp_path, task=task)

    with pytest.raises(extrarepos.Refused) as raised:
        worker._clone_additional_keeping_lease(task)

    assert raised.value.cause == extrarepos.CLONE_FAILED and raised.value.retryable
    assert "'sdk'" in raised.value.reason
    assert not (worker.ws.work / "repos").exists()
    assert not (worker.ws.cache / "repos").exists()


# ---------------------------------------------------------------------------
# The size budget is a platform setting
# ---------------------------------------------------------------------------


def test_a_total_over_the_platform_budget_fails_terminally(
    db, worker_factory, monkeypatch, tmp_path, local_urls
):
    """MUTATION: read a constant instead of `control/settings` and this clones."""
    web, web_shas = make_bare(tmp_path, "web")
    redirect_clones(monkeypatch, {"web": web})
    # An admin set the budget to a few bytes: any real checkout is over it.
    db.seed("control/settings", {extrarepos.BUDGET_SETTING: 1e-8})
    task = {"task_id": "task_1", "additional_repositories": [entry("web", web_shas[-1])]}
    worker, _ = make_worker(worker_factory, tmp_path, task=task)

    with pytest.raises(extrarepos.Refused) as raised:
        worker._clone_additional_keeping_lease(task)

    assert raised.value.cause == extrarepos.TOO_LARGE
    assert raised.value.retryable is False
    assert not (worker.ws.work / "repos").exists()


def test_the_budget_is_read_from_the_settings_and_defaults_to_six_gib(db, log_stream):
    from agent_worker.logs import build_logger

    log = build_logger(task_id="t", attempt_id="a", tenant_id=TENANT, generation=1,
                       runner_profile="mock", stream=log_stream)
    assert extrarepos.budget_bytes(db, logger=log) == 6 * 1024 ** 3
    db.seed("control/settings", {extrarepos.BUDGET_SETTING: 12})
    assert extrarepos.budget_bytes(db, logger=log) == 12 * 1024 ** 3
    db.seed("control/settings", {extrarepos.BUDGET_SETTING: True})
    assert extrarepos.budget_bytes(db, logger=log) == 6 * 1024 ** 3


# ---------------------------------------------------------------------------
# A resume clones the same commits again
# ---------------------------------------------------------------------------


def test_a_resumed_attempt_clones_the_same_commits_into_its_own_cache(
    worker_factory, monkeypatch, tmp_path, local_urls
):
    web, web_shas = make_bare(tmp_path, "web", commits=3)
    seen = redirect_clones(monkeypatch, {"web": web})
    task = {"task_id": "task_1", "additional_repositories": [entry("web", web_shas[1])]}
    first, _ = make_worker(worker_factory, tmp_path, task=task)
    first._clone_additional_keeping_lease(task)
    (first.ws.work / "notes.md").write_text("n\n")
    record = first.checkpoints.create(first.ws)

    second, _ = make_worker(worker_factory, tmp_path, task=task, attempt_id="att_2")
    second.checkpoints.restore(record, second.ws)
    second._restored_from = record
    assert not (second.ws.work / "repos").exists(), "a checkpoint carried the checkouts"

    second._clone_additional_keeping_lease(task)

    assert [call["commit"] for call in seen] == [web_shas[1], web_shas[1]]
    assert head(second.ws.work / "repos" / "web") == web_shas[1]
    assert (second.ws.work / "repos" / "web").resolve().is_relative_to(
        second.ws.cache.resolve()
    )
    assert second.ws.cache != first.ws.cache


# ---------------------------------------------------------------------------
# The forge token: Secret Manager at the clone, gone before the agent
# ---------------------------------------------------------------------------


def test_the_credentials_file_is_gone_before_the_agent_starts(
    worker_factory, monkeypatch, tmp_path, local_urls, log_stream
):
    """Even a clone that left its credentials file behind leaves nothing in
    `private/` once the checkouts are ready, and the token is in no file the
    agent is pointed at, no environment and no log line."""
    web, web_shas = make_bare(tmp_path, "web")
    token = "ghu_" + secrets.token_hex(18)
    real = gitops.clone_at_commit
    handed: list[str | None] = []

    def clone(**kwargs: Any) -> CloneResult:
        handed.append(kwargs["token"])
        private = Path(kwargs["private_dir"])
        private.mkdir(parents=True, exist_ok=True)
        (private / ".git-credentials").write_text(f"https://x:{kwargs['token']}@github.com\n")
        return real(**{**kwargs, "url": f"file://{web}"})

    monkeypatch.setattr(extrarepos, "clone_at_commit", clone)
    task = {"task_id": "task_1", "additional_repositories": [entry("web", web_shas[-1])]}
    worker, _ = make_worker(worker_factory, tmp_path, task=task)
    monkeypatch.setattr(worker, "_additional_git_token", lambda _entry: token)

    checkouts = worker._clone_additional_keeping_lease(task)

    assert handed == [token]
    leftovers = [p for p in worker.ws.private.rglob("*") if p.is_file()]
    assert not [p for p in leftovers if token in p.read_text(errors="replace")], leftovers
    assert not (worker.ws.private / "repos").exists()
    assert token not in (worker.ws.work / ".swarm-context" / "repositories.json").read_text()
    assert token not in json.dumps(extrarepos.child_env(worker.ws, checkouts))
    assert token not in json.dumps([c.as_dict() for c in checkouts])
    assert token not in log_stream.getvalue()


def test_an_unsigned_task_reads_the_tenant_token_not_the_entrys_slot(
    worker_factory, tmp_path
):
    """As `_forge_suffix` for the primary: a field anyone with write on the
    document could set never chooses whose slot is read."""
    task = {"task_id": "task_1"}
    worker, _ = make_worker(worker_factory, tmp_path, task=task)
    named = entry("web", "d" * 40, forge_credential=user_slot())

    assert worker._additional_suffix(named) == "git"
    worker._forge_signed = True
    assert worker._additional_suffix(named) == user_slot()


# ---------------------------------------------------------------------------
# No cache volume of its own: refused rather than filling the workspace
# ---------------------------------------------------------------------------


def test_an_attempt_without_a_cache_volume_refuses_them(worker_factory, monkeypatch, tmp_path):
    cloned: list[bool] = []
    monkeypatch.setattr(extrarepos, "clone_at_commit", lambda **_: cloned.append(True))
    task = {"task_id": "task_1", "additional_repositories": [entry("web", "e" * 40)]}
    worker, _ = make_worker(worker_factory, tmp_path, task=task, cache=False)

    with pytest.raises(extrarepos.Refused) as raised:
        worker._clone_additional_keeping_lease(task)

    assert raised.value.cause == extrarepos.NO_CACHE_VOLUME
    assert raised.value.retryable is False
    assert cloned == []
