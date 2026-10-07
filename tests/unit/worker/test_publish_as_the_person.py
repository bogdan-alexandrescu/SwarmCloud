"""Every commit the worker pushes names the person who dispatched the work (#765).

Owner decision, 2026-10-07: publish FULLY as the person. #764 records
`metadata.dispatch.git_identity` at submission and
`agent_worker.gitidentity.commit_identity` reads it; the agent's own commits
have carried that person since. But the worker replaces them -- the replay,
the fold, its auto-commit of uncommitted work, the integrator's merges -- and
every one of those was made as `WorkerConfig.git_author_*`, so on GitHub
every SwarmCloud commit still read "swarmcloud agent".

What is pinned, against a real bare remote:

* the replayed commit, the folded commit, the auto-commit and the
  integrator's merge commit each carry the person as author AND committer;
* a task with no person (a service account's, or one with no submitter)
  publishes as the bot, `WorkerConfig.git_author_*`, as before;
* #219's guard still refuses a commit by anyone else: the person joins the
  worker's own identities, nobody else does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.gitops import GitError, verify_worker_authorship
from agent_worker.logs import build_logger

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _commit_as_claude,
    _the_agent_titles_its_pull_request,
    agent_commits_with_claude_attribution,
    agent_edits_without_committing,
    contributor_edit,
    forge,
    local_urls,
    origin,
    pushed_commits,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PERSON = ("Ada Lovelace", "ada@example.com")
#: A Google service account, as `swarm_common.identity` reads one.
SERVICE_ACCOUNT = "swarm-dispatch@saga-example.iam.gserviceaccount.com"


def _person_task(task_id: str, dispatch: dict, *, submitted_by: str = PERSON[1],
                 record: tuple[str, str] | None = PERSON) -> dict:
    dispatch = dict(dispatch)
    if record is not None:
        dispatch["git_identity"] = {"name": record[0], "email": record[1]}
    return {"task_id": task_id, "submitted_by": submitted_by, "metadata": {"dispatch": dispatch}}


def _run(worker_factory, monkeypatch, origin: Path, *, task: dict, edit):
    """`run_attempt`, for a task document that names its submitter."""
    task_id = task["task_id"]
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME)
    return worker, config, worker._harvest_git(publish=True)


def _assert_all_by(commits: list[dict], who: tuple[str, str]) -> None:
    assert commits, "the branch adds no commits, so there was nothing to check"
    for commit in commits:
        assert commit["author"] == who, f"authored by {commit['author']}, not {who}"
        assert commit["committer"] == who, f"committed by {commit['committer']}, not {who}"


# ---------------------------------------------------------------------------
# direct-pr: every commit the worker writes is the person's
# ---------------------------------------------------------------------------


def test_the_auto_commit_of_uncommitted_work_is_the_persons(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, config, out = _run(
        worker_factory, monkeypatch, origin,
        task=_person_task("t-person-auto", {"strategy": "direct-pr"}),
        edit=agent_edits_without_committing,
    )
    assert out["published"] is True, out.get("publish_reason")
    assert out["auto_committed"] is True
    _assert_all_by(pushed_commits(origin, f"{config.git_branch_prefix}t-person-auto"), PERSON)


def test_the_replayed_commits_and_the_auto_commit_are_the_persons(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The agent committed as Claude, then left more work: the replay rewrites
    its commit and the auto-commit follows, both as the person."""
    _, config, out = _run(
        worker_factory, monkeypatch, origin,
        task=_person_task("t-person-replay", {"strategy": "direct-pr"}),
        edit=agent_commits_with_claude_attribution,
    )
    assert out["published"] is True, out.get("publish_reason")
    commits = pushed_commits(origin, f"{config.git_branch_prefix}t-person-replay")
    assert len(commits) == 2, commits
    _assert_all_by(commits, PERSON)


