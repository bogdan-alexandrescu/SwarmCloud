"""A restore continues the chain by a server-side copy, not a re-upload (CHECKPOINT-COPY, #1822).

CHECKPOINT-FAST made the restore re-upload every layer it replayed under the
new attempt's own prefix, so the first checkpoint after it could be
incremental. Those bytes were already in the bucket: the copy is now the
store's `copy(src_key, dst_key)` -- a GCS rewrite within the bucket, a file
copy for the local store -- and no archive byte leaves the worker for it. Both
keys are under the tenant's own prefix (invariant 9). A copy that fails keeps
CHECKPOINT-FAST's fallback: the tree is restored, the chain is left unseeded,
and the next checkpoint is a full archive.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from agent_worker import checkpoint as checkpoint_mod
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import ARCHIVE_NAME, MANIFEST_NAME, CheckpointManager, checkpoint_prefix
from agent_worker.logs import build_logger
from agent_worker.objectstore import GcsObjectStore, LocalObjectStore, ObjectStoreError

from worker_seeds import BUCKET, TENANT


def _logger(stream):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=stream,
    )


def _manager(store, logger, attempt_id="att_1"):
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id=attempt_id,
        generation=1, logger=logger,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _records(log_stream) -> list[dict]:
    return [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]


def _key(attempt_id: str, checkpoint_id: str, name: str = ARCHIVE_NAME) -> str:
    prefix = checkpoint_prefix(
        tenant_id=TENANT, task_id="task_1", attempt_id=attempt_id, checkpoint_id=checkpoint_id
    )
    return f"{prefix}/{name}"


class SpyStore(LocalObjectStore):
    """The local store, recording every archive upload and every copy."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, bucket=BUCKET)
        self.uploaded_files: list[str] = []
        self.copies: list[tuple[str, str]] = []
        self.fail_copy = False
        self.short_copy = False

    def upload_file(self, key, source, content_type=None):
        self.uploaded_files.append(key)
        return super().upload_file(key, source, content_type=content_type)

    def copy(self, src_key, dst_key):
        self.copies.append((src_key, dst_key))
        if self.fail_copy:
            raise ObjectStoreError("copy refused")
        written = super().copy(src_key, dst_key)
        return written - 1 if self.short_copy else written


@pytest.fixture
def spy(tmp_path) -> SpyStore:
    return SpyStore(tmp_path / "gcs")


