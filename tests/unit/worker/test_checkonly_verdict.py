"""A check-only `publish_to_pr` step that changes nothing ends with a verdict (#2241).

On 2026-10-24 PR 2235's render lane (wf_8f732b1d5f8142969494) rendered every
frame, found no defect, rightly changed nothing, and failed its implement step
with `expected outputs missing ... empty_diff`; its review was cancelled and
the evidence never became a verdict. Now a step that publishes onto an
existing pull request and changes nothing ends SUCCEEDED when its agent wrote
`verification.md` or a readable `verdict.json`, and its evidence goes onto
the pull request as ONE comment, not a commit. An ordinary implement step
that changed nothing still fails `empty_diff`, as before.

MUTATIONS: drop the `_post_no_change_evidence` call from `_record_finalised`
-- the comment tests see no comment. Drop `_check_verdict_text` from
`_verified_no_change` -- the verdict test sees FAILED. Drop the marker check
-- the repeat test posts twice. Drop the `publishpr.refusal` read -- the
closed test posts on a closed pull request.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

from agent_worker import forge as forge_mod
from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from agent_worker.forge import PullHead, RepoAccess, RepoRef
from agent_worker.lifecycle import PATCH_NAME
from swarm_common.states import TaskState

from worker_seeds import seed_attempt

NUMBER = 2235
#: The lane task that opened the pull request; built at runtime.
PR_TASK = "task_" + secrets.token_hex(10)
PR_BRANCH = f"swarm/{PR_TASK}"
REPO = "acme/widgets"
HEAD = "c" * 40
ONTO = {"strategy": "direct-pr", "carrier": "checkpoints",
        "publish_to_pr": {"number": NUMBER, "task_id": PR_TASK}}

#: What `_harvest_git` reports for a clone the agent left exactly as it found it.
EMPTY_DIFF = {
    "base": "a" * 40, "head": "a" * 40, "patch": None, "patch_bytes": 0,
    "patch_omitted": False, "commit_count": 0, "dirty_count": 0,
    "published": False, "publish_reason": "the agent changed nothing in the repository",
}

VERIFIED = "Rendered every frame in both themes at 1440 and 390; no defect found.\n"


class PullForge:
    """GitHub, for pull request #NUMBER and its comments."""

    def __init__(self) -> None:
        self.overrides: dict[str, Any] = {}
        self.comments: list[dict[str, Any]] = []

    def probe(self, *, url: str, token: str | None) -> RepoAccess:
        return RepoAccess(ref=RepoRef(host="github.com", owner="acme", name="widgets"),
                          default_branch="main", can_push=True,
                          reason="the token has write permission on this repository")

    def read(self, *, ref, token, number) -> PullHead:
        fields = {
            "number": number, "url": f"https://github.com/{REPO}/pull/{number}",
            "state": "open", "merged": False, "head_ref": PR_BRANCH, "head_sha": HEAD,
            "head_repo": REPO, "base_repo": REPO, "title": "Mock-up", "body": "part of #2183",
        }
        fields.update(self.overrides)
        return PullHead(**fields)

    def list_comments(self, *, ref, token, number) -> list[str]:
        return [c["body"] for c in self.comments if c["number"] == number]

    def create_comment(self, *, ref, token, number, body) -> int:
        self.comments.append({"number": number, "body": body})
        return len(self.comments)


@pytest.fixture
def pull(monkeypatch) -> PullForge:
    fake = PullForge()
    monkeypatch.setattr(lifecycle, "probe_repository", fake.probe)
    monkeypatch.setattr(lifecycle, "read_pull_request", fake.read)
    monkeypatch.setattr(forge_mod, "list_issue_comments", fake.list_comments)
    monkeypatch.setattr(forge_mod, "create_issue_comment", fake.create_comment)
    return fake


def _seed(db: Any, *, dispatch: dict[str, Any], artifact_name: str, artifact_text: str,
          expected: list[str] | None = None) -> None:
    seed_attempt(
        db,
        task_input={
            "prompt": "render the pull request's pages and fix what is broken",
            "steps": 1, "sleep_seconds": 0.01,
            "artifact_name": artifact_name, "artifact_text": artifact_text,
        },
    )
    metadata: dict[str, Any] = {"dispatch": dict(dispatch)}
    if expected is not None:
        metadata["expected_outputs"] = expected
    db.doc("tasks/task_1")["metadata"] = metadata


