"""Nothing the worker writes under tenants/<t>/repos/ carries a customTime (lane IX3).

The artifact bucket's customTime Delete is bucket-wide (terraform/modules/
storage/main.tf), because a per-tenant prefix list would take every runtime
personal tenant off the clock. GCS never matches `days_since_custom_time`
against an object with no customTime, so the only thing keeping an index copy
or a graph object out of that rule is the worker not stamping one. These tests
drive the production `GcsObjectStore._upload_blob` over a recording blob, with
the key `indexrun.Target.index_key` builds, and hold the other side too: a
task artifact, a log and a verdict are still stamped, and a checkpoint is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_worker.indexrun import Target, repo_id_for
from agent_worker.objectstore import GcsObjectStore, is_off_the_clock, is_repos_key


class _Blob:
    def __init__(self, name: str, sink: dict) -> None:
        self.name = name
        self.custom_time = None
        self._sink = sink

    def upload_from_string(self, data, content_type=None, **_):
        self._sink[self.name] = self.custom_time

    def upload_from_filename(self, filename, content_type=None, **_):
        self._sink[self.name] = self.custom_time


class _Bucket:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    def blob(self, name: str) -> _Blob:
        return _Blob(name, self._sink)


class _Client:
    def __init__(self) -> None:
        self.uploaded: dict[str, object] = {}

    def bucket(self, name: str) -> _Bucket:
        return _Bucket(self.uploaded)


def _store() -> tuple[GcsObjectStore, _Client]:
    client = _Client()
    return GcsObjectStore("swarm-artifacts-test", client=client), client


REPO_ID = repo_id_for("u-someone", "acme", "widgets")
SHA = "a" * 40

REPOS_KEYS = [
    Target("eng", REPO_ID).index_key(SHA),
    Target("u-someone", REPO_ID).index_key(SHA),
    f"{Target('eng', REPO_ID).destination}/manifests/{SHA}.json",
    f"{Target('eng', REPO_ID).destination}/blobs/{'0' * 64}.jsonl.gz",
]

STAMPED_KEYS = [
    "tenants/eng/tasks/t/attempts/a/artifacts/repo-index.json",
    "tenants/u-someone/tasks/t/attempts/a/artifacts/summary.md",
    "tenants/eng/tasks/t/attempts/a/logs/runner/stdout.log",
    "tenants/eng/verdicts/wf/t/review.json",
    # Not a repos/ layout: `repos` must be the tenant's own third segment.
    "tenants/eng/tasks/repos/attempts/a/artifacts/x.json",
    "tenants/repos/tasks/t/attempts/a/artifacts/x.json",
]


@pytest.mark.parametrize("key", REPOS_KEYS)
def test_an_upload_under_repos_carries_no_custom_time(key, tmp_path: Path):
    store, client = _store()
    assert is_repos_key(key)
    store.upload_bytes(key, b"{}", content_type="application/json")
    assert client.uploaded[key] is None, f"{key} carries a customTime the bucket Delete can match"

    (tmp_path / "f").write_bytes(b"{}")
    store.upload_file(key, tmp_path / "f")
    assert client.uploaded[key] is None


@pytest.mark.parametrize("key", STAMPED_KEYS)
def test_task_artifacts_logs_and_verdicts_are_still_stamped(key):
    store, client = _store()
    assert not is_repos_key(key)
    store.upload_bytes(key, b"x")
    assert client.uploaded[key] is not None, f"{key} has no customTime and would never expire"


def test_a_checkpoint_is_still_unstamped():
    key = "tenants/eng/tasks/t/attempts/a/checkpoints/ckpt-1/archive.tar.gz"
    store, client = _store()
    assert is_off_the_clock(key) and not is_repos_key(key)
    store.upload_bytes(key, b"x")
    assert client.uploaded[key] is None


@pytest.mark.parametrize(
    "key",
    [
        "tenants/eng/repos",
        "tenants/eng/repos/",
        "tenants/eng/repos/r",
        "tenants//repos/r/x",
        "tenants/eng/repos//x",
        "elsewhere/eng/repos/r/x",
    ],
)
def test_the_repos_rule_refuses_a_key_with_no_object_under_a_repository(key):
    assert not is_repos_key(key)
