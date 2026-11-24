"""The `publish_to_pr` read before the push re-reads a refused credential once,
and a publish that fails there is recorded as a failed publish (issue 284).

MEASURED (2026-11-23, two re-review integrators): `_check_pull_before_push`
read the pull request again with the token the publish read at its probe,
the forge answered 401, and that became the final GitError "could not read
pull request #N before the push ... (401); nothing was pushed" -- with no
re-read, unlike the push (`_push_reading_again`, archived issue 3223) and
the pull-request open (`_open_pull_request_reading_again`). The finish then
asked `_published_nothing` before `_publish_failed`; the integrator's
`commit_count` is counted from its clone base, which was already the
contributor's head, so the 401 was recorded as "published_nothing ... no
commits beyond <base>" with an `allow_empty_diff` remedy.

What is pinned:

  * a 401 on the pre-push read, then a good read: the slot is read again
    ONCE, the read and the push both use the new token, and the pull
    request's branch moves;
  * two 401s: one re-read, no third read, nothing pushed, and the publish
    fails with `FORGE_REFUSED_TWICE` in its reason;
  * that failure is a failed publish naming the 401, never
    `published_nothing` -- also in the measured shape (`commit_count` 0);
  * no token value in any log line, the publish's reason or the summary.

MUTATIONS: drop the re-read -- the refused-then-accepted test sees
`published: False`. Ask `_published_nothing` without looking at
`publish_failed` -- the finish tests see "published_nothing".

Every fake token is built at runtime, never written as one literal.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker import forge as forge_mod, lifecycle
from agent_worker import secrets as secrets_mod
from agent_worker import workspace as workspace_mod

from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_fence_holds,
    forge,
    local_urls,
    origin,
    refs,
)
from test_publish_to_pr import (  # noqa: F401 -- fixtures and helpers
    NUMBER,
    ONTO,
    PR_BRANCH,
    REPO,
    _edit,
    _no_agent_text,
    _titled,
    pull,
)
from test_publish_push_reread import _Reads

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

INTEGRATOR = {
    "strategy": "integrate", "carrier": "checkpoints", "role": "integrator",
    "publish_to_pr": ONTO,
}


class _RefusingReads:
    """`read_pull_request`: the start read answers; the next `refusals` reads
    answer 401, as GitHub does for a token a refresh has ended."""

    def __init__(self, pull, refusals: int) -> None:
        self.pull = pull
        self.refusals = refusals
        self.tokens: list[str] = []

    def __call__(self, *, ref, token, number):
        self.tokens.append(token)
        if len(self.tokens) > 1 and self.refusals:
            self.refusals -= 1
            raise forge_mod.ForgeCredentialRejected(
                f"could not read pull request {REPO}#{number} (401)"
            )
        return self.pull.read(ref=ref, token=token, number=number)


class _Pushes:
    def __init__(self, real) -> None:
        self.real = real
        self.tokens: list[str | None] = []

    def __call__(self, **kwargs: Any) -> str | None:
        self.tokens.append(kwargs.get("token"))
        return self.real(**kwargs)


def _attempt(worker_factory, monkeypatch, origin: Path, pull, *, refusals: int):
    """`run_attempt` with a new token at every read of the slot."""
    _no_agent_text(monkeypatch)
    worker, config, _ = worker_factory(
        task_id="t-integ", attempt_id="att-t-integ", lease_id="lease-t-integ",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-integ", "metadata": {"dispatch": dict(INTEGRATOR)}}
    worker._task = task
    reads = _Reads()
    monkeypatch.setattr(worker, "_git_token", reads)
    monkeypatch.setattr(worker, "_forge_write_refusal", lambda: None)
    pr_reads = _RefusingReads(pull, refusals)
    monkeypatch.setattr(lifecycle, "read_pull_request", pr_reads)
    pushes = _Pushes(lifecycle.push_branch)
    monkeypatch.setattr(lifecycle, "push_branch", pushes)

    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    _edit("fix")(worker.ws.work / lifecycle.REPO_DIR_NAME)
    return worker, reads, pr_reads, pushes, worker._harvest_git(publish=True)


def _no_token_in(values: list[str], *texts: str) -> None:
    for value in values:
        for text in texts:
            assert value not in text, "a token value was logged or recorded"


def test_a_401_on_the_pre_push_read_rereads_once_and_pushes(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, log_stream
):
    start = refs(origin)[PR_BRANCH]
    worker, reads, pr_reads, pushes, out = _attempt(
        worker_factory, monkeypatch, origin, pull, refusals=1
    )

    assert out["published"] is True, (out.get("publish_reason"), out.get("commit_count"), out.get("dirty_count"))
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD)
    new_tip = refs(origin)[PR_BRANCH]
    assert new_tip != start and out["pushed_head"] == new_tip
    # The start read, the refused pre-push read, and the read made again.
    assert len(pr_reads.tokens) == 3
    refused, again = pr_reads.tokens[1], pr_reads.tokens[2]
    assert again != refused
    # The slot was read exactly once more after the refusal, and the push
    # went with that read, not the dead one.
    assert reads.values.index(again) == reads.values.index(refused) + 1
    assert reads.values[-1] == again, "the slot was read more than once again"
    assert pushes.tokens == [again]
    _no_token_in(reads.values, log_stream.getvalue(), repr(out))


def test_two_401s_on_the_pre_push_read_fail_the_publish_and_push_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, log_stream
):
    start = refs(origin)[PR_BRANCH]
    worker, reads, pr_reads, pushes, out = _attempt(
        worker_factory, monkeypatch, origin, pull, refusals=2
    )

    assert out["published"] is False, out
    assert out.get(lifecycle.PUBLISH_FAILED_FIELD) is True
    assert secrets_mod.FORGE_REFUSED_TWICE in out["publish_reason"]
    assert "401" in out["publish_reason"]
    assert "nothing was pushed" in out["publish_reason"]
    assert len(pr_reads.tokens) == 3, "exactly one more read"
    assert reads.values.index(pr_reads.tokens[2]) == reads.values.index(pr_reads.tokens[1]) + 1
    assert reads.values[-1] == pr_reads.tokens[2], "exactly one re-read"
    assert pushes.tokens == [], "something was pushed"
    assert refs(origin)[PR_BRANCH] == start
    _no_token_in(reads.values, log_stream.getvalue(), repr(out))


def test_the_twice_refused_read_is_recorded_as_a_failed_publish_not_published_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, log_stream
):
    worker, reads, _pr_reads, _pushes, out = _attempt(
        worker_factory, monkeypatch, origin, pull, refusals=2
    )
    # As measured: the integrator's commit count is taken from its clone
    # base, the contributor's head, so it read 0 although it had work.
    git = {**out, "commit_count": 0, "dirty_count": 0}
    summary: dict[str, Any] = {
        "git": git,
        "artifacts": [{"name": "verdict.json", "uri": "gs://swarm-artifacts-example-project/verdict.json"}],
    }

    assert worker._published_nothing(summary) is None
    failed = worker._publish_failed(summary)
    assert failed is not None
    assert "published_nothing" not in failed
    assert "allow_empty_diff" not in failed
    assert f"#{NUMBER}" in failed and "401" in failed
    assert secrets_mod.FORGE_REFUSED_TWICE in failed
    _no_token_in(reads.values, failed, log_stream.getvalue())


def test_a_failed_publish_with_no_commits_is_not_published_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The finish's question in isolation, the measured summary's shape."""
    worker, _config, _ = worker_factory(repository_url=f"file://{origin}")
    worker._task = {"task_id": "task_1", "metadata": {"dispatch": dict(INTEGRATOR)}}
    reason = (
        f"publish_to_pr: could not read pull request #{NUMBER} before the push: "
        f"could not read pull request {REPO}#{NUMBER} (401); nothing was pushed"
    )
    summary = {
        "git": {
            "published": False, lifecycle.PUBLISH_FAILED_FIELD: True,
            "commit_count": 0, "dirty_count": 0, "base": "a" * 40,
            "publish_reason": reason,
        },
        "artifacts": [{"name": "verification.md", "uri": "gs://swarm-artifacts-example-project/v.md"}],
    }

    assert worker._opens_pull_request()
    assert worker._published_nothing(summary) is None
    failed = worker._publish_failed(summary)
    assert failed is not None and reason in failed


def test_a_step_that_changed_nothing_and_failed_nothing_is_still_published_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control: without a failed publish the old answer stands."""
    worker, _config, _ = worker_factory(repository_url=f"file://{origin}")
    worker._task = {"task_id": "task_1", "metadata": {"dispatch": dict(INTEGRATOR)}}
    summary = {"git": {"published": False, "commit_count": 0, "dirty_count": 0,
                       "base": "b" * 40}}

    nothing = worker._published_nothing(summary)
    assert nothing is not None and nothing.startswith("published_nothing:")
