"""A lane with `publish_to_pr` lands on that pull request's branch (#2183).

Owner approval, BGD, 2026-10-24: ten mock-up review lanes had to open
replacement pull requests for pull requests that already existed. A step whose
signed dispatch block carries `publish_to_pr: {number, task_id}` reads that
pull request before anything is cloned, starts from its head, and at publish
fast-forward pushes onto its branch and opens nothing. Refused -- before any
push, and before the agent when it is known then -- are a closed or merged
pull request, one from a fork, one whose head branch is not the
`swarm/<task_id>` swarm-api signed, and a head that moved since the step
started. Never a force.

Real git against a bare repository on disk; only HTTP is faked
(test_strategy_end_to_end.py says why). Assertions are made against the
REMOTE.

MUTATIONS: clone the workflow's ref instead of the pull request's head -- the
clone commit is main's. Push `swarm/<own id>` and open a pull request -- the
fake forge records one and the branch appears. Drop the re-read before the
push -- the moved-head test pushes over nothing and reports success. Push
without the lease -- the lease test's push lands. Skip `publishpr.refusal`
at the start -- the closed/fork/foreign tests clone and run.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, publishpr
from agent_worker import workspace as workspace_mod
from agent_worker.errors import WorkerError
from agent_worker.forge import PullHead, PullRequest

from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_fence_holds,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

@pytest.fixture(autouse=True)
def _titled(monkeypatch):
    monkeypatch.setattr(
        lifecycle.Worker, "_generated_pull_request_title", lambda self: "The lane's title"
    )


NUMBER = 2164
#: The lane task that opened the pull request; built at runtime.
PR_TASK = "task_" + secrets.token_hex(10)
PR_BRANCH = f"swarm/{PR_TASK}"
REPO = "acme/widgets"  # what the fake probe in test_strategy_end_to_end reports


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _push_commit(origin: Path, tmp: Path, branch: str, name: str, *, start: str = "main") -> str:
    """A commit on `branch` of the remote, made by someone other than the worker."""
    work = tmp / f"other-{name}"
    subprocess.run(["git", "clone", "--quiet", "--branch", start, str(origin), str(work)],
                   check=True, capture_output=True)
    (work / f"{name}.txt").write_text(f"{name}\n")
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", name)
    _git(work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
    return _git(work, "rev-parse", "HEAD")


class PullForge:
    """GitHub's pull request #NUMBER, its head read live from the remote."""

    def __init__(self, origin: Path) -> None:
        self.origin = origin
        self.reads = 0
        self.overrides: dict = {}
        self.frozen_head: str | None = None
        self.updates: list[dict] = []

    def read(self, *, ref, token, number) -> PullHead:
        self.reads += 1
        fields = {
            "number": number, "url": f"https://github.com/{REPO}/pull/{number}",
            "state": "open", "merged": False, "head_ref": PR_BRANCH,
            "head_sha": self.frozen_head or refs(self.origin).get(PR_BRANCH, ""),
            "head_repo": REPO, "base_repo": REPO, "title": "Mock-up v1", "body": "part of #2183",
        }
        fields.update(self.overrides)
        return PullHead(**fields)

    def update(self, *, access, token, pull, title, body) -> PullRequest:
        self.updates.append({"title": title, "body": body})
        return PullRequest(number=pull.number, url=pull.url, state=pull.state, created=False,
                           updated=title is not None or body is not None,
                           retitled=title is not None, appended=body is not None)


@pytest.fixture
def pull(monkeypatch, origin, tmp_path) -> PullForge:
    _push_commit(origin, tmp_path, PR_BRANCH, "mockup")
    fake = PullForge(origin)
    monkeypatch.setattr(lifecycle, "read_pull_request", fake.read)
    monkeypatch.setattr(lifecycle, "update_pull_request_text", fake.update)
    return fake


ONTO = {"number": NUMBER, "task_id": PR_TASK}
DIRECT = {"strategy": "direct-pr", "carrier": "checkpoints", "publish_to_pr": ONTO}


def _edit(name: str):
    def edit(repo: Path) -> None:
        (repo / f"{name}.txt").write_text(f"work from {name}\n")
    return edit