def test_the_folded_commit_is_the_persons(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Past `MAX_KEPT_COMMITS` the history is folded into one worker commit."""
    monkeypatch.setattr(lifecycle, "MAX_KEPT_COMMITS", 0)

    def edit(repo: Path) -> None:
        for name in ("one.txt", "two.txt"):
            (repo / name).write_text(f"{name}\n")
            _commit_as_claude(repo, name)

    _, config, out = _run(
        worker_factory, monkeypatch, origin,
        task=_person_task("t-person-fold", {"strategy": "direct-pr"}),
        edit=edit,
    )
    assert out["published"] is True, out.get("publish_reason")
    assert out.get("agent_commits_folded") == 2, out
    commits = pushed_commits(origin, f"{config.git_branch_prefix}t-person-fold")
    assert len(commits) == 1, commits
    _assert_all_by(commits, PERSON)


def test_a_bare_submitter_with_no_record_is_named_by_their_address(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """`commit_identity`'s own fallback for a person: the local part names them."""
    _, config, out = _run(
        worker_factory, monkeypatch, origin,
        task=_person_task("t-person-bare", {"strategy": "direct-pr"}, record=None),
        edit=agent_edits_without_committing,
    )
    assert out["published"] is True, out.get("publish_reason")
    _assert_all_by(
        pushed_commits(origin, f"{config.git_branch_prefix}t-person-bare"),
        ("ada", "ada@example.com"),
    )


# ---------------------------------------------------------------------------
# no person: the bot, as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_of",
    [
        lambda tid: _person_task(tid, {"strategy": "direct-pr"},
                                 submitted_by=SERVICE_ACCOUNT, record=None),
        lambda tid: {"task_id": tid, "metadata": {"dispatch": {"strategy": "direct-pr"}}},
    ],
    ids=["service-account", "no-submitter"],
)
def test_a_task_with_no_person_publishes_as_the_bot(
    worker_factory, monkeypatch, origin, local_urls, forge, task_of
):
    _, config, out = _run(
        worker_factory, monkeypatch, origin,
        task=task_of("t-bot"), edit=agent_commits_with_claude_attribution,
    )
    assert out["published"] is True, out.get("publish_reason")
    _assert_all_by(
        pushed_commits(origin, f"{config.git_branch_prefix}t-bot"),
        (config.git_author_name, config.git_author_email),
    )


# ---------------------------------------------------------------------------
# integrate: the merge commits are the person's, and each contributor's
# branch keeps its own person
# ---------------------------------------------------------------------------


OTHER = ("Grace Hopper", "grace@example.com")