def _worker(worker_factory, monkeypatch) -> Any:
    worker, _, _ = worker_factory()
    # The harvest, stood in for: the clone the agent left exactly as cloned.
    worker._harvest_git = lambda **_kwargs: dict(EMPTY_DIFF)  # type: ignore[method-assign]
    worker._repo_url = f"https://github.com/{REPO}"
    monkeypatch.setattr(worker, "_git_token", lambda: "fake-" + secrets.token_hex(8))
    return worker


def test_a_publish_to_pr_step_that_verified_and_changed_nothing_posts_its_evidence(
    db, worker_factory, monkeypatch, pull
):
    _seed(db, dispatch=ONTO, artifact_name="verification.md", artifact_text=VERIFIED,
          expected=[PATCH_NAME, "verification.md"])
    worker = _worker(worker_factory, monkeypatch)

    assert worker.run() == ExitCode.OK

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    summary = task["result_summary"]
    assert summary["no_change"] is True
    assert "expected_outputs_missing" not in summary
    (comment,) = pull.comments
    assert comment["number"] == NUMBER
    assert VERIFIED.strip() in comment["body"]
    assert "changed nothing" in comment["body"]
    assert "swarm:no-change task=task_1" in comment["body"]
    assert summary["no_change_comment"]["posted"] is True
    assert summary["no_change_comment"]["number"] == NUMBER


def test_a_publish_to_pr_step_whose_verdict_found_nothing_to_fix_succeeds_with_it(
    db, worker_factory, monkeypatch, pull
):
    verdict = {"verdict": "MERGE", "findings": ["every frame renders in both themes"]}
    _seed(db, dispatch=ONTO, artifact_name="verdict.json", artifact_text=json.dumps(verdict),
          expected=[PATCH_NAME])
    worker = _worker(worker_factory, monkeypatch)

    assert worker.run() == ExitCode.OK

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    summary = task["result_summary"]
    assert summary["no_change"] is True
    assert "MERGE" in summary["no_change_reason"]
    (comment,) = pull.comments
    assert "Review verdict (verdict.json): MERGE" in comment["body"]
    assert "every frame renders in both themes" in comment["body"]


def test_an_unreadable_verdict_is_no_evidence(db, worker_factory, monkeypatch, pull):
    _seed(db, dispatch=ONTO, artifact_name="verdict.json",
          artifact_text=json.dumps({"verdict": "LOOKS FINE"}), expected=[PATCH_NAME])
    worker = _worker(worker_factory, monkeypatch)

    assert worker.run() == ExitCode.FAILED
    assert pull.comments == []


def test_an_ordinary_implement_step_with_no_diff_still_fails_empty_diff(
    db, worker_factory, monkeypatch, pull
):
    # A verdict.json from a step that publishes onto NO pull request is not
    # evidence: the step was asked to change code and changed nothing.
    _seed(db, dispatch={"strategy": "collect", "carrier": "checkpoints"},
          artifact_name="verdict.json", artifact_text=json.dumps({"verdict": "MERGE"}),
          expected=[PATCH_NAME])
    worker = _worker(worker_factory, monkeypatch)

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] != TaskState.SUCCEEDED.value
    assert "empty_diff" in task["last_error"], task["last_error"]
    assert "no_change" not in task["result_summary"]
    assert pull.comments == []


def test_a_closed_pull_request_gets_no_comment_and_the_step_still_succeeds(
    db, worker_factory, monkeypatch, pull
):
    pull.overrides = {"state": "closed"}
    _seed(db, dispatch=ONTO, artifact_name="verification.md", artifact_text=VERIFIED)
    worker = _worker(worker_factory, monkeypatch)

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert pull.comments == []
    record = task["result_summary"]["no_change_comment"]
    assert record["posted"] is False and "is closed" in record["reason"]


def test_the_same_attempt_finishing_again_posts_nothing_twice(
    db, worker_factory, monkeypatch, pull
):
    _seed(db, dispatch=ONTO, artifact_name="verification.md", artifact_text=VERIFIED)
    worker = _worker(worker_factory, monkeypatch)
    summary = {"no_change": True, "no_change_reason": VERIFIED.strip()}
    worker._task = db.doc("tasks/task_1")

    worker._post_no_change_evidence(summary)
    worker._post_no_change_evidence(summary)

    assert len(pull.comments) == 1
    assert summary["no_change_comment"]["comment"] == "already posted"