def _no_agent_text(monkeypatch, title=None, body=None):
    monkeypatch.setattr(lifecycle.Worker, "_agent_pull_request_text",
                        lambda self: (title, body, []))


# -- the control: a lane with the field lands on the pull request -----------


def test_a_lane_fast_forwards_the_pull_requests_branch_and_opens_no_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge, pull
):
    _no_agent_text(monkeypatch)
    start = refs(origin)[PR_BRANCH]
    worker, config, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-follow",
                                      dispatch=DIRECT, edit=_edit("followup"))
    after = refs(origin)
    assert out["clone_commit"] == start, "the lane did not start from the pull request's head"
    assert out["published"] is True, out
    assert out["branch"] == PR_BRANCH
    new_tip = after[PR_BRANCH]
    assert new_tip != start and out["pushed_head"] == new_tip
    _git(origin, "merge-base", "--is-ancestor", start, new_tip)  # a fast-forward
    assert {"mockup.txt", "followup.txt"} <= tree_at(origin, new_tip)
    assert forge.pulls == [], "a lane with publish_to_pr opened a pull request"
    assert f"swarm/{config.task_id}" not in after, "the lane pushed a branch of its own"
    assert out["pull_request"]["number"] == NUMBER and out["pull_request"]["created"] is False
    assert out["publish_to_pr"] == {"number": NUMBER, "branch": PR_BRANCH, "started_head": start}
    # Wrote neither file: the pull request's title and body are left alone.
    assert pull.updates == [{"title": None, "body": None}]
    assert out["pull_request"]["retitled"] is False


def test_the_lanes_title_and_body_replace_the_pull_requests_only_when_written(
    worker_factory, monkeypatch, origin, local_urls, forge, pull
):
    _no_agent_text(monkeypatch, title="Mock-up v2 with the owner's answers",
                   body="Applies the answers. part of #2183")
    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-follow",
                            dispatch=DIRECT, edit=_edit("followup"))
    assert out["published"] is True, out
    assert pull.updates == [{"title": "Mock-up v2 with the owner's answers",
                             "body": "Applies the answers. part of #2183"}]
    assert out["pull_request"]["retitled"] is True
    assert forge.pulls == []


def test_without_the_field_publishing_is_unchanged(
    worker_factory, monkeypatch, origin, local_urls, forge, pull
):
    start = refs(origin)[PR_BRANCH]
    dispatch = {"strategy": "direct-pr", "carrier": "checkpoints"}
    _, config, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-plain",
                                 dispatch=dispatch, edit=_edit("plain"))
    assert out["published"] is True, out
    assert out["clone_commit"] == refs(origin)["main"]
    assert refs(origin)[PR_BRANCH] == start, "a lane without the field moved the pull request"
    assert f"swarm/{config.task_id}" in refs(origin)
    assert len(forge.pulls) == 1 and forge.pulls[0]["head"] == f"swarm/{config.task_id}"
    assert pull.reads == 0 and pull.updates == []
    assert "publish_to_pr" not in out


# -- refused before the agent: nothing cloned, nothing pushed -----------------


@pytest.mark.parametrize(
    ("overrides", "words"),
    [
        ({"state": "closed"}, "is closed"),
        ({"state": "closed", "merged": True}, "already merged"),
        ({"head_repo": "someone/widgets"}, "comes from a fork"),
        ({"head_ref": "swarm/task_" + "0" * 20}, "not 'swarm/"),
        ({"head_ref": "feature/by-hand"}, "not 'swarm/"),
    ],
    ids=["closed", "merged", "fork", "another-tasks-branch", "not-a-swarm-branch"],
)
def test_a_pull_request_the_lane_may_not_publish_onto_is_refused_before_the_clone(
    worker_factory, origin, local_urls, forge, pull, monkeypatch, overrides, words
):
    pull.overrides = overrides
    before = refs(origin)
    worker, config, _ = worker_factory(task_id="t-follow", repository_url=f"file://{origin}")
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-follow", "metadata": {"dispatch": DIRECT}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    with pytest.raises(WorkerError, match="publish_to_pr refused before the agent started") as err:
        worker._maybe_clone(task)
    assert words in str(err.value)
    assert not (worker.ws.work / lifecycle.REPO_DIR_NAME / ".git").exists()
    assert refs(origin) == before
    assert forge.pulls == []


