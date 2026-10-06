"""Retention of tenants/<t>/repos/ by the graph writer's sweep (docs/repo-index.md §2.3, lane IX3).

Owner decision 2026-10-06: the bucket's lifecycle no longer touches
`tenants/<t>/repos/` (terraform/modules/storage, held by
tests/terraform/artifact_lifecycle.tftest.hcl), so the sweep is the only thing
that deletes there. What is held here:

* given the kept commits (`--keep-commit`: the last 20 versions swarm-api
  holds), the sweep deletes every other commit's manifest and index copy and
  then exactly the blobs no kept manifest names -- a blob a kept manifest
  shares with a retired one stays;
* a retired commit younger than a day is kept (a writer may be mid-run);
* without a keep set nothing but orphan blobs is deleted, as before;
* one sweep deletes at most `max_deletes` objects, and a retired manifest past
  the bound still protects its blobs;
* another registration's objects are never touched;
* the CLI's `write --keep-commit` keeps the commit it just wrote, and refuses
  a value that is not a sha;
* on GCS the deletes go in batched `gcloud storage rm` calls.

Offline: the store is a directory, or a fake `gcloud` runner.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from test_repo_graph_shards import (  # noqa: F401 - `shards` is a fixture
    REPO_ID,
    TENANT,
    _container,
    _destination,
    graph_doc,
    shards,
)

OTHER_REPO = "gh-saga-xyz-other"


def _sha(word: str) -> str:
    return hashlib.sha1(word.encode("utf-8")).hexdigest()


def _doc(n: int) -> dict:
    """Commit `c<n>`: src/store/db.py's shard differs per commit, the rest are shared."""
    document = copy.deepcopy(graph_doc(f"c{n}"))
    for symbol in document["symbols"]:
        if symbol["path"] == "src/store/db.py":
            symbol["start_line"] = n + 1
            symbol["end_line"] = n + 3
    return document


def _age_tree(root: Path, days: float) -> None:
    moment = time.time() - days * 86_400
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (moment, moment))


def _blobs_of(root: Path, manifest_key: str) -> set[str]:
    manifest = json.loads((root / manifest_key).read_text())
    return {entry["blob"].split(":", 1)[1] for layer in manifest["shards"].values()
            for entry in layer.values()}


def _blob_digests(shards: Any, root: Path, repo: str = REPO_ID) -> set[str]:
    blobs = root / shards.graph_root(TENANT, repo) / "blobs"
    return {p.name[:-len(shards.BLOB_SUFFIX)] for p in blobs.iterdir()} if blobs.is_dir() \
        else set()


def _history(shards: Any, root: Path, count: int, repo: str = REPO_ID) -> list[dict]:
    """`count` commits written, each with its index copy beside it, all two days old."""
    store = shards.LocalStore(root)
    rows = []
    for n in range(count):
        document = _doc(n)
        report = shards.write_graph(document, store, tenant_id=TENANT, repo_id=repo)
        index = shards.index_key(TENANT, repo, document["commit_sha"])
        store.put(index, json.dumps({"commit_sha": document["commit_sha"]}).encode(),
                  no_clobber=False)
        rows.append({"commit": document["commit_sha"], "manifest": report["manifest"],
                     "index": index})
    _age_tree(root, 2)
    return rows


def test_the_index_copy_lives_beside_the_graph_under_the_registration(shards):
    module = shards
    sha = _sha("one")
    assert module.index_key(TENANT, REPO_ID, sha) == \
        f"tenants/{TENANT}/repos/{REPO_ID}/index/{sha}/repo-index.json"
    assert module.graph_root(TENANT, REPO_ID) == f"tenants/{TENANT}/repos/{REPO_ID}/graph"
    with pytest.raises(ValueError):
        module.index_key(TENANT, REPO_ID, "../x")


