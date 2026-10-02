"""Why an expected output is missing decides whether a retry can help (#165).

Owner decision, 2026-09-28. `_upload_outputs` records WHY each file it did not
upload was skipped (each `artifacts_skipped` entry is `{name, cause}`), and
the missing-output check
reads that cause:

  * the artifact cap, an empty or omitted `swarm-work.patch`, a patch with no
    base to diff against: the next attempt meets the same cap and the same
    repository, so the attempt fails for good, NOT retryably;
  * a file never written, or one whose upload raised: a retry can change
    that, so the attempt is failed retryably as #149 decided.

And nothing is pushed and no pull request is opened until the check has
passed, so an attempt that is then failed or retried has published nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import inputs as inputs_mod
from agent_worker.errors import ExitCode, InputUnavailable
from agent_worker.lifecycle import PATCH_NAME
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_attempt

MISSING_KEY = "expected_outputs_missing"
MISSING_CAUSES_KEY = "expected_outputs_missing_causes"


def _seed(db: Any, expected: list[str], *, artifact_name: str = "notes.md") -> None:
    seed_attempt(
        db,
        task_input={
            "prompt": "write the notes",
            "steps": 1,
            "sleep_seconds": 0.01,
            "artifact_name": artifact_name,
            "artifact_text": "the notes\n",
        },
    )
    db.doc("tasks/task_1")["metadata"] = {"expected_outputs": expected}


def _retrying(db: Any) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value]


def test_a_cap_skip_fails_the_attempt_for_good_and_names_the_cause(db, worker_factory):
    _seed(db, ["notes.md"])
    worker, _, _ = worker_factory(max_artifact_bytes=4)

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, "a retry meets the same cap"
    assert task["completed_at"] is not None
    assert task["end_cause"] == "outputs_missing"
    summary = task["result_summary"]
    assert summary["artifacts_skipped"] == [{"name": "notes.md", "cause": "cap"}]
    assert summary[MISSING_CAUSES_KEY] == [{"name": "notes.md", "cause": "cap"}]
    assert "notes.md" in task["last_error"] and "cap" in task["last_error"]
    assert "retried" not in task["last_error"]
    assert _retrying(db) == []


def test_an_upload_error_stays_retryable_and_says_so(db, worker_factory):
    _seed(db, ["notes.md"])
    worker, _, _ = worker_factory()
    real = worker.store.upload_file

    def flaky(key, source, content_type=None):
        if key.endswith("/artifacts/notes.md"):
            raise OSError("the bucket refused the write")
        return real(key, source, content_type) if content_type else real(key, source)

    worker.store.upload_file = flaky  # type: ignore[method-assign]

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, "an upload error may not recur"
    assert task["result_summary"]["artifacts_skipped"] == [
        {"name": "notes.md", "cause": "upload_error"}
    ]
    assert "upload" in task["last_error"]
    assert len(_retrying(db)) == 1


def test_a_file_never_written_stays_retryable(db, worker_factory):
    _seed(db, ["scan-01.md"], artifact_name="notes.md")
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value
    assert task["result_summary"][MISSING_KEY] == ["scan-01.md"]
    assert task["result_summary"][MISSING_CAUSES_KEY] == [
        {"name": "scan-01.md", "cause": "not_written"}
    ]


def _harvest_reporting(worker, git: dict[str, Any], published: list[bool]):
    """Stand in for a repository: the harvest reports `git`, the publish records."""

    def harvest(**kwargs: Any) -> dict[str, Any]:
        worker._deferred_publish = {"repo": None, "work_head": git.get("head"), "publish_repo": None}
        return dict(git)

    def publish_git(*, publish: bool, withheld: str = "", **_kwargs: Any) -> dict[str, Any]:
        published.append(publish)
        if not publish:
            return {"published": False, "publish_reason": withheld}
        return {"published": True, "branch": "swarm/task_1", "pull_request": {"number": 1}}

    worker._harvest_git = harvest  # type: ignore[method-assign]
    worker._publish_git = publish_git  # type: ignore[method-assign]


@pytest.mark.parametrize(
    "git, cause",
    [
        ({"base": "a" * 40, "head": "a" * 40, "patch": None, "patch_bytes": 0,
          "patch_omitted": False}, "empty_diff"),
        ({"base": "a" * 40, "head": "b" * 40, "patch": None, "patch_bytes": 9_000_000,
          "patch_omitted": True}, "patch_omitted"),
        ({"base": None, "head": "b" * 40, "patch": None, "patch_bytes": 0,
          "patch_omitted": False}, "no_base"),
    ],
    ids=["empty-diff", "patch-omitted", "no-base"],
)
def test_a_patch_a_retry_cannot_produce_fails_for_good(db, worker_factory, git, cause):
    _seed(db, [PATCH_NAME, "notes.md"])
    worker, _, _ = worker_factory()
    published: list[bool] = []
    _harvest_reporting(worker, git, published)

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, cause
    assert task["result_summary"][MISSING_CAUSES_KEY] == [{"name": PATCH_NAME, "cause": cause}]
    assert cause in task["last_error"]
    assert _retrying(db) == []
    assert True not in published, "a failed attempt pushed"


@pytest.mark.parametrize("attempt_count", [1, 3], ids=["retried", "last-attempt"])
def test_a_missing_output_means_no_push_and_no_pull_request(db, worker_factory, attempt_count):
    _seed(db, ["scan-01.md"], artifact_name="notes.md")
    db.doc("tasks/task_1")["attempt_count"] = attempt_count
    worker, _, _ = worker_factory()
    published: list[bool] = []
    _harvest_reporting(worker, {"base": "a" * 40, "head": "b" * 40, "patch": "p"}, published)

    assert worker.run() == ExitCode.FAILED
    assert True not in published, published
    git = db.doc("tasks/task_1")["result_summary"]["git"]
    assert git["published"] is False
    assert "scan-01.md" in git["publish_reason"]


def test_the_publish_happens_after_the_check_passes(db, worker_factory):
    """The control: every expected output uploaded, so the publish runs, once,
    after the upload, and its result lands in the summary."""
    _seed(db, ["notes.md"])
    worker, _, _ = worker_factory()
    published: list[bool] = []
    order: list[str] = []
    _harvest_reporting(worker, {"base": "a" * 40, "head": "b" * 40, "patch": "p"}, published)
    upload = worker._upload_outputs
    publish = worker._publish_git

    def upload_then_mark(**kwargs: Any) -> dict[str, Any]:
        out = upload(**kwargs)
        order.append("uploaded")
        return out

    def mark_publish(**kwargs: Any) -> dict[str, Any]:
        order.append("published")
        return publish(**kwargs)

    worker._upload_outputs = upload_then_mark  # type: ignore[method-assign]
    worker._publish_git = mark_publish  # type: ignore[method-assign]

    assert worker.run() == ExitCode.OK
    assert published == [True]
    assert order == ["uploaded", "published"]
    assert db.doc("tasks/task_1")["result_summary"]["git"]["branch"] == "swarm/task_1"


# -- the dependant's staging message -----------------------------------------


def _upstream(skipped: list[Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"artifacts": [], "artifacts_skipped": skipped}
    return {"state": TaskState.SUCCEEDED.value, "tenant_id": TENANT, "result_summary": summary}


@pytest.mark.parametrize(
    "cause, says",
    [("cap", "artifact size cap"), ("upload_error", "upload failed")],
)
def test_the_dependant_is_told_the_recorded_cause(cause, says):
    doc = _upstream([{"name": "huge.bin", "cause": cause}])
    with pytest.raises(InputUnavailable) as caught:
        inputs_mod.artifact_reference(
            doc, tenant_id=TENANT, upstream_task_id="task_up", filename="huge.bin"
        )
    assert says in str(caught.value)
    if cause == "upload_error":
        assert "size cap" not in str(caught.value), "the cap was assumed"


def test_a_summary_with_no_recorded_cause_does_not_claim_one():
    """A summary written before causes were recorded holds a bare name."""
    doc = _upstream(["huge.bin"])
    with pytest.raises(InputUnavailable) as caught:
        inputs_mod.artifact_reference(
            doc, tenant_id=TENANT, upstream_task_id="task_up", filename="huge.bin"
        )
    assert "recorded no cause" in str(caught.value)