def test_a_task_with_no_forge_token_is_refused_before_the_clone(
    worker_factory, origin, local_urls, forge, pull, monkeypatch
):
    worker, config, _ = worker_factory(task_id="t-follow", repository_url=f"file://{origin}")
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-follow", "metadata": {"dispatch": DIRECT}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: None)
    with pytest.raises(WorkerError, match="has none"):
        worker._maybe_clone(task)
    assert not (worker.ws.work / lifecycle.REPO_DIR_NAME / ".git").exists()


@pytest.mark.parametrize("bad", [0, -3, "2164", True, None])
def test_a_number_swarm_api_could_not_have_signed_is_refused(bad):
    with pytest.raises(WorkerError, match="number"):
        publishpr.target({"dispatch": {"publish_to_pr": {"number": bad, "task_id": PR_TASK}}})


@pytest.mark.parametrize("bad", ["../main", "task_short", "", None])
def test_a_task_id_swarm_api_could_not_have_signed_is_refused(bad):
    with pytest.raises(WorkerError, match="task id"):
        publishpr.target({"dispatch": {"publish_to_pr": {"number": NUMBER, "task_id": bad}}})


def test_the_field_is_absent_from_a_block_without_it():
    assert publishpr.target({"dispatch": {"strategy": "direct-pr"}}) is None
    assert publishpr.target(None) is None


# -- refused at the push: nothing pushed, nothing forced -------------------


def test_a_head_that_moved_while_the_lane_ran_is_refused_and_never_forced(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    _no_agent_text(monkeypatch)

    def edit_then_someone_pushes(repo: Path) -> None:
        _edit("followup")(repo)
        _push_commit(origin, tmp_path, PR_BRANCH, "by-hand", start=PR_BRANCH)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-follow",
                            dispatch=DIRECT, edit=edit_then_someone_pushes)
    moved = refs(origin)[PR_BRANCH]
    assert out["published"] is False, out
    assert "head moved" in out["publish_reason"]
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert "followup.txt" not in tree_at(origin, moved), "the lane's push landed"
    assert "by-hand.txt" in tree_at(origin, moved), "someone's push was replaced"
    assert forge.pulls == [] and pull.updates == []


def test_a_head_moved_between_the_read_and_the_push_is_refused_by_the_lease(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    """The forge's answer is stale (it says the start head), but the branch
    moved: the leased push is refused, and the lane reports a moved head."""
    _no_agent_text(monkeypatch)
    pull.frozen_head = refs(origin)[PR_BRANCH]

    def edit_then_someone_pushes(repo: Path) -> None:
        _edit("followup")(repo)
        _push_commit(origin, tmp_path, PR_BRANCH, "by-hand", start=PR_BRANCH)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-follow",
                            dispatch=DIRECT, edit=edit_then_someone_pushes)
    moved = refs(origin)[PR_BRANCH]
    assert out["published"] is False, out
    assert "head moved" in out["publish_reason"]
    assert "followup.txt" not in tree_at(origin, moved)
    assert forge.pulls == []


@pytest.mark.parametrize(
    ("overrides", "words"),
    [
        ({"state": "closed"}, "is closed"),
        ({"state": "closed", "merged": True}, "already merged"),
        ({"head_repo": "someone/widgets"}, "comes from a fork"),
    ],
    ids=["closed", "merged", "fork"],
)
def test_a_pull_request_closed_while_the_lane_ran_is_refused_before_the_push(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, overrides, words
):
    _no_agent_text(monkeypatch)
    start = refs(origin)[PR_BRANCH]

    def edit_then_close(repo: Path) -> None:
        _edit("followup")(repo)
        pull.overrides = overrides

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-follow",
                            dispatch=DIRECT, edit=edit_then_close)
    assert out["published"] is False, out
    assert words in out["publish_reason"]
    assert refs(origin)[PR_BRANCH] == start
    assert forge.pulls == [] and pull.updates == []


