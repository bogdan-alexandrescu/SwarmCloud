"""A fenced attempt pushes nothing and opens, updates or comments on nothing (#2693).

MEASURED: task_ffc37f64's generation 1 was fenced `dead_worker` at 18:54:19
and pushed onto PR #2613 at 18:54:35; generation 5's publish then found the
pull request's head moved and failed `head_moved`. task_118cd828 opened
ai-studio PR #1170 22 s after its fence. `Worker._publish_git` read the fence
before the carrier push and the moved-tip replay only: the plain push, the
broker push, the replacing push over an earlier attempt, the pull request's
open or text update and the territory comment were all made without it.

The property (invariant 5), against a real bare repository on disk and the
REAL `ControlPlane.ensure_owner` over a seeded task and lease (only HTTP to the
forge is faked, as in test_strategy_end_to_end.py). The fence lands the way the
reconciler lands it -- the task's `current_generation` moves past this
attempt's -- at a chosen read:

  * fenced before the push: `FencedError` out of the publish, the remote
    untouched, `push_branch` never called, no pull request, the lease as it
    was;
  * onto an existing pull request: no push, no title or body update;
  * a workflow-file branch: the broker is never asked;
  * fenced after the push: the branch is there, but no pull request is
    opened or commented on;
  * a retry's replacing push over its own earlier attempt is not made;
  * the deferred publish (`_publish_checked`) lets the fence through instead
    of recording publish_failed;
  * unfenced, everything publishes as before, each forge write after a read.

WRITTEN TO FAIL BEFORE THE CHANGE: on main no forge write read the fence, so
every fenced case pushed (or opened) and returned instead of raising.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker import forge as forge_mod
from agent_worker import lifecycle
from agent_worker.errors import FencedError

from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _attempt,
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    tree_at,
)
from test_publish_to_pr import (  # noqa: F401 -- fixtures
    DIRECT as ONTO_PULL,
    PR_BRANCH,
    _no_agent_text,
    pull,
)
from test_publish_workflow_files import FakeBroker, _with_broker, adds_a_workflow
from worker_seeds import seed_attempt

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

DIRECT = {"strategy": "direct-pr", "carrier": "checkpoints"}
TASK = "task_fenced0001"


@pytest.fixture
def pushes(monkeypatch) -> list[dict[str, Any]]:
    """Every push the worker made with the tenant token (`gitops.push_branch`)."""
    calls: list[dict[str, Any]] = []
    real = lifecycle.push_branch

    def spy(**kwargs: Any) -> str:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "push_branch", spy)
    return calls


class Fence:
    """The real `ensure_owner`, with the reconciler's fence landing on read `at`.

    Every read is recorded by what it was for. From read `at` (1-based) on,
    the task document's `current_generation` is one past this attempt's
    before the real check runs, so it is the real predicate that refuses.
    `at=None` never fences.
    """

    def __init__(self, db, worker, *, at: int | None) -> None:
        self.db = db
        self.worker = worker
        self.at = at
        self.reads: list[str] = []
        self.real = worker.control.ensure_owner

    def __call__(self, *, write: str) -> None:
        self.reads.append(write)
        if self.at is not None and len(self.reads) >= self.at:
            task = self.db.documents[f"tasks/{self.worker.cfg.task_id}"]
            task["current_generation"] = self.worker.cfg.generation + 1
        return self.real(write=write)


def _fenced_attempt(db, worker_factory, monkeypatch, origin: Path, *, dispatch: dict,
                    files: dict[str, str], at: int | None, attempt: int = 1,
                    task_id: str = TASK):
    """One attempt, seeded as dispatched, cloned and edited; the fence on read `at`."""
    worker, config, task = _attempt(
        worker_factory, monkeypatch, origin, task_id=task_id, attempt=attempt, dispatch=dispatch
    )
    seed_attempt(db, task_id=task_id, attempt_id=config.attempt_id, lease_id=config.lease_id,
                 generation=config.generation, attempt_count=attempt)
    assert worker._maybe_clone(task)["commit"], "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    fence = Fence(db, worker, at=at)
    monkeypatch.setattr(worker.control, "ensure_owner", fence)
    lease = copy.deepcopy(db.documents[f"leases/{config.lease_id}"])
    return worker, config, fence, lease


def _lease_untouched(db, config, lease: dict) -> None:
    assert db.documents[f"leases/{config.lease_id}"] == lease, "a fenced attempt wrote its lease"


def test_a_direct_pr_fenced_before_its_push_pushes_and_opens_nothing(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pushes
):
    worker, config, fence, lease = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=DIRECT,
        files={"agent.txt": "work\n"}, at=1,
    )
    before = refs(origin)

    with pytest.raises(FencedError):
        worker._harvest_git(publish=True)

    branch = f"{config.git_branch_prefix}{TASK}"
    assert refs(origin) == before and branch not in refs(origin), "a fenced attempt pushed"
    assert pushes == [], "push_branch was called by a fenced attempt"
    assert forge.pulls == [], "a fenced attempt opened a pull request"
    assert fence.reads == ["branch push"]
    _lease_untouched(db, config, lease)


def test_a_publish_onto_a_pull_request_fenced_before_its_push_changes_nothing(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pull, pushes
):
    _no_agent_text(monkeypatch, title="A title the fenced lane wrote", body="part of #2693")
    start = refs(origin)[PR_BRANCH]
    worker, config, fence, lease = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=ONTO_PULL,
        files={"followup.txt": "work\n"}, at=1, task_id="task_fenced0002",
    )

    with pytest.raises(FencedError):
        worker._harvest_git(publish=True)

    assert refs(origin)[PR_BRANCH] == start, "a fenced attempt pushed onto the pull request"
    assert pushes == []
    assert pull.updates == [], "a fenced attempt updated the pull request's text"
    assert forge.pulls == []
    _lease_untouched(db, config, lease)


def test_a_publish_onto_a_pull_request_fenced_after_its_push_updates_no_text(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pull, pushes
):
    _no_agent_text(monkeypatch, title="A title the fenced lane wrote", body="part of #2693")
    start = refs(origin)[PR_BRANCH]
    worker, config, fence, lease = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=ONTO_PULL,
        files={"followup.txt": "work\n"}, at=2, task_id="task_fenced0003",
    )

    with pytest.raises(FencedError):
        worker._harvest_git(publish=True)

    assert refs(origin)[PR_BRANCH] != start, "the push before the fence was not made"
    assert pull.updates == [], "a fenced attempt updated the pull request's text"
    assert fence.reads == ["branch push", "pull request text"]
    _lease_untouched(db, config, lease)


def test_a_fenced_workflow_branch_never_asks_the_broker(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    _with_broker(monkeypatch, broker)
    worker, config, fence, lease = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=DIRECT, files={}, at=1,
        task_id="task_fenced0004",
    )
    adds_a_workflow(worker.ws.work / lifecycle.REPO_DIR_NAME)
    before = refs(origin)

    with pytest.raises(FencedError):
        worker._harvest_git(publish=True)

    assert broker.calls == [], "a fenced attempt asked swarm-api to push"
    assert pushes == []
    assert refs(origin) == before
    assert forge.pulls == []
    assert fence.reads == ["workflow push"]
    _lease_untouched(db, config, lease)


def test_a_fence_after_the_push_opens_no_pull_request(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pushes
):
    worker, config, fence, lease = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=DIRECT,
        files={"agent.txt": "work\n"}, at=2, task_id="task_fenced0005",
    )

    with pytest.raises(FencedError):
        worker._harvest_git(publish=True)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch in refs(origin) and "agent.txt" in tree_at(origin, branch)
    assert [call["branch"] for call in pushes] == [branch]
    assert forge.pulls == [], "a fenced attempt opened a pull request"
    assert fence.reads == ["branch push", "pull request"]
    _lease_untouched(db, config, lease)


class _AdoptedPullRequest:
    """An open pull request every publish adopts; its comments live here."""

    def __init__(self) -> None:
        self.pulls: list[dict] = []
        self.posts: list[str] = []

    def open_pull_request(self, **kwargs):
        self.pulls.append(kwargs)
        return lifecycle.PullRequest(
            number=7, url="https://github.com/acme/widgets/pull/7", state="open",
            created=False, updated=True, appended=True,
            labelled=tuple(kwargs.get("labels") or ()),
        )

    def list_comments(self, *, ref, token, number):
        return []

    def post_comment(self, *, ref, token, number, body):
        self.posts.append(body)
        return 1000 + len(self.posts)


def test_a_fence_after_the_pull_request_posts_no_territory_comment(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pushes
):
    adopted = _AdoptedPullRequest()
    monkeypatch.setattr(lifecycle, "open_pull_request", adopted.open_pull_request)
    monkeypatch.setattr(forge_mod, "list_issue_comments", adopted.list_comments)
    monkeypatch.setattr(forge_mod, "create_issue_comment", adopted.post_comment)
    dispatch = {"strategy": "direct-pr"}
    worker, config, fence, lease = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=dispatch,
        files={"a.py": "a\n", "b.py": "b\n"}, at=3, task_id="task_fenced0006",
    )
    worker._task["input"] = {"prompt": "fix CI", "territory": ["a.py"]}

    with pytest.raises(FencedError):
        worker._harvest_git(publish=True)

    assert len(adopted.pulls) == 1
    assert adopted.posts == [], "a fenced attempt commented on the pull request"
    assert fence.reads == ["branch push", "pull request", "pull request comment"]
    _lease_untouched(db, config, lease)


def test_a_fenced_retry_does_not_replace_its_earlier_attempts_branch(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pushes
):
    task_id = "task_fenced0007"
    first, config, _, _ = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=DIRECT,
        files={"first.txt": "attempt 1\n"}, at=None, task_id=task_id,
    )
    assert first._harvest_git(publish=True)["published"] is True
    branch = f"{config.git_branch_prefix}{task_id}"
    first_tip = refs(origin)[branch]
    del pushes[:]

    # The second read is the replacing push's: the plain push is refused by
    # attempt 1's commit first, as in test_retry_publishes_onto_its_own_branch.
    second, config, fence, lease = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=DIRECT,
        files={"second.txt": "attempt 2\n"}, at=2, attempt=2, task_id=task_id,
    )

    with pytest.raises(FencedError):
        second._harvest_git(publish=True)

    assert refs(origin)[branch] == first_tip, "a fenced attempt replaced the branch"
    assert [call.get("lease") for call in pushes] == [None], "the replacing push was made"
    assert fence.reads == ["branch push", "replacing push"]
    assert len(forge.pulls) == 1, "a fenced retry opened a pull request"
    _lease_untouched(db, config, lease)


def test_the_deferred_publish_lets_the_fence_through(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pushes
):
    """`_publish_checked` records any other exception as publish_failed; a
    fence is a stale generation, which `run` stands down on."""
    worker, config, fence, lease = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=DIRECT,
        files={"agent.txt": "work\n"}, at=1, task_id="task_fenced0008",
    )
    summary = {"git": worker._harvest_git(publish=True, defer=True)}

    with pytest.raises(FencedError):
        worker._publish_checked(summary, None)

    assert pushes == [] and forge.pulls == []
    assert lifecycle.PUBLISH_FAILED_FIELD not in summary["git"]
    _lease_untouched(db, config, lease)


def test_unfenced_everything_publishes_after_a_read(
    db, worker_factory, monkeypatch, origin, local_urls, forge, pushes
):
    worker, config, fence, _ = _fenced_attempt(
        db, worker_factory, monkeypatch, origin, dispatch=DIRECT,
        files={"agent.txt": "work\n"}, at=None, task_id="task_fenced0009",
    )

    out = worker._harvest_git(publish=True)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert out["pushed_head"] == refs(origin)[branch]
    assert [p["head"] for p in forge.pulls] == [branch]
    assert fence.reads == ["branch push", "pull request"]
