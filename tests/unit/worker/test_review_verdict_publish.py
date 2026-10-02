"""The review publishes its verdict once, to the verdicts prefix (CR 36, #295).

docs/merge-step.md §4.3: `claude-code-review`'s worker writes the agent's
`review.json` to

    tenants/<tenant>/verdicts/<workflow id>/<review task id>/review.json

with GCS's `ifGenerationMatch=0`, and REFUSES when a different object is
already there: after the M2 bucket split only the review's account can create
under `verdicts/`, and with objectCreator alone, so a verdict already at the
path is never replaced. The same bytes again (an attempt that died between
its write and its finish) are already published.

MUTATIONS: write with `upload_bytes` -- the existing object is overwritten.
Publish from any profile -- the claude-code control writes a verdict. Derive
the key from anything but the task's own id -- `post_verdict.verdict_key`
and the published key disagree.
"""

from __future__ import annotations

import json

import pytest

from agent_worker import post_verdict
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt

SHA = "a" * 40
REVIEW = {"verdict": "MERGE", "sha": SHA, "title": "The merge step lands the reviewed head",
          "summary": "Looks right."}


def _review_bytes(**override) -> bytes:
    return json.dumps({**REVIEW, **override}).encode()


def test_the_key_is_derived_from_tenant_workflow_and_review_task():
    assert post_verdict.verdict_key("eng", "wf_1", "task_r") == \
        "tenants/eng/verdicts/wf_1/task_r/review.json"
    for bad in (("eng", "../wf", "task_r"), ("eng", "wf_1", "a/b"), ("eng", "", "task_r")):
        with pytest.raises(post_verdict.VerdictUnreadable):
            post_verdict.verdict_key(*bad)


def test_the_write_refuses_an_object_already_at_the_path(store):
    key = post_verdict.verdict_key(TENANT, "wf_1", "task_r")
    planted = _review_bytes(summary="planted by someone else")
    store.upload_bytes(key, planted)
    with pytest.raises(post_verdict.VerdictAlreadyPublished):
        post_verdict.publish_verdict(store, key, _review_bytes())
    assert store.download_bytes(key) == planted, "the existing verdict was overwritten"


def test_the_write_creates_once_and_the_same_bytes_again_are_unchanged(store):
    key = post_verdict.verdict_key(TENANT, "wf_1", "task_r")
    assert post_verdict.publish_verdict(store, key, _review_bytes()) == "created"
    assert post_verdict.publish_verdict(store, key, _review_bytes()) == "unchanged"


class _GcsBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        from google.api_core.exceptions import PreconditionFailed

        self.bucket.calls.append(if_generation_match)
        if if_generation_match == 0 and self.name in self.bucket.objects:
            raise PreconditionFailed("exists")
        self.bucket.objects[self.name] = data

    def download_as_bytes(self):
        return self.bucket.objects[self.name]


class _GcsBucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[object] = []

    def blob(self, name):
        return _GcsBlob(self, name)


def test_on_gcs_the_write_carries_if_generation_match_zero():
    pytest.importorskip("google.api_core")
    from agent_worker.objectstore import GcsObjectStore

    gcs = GcsObjectStore("bucket", client=object())
    bucket = _GcsBucket()
    gcs._bucket_obj = bucket
    key = post_verdict.verdict_key(TENANT, "wf_1", "task_r")
    assert post_verdict.publish_verdict(gcs, key, _review_bytes()) == "created"
    with pytest.raises(post_verdict.VerdictAlreadyPublished):
        post_verdict.publish_verdict(gcs, key, _review_bytes(verdict="NOT_YET"))
    assert bucket.calls == [0, 0]


def _review_worker(db, worker_factory, profile: str = "claude-code-review"):
    seed_attempt(db, runner_profile=profile, task_input={"prompt": "review"})
    worker, _, _ = worker_factory(runner_profile=profile)
    worker._task = {"workflow_id": "wf_1", "metadata": {"dispatch": {
        "strategy": "single-pr", "pr_role": "reader", "pr_author": "task_a"}}}
    from agent_worker import workspace as workspace_mod

    worker.ws = workspace_mod.create(worker.cfg.workspace_root, worker.cfg.attempt_id)
    return worker


def test_the_review_worker_publishes_its_agents_review_json(db, store, worker_factory):
    worker = _review_worker(db, worker_factory)
    (worker.ws.artifacts / "review.json").write_bytes(_review_bytes())
    summary: dict = {}
    assert worker._publish_review_verdict(summary) is None
    key = post_verdict.verdict_key(TENANT, "wf_1", "task_1")
    assert store.download_bytes(key) == _review_bytes()
    assert summary["verdict_published"] == {"key": key, "result": "created"}


def test_the_review_worker_refuses_when_a_different_verdict_is_there(db, store, worker_factory):
    worker = _review_worker(db, worker_factory)
    key = post_verdict.verdict_key(TENANT, "wf_1", "task_1")
    store.upload_bytes(key, _review_bytes(verdict="NOT_YET"))
    (worker.ws.artifacts / "review.json").write_bytes(_review_bytes())
    outcome = worker._publish_review_verdict({})
    assert outcome is not None and outcome.exit_code == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, "a refusal a retry would meet was retried"
    assert task["end_cause"] == EndCause.PUBLISH_REFUSED.value
    assert task["last_error"].startswith("verdict_exists")
    assert json.loads(store.download_bytes(key))["verdict"] == "NOT_YET"


def test_a_review_that_wrote_no_usable_verdict_fails_its_attempt_retryably(
    db, store, worker_factory
):
    worker = _review_worker(db, worker_factory)
    (worker.ws.artifacts / "review.json").write_text('{"verdict": "SHIP IT"}')
    outcome = worker._publish_review_verdict({})
    assert outcome is not None
    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value
    assert store.list_keys(f"tenants/{TENANT}/verdicts/") == []


def test_a_linked_review_json_is_not_followed(db, store, worker_factory, tmp_path):
    worker = _review_worker(db, worker_factory)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(_review_bytes())
    (worker.ws.artifacts / "review.json").symlink_to(elsewhere)
    assert worker._publish_review_verdict({}) is not None
    assert store.list_keys(f"tenants/{TENANT}/verdicts/") == []


def test_no_other_profile_publishes_a_verdict(db, store, worker_factory):
    """The control: claude-code's account cannot create under verdicts/."""
    worker = _review_worker(db, worker_factory, profile="claude-code")
    (worker.ws.artifacts / "review.json").write_bytes(_review_bytes())
    assert worker._publish_review_verdict({}) is None
    assert store.list_keys(f"tenants/{TENANT}/verdicts/") == []
