"""GCS as the service reads it: names only from the ticket, `TenantSource`, and
the two failures that are statuses, not answers (graph-service.md §3.3, §5.3, §10).

The object names are swarm-api's own layout (`repograph.manifest_key`,
`repograph.blob_key`), held equal here, so the service reads exactly what the
indexer wrote and the §3.4 IAM condition admits.
"""

from __future__ import annotations

import hashlib
import time

import pytest
from graphsvc_fixtures import (
    OTHER_TENANT,
    REPO_ID,
    TENANT,
    MemoryStore,
    hand_graph,
    mint,
    new_key,
)
from test_graphmcp_source import COMMIT

from agent_worker.graphmcp.snapshot import SnapshotError
from swarm_api import repograph
from swarm_graph.cache import ShardCache
from swarm_graph.store import (
    GcsStore,
    IndexMissing,
    ManifestMismatch,
    ObjectMissing,
    StoreUnavailable,
    TenantSource,
    blob_name,
    manifest_name,
    open_snapshot,
)
from swarm_graph.ticket import verify_graph_ticket


def _ticket(raw: bytes, **overrides):
    key = new_key()
    return verify_graph_ticket([key.encode()], mint(key, raw=raw, **overrides), now=time.time())


def test_the_names_are_swarm_apis_layout():
    raw, blobs = hand_graph()
    ticket = _ticket(raw)
    assert manifest_name(ticket) == repograph.manifest_key(TENANT, REPO_ID, COMMIT)
    hexdigest = next(iter(blobs))
    assert blob_name(ticket, hexdigest) == repograph.blob_key(TENANT, REPO_ID,
                                                              "sha256:" + hexdigest)
    assert manifest_name(ticket).startswith(f"tenants/{TENANT}/repos/{REPO_ID}/graph/")


def test_a_blob_name_is_64_hex_or_nothing():
    raw, _ = hand_graph()
    with pytest.raises(SnapshotError):
        blob_name(_ticket(raw), "../../../tasks/x")


def test_the_source_reads_and_caches_under_the_tickets_tenant():
    raw, blobs = hand_graph()
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    shards = ShardCache()
    ticket = _ticket(raw)
    snap = open_snapshot(store, ticket, TenantSource(ticket, store, shards))
    snap.symbols_in("src/app/service.py")
    assert shards.keys() and all(key[0] == TENANT for key in shards.keys())
    assert all(name.startswith(f"tenants/{TENANT}/repos/{REPO_ID}/graph/")
               for name in store.reads)
    reads = len(store.reads)
    snap.symbols_in("src/app/service.py")
    assert len(store.reads) == reads, "a cached shard is not read again"


def test_another_tenant_with_equal_blobs_shares_nothing():
    """Owner decision 3: even byte-identical blobs are read and kept per tenant."""
    raw, blobs = hand_graph()
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    store.put_graph(OTHER_TENANT, REPO_ID, raw, blobs)
    shards = ShardCache()
    for tenant in (TENANT, OTHER_TENANT):
        ticket = _ticket(raw, tenant_id=tenant)
        open_snapshot(store, ticket, TenantSource(ticket, store, shards)).symbols_in(
            "src/app/service.py")
    tenants = {key[0] for key in shards.keys()}
    assert tenants == {TENANT, OTHER_TENANT}
    by_tenant = {t: {k[1] for k in shards.keys() if k[0] == t} for t in tenants}
    assert by_tenant[TENANT] == by_tenant[OTHER_TENANT]
    assert {n.split("/")[1] for n in store.reads if "/blobs/" in n} == {TENANT, OTHER_TENANT}


def test_a_missing_manifest_or_blob_is_index_missing():
    raw, blobs = hand_graph()
    store = MemoryStore()
    ticket = _ticket(raw)
    with pytest.raises(IndexMissing):
        open_snapshot(store, ticket, TenantSource(ticket, store, ShardCache()))
    store.put_graph(TENANT, REPO_ID, raw, {})
    snap = open_snapshot(store, ticket, TenantSource(ticket, store, ShardCache()))
    with pytest.raises(IndexMissing):
        snap.symbols_in("src/app/service.py")


def test_a_rewritten_manifest_is_a_mismatch():
    raw, blobs = hand_graph()
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw + b" ", blobs)
    ticket = _ticket(raw)
    with pytest.raises(ManifestMismatch):
        open_snapshot(store, ticket, TenantSource(ticket, store, ShardCache()))


def test_a_verified_manifest_for_another_commit_is_a_mismatch():
    raw, blobs = hand_graph()
    store = MemoryStore()
    other = "d" * 40
    store.put_graph(TENANT, REPO_ID, raw, blobs, commit=other)
    ticket = _ticket(raw, index_sha=other)
    with pytest.raises(ManifestMismatch):
        open_snapshot(store, ticket, TenantSource(ticket, store, ShardCache()))


def test_a_blob_that_does_not_match_its_name_is_refused_and_not_kept():
    raw, blobs = hand_graph()
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    victim = next(iter(blobs))
    store.objects[f"tenants/{TENANT}/repos/{REPO_ID}/graph/blobs/{victim}.jsonl.gz"] = b"x"
    shards = ShardCache()
    source = TenantSource(_ticket(raw), store, shards)
    with pytest.raises(SnapshotError):
        source.decoded(victim)
    assert (TENANT, victim) not in shards.keys()


class _Response:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status_code = status
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size: int):
        yield self._body

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, answer) -> None:
        self.answer = answer
        self.urls: list[str] = []

    def get(self, url: str, **kwargs):
        self.urls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_gcs_reads_one_object_by_its_exact_quoted_name():
    body = b"bytes"
    session = _Session(_Response(200, body))
    store = GcsStore("swarm-artifacts-dev", session=lambda: session)
    name = f"tenants/{TENANT}/repos/{REPO_ID}/graph/{COMMIT}/manifest.json"
    assert store.read(name) == body
    assert session.urls == [
        "https://storage.googleapis.com/storage/v1/b/swarm-artifacts-dev/o/"
        f"tenants%2F{TENANT}%2Frepos%2F{REPO_ID}%2Fgraph%2F{COMMIT}%2Fmanifest.json?alt=media"]


@pytest.mark.parametrize("answer,error", [
    (_Response(404), ObjectMissing), (_Response(403), StoreUnavailable),
    (_Response(500, b"secret-ish body"), StoreUnavailable),
    (ConnectionError("refused"), StoreUnavailable), (TimeoutError(), StoreUnavailable),
])
def test_gcs_failures(answer, error):
    store = GcsStore("swarm-artifacts-dev", session=lambda: _Session(answer))
    with pytest.raises(error) as raised:
        store.read("tenants/eng/repos/r/graph/x")
    assert "secret-ish" not in str(raised.value)


def test_no_identity_is_the_store_unavailable():
    def broken():
        raise RuntimeError("no credentials")
    with pytest.raises(StoreUnavailable):
        GcsStore("swarm-artifacts-dev", session=broken).read("x")


def test_the_bucket_is_a_bucket_name():
    with pytest.raises(ValueError):
        GcsStore("a/b")


def test_tenant_source_digest_check_is_decode_blob():
    """The source never decodes unchecked: a correct blob decodes, and its
    sha256 is its name."""
    raw, blobs = hand_graph()
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    hexdigest, data = next(iter(blobs.items()))
    assert hashlib.sha256(data).hexdigest() == hexdigest
    shard = TenantSource(_ticket(raw), store, ShardCache()).decoded(hexdigest)
    assert shard.raw_bytes > 0