def _two_layer_chain(store, tmp_path, log_stream, monkeypatch):
    """att_1: a full checkpoint, then an incremental on it; returns the head."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "repo").mkdir(parents=True)
    (ws.work / "repo" / "big.bin").write_bytes(os.urandom(200_000))
    _write(ws.work / "repo" / "edit.txt", "v1\n")
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 50_000_000)
    time.sleep(0.1)
    manager = _manager(store, _logger(log_stream))
    manager.create(ws)
    _write(ws.work / "repo" / "second.txt", "from the second checkpoint\n")
    time.sleep(0.1)
    head = manager.create(ws)
    assert head.base_checkpoint_id == "ckpt-00001"
    return head


# ---------------------------------------------------------------------------
# the restore copies each layer in the bucket and uploads no archive
# ---------------------------------------------------------------------------

def test_the_restore_copies_each_layer_and_uploads_no_archive_bytes(
    spy, tmp_path, log_stream, monkeypatch
):
    head = _two_layer_chain(spy, tmp_path, log_stream, monkeypatch)
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 0)
    spy.uploaded_files.clear()

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(spy, _logger(log_stream), attempt_id="att_2")
    manager.restore(head, resumed)

    # MUTATION: keep `_copy_layer`'s `upload_file` of the downloaded archive
    # -> both layers' archives are uploaded again from the worker.
    assert spy.uploaded_files == []
    # MUTATION: copy from this attempt's prefix, or to the earlier one's ->
    # the pairs name the wrong attempt. Oldest layer first, as applied.
    assert spy.copies == [
        (_key("att_1", "ckpt-00001"), _key("att_2", "ckpt-00001")),
        (_key("att_1", "ckpt-00002"), _key("att_2", "ckpt-00002")),
    ]
    for checkpoint_id in ("ckpt-00001", "ckpt-00002"):
        # MUTATION: copy to a key other than the layer's own id -> the bytes
        # under att_2 differ from (or are missing beside) the original.
        assert spy.download_bytes(_key("att_2", checkpoint_id)) == spy.download_bytes(
            _key("att_1", checkpoint_id)
        )
        manifest = json.loads(spy.download_bytes(_key("att_2", checkpoint_id, MANIFEST_NAME)))
        # MUTATION: drop the manifest upload with the archive's -> nothing
        # marks the copy committed.
        assert manifest["label"] == "restored"
        assert manifest["restored_from_attempt"] == "att_1"

    # The chain continues: the first checkpoint after the restore is
    # incremental on the restored head, and restores from att_2 alone.
    _write(resumed.work / "repo" / "edit.txt", "v2\n")
    first = manager.create_if_changed(resumed)
    # MUTATION: return False from `_copy_layer` after a successful copy ->
    # the chain is unseeded and this checkpoint is full.
    assert first is not None and first.base_checkpoint_id == head.checkpoint_id
    again = workspace_mod.create(tmp_path / "ws3", "att_3")
    _manager(spy, _logger(log_stream), attempt_id="att_3").restore(first, again)
    assert (again.work / "repo" / "edit.txt").read_text() == "v2\n"
    assert (again.work / "repo" / "second.txt").exists()


def test_a_failed_copy_leaves_the_next_checkpoint_full(spy, tmp_path, log_stream, monkeypatch):
    head = _two_layer_chain(spy, tmp_path, log_stream, monkeypatch)
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 0)
    spy.fail_copy = True

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(spy, _logger(log_stream), attempt_id="att_2")
    # A failed copy is not a failed restore: the tree is there.
    manager.restore(head, resumed)
    assert (resumed.work / "repo" / "second.txt").exists()
    # MUTATION: never call the store's copy -> there is no failure to fall back on.
    assert spy.copies, "the restore never asked the store to copy"

    _write(resumed.work / "repo" / "edit.txt", "v2\n")
    first = manager.create_if_changed(resumed)

    # MUTATION: let the copy's error escape `_copy_layer`, or return True
    # from it -> the restore raises, or the chain is seeded on a base that
    # was never copied and this checkpoint is incremental.
    assert first is not None and first.base_checkpoint_id is None
    restored = [r for r in _records(log_stream) if r.get("message") == "checkpoint restored"]
    assert restored and restored[-1]["chain_continued"] is False


def test_a_copy_of_the_wrong_size_leaves_the_next_checkpoint_full(
    spy, tmp_path, log_stream, monkeypatch
):
    head = _two_layer_chain(spy, tmp_path, log_stream, monkeypatch)
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 0)
    spy.short_copy = True

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(spy, _logger(log_stream), attempt_id="att_2")
    manager.restore(head, resumed)
    _write(resumed.work / "repo" / "edit.txt", "v2\n")
    first = manager.create_if_changed(resumed)

    # MUTATION: ignore the size `copy` returns -> a copy that is not the
    # archive the restore checked seeds the chain, and this is incremental.
    assert first is not None and first.base_checkpoint_id is None


# ---------------------------------------------------------------------------
# the stores' copy
# ---------------------------------------------------------------------------

def test_the_local_stores_copy_is_byte_identical(tmp_path):
    store = LocalObjectStore(tmp_path / "gcs", bucket=BUCKET)
    data = os.urandom(70_000)
    store.upload_bytes("tenants/t/tasks/a/x.bin", data)

    size = store.copy("tenants/t/tasks/a/x.bin", "tenants/t/tasks/b/deeper/x.bin")

    # MUTATION: return 0, or the source's path length -> the size is wrong.
    assert size == len(data)
    # MUTATION: write anything but the source's bytes -> not identical.
    assert store.download_bytes("tenants/t/tasks/b/deeper/x.bin") == data
    # The source stays where it was.
    assert store.download_bytes("tenants/t/tasks/a/x.bin") == data


def test_the_local_stores_copy_of_a_missing_object_raises(tmp_path):
    store = LocalObjectStore(tmp_path / "gcs", bucket=BUCKET)
    # MUTATION: let shutil's FileNotFoundError out -> not an ObjectStoreError.
    with pytest.raises(ObjectStoreError):
        store.copy("tenants/t/missing.bin", "tenants/t/copy.bin")
    # MUTATION: validate only the source key -> an unsafe destination is written.
    store.upload_bytes("tenants/t/x.bin", b"x")
    with pytest.raises(ObjectStoreError):
        store.copy("tenants/t/x.bin", "tenants/t/../../escape.bin")


class _FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.custom_time = None

    def rewrite(self, source, token=None):
        """GCS's rewrite: a token until the last of `rewrite_steps` calls."""
        self.bucket.rewrite_calls.append((source.bucket.name, source.name, self.name, token))
        total = len(self.bucket.objects[source.name])
        done = 1 if token is None else int(token.split("-")[1]) + 1
        if done < self.bucket.rewrite_steps:
            return f"step-{done}", 0, total
        self.bucket.objects[self.name] = self.bucket.objects[source.name]
        self.bucket.custom_times[self.name] = self.custom_time
        return None, total, total