def test_the_integrators_merge_commits_are_the_persons(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Each contributor publishes as ITS person; the integrator's merges and
    its own commit are the integrator's person. The integrator's authorship
    check reads the first-parent span -- its own commits -- so a contributor's
    person on that contributor's branch is accepted, not refused."""
    contributor = {"strategy": "integrate", "carrier": "branches", "role": "contributor"}
    for name, who in (("t-pa", PERSON), ("t-pb", OTHER)):
        _, config, out = _run(
            worker_factory, monkeypatch, origin,
            task=_person_task(name, contributor, submitted_by=who[1], record=who),
            edit=contributor_edit(name),
        )
        assert out["published"] is True, out.get("publish_reason")
        _assert_all_by(pushed_commits(origin, f"{config.git_branch_prefix}{name}"), who)

    _, config, out = _run(
        worker_factory, monkeypatch, origin,
        task=_person_task("t-pint", {
            "strategy": "integrate", "carrier": "branches",
            "role": "integrator", "integrates": ["t-pa", "t-pb"],
        }),
        edit=contributor_edit("t-pint"),
    )
    assert out["published"] is True, out.get("publish_reason")
    assert out["integrated"]["merged"] == ["swarm/t-pa", "swarm/t-pb"], out["integrated"]
    branch = f"{config.git_branch_prefix}t-pint"
    assert {"t-pa.txt", "t-pb.txt", "t-pint.txt"} <= tree_at(origin, branch)

    first_parent = subprocess.run(
        ["git", "log", "--first-parent", "--format=%an%x1f%ae%x1f%cn%x1f%ce%x1f%P", f"main..{branch}"],
        cwd=str(origin), check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    merges = [line for line in first_parent if len(line.split("\x1f")[4].split()) == 2]
    assert len(merges) == 2, first_parent
    for line in first_parent:
        an, ae, cn, ce, _ = line.split("\x1f")
        assert (an, ae) == PERSON and (cn, ce) == PERSON, line


# ---------------------------------------------------------------------------
# #219's guard: the person joins the worker's identities, nobody else does
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, who: tuple[str, str] | None = None) -> str:
    env_args = []
    if who is not None:
        env_args = ["-c", f"user.name={who[0]}", "-c", f"user.email={who[1]}"]
    return subprocess.run(
        ["git", *env_args, *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo_with_base(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "publish"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", ".")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base", who=("t", "t@example.invalid"))
    return repo, _git(repo, "rev-parse", "HEAD")


def _logger():
    import io

    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="u-test", generation=1,
        runner_profile="mock", stream=io.StringIO(),
    )


def _verify(repo: Path, base: str, tmp_path: Path, *, author: tuple[str, str],
            also_own: tuple[tuple[str, str], ...] = ()) -> int:
    return verify_worker_authorship(
        repo=repo, base=base, author_name=author[0], author_email=author[1],
        also_own=also_own,
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs",
        timeout_seconds=30, logger=_logger(),
    )


def _commit(repo: Path, name: str, who: tuple[str, str]) -> None:
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", f"add {name}", who=who)


def test_the_person_and_the_bot_are_both_the_workers_own(repo_with_base, tmp_path):
    """A branch an earlier attempt pushed as the bot, continued as the person."""
    repo, base = repo_with_base
    bot = ("swarmcloud agent", "swarmcloud-agent@users.noreply.github.com")
    _commit(repo, "a.txt", bot)
    _commit(repo, "b.txt", PERSON)
    assert _verify(repo, base, tmp_path, author=PERSON, also_own=(bot,)) == 2


def test_a_third_identitys_commit_is_still_refused(repo_with_base, tmp_path):
    repo, base = repo_with_base
    bot = ("swarmcloud agent", "swarmcloud-agent@users.noreply.github.com")
    _commit(repo, "a.txt", PERSON)
    _commit(repo, "b.txt", ("Mallory", "mallory@example.com"))
    with pytest.raises(GitError, match="authored by Mallory <mallory@example.com>"):
        _verify(repo, base, tmp_path, author=PERSON, also_own=(bot,))


def test_a_commit_mixing_two_own_identities_is_refused(repo_with_base, tmp_path):
    """Author one identity, committer another: no worker commit is written so."""
    repo, base = repo_with_base
    bot = ("swarmcloud agent", "swarmcloud-agent@users.noreply.github.com")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", f"--author={PERSON[0]} <{PERSON[1]}>", "-m", "mixed", who=bot)
    with pytest.raises(GitError, match="refusing to push"):
        _verify(repo, base, tmp_path, author=PERSON, also_own=(bot,))


def test_without_also_own_the_person_alone_is_the_worker(repo_with_base, tmp_path):
    """The bot is the worker's only when the caller says so."""
    repo, base = repo_with_base
    bot = ("swarmcloud agent", "swarmcloud-agent@users.noreply.github.com")
    _commit(repo, "a.txt", bot)
    with pytest.raises(GitError, match="authored by swarmcloud agent"):
        _verify(repo, base, tmp_path, author=PERSON)


def test_the_worker_names_the_person_through_commit_identity(worker_factory, monkeypatch):
    """One reader of the record (#764): the publish identity is
    `gitidentity.commit_identity`'s answer, and the bot only when it names nobody."""
    calls: list[dict] = []
    real = lifecycle.commit_identity

    def spy(task):  # type: ignore[no-untyped-def]
        calls.append(task)
        return real(task)

    monkeypatch.setattr(lifecycle, "commit_identity", spy)
    worker, config, _ = worker_factory()
    worker._task = _person_task("t-ident", {"strategy": "direct-pr"})
    assert worker._publish_identity() == PERSON
    assert calls == [worker._task]
    worker._task = {"task_id": "t-ident", "submitted_by": SERVICE_ACCOUNT}
    assert worker._publish_identity() == (config.git_author_name, config.git_author_email)