# -- builds_on: the fix step publishes to the same pull request ------------


def test_an_integrate_lane_whose_fix_builds_on_the_implementer_lands_on_the_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge, pull
):
    _no_agent_text(monkeypatch)
    start = refs(origin)[PR_BRANCH]
    _, _, impl = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-impl",
        dispatch={"strategy": "integrate", "carrier": "checkpoints", "role": "contributor",
                  "publish_to_pr": ONTO},
        edit=_edit("impl"),
    )
    assert impl["clone_commit"] == start, "the implementer did not start from the PR's head"
    assert impl["branch"] == "swarm/t-impl" and refs(origin)[PR_BRANCH] == start
    monkeypatch.setattr(lifecycle.Worker, "_builds_on_base", lambda self: "t-impl")
    _, _, fix = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-fix",
        dispatch={"strategy": "integrate", "carrier": "checkpoints", "role": "integrator",
                  "integrates": ["t-impl"], "builds_on": "t-impl", "publish_to_pr": ONTO},
        edit=_edit("fix"),
    )
    assert fix["published"] is True, fix
    assert fix["clone_commit"] == refs(origin)["swarm/t-impl"]
    new_tip = refs(origin)[PR_BRANCH]
    _git(origin, "merge-base", "--is-ancestor", start, new_tip)
    assert {"mockup.txt", "impl.txt", "fix.txt"} <= tree_at(origin, new_tip)
    assert "swarm/t-fix" not in refs(origin)
    assert forge.pulls == []


# -- a resumed attempt keeps the head its first attempt started from -------


def test_a_resumed_attempt_keeps_the_start_head_from_the_manifest(
    worker_factory, origin, local_urls, forge, pull
):
    from agent_worker.checkpoint import CheckpointRecord

    started = "a" * 40
    record = CheckpointRecord(
        checkpoint_id="ckpt-000001", seq=1, task_id="t-follow", attempt_id="att-1",
        tenant_id="eng", generation=1, created_at="2026-10-24T00:00:00Z",
        archive_key="k", manifest_key="m", archive_bytes=1, archive_sha256="0" * 64,
        file_count=1, uri="gs://b/k/", clone_base="b" * 40, pr_start_head=started,
    )
    assert CheckpointRecord.from_dict(
        {**record.__dict__, "unknown_later_field": 1}
    ).pr_start_head == started
    worker, config, _ = worker_factory(task_id="t-follow", repository_url=f"file://{origin}")
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-follow", "metadata": {"dispatch": DIRECT}}
    worker._task = task
    worker._restored_from = record
    restored = worker.ws.work / lifecycle.REPO_DIR_NAME
    restored.mkdir(parents=True, exist_ok=True)
    (restored / "README.md").write_text("restored\n")
    worker._maybe_clone(task)
    assert worker._pr_start_head == started
    assert worker.checkpoints.pr_start_head == started
    assert pull.reads == 0, "a resume read the pull request's head again as its start"


# -- the forge read, at its HTTP boundary ----------------------------------


