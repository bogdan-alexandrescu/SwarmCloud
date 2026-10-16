"""GCS, the store of record, read only through names built from the ticket
(docs/design/graph-service.md §3.3, §5.3).

    tenants/<tenant_id>/repos/<repo_id>/graph/<index_sha>/manifest.json
    tenants/<tenant_id>/repos/<repo_id>/graph/blobs/<sha256>.jsonl.gz

the layout of `swarm_api.repograph.manifest_key` and `repograph.blob_key`
(tests/unit/graphsvc/test_graphsvc_store.py holds the two equal). Every part
of a name comes from a VERIFIED ticket (`ticket.verify_graph_ticket` checked
each against its exact shape) or from a manifest entry's sha256; no tool
argument ever reaches a name. As knowledge-graph.md §5.3 says of the local
server, swarm-graph "has no repository argument at all".

READ BY EXACT NAME, NEVER LISTED. The service's one storage grant (§3.4) is
`roles/storage.objectViewer` conditioned to `tenants/<t>/repos/<r>/graph/**`;
a list call is evaluated against the bucket and that condition refuses it,
which is intended.

`TenantSource` is the service's `BlobSource` (graph-service.md §2.4, seam 1): a
view of the process-wide `ShardCache` scoped to one tenant and one repository.
Every blob it decodes goes through `snapshot.decode_blob`, so the
sha256-against-name check holds here as it does for a staged directory.

TWO FAILURES ARE NOT ANSWERS, and are raised past the tool handlers to become
HTTP statuses (§2.2, §10): `IndexMissing` (the pinned manifest or one of its
blobs is gone, e.g. swept past KEPT_VERSIONS during a long attempt) is 404,
and `StoreUnavailable` (GCS unreachable, or answering anything but 200 or 404)
is 503. Both subclass `SnapshotError`, so the graph code they pass through
treats them as it treats any unreadable snapshot: it never answers from one.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Protocol
from urllib.parse import quote

from agent_worker.graphmcp.snapshot import (
    BLOB_SUFFIX,
    BLOBS_DIR,
    MANIFEST_NAME,
    Snapshot,
    SnapshotError,
    _Shard,
    decode_blob,
)

from .cache import ShardCache
from .ticket import GraphTicket

GCS_OBJECTS = "https://storage.googleapis.com/storage/v1/b/"
#: One object read; a GCS read that takes longer is the store unreachable.
OBJECT_TIMEOUT_SECONDS = 10
#: The largest object this service reads. A blob is one module's shard of one
#: layer (this repository's largest is under 1 MB stored); a manifest is a
#: table of contents. Anything larger is not one.
MAX_OBJECT_BYTES = 64 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")


class IndexMissing(SnapshotError):
    """The pinned manifest, or a blob it names, is not stored (404)."""


class StoreUnavailable(SnapshotError):
    """GCS could not be read (503). The message is a cause word, never a body."""


class ManifestMismatch(SnapshotError):
    """The stored manifest is not the one the ticket pins (409)."""


class ObjectMissing(Exception):
    """The store has no object by that name."""


class ObjectStore(Protocol):
    def read(self, name: str) -> bytes:
        """The object's bytes; `ObjectMissing` or `StoreUnavailable` otherwise."""
        ...


def graph_root(ticket: GraphTicket) -> str:
    return f"tenants/{ticket.tenant_id}/repos/{ticket.repo_id}/graph"


def manifest_name(ticket: GraphTicket) -> str:
    return f"{graph_root(ticket)}/{ticket.index_sha}/{MANIFEST_NAME}"


def blob_name(ticket: GraphTicket, hexdigest: str) -> str:
    if not _HEX.fullmatch(hexdigest):
        raise SnapshotError("a blob is named by 64 hex characters")
    return f"{graph_root(ticket)}/{BLOBS_DIR}/{hexdigest}{BLOB_SUFFIX}"