class _FakeBucket:
    def __init__(self, name):
        self.name = name
        self.objects: dict[str, bytes] = {}
        self.custom_times: dict[str, object] = {}
        self.rewrite_calls: list[tuple] = []
        self.rewrite_steps = 1

    def blob(self, name):
        return _FakeBlob(self, name)


class _FakeClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, name):
        assert name == self._bucket.name
        return self._bucket


def test_the_gcs_stores_copy_rewrites_within_the_bucket_until_done():
    bucket = _FakeBucket(BUCKET)
    bucket.rewrite_steps = 3
    src = _key("att_1", "ckpt-00001")
    dst = _key("att_2", "ckpt-00001")
    bucket.objects[src] = b"archive bytes"
    store = GcsObjectStore(bucket=BUCKET, client=_FakeClient(bucket))

    size = store.copy(src, dst)

    # MUTATION: call `rewrite` once and ignore its token -> a large object's
    # rewrite stops part-way and the destination is never written.
    assert [call[3] for call in bucket.rewrite_calls] == [None, "step-1", "step-2"]
    # MUTATION: copy from or into another bucket -> the names differ.
    assert {call[0] for call in bucket.rewrite_calls} == {BUCKET}
    assert bucket.objects[dst] == b"archive bytes"
    assert size == len(b"archive bytes")
    # A checkpoint stays off the bucket's clock (objectstore module docstring).
    # MUTATION: stamp customTime on every copy -> the Delete rule reaches it.
    assert bucket.custom_times[dst] is None


def test_the_gcs_stores_copy_stamps_an_object_on_the_clock():
    bucket = _FakeBucket(BUCKET)
    bucket.objects["tenants/t/tasks/a/attempts/b/artifacts/x.txt"] = b"x"
    store = GcsObjectStore(bucket=BUCKET, client=_FakeClient(bucket))

    store.copy(
        "tenants/t/tasks/a/attempts/b/artifacts/x.txt",
        "tenants/t/tasks/a/attempts/c/artifacts/x.txt",
    )

    # MUTATION: build the destination blob without `_upload_blob` -> an
    # artifact copy has no customTime and never expires.
    assert isinstance(bucket.custom_times["tenants/t/tasks/a/attempts/c/artifacts/x.txt"], datetime)


def test_the_gcs_stores_copy_refuses_an_unsafe_key():
    bucket = _FakeBucket(BUCKET)
    store = GcsObjectStore(bucket=BUCKET, client=_FakeClient(bucket))
    # MUTATION: skip `validate_key` on either side -> the rewrite is sent.
    with pytest.raises(ObjectStoreError):
        store.copy("tenants/t/../x", "tenants/t/y")
    with pytest.raises(ObjectStoreError):
        store.copy("tenants/t/x", "/tenants/t/y")
    assert bucket.rewrite_calls == []


def test_the_store_protocol_names_copy():
    # MUTATION: leave `copy` off the Protocol -> a store without it still
    # passes as an ObjectStore and the restore's copy fails on every resume.
    from agent_worker.objectstore import ObjectStore

    assert "copy" in ObjectStore.__dict__