def test_read_pull_request_reads_the_head_and_the_repositories(monkeypatch):
    from agent_worker import forge as forge_mod

    asked: list[str] = []
    answer = {
        "number": NUMBER, "html_url": "u", "state": "closed", "merged": False,
        "merged_at": "2026-10-24T00:00:00Z", "title": "t", "body": None,
        "head": {"ref": PR_BRANCH, "sha": "c" * 40, "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }
    monkeypatch.setattr(forge_mod, "_request",
                        lambda url, **kw: (asked.append(url), (200, answer))[1])
    ref = forge_mod.RepoRef(host="github.com", owner="acme", name="widgets")
    head = forge_mod.read_pull_request(ref=ref, token="x" * 8, number=NUMBER)
    assert asked == [f"https://api.github.com/repos/acme/widgets/pulls/{NUMBER}"]
    assert head.merged is True, "merged_at alone is a merged pull request"
    assert (head.head_ref, head.head_repo, head.base_repo, head.body) == (
        PR_BRANCH, REPO, REPO, "")


@pytest.mark.parametrize("status", [404, 403])
def test_read_pull_request_that_is_not_answered_is_an_error(monkeypatch, status):
    from agent_worker import forge as forge_mod

    monkeypatch.setattr(forge_mod, "_request", lambda url, **kw: (status, {"message": "no"}))
    ref = forge_mod.RepoRef(host="github.com", owner="acme", name="widgets")
    with pytest.raises(forge_mod.ForgeError):
        forge_mod.read_pull_request(ref=ref, token="x" * 8, number=NUMBER)


def test_the_token_goes_only_to_github(monkeypatch):
    from agent_worker import forge as forge_mod

    monkeypatch.setattr(forge_mod, "_request", lambda url, **kw: pytest.fail("sent"))
    ref = forge_mod.RepoRef(host="git.example.invalid", owner="acme", name="widgets")
    with pytest.raises(forge_mod.ForgeError, match="only to github.com"):
        forge_mod.read_pull_request(ref=ref, token="x" * 8, number=NUMBER)


# -- the pull request's branch is the lane's own (F04, part of #2379) ---------


def _checkout_and_commit(branch: str, name: str, *, fetch: str | None = None):
    """An agent that fetches `fetch` (default `branch`), runs `git checkout -B
    <branch>` there, and commits -- as the F04 lane did on its PR's branch."""
    def edit(repo: Path) -> None:
        _git(repo, "fetch", "--quiet", "origin", fetch or branch)
        _git(repo, "checkout", "--quiet", "-B", branch, f"origin/{fetch or branch}")
        (repo / f"{name}.txt").write_text(f"work from {name}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", name)
    return edit


def test_a_lane_that_checks_out_the_pull_requests_branch_publishes_onto_it(
    worker_factory, monkeypatch, origin, local_urls, forge, pull
):
    """MEASURED 2026-10-26 (F04 of #2379): a `publish_to_pr` lane ran `git
    checkout -B <the PR's branch>`, committed, and was refused `head_moved`
    -- the guard exempted only `swarm/<own task>`, while the publish pushes
    the pull request's branch. MUTATION: exempt only `swarm/<own task>` in
    `_fetched_line` -- this ends head_moved == PR_BRANCH, published False."""
    _no_agent_text(monkeypatch)
    start = refs(origin)[PR_BRANCH]
    _, config, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-own-pr",
                                 dispatch=DIRECT, edit=_checkout_and_commit(PR_BRANCH, "onpr"))
    assert "head_moved" not in out, out
    assert out["published"] is True, out
    assert out["branch"] == PR_BRANCH
    new_tip = refs(origin)[PR_BRANCH]
    assert new_tip != start
    _git(origin, "merge-base", "--is-ancestor", start, new_tip)
    assert {"mockup.txt", "onpr.txt"} <= tree_at(origin, new_tip)
    assert forge.pulls == [] and f"swarm/{config.task_id}" not in refs(origin)


def test_a_publish_to_pr_lane_on_another_fetched_branch_still_moved(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    """The control: only the pull request's branch is the lane's own."""
    _no_agent_text(monkeypatch)
    _push_commit(origin, tmp_path, "feature/elsewhere", "elsewhere")
    start = refs(origin)[PR_BRANCH]
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-elsewhere", dispatch=DIRECT,
        edit=_checkout_and_commit("feature/elsewhere", "stray"),
    )
    assert out["head_moved"] == "feature/elsewhere", out
    assert out["published"] is False
    assert refs(origin)[PR_BRANCH] == start and pull.updates == []


def test_a_lane_without_publish_to_pr_on_that_branch_still_moved(
    worker_factory, monkeypatch, origin, local_urls, forge, pull
):
    """The control: without the field the pull request's branch is someone
    else's, and checking it out is leaving this step's line."""
    start = refs(origin)[PR_BRANCH]
    dispatch = {"strategy": "direct-pr", "carrier": "checkpoints"}
    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-not-onto",
                            dispatch=dispatch, edit=_checkout_and_commit(PR_BRANCH, "stray"))
    assert out["head_moved"] == PR_BRANCH, out
    assert out["published"] is False
    assert refs(origin)[PR_BRANCH] == start and forge.pulls == []