def test_the_sweep_keeps_twenty_versions_and_retires_the_rest(shards, tmp_path):
    rows = _history(shards, tmp_path, 22)
    kept, retired = rows[2:], rows[:2]
    kept_blobs = set().union(*(_blobs_of(tmp_path, r["manifest"]) for r in kept))
    retired_only = set().union(*(_blobs_of(tmp_path, r["manifest"]) for r in retired)) \
        - kept_blobs
    assert retired_only, "the fixture must give the retired commits blobs of their own"

    result = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                          keep=[r["commit"] for r in kept], now=datetime.now(timezone.utc))

    assert result["refused"] is None and result["truncated"] is False
    assert result["retired_manifests"] == 2 and result["retired_indexes"] == 2
    for row in retired:
        assert not (tmp_path / row["manifest"]).exists()
        assert not (tmp_path / row["index"]).exists()
    for row in kept:
        assert (tmp_path / row["manifest"]).exists()
        assert (tmp_path / row["index"]).exists()
    # Exactly the blobs only a retired manifest named; every shared one stays.
    assert _blob_digests(shards, tmp_path) == kept_blobs
    assert result["deleted"] == len(retired_only)


def test_a_retired_commit_younger_than_a_day_is_kept(shards, tmp_path):
    rows = _history(shards, tmp_path, 3)
    fresh = rows[0]
    os.utime(tmp_path / fresh["manifest"], None)
    os.utime(tmp_path / fresh["index"], None)
    before = _blob_digests(shards, tmp_path)

    result = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                          keep=[rows[2]["commit"]], now=datetime.now(timezone.utc))

    assert (tmp_path / fresh["manifest"]).exists() and (tmp_path / fresh["index"]).exists()
    assert not (tmp_path / rows[1]["manifest"]).exists()
    # The young manifest's blobs are still referenced.
    assert _blobs_of(tmp_path, fresh["manifest"]) <= _blob_digests(shards, tmp_path)
    assert result["retired_manifests"] == 1 and before != _blob_digests(shards, tmp_path)


def test_without_a_keep_set_no_manifest_or_index_is_retired(shards, tmp_path):
    rows = _history(shards, tmp_path, 3)
    before = _blob_digests(shards, tmp_path)

    result = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                          now=datetime.now(timezone.utc))

    for row in rows:
        assert (tmp_path / row["manifest"]).exists() and (tmp_path / row["index"]).exists()
    assert result["retired_manifests"] == 0 and result["deleted"] == 0
    assert _blob_digests(shards, tmp_path) == before


def test_one_sweep_is_bounded_and_a_manifest_past_the_bound_keeps_its_blobs(shards, tmp_path):
    rows = _history(shards, tmp_path, 4)
    kept = rows[2:]

    result = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                          keep=[r["commit"] for r in kept], max_deletes=1,
                          now=datetime.now(timezone.utc))

    assert result["truncated"] is True
    assert result["retired_manifests"] == 1 and result["retired_indexes"] == 0
    assert result["deleted"] == 0
    survivor = next(r for r in rows[:2] if (tmp_path / r["manifest"]).exists())
    assert _blobs_of(tmp_path, survivor["manifest"]) <= _blob_digests(shards, tmp_path)

    # The next sweep finishes the work.
    again = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                         keep=[r["commit"] for r in kept], now=datetime.now(timezone.utc))
    assert again["truncated"] is False
    assert not any((tmp_path / r["manifest"]).exists() for r in rows[:2])
    assert not any((tmp_path / r["index"]).exists() for r in rows[:2])


def test_the_sweep_never_touches_another_registration(shards, tmp_path):
    rows = _history(shards, tmp_path, 2)
    other = _history(shards, tmp_path, 2, repo=OTHER_REPO)
    other_blobs = _blob_digests(shards, tmp_path, OTHER_REPO)

    shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                 keep=[rows[1]["commit"]], now=datetime.now(timezone.utc))

    assert not (tmp_path / rows[0]["manifest"]).exists()
    for row in other:
        assert (tmp_path / row["manifest"]).exists() and (tmp_path / row["index"]).exists()
    assert _blob_digests(shards, tmp_path, OTHER_REPO) == other_blobs


