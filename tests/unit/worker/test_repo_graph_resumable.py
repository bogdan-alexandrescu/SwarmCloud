"""The graph write completes after an interruption (owner decision 2026-10-06, lane IX1).

Measured on task_209ba9e0c9c948e284e9 (repo_4c5105947752b3f3, 2026-10-06):
the agent's `swarm-repo-graph write` was killed by Claude Code's 10-minute
command limit after 150 blobs, and the retry failed on its FIRST blob with
`HTTPError 412: At least one of the pre-conditions you specified did not
hold`. Two causes, both pinned here:

* `GcsStore.list` keyed each object by the `url` of `gcloud storage ls
  --json`, which on the versioned artifact bucket ends in `#<generation>`.
  No listed key ever equalled a blob key, so every blob read as absent and
  was put again with `--no-clobber` -- a 412 on the first one that existed.
  The sweep used the same list, so it never matched a manifest either.
* The writer put one blob per `gcloud storage cp` (~3.9 s each; a full
  graph of ~370 blobs is ~24 minutes), so a full graph could not be stored
  inside the 30-minute run at all. Blobs now go up in batches, one `cp` of
  many files, and only the ones not already there.

And the rule that makes a re-run complete: a blob that already exists at its
content-addressed path with the SAME content is written; one holding
DIFFERENT content is a hard error, and no manifest is written over it.

Offline: a directory store, and `gcloud` faked over a dict.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from test_repo_graph_shards import REPO_ID, TENANT, WRITER, _load, graph_doc

BUCKET = "swarm-artifacts-test"


@pytest.fixture(scope="module")
def shards() -> Any:
    return _load("repo_graph_shards_resumable", WRITER)


def _blob_keys(shards: Any) -> str:
    return f"{shards.graph_root(TENANT, REPO_ID)}/blobs/"


# --------------------------------------------------------------------------
# a directory store: interrupted after N shards, then run again
# --------------------------------------------------------------------------

class Interrupted(Exception):
    pass


def _interrupting(shards: Any, root: Path, after: int) -> Any:
    """A directory store that dies after `after` blob puts, as the killed run did."""

    class Dying(shards.LocalStore):
        puts = 0

        def put(self, key: str, data: bytes, *, no_clobber: bool) -> None:
            if "/blobs/" in key:
                if Dying.puts >= after:
                    raise Interrupted(key)
                Dying.puts += 1
            super().put(key, data, no_clobber=no_clobber)

        def put_many(self, items: dict[str, bytes], *, no_clobber: bool) -> None:
            for key in sorted(items):
                self.put(key, items[key], no_clobber=no_clobber)

    return Dying(root)


def test_a_write_interrupted_after_n_shards_completes_on_rerun(shards, tmp_path):
    document = graph_doc()
    with pytest.raises(Interrupted):
        shards.write_graph(document, _interrupting(shards, tmp_path, after=3),
                           tenant_id=TENANT, repo_id=REPO_ID)
    blobs = sorted((tmp_path / _blob_keys(shards)).iterdir())
    assert len(blobs) == 3, "the interrupted run left its first three shards"
    assert not list(tmp_path.rglob("manifest.json")), "and no manifest: it is written last"

    report = shards.write_graph(document, shards.LocalStore(tmp_path),
                                tenant_id=TENANT, repo_id=REPO_ID)

    assert report["blobs_reused"] == 3
    assert report["blobs_written"] > 0
    manifest = json.loads((tmp_path / report["manifest"]).read_bytes())
    referenced = shards.referenced_blobs(manifest)
    present = {p.name.split(".", 1)[0] for p in (tmp_path / _blob_keys(shards)).iterdir()}
    assert referenced <= present, "every shard the manifest names is there"
    # The same graph written in one go gives the same manifest.
    fresh = shards.write_graph(document, shards.LocalStore(tmp_path / "fresh"),
                               tenant_id=TENANT, repo_id=REPO_ID)
    assert fresh["manifest_digest"] == report["manifest_digest"]


def test_a_shard_path_holding_different_content_is_a_hard_error(shards, tmp_path):
    document = graph_doc()
    _key, _manifest, blobs = shards.build(document, tenant_id=TENANT, repo_id=REPO_ID)
    victim = sorted(blobs)[0]
    path = tmp_path / shards.blob_key(TENANT, REPO_ID, victim)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not the shard this name is the digest of")

    with pytest.raises(shards.ConflictError) as raised:
        shards.write_graph(document, shards.LocalStore(tmp_path),
                           tenant_id=TENANT, repo_id=REPO_ID)

    assert victim in str(raised.value)
    assert path.read_bytes() == b"not the shard this name is the digest of", "never overwritten"
    assert not list(tmp_path.rglob("manifest.json")), "no manifest over a conflicting shard"


def test_the_cli_exits_non_zero_on_a_conflicting_shard(shards, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(shards, "CONTAINER_ENVIRON", tmp_path / "no-environ")
    monkeypatch.delenv("TENANT_ID", raising=False)
    monkeypatch.delenv("ARTIFACT_BUCKET", raising=False)
    document = graph_doc()
    _key, _manifest, blobs = shards.build(document, tenant_id=TENANT, repo_id=REPO_ID)
    store = tmp_path / "bucket"
    path = store / shards.blob_key(TENANT, REPO_ID, sorted(blobs)[-1])
    path.parent.mkdir(parents=True)
    path.write_bytes(b"different")
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(document))

    code = shards.main(["write", "--graph", str(graph), "--store", str(store),
                        "--tenant", TENANT, "--repo-id", REPO_ID, "--no-sweep"])

    assert code == 1
    assert "different content" in capsys.readouterr().err


# --------------------------------------------------------------------------
# the GCS store, as the versioned bucket answers
# --------------------------------------------------------------------------

def _md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


class VersionedGcloud:
    """`gcloud storage` over a dict, answering `ls --json` as a VERSIONED bucket does.

    Every listed url carries `#<generation>`, and the object's name is in
    `metadata`. A `--no-clobber` copy onto an existing object fails with the
    412 the incident met. A multi-file `cp` copies each source into the
    destination prefix by its basename.
    """

    def __init__(self, shards: Any, *, with_md5: bool = True) -> None:
        self.store_error = shards.StoreError
        self.objects: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.generation = 1791300113458078
        self.with_md5 = with_md5

    def _store(self, url: str, data: bytes, no_clobber: bool) -> None:
        if no_clobber and url in self.objects:
            raise self.store_error("gcloud storage cp exited 1: HTTPError 412: At least one of "
                             "the pre-conditions you specified did not hold.")
        self.objects[url] = data

    def __call__(self, argv: list[str], data: bytes | None = None) -> bytes:
        self.calls.append(list(argv))
        assert argv[:2] == ["gcloud", "storage"], argv
        verb, rest = argv[2], [a for a in argv[3:] if not a.startswith("--")]
        no_clobber = "--no-clobber" in argv
        if verb == "cp":
            *sources, target = rest
            if sources == ["-"]:
                self._store(target, data or b"", no_clobber)
                return b""
            if target.startswith("gs://"):
                for source in sources:
                    self._store(target.rstrip("/") + "/" + Path(source).name,
                                Path(source).read_bytes(), no_clobber)
                return b""
            for source in sources:
                if source not in self.objects:
                    raise FileNotFoundError(source)
                (Path(target) / source.rsplit("/", 1)[-1]).write_bytes(self.objects[source])
            return b""
        if verb == "cat":
            if rest[-1] not in self.objects:
                raise FileNotFoundError(rest[-1])
            return self.objects[rest[-1]]
        if verb == "ls":
            prefix = rest[-1].rstrip("*")
            rows = []
            for url in sorted(self.objects):
                if not url.startswith(prefix):
                    continue
                name = url[len(f"gs://{BUCKET}/"):]
                meta: dict[str, Any] = {"bucket": BUCKET, "name": name,
                                        "generation": str(self.generation),
                                        "size": str(len(self.objects[url])),
                                        "timeCreated": "2026-10-06T15:20:00Z"}
                if self.with_md5:
                    meta["md5Hash"] = _md5(self.objects[url])
                rows.append({"url": f"{url}#{self.generation}", "type": "cloud_object",
                             "metadata": meta})
            return json.dumps(rows).encode()
        if verb == "rm":
            self.objects.pop(rest[-1], None)
            return b""
        raise AssertionError(f"unexpected gcloud call {argv}")


def _url(key: str) -> str:
    return f"gs://{BUCKET}/{key}"


def _leave_partial(shards: Any, fake: VersionedGcloud, count: int) -> dict[str, bytes]:
    """What the killed run left: `count` blobs and no manifest."""
    _key, _manifest, blobs = shards.build(graph_doc(), tenant_id=TENANT, repo_id=REPO_ID)
    for hexdigest in sorted(blobs)[:count]:
        fake.objects[_url(shards.blob_key(TENANT, REPO_ID, hexdigest))] = blobs[hexdigest]
    return blobs


def test_the_gcs_list_names_objects_without_their_generation(shards):
    fake = VersionedGcloud(shards)
    fake.objects[_url("tenants/eng/repos/r/graph/blobs/ab.jsonl.gz")] = b"x"
    store = shards.GcsStore(BUCKET, run=fake)
    assert [key for key, _ in store.list("tenants/eng/")] == [
        "tenants/eng/repos/r/graph/blobs/ab.jsonl.gz"
    ]


@pytest.mark.parametrize("with_md5", [True, False])
def test_a_rerun_on_the_versioned_bucket_reuses_the_killed_runs_blobs(shards, with_md5):
    fake = VersionedGcloud(shards, with_md5=with_md5)
    blobs = _leave_partial(shards, fake, count=4)
    store = shards.GcsStore(BUCKET, run=fake)

    report = shards.write_graph(graph_doc(), store, tenant_id=TENANT, repo_id=REPO_ID)

    assert report["blobs_reused"] == 4
    assert report["blobs_written"] == len(blobs) - 4
    assert _url(report["manifest"]) in fake.objects
    for hexdigest, data in blobs.items():
        assert fake.objects[_url(shards.blob_key(TENANT, REPO_ID, hexdigest))] == data


def test_the_gcs_writer_uploads_in_batches_not_one_process_per_blob(shards):
    fake = VersionedGcloud(shards)
    store = shards.GcsStore(BUCKET, run=fake)
    report = shards.write_graph(graph_doc(), store, tenant_id=TENANT, repo_id=REPO_ID)
    copies = [c for c in fake.calls if c[2] == "cp"]
    assert report["blobs_written"] > 3
    # One batch of blobs, then the manifest, last.
    assert len(copies) == 2, copies
    assert "--no-clobber" in copies[0]
    assert copies[-1][-1] == _url(report["manifest"])
    for call in fake.calls:
        assert all(part.startswith(f"gs://{BUCKET}/tenants/{TENANT}/")
                   for part in call if part.startswith("gs://")), call


def test_a_gcs_blob_with_different_content_is_a_hard_error(shards):
    fake = VersionedGcloud(shards)
    blobs = _leave_partial(shards, fake, count=2)
    victim = _url(shards.blob_key(TENANT, REPO_ID, sorted(blobs)[1]))
    fake.objects[victim] = b"someone else's bytes"
    store = shards.GcsStore(BUCKET, run=fake)

    with pytest.raises(shards.ConflictError):
        shards.write_graph(graph_doc(), store, tenant_id=TENANT, repo_id=REPO_ID)

    assert fake.objects[victim] == b"someone else's bytes"
    assert not any(url.endswith("/manifest.json") for url in fake.objects)


def test_a_blob_that_appeared_during_the_upload_is_checked_not_failed(shards):
    """A 412 in the batch is a blob somebody wrote meanwhile: same bytes, written."""
    fake = VersionedGcloud(shards)
    _key, _manifest, blobs = shards.build(graph_doc(), tenant_id=TENANT, repo_id=REPO_ID)
    racing = sorted(blobs)[0]
    real = fake.__call__

    def racing_cp(argv: list[str], data: bytes | None = None) -> bytes:
        if argv[2] == "cp" and "--no-clobber" in argv and "-" not in argv:
            fake.objects[_url(shards.blob_key(TENANT, REPO_ID, racing))] = blobs[racing]
        return real(argv, data)

    store = shards.GcsStore(BUCKET, run=racing_cp)
    report = shards.write_graph(graph_doc(), store, tenant_id=TENANT, repo_id=REPO_ID)
    assert _url(report["manifest"]) in fake.objects


def test_the_gcs_sweep_sees_manifests_on_the_versioned_bucket(shards):
    """Before the fix no listed key ended in /manifest.json: no reference was ever read."""
    fake = VersionedGcloud(shards)
    store = shards.GcsStore(BUCKET, run=fake)
    shards.write_graph(graph_doc(), store, tenant_id=TENANT, repo_id=REPO_ID)
    result = shards.sweep(store, tenant_id=TENANT, repo_id=REPO_ID,
                          now=datetime(2026, 10, 9, tzinfo=timezone.utc),
                          grace=timedelta(days=1))
    assert result["manifests"] == 1
    assert result["kept_referenced"] > 0 and result["deleted"] == 0
