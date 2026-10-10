"""A clone bundle's place in the tenant's own prefix, and its write-once store (#940).

A step that clones a sha some earlier step already cloned should read that
commit from the tenant's own GCS prefix instead of from GitHub. This file holds
the store half of that: where a bundle and a branch's head pointer live (built
only from the worker's own tenant id and the registration's repo_id, never URL
text), that a bundle is written once per sha by whoever clones first
(`upload_file_if_absent`: GCS's `ifGenerationMatch=0`, `O_EXCL` locally), that
the object carries a content type and nothing else a token could ride in, and
that every way a read can go wrong is a miss the caller falls back from, never
an exception that fails the step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_worker import clonebundle
from agent_worker.config import WorkerConfig
from agent_worker.indexrun import target
from agent_worker.objectstore import (
    GcsObjectStore,
    LocalObjectStore,
    is_checkpoint_key,
    is_off_the_clock,
    is_repos_key,
)

URL = "https://github.com/Acme/Widgets.git"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "f" * 40


# ---------------------------------------------------------------------------
# The key layout
# ---------------------------------------------------------------------------


def test_keys_sit_under_the_workers_own_tenant_prefix_only():
    key = clonebundle.bundle_key("eng", URL, SHA)
    head = clonebundle.head_key("eng", URL, "refs/heads/main")
    repo_id = target("eng", URL).repo_id

    assert key == f"tenants/eng/bundles/{repo_id}/{SHA}{clonebundle.BUNDLE_SUFFIX}"
    assert head.startswith(f"tenants/eng/bundles/{repo_id}/heads/")
    assert head.endswith(clonebundle.HEAD_SUFFIX)
    assert len(head.rsplit("/", 1)[1]) == 32 + len(clonebundle.HEAD_SUFFIX)

    # The suffixes the bucket-wide lifecycle rule keys on are distinctive: an
    # agent artifact named `*.bundle` never matches them.
    assert clonebundle.BUNDLE_SUFFIX == ".swarm-clone.bundle"
    assert clonebundle.HEAD_SUFFIX == ".swarm-clone.head"

    for built in (key, head):
        # No URL text reaches a key: the owner and repository are hashed into
        # the registration's repo_id.
        assert "acme" not in built.lower() and "widgets" not in built.lower()
        assert "github" not in built
        # Off repos/ (which is off the expiry clock) and not a checkpoint, so
        # the upload is stamped with customTime and the bundle expires.
        assert not is_repos_key(built)
        assert not is_checkpoint_key(built)
        assert not is_off_the_clock(built)

    # Another tenant cloning the same repository and sha gets its own key,
    # under its own prefix, with its own repo_id: never a shared object.
    theirs = clonebundle.bundle_key("u-someone", URL, SHA)
    assert theirs.startswith("tenants/u-someone/bundles/")
    assert target("u-someone", URL).repo_id != repo_id
    assert theirs.split("/")[3] != key.split("/")[3]

    # Two branches never share a head pointer.
    assert head != clonebundle.head_key("eng", URL, "refs/heads/dev")


@pytest.mark.parametrize(
    "tenant, url, sha",
    [
        ("eng", URL, SHA[:12]),
        ("eng", URL, SHA.upper()),
        ("eng", URL, SHA + "0"),
        ("eng", URL, ""),
        ("eng", URL, None),
        ("eng", "https://gitlab.com/acme/widgets.git", SHA),
        ("eng", "git@github.com:acme/widgets.git", SHA),
        ("eng", None, SHA),
        ("", URL, SHA),
        ("eng/../other", URL, SHA),
    ],
)
def test_no_key_for_a_short_sha_or_a_non_github_url(tenant, url, sha):
    built = clonebundle.bundle_key(tenant, url, sha)
    assert isinstance(built, clonebundle.NoKey), built
    assert built.reason

    head = clonebundle.head_key(tenant, url, "refs/heads/main")
    if url == URL and tenant == "eng":
        assert isinstance(head, str)
    else:
        assert isinstance(head, clonebundle.NoKey)
    assert isinstance(clonebundle.head_key("eng", URL, ""), clonebundle.NoKey)


# ---------------------------------------------------------------------------
# Write once
# ---------------------------------------------------------------------------


def test_upload_if_absent_writes_once_and_the_second_writer_gets_false(tmp_path: Path):
    store = LocalObjectStore(tmp_path / "bucket")
    key = clonebundle.bundle_key("eng", URL, SHA)
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"first clone")
    second.write_bytes(b"second clone")

    assert store.upload_file_if_absent(key, first, clonebundle.BUNDLE_CONTENT_TYPE) is True
    assert store.upload_file_if_absent(key, second, clonebundle.BUNDLE_CONTENT_TYPE) is False
    assert store.download_bytes(key) == b"first clone"

    # The same through the module: the second publisher is told it lost, and
    # the object is the first one's.
    other = clonebundle.bundle_key("eng", URL, OTHER_SHA)
    assert clonebundle.publish_bundle(store, other, first) is True
    assert clonebundle.publish_bundle(store, other, second) is False
    assert store.download_bytes(other) == b"first clone"


class _Blob:
    def __init__(self, bucket: "_Bucket", name: str) -> None:
        self.bucket, self.name = bucket, name
        self.custom_time = None
        self.metadata = None
        self.content_type = None

    def upload_from_filename(self, filename, content_type=None, if_generation_match=None, **kw):
        from google.api_core.exceptions import PreconditionFailed

        self.bucket.calls.append(
            {
                "if_generation_match": if_generation_match,
                "custom_time": self.custom_time,
                "content_type": content_type,
                "metadata": self.metadata,
                "extra": kw,
            }
        )
        if if_generation_match == 0 and self.name in self.bucket.objects:
            raise PreconditionFailed("exists")
        self.bucket.objects[self.name] = Path(filename).read_bytes()


class _Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[dict] = []

    def blob(self, name: str) -> _Blob:
        return _Blob(self, name)


def _gcs() -> tuple[GcsObjectStore, _Bucket]:
    store = GcsObjectStore("swarm-artifacts-test", client=object())
    bucket = _Bucket()
    store._bucket_obj = bucket
    return store, bucket


def test_gcs_upload_if_absent_sends_if_generation_match_zero_and_stamps_custom_time(tmp_path):
    pytest.importorskip("google.api_core")
    store, bucket = _gcs()
    key = clonebundle.bundle_key("eng", URL, SHA)
    source = tmp_path / "b"
    source.write_bytes(b"bundle")

    assert store.upload_file_if_absent(key, source, clonebundle.BUNDLE_CONTENT_TYPE) is True
    assert store.upload_file_if_absent(key, source, clonebundle.BUNDLE_CONTENT_TYPE) is False
    assert [c["if_generation_match"] for c in bucket.calls] == [0, 0]
    # Stamped exactly as any other non-checkpoint, non-repos object, so the
    # bucket's clock reaches it.
    assert all(c["custom_time"] is not None for c in bucket.calls)
    assert all(c["content_type"] == "application/x-git-bundle" for c in bucket.calls)
    assert bucket.objects[key] == b"bundle"


def test_a_bundle_object_carries_no_custom_metadata(tmp_path):
    pytest.importorskip("google.api_core")
    store, bucket = _gcs()
    key = clonebundle.bundle_key("eng", URL, SHA)
    source = tmp_path / "b"
    source.write_bytes(b"bundle")
    token = "ghp_" + "x" * 36

    assert clonebundle.publish_bundle(store, key, source) is True
    (call,) = bucket.calls
    assert call["metadata"] is None
    assert call["extra"] == {}
    assert token not in repr(call)
    # Nothing the upload sent carries anything but the content type and the
    # write-once precondition.
    assert call["content_type"] == clonebundle.BUNDLE_CONTENT_TYPE


# ---------------------------------------------------------------------------
# Every read failure is a miss
# ---------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict]] = []

    def info(self, message, **fields):
        self.lines.append((message, fields))

    warning = info


class _BrokenStore(LocalObjectStore):
    def download_file(self, key, destination):
        destination.write_bytes(b"partial")
        raise RuntimeError("connection reset")

    def download_bytes(self, key):
        raise RuntimeError("connection reset")


def test_an_over_cap_or_missing_bundle_is_a_miss_not_an_error(tmp_path: Path):
    store = LocalObjectStore(tmp_path / "bucket")
    log = _Log()
    key = clonebundle.bundle_key("eng", URL, SHA)
    dest = tmp_path / "ws" / "clone.bundle"

    # Absent: a miss, logged, and nothing left behind.
    assert clonebundle.fetch_bundle(store, key, dest, max_bytes=1024, log=log) is None
    assert not dest.exists()
    assert log.lines and "miss" in log.lines[-1][0]

    # Over the cap: a miss, and the oversized copy is removed.
    source = tmp_path / "big"
    source.write_bytes(b"x" * 2048)
    assert store.upload_file_if_absent(key, source) is True
    assert clonebundle.fetch_bundle(store, key, dest, max_bytes=1024, log=log) is None
    assert not dest.exists()

    # Within the cap: its size.
    assert clonebundle.fetch_bundle(store, key, dest, max_bytes=4096, log=log) == 2048
    assert dest.read_bytes() == b"x" * 2048
    dest.unlink()

    # A store error mid-download: a miss, and the partial file is removed.
    broken = _BrokenStore(tmp_path / "bucket")
    assert clonebundle.fetch_bundle(broken, key, dest, max_bytes=4096, log=log) is None
    assert not dest.exists()
    assert clonebundle.read_head(broken, clonebundle.head_key("eng", URL, "main")) is None

    # A publish over the cap is refused, not attempted.
    big_key = clonebundle.bundle_key("eng", URL, OTHER_SHA)
    assert clonebundle.publish_bundle(store, big_key, source, max_bytes=1024) is False
    assert not store.exists(big_key)

    # A head pointer: absent, garbled and good.
    head = clonebundle.head_key("eng", URL, "refs/heads/main")
    assert clonebundle.read_head(store, head) is None
    store.upload_bytes(head, b"not a sha\n")
    assert clonebundle.read_head(store, head) is None
    assert clonebundle.publish_head(store, head, SHA) is True
    assert clonebundle.read_head(store, head) == SHA
    # The pointer moves with the branch.
    assert clonebundle.publish_head(store, head, OTHER_SHA) is True
    assert clonebundle.read_head(store, head) == OTHER_SHA
    assert clonebundle.publish_head(store, head, "short") is False
    assert clonebundle.read_head(store, head) == OTHER_SHA


# ---------------------------------------------------------------------------
# The worker's switches
# ---------------------------------------------------------------------------

_IDENTITY = {
    "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
    "TENANT_ID": "eng", "GENERATION": "1", "RUNNER_PROFILE": "mock",
    "PROJECT_ID": "swarm-test",
}


def test_clone_bundles_are_on_by_default_and_swarm_clone_bundles_0_turns_them_off(monkeypatch):
    for name in ("SWARM_CLONE_BUNDLES", "TASK_TIMEOUT_SECONDS", "CLOUD_RUN_JOB",
                 "RUNNER_JOB_NAME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in _IDENTITY.items():
        monkeypatch.setenv(name, value)

    cfg = WorkerConfig.from_env()
    assert cfg.clone_bundles_enabled is True
    assert cfg.clone_bundle_max_bytes == 512 * 1024 * 1024

    monkeypatch.setenv("SWARM_CLONE_BUNDLES", "0")
    assert WorkerConfig.from_env().clone_bundles_enabled is False