def test_a_kept_manifest_that_cannot_be_read_stops_every_deletion(shards, tmp_path):
    rows = _history(shards, tmp_path, 3)
    (tmp_path / rows[2]["manifest"]).write_text("{not json")

    result = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                          keep=[rows[2]["commit"]], now=datetime.now(timezone.utc))

    assert result["refused"] and result["retired_manifests"] == 0
    assert all((tmp_path / r["manifest"]).exists() for r in rows)


def test_a_keep_value_that_is_not_a_sha_is_refused(shards, tmp_path):
    with pytest.raises(ValueError, match="40-hex"):
        shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                     keep=["main"])


def test_the_cli_write_keeps_the_commit_it_wrote(shards, tmp_path, monkeypatch):
    _container(shards, tmp_path, monkeypatch, TENANT_ID=TENANT)
    store = tmp_path / "bucket"
    rows = _history(shards, store, 3)
    written = _doc(9)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(written))

    code = shards.main(["write", "--graph", str(graph), "--store", str(store),
                        "--repo-id", REPO_ID, "--destination", _destination(),
                        "--keep-commit", rows[2]["commit"]])

    assert code == 0
    assert (store / shards.manifest_key(TENANT, REPO_ID, written["commit_sha"])).exists()
    assert (store / rows[2]["manifest"]).exists()
    assert not (store / rows[0]["manifest"]).exists()
    assert not (store / rows[1]["index"]).exists()


def test_the_cli_refuses_a_keep_commit_that_is_not_a_sha(shards, tmp_path, monkeypatch, capsys):
    _container(shards, tmp_path, monkeypatch, TENANT_ID=TENANT)
    store = tmp_path / "bucket"
    _history(shards, store, 1)
    code = shards.main(["sweep", "--store", str(store), "--repo-id", REPO_ID,
                        "--destination", _destination(), "--keep-commit", "HEAD"])
    assert code == 1
    assert "40-hex" in capsys.readouterr().err


class BatchGcloud:
    """`gcloud storage ls/cp/rm` over a dict; `rm` takes many urls in one call."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.created: dict[str, str] = {}
        self.rm_calls: list[list[str]] = []

    def __call__(self, argv: list[str], data: bytes | None = None) -> bytes:
        verb = argv[2]
        if verb == "ls":
            prefix = argv[-1].rstrip("*")
            rows = [{"url": k, "creation_time": self.created[k]}
                    for k in sorted(self.objects) if k.startswith(prefix)]
            return json.dumps(rows).encode()
        if verb == "cat":
            return self.objects[argv[-1]]
        if verb == "rm":
            urls = [part for part in argv[3:] if part.startswith("gs://")]
            self.rm_calls.append(urls)
            for url in urls:
                self.objects.pop(url, None)
            return b""
        raise AssertionError(f"unexpected gcloud call {argv}")


def test_the_gcs_sweep_deletes_in_batches(shards, tmp_path):
    local = tmp_path / "local"
    rows = _history(shards, local, 3)
    fake = BatchGcloud()
    bucket = "swarm-artifacts-test"
    for path in local.rglob("*"):
        if path.is_file():
            url = f"gs://{bucket}/{path.relative_to(local).as_posix()}"
            fake.objects[url] = path.read_bytes()
            fake.created[url] = "2026-09-01T00:00:00Z"
    store = shards.GcsStore(bucket, run=fake)

    result = shards.sweep(store, tenant_id=TENANT, repo_id=REPO_ID,
                          keep=[rows[2]["commit"]],
                          now=datetime(2026, 10, 6, tzinfo=timezone.utc))

    assert result["refused"] is None
    assert result["retired_manifests"] == 2 and result["retired_indexes"] == 2
    # Two calls: the retired manifests and index copies, then the blobs.
    assert len(fake.rm_calls) == 2
    assert len(fake.rm_calls[0]) == 4 and len(fake.rm_calls[1]) == result["deleted"] > 0
    for url in (u for call in fake.rm_calls for u in call):
        assert url.startswith(f"gs://{bucket}/tenants/{TENANT}/repos/{REPO_ID}/")
    assert f"gs://{bucket}/{rows[2]['manifest']}" in fake.objects