class GcsStore:
    """Objects of one bucket over the JSON API, with this pod's own Workload
    Identity (the KSA `swarm-graph`), into memory."""

    def __init__(self, bucket: str, *, session: Callable[[], Any] | None = None,
                 timeout: float = OBJECT_TIMEOUT_SECONDS) -> None:
        if not _BUCKET.fullmatch(bucket or ""):
            raise ValueError("SWARM_GRAPH_BUCKET is a bucket name")
        self.bucket = bucket
        self._make_session = session or _authorized_session
        self._session: Any = None
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"GcsStore(bucket={self.bucket!r})"

    def url(self, name: str) -> str:
        return f"{GCS_OBJECTS}{self.bucket}/o/{quote(name, safe='')}?alt=media"

    def read(self, name: str) -> bytes:
        if self._session is None:
            try:
                self._session = self._make_session()
            except Exception as exc:  # noqa: BLE001 - no identity is the store unreachable
                raise StoreUnavailable(f"no storage session ({type(exc).__name__})") from None
        try:
            response = self._session.get(self.url(name), timeout=self._timeout, stream=True)
        except Exception as exc:  # noqa: BLE001 - every transport failure is one status
            raise StoreUnavailable(f"storage unreachable ({type(exc).__name__})") from None
        try:
            if response.status_code == 404:
                raise ObjectMissing(name)
            if response.status_code != 200:
                # The status only: an error body may quote the request.
                raise StoreUnavailable(f"storage answered HTTP {response.status_code}")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=1 << 20):
                body.extend(chunk)
                if len(body) > MAX_OBJECT_BYTES:
                    raise StoreUnavailable("an object is over the size a graph object may be")
            return bytes(body)
        except (ObjectMissing, StoreUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - a body cut off mid-read
            raise StoreUnavailable(f"storage read failed ({type(exc).__name__})") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()


def _authorized_session() -> Any:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/devstorage.read_only"])
    return AuthorizedSession(credentials)


class TenantSource:
    """The `BlobSource` of one tenant's repository: the process-wide shard
    cache, keyed by this tenant, over this repository's `graph/blobs/`."""

    def __init__(self, ticket: GraphTicket, store: ObjectStore, shards: ShardCache,
                 *, on_load: Callable[[bool, int], None] | None = None) -> None:
        self._ticket = ticket
        self._store = store
        self._shards = shards
        self._on_load = on_load
        self.tenant_id = ticket.tenant_id
        self.repo_id = ticket.repo_id

    def decoded(self, hexdigest: str) -> _Shard:
        name = blob_name(self._ticket, hexdigest)

        def load() -> _Shard:
            try:
                data = self._store.read(name)
            except ObjectMissing:
                raise IndexMissing(f"the index of {self._ticket.index_sha[:12]} is no longer "
                                   f"stored (blob {hexdigest[:12]} is missing)") from None
            return decode_blob(hexdigest, data)

        shard, hit = self._shards.get_or_load((self.tenant_id, hexdigest), load)
        if self._on_load is not None:
            self._on_load(hit, shard.raw_bytes)
        return shard


def read_manifest(store: ObjectStore, ticket: GraphTicket) -> bytes:
    """The pinned manifest's bytes, checked against the ticket's digest."""
    try:
        raw = store.read(manifest_name(ticket))
    except ObjectMissing:
        raise IndexMissing(f"the index of {ticket.index_sha[:12]} is no longer stored "
                           "(pruned or missing)") from None
    if "sha256:" + hashlib.sha256(raw).hexdigest() != ticket.manifest_digest:
        raise ManifestMismatch(f"the stored index of {ticket.index_sha[:12]} does not match "
                               "its promoted digest")
    return raw


def open_snapshot(store: ObjectStore, ticket: GraphTicket, source: TenantSource) -> Snapshot:
    """`Snapshot.from_manifest` over the pinned manifest. A manifest that
    verifies but names another commit than the ticket's is a mismatch too."""
    raw = read_manifest(store, ticket)
    snap = Snapshot.from_manifest(raw, manifest_digest=ticket.manifest_digest, source=source)
    if snap.commit_sha and snap.commit_sha != ticket.index_sha:
        raise ManifestMismatch(f"the stored index of {ticket.index_sha[:12]} names another "
                               "commit")
    return snap


__all__ = [
    "GCS_OBJECTS",
    "GcsStore",
    "IndexMissing",
    "ManifestMismatch",
    "ObjectMissing",
    "ObjectStore",
    "StoreUnavailable",
    "TenantSource",
    "blob_name",
    "graph_root",
    "manifest_name",
    "open_snapshot",
    "read_manifest",
]
