"""The repository graph's shards, read for the API (docs/repo-index.md §2.5, lane RI9).

The indexer stores a commit's symbol and call graph with `swarm-repo-graph`
(images/agent-runtime-indexer/repo-index/repo_graph_shards.py) under the
tenant's own prefix:

    tenants/<tenant>/repos/<repo_id>/graph/<commit_sha>/manifest.json
    tenants/<tenant>/repos/<repo_id>/graph/blobs/<sha256>.jsonl.gz

and names the manifest's digest in `repo-index.json` as
`graph.manifest_digest`. Promotion (`repoindex.py`) checks the manifest
against that digest with `RepoGraph.verify` and records `graph_manifest` and
`graph_digest` on the version. This module is how the API reads a promoted
graph back; the impact, symbol and graph routes (lane RI11) answer from it.

WHY EACH RULE:

  * EVERY KEY IS BUILT FROM THE CALLER'S TENANT (invariant 9). A version
    record's stored `graph_manifest` is compared with the key derived from
    the tenant and repository the caller is scoped to, and refused if they
    differ; it is never followed. Another tenant's graph is therefore not
    filtered out -- it is never named. The manifest's own `tenant_id`,
    `repo_id` and `commit_sha` must match too, so a manifest copied from
    another prefix is refused even when its digest is consistent.
  * THE MANIFEST'S DIGEST IS CHECKED ON EVERY OPEN (§2.5, §5.2). Any agent in
    the tenant can write `tenants/<tenant>/` (request B would carve `graph/`
    out of that); a manifest rewritten after promotion no longer matches the
    digest recorded then and is refused, never served.
  * A BLOB IS CHECKED AGAINST ITS NAME BEFORE IT IS DECOMPRESSED, and then
    inflated only up to a bound, so a rewritten or hostile blob is neither
    believed nor allowed to expand without limit.
  * ABSENT AND UNREADABLE STAY DIFFERENT (`objects.py`): a missing object is
    `GraphUnavailable`, a store that could not answer is
    `UpstreamUnavailable`, and promotion retries the second, never the first.

The API's bucket grant is `roles/storage.objectViewer`: it never writes or
deletes here. The blob sweep is the writer's (`swarm-repo-graph sweep`),
run as the tenant's own worker account.

The format constants are restated from the writer, which runs in another
image; tests/unit/control_plane/test_repository_graph.py reads manifests and
blobs the shipped writer wrote, so a drift between the two is a red test.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import zlib
from typing import Any, Callable, Mapping

from .errors import Conflict, Gone, NotFound, UpstreamUnavailable, ValidationFailed
from .objects import (
    ObjectAbsent,
    ObjectReader,
    ObjectUnreadable,
    UnsafeKeySegment,
    safe_segment,
)

log = logging.getLogger(__name__)

#: The writer's manifest schema and blob encoding (repo_graph_shards.py).
MANIFEST_SCHEMA = "swarm.repo-graph-manifest/v1"
COMPRESSION = "gzip"
BLOB_SUFFIX = ".jsonl.gz"
#: The layers a manifest names (repo_graph_shards.LAYERS).
LAYERS = ("symbols", "callers", "callees", "tests", "files")

#: A manifest lists one entry per module per layer. This repository's has 58
#: modules and is 52 KB (measured 2026-10-05); a 10,000-file monorepo with a
#: few thousand directories is a few MB. 16 MiB bounds the read, not the use.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
#: One shard, stored and inflated. The largest shard of this repository is
#: 164 KB stored and 3.3 MB inflated; these bounds leave a monorepo two
#: orders of magnitude and keep one request's memory bounded.
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_SHARD_RAW_BYTES = 128 * 1024 * 1024

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class InvalidGraph(ValidationFailed):
    """A graph object, key or identifier that is not what the writer writes."""

    code = "invalid_graph"


class GraphDigestMismatch(Conflict):
    """A manifest or blob no longer matches the digest it was recorded under."""

    code = "graph_digest_mismatch"


class GraphUnavailable(Gone):
    """A manifest or blob that should be in the bucket is not."""

    code = "graph_gone"


class NoGraph(NotFound):
    """The version was promoted without a graph."""

    code = "no_graph"


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------

def _segment(value: Any, what: str) -> str:
    try:
        return safe_segment(value if isinstance(value, str) else "", what=what)
    except UnsafeKeySegment:
        raise InvalidGraph(f"{what} is not a single safe path segment") from None


def graph_root(tenant_id: str, repo_id: str) -> str:
    return (f"tenants/{_segment(tenant_id, 'tenant_id')}/repos/"
            f"{_segment(repo_id, 'repo_id')}/graph")


def manifest_key(tenant_id: str, repo_id: str, commit_sha: str) -> str:
    if not isinstance(commit_sha, str) or not _SHA.match(commit_sha):
        raise InvalidGraph("commit_sha is not a 40-hex commit sha")
    return f"{graph_root(tenant_id, repo_id)}/{commit_sha}/manifest.json"


def blob_key(tenant_id: str, repo_id: str, blob: str) -> str:
    """The key of a blob named `sha256:<hex>` in a manifest."""
    match = _DIGEST.match(blob or "")
    if match is None:
        raise InvalidGraph("a shard names no sha256 blob")
    return f"{graph_root(tenant_id, repo_id)}/blobs/{match.group(1)}{BLOB_SUFFIX}"


def module_of(symbol_id: str) -> str:
    """The module a symbol or file id lives in, as the writer shards it."""
    path = symbol_id.split("#", 1)[0]
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    return directory or "."


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# the manifest
# --------------------------------------------------------------------------

def parse_manifest(raw: bytes, *, tenant_id: str, repo_id: str, commit_sha: str
                   ) -> dict[str, Any]:
    """The manifest, checked to describe exactly this tenant, repository and commit."""
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InvalidGraph("the graph manifest is not JSON") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise InvalidGraph(f"the graph manifest is not a {MANIFEST_SCHEMA} document")
    if manifest.get("tenant_id") != tenant_id:
        raise InvalidGraph("the graph manifest describes another tenant's graph")
    if manifest.get("repo_id") != repo_id:
        raise InvalidGraph("the graph manifest describes another repository's graph")
    if manifest.get("commit_sha") != commit_sha:
        raise InvalidGraph(
            f"the graph manifest describes commit {str(manifest.get('commit_sha'))[:40]!r}, "
            f"not {commit_sha}"
        )
    if manifest.get("compression") != COMPRESSION or manifest.get("blob_suffix") != BLOB_SUFFIX:
        raise InvalidGraph(f"the graph manifest's blobs are not {COMPRESSION} {BLOB_SUFFIX}")
    shards = manifest.get("shards")
    if not isinstance(shards, dict) or set(shards) - set(LAYERS):
        raise InvalidGraph("the graph manifest's shards are not the writer's layers")
    for layer in shards.values():
        if not isinstance(layer, dict):
            raise InvalidGraph("a graph manifest layer is not an object")
        for entry in layer.values():
            if not isinstance(entry, dict) or not _DIGEST.match(str(entry.get("blob") or "")):
                raise InvalidGraph("a graph manifest shard names no sha256 blob")
    return manifest


def _inflate(data: bytes, limit: int) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(data, limit + 1)
    except zlib.error:
        raise InvalidGraph("a graph shard is not gzip") from None
    if len(out) > limit or inflater.unconsumed_tail:
        raise InvalidGraph(f"a graph shard inflates past {limit} bytes")
    if not inflater.eof:
        raise InvalidGraph("a graph shard is truncated")
    return out


class Graph:
    """One promoted commit's graph: a digest-checked manifest and its shards."""

    def __init__(self, reader: ObjectReader, *, tenant_id: str, repo_id: str, key: str,
                 digest: str, manifest: dict[str, Any]) -> None:
        self._reader = reader
        self.tenant_id = tenant_id
        self.repo_id = repo_id
        self.key = key
        self.digest = digest
        self.manifest = manifest
        self._shards: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def modules(self, layer: str = "symbols") -> list[str]:
        return sorted((self.manifest["shards"].get(layer) or {}).keys())

    def shard(self, layer: str, module: str) -> list[dict[str, Any]]:
        """One shard's rows; a module the layer does not name has none."""
        if layer not in LAYERS:
            raise InvalidGraph(f"{layer!r} is not a graph layer")
        cached = self._shards.get((layer, module))
        if cached is not None:
            return cached
        entry = (self.manifest["shards"].get(layer) or {}).get(module)
        if entry is None:
            return []
        key = blob_key(self.tenant_id, self.repo_id, entry["blob"])
        stored = _read_whole(self._reader, key, MAX_BLOB_BYTES, what="graph shard")
        if digest_of(stored) != entry["blob"]:
            log.warning("repo graph tenant=%s repo_id=%s blob=mismatch", self.tenant_id,
                        self.repo_id)
            raise GraphDigestMismatch(
                "a graph shard no longer matches the digest it is named by, so it is not "
                "served. Run the index again."
            )
        rows = []
        for line in _inflate(stored, MAX_SHARD_RAW_BYTES).splitlines():
            if line:
                try:
                    row = json.loads(line)
                except ValueError:
                    raise InvalidGraph("a graph shard line is not JSON") from None
                if isinstance(row, dict):
                    rows.append(row)
        self._shards[(layer, module)] = rows
        return rows

    def symbols(self, module: str) -> list[dict[str, Any]]:
        return self.shard("symbols", module)

    def callers(self, symbol_id: str) -> list[dict[str, Any]]:
        """Every edge INTO `symbol_id`: one read of its module's reverse shard."""
        return [e for e in self.shard("callers", module_of(symbol_id)) if e.get("to") == symbol_id]

    def callees(self, symbol_id: str) -> list[dict[str, Any]]:
        """Every edge OUT of `symbol_id`: one read of its module's forward shard."""
        return [e for e in self.shard("callees", module_of(symbol_id))
                if e.get("from") == symbol_id]

    def tests_for(self, symbol_id: str) -> list[dict[str, Any]]:
        return [t for t in self.shard("tests", module_of(symbol_id))
                if t.get("symbol") == symbol_id]


def _read_whole(reader: ObjectReader, key: str, limit: int, *, what: str) -> bytes:
    try:
        window = reader.read_range(key, offset=0, length=limit + 1)
    except ObjectAbsent:
        raise GraphUnavailable(f"the {what} is not in the bucket at {key}") from None
    except ObjectUnreadable:
        # Not the reason: a GCS error can quote the failed request, and a
        # signed URL is a credential (objects.ObjectUnreadable).
        raise UpstreamUnavailable(f"the artifact store could not be read for the {what}") \
            from None
    if window.total_bytes > limit:
        raise InvalidGraph(f"the {what} is larger than {limit} bytes")
    return window.data


class RepoGraph:
    """Opens a tenant's promoted graphs. Read-only; every key is the caller's tenant's."""

    def __init__(self, reader: Callable[[], ObjectReader]) -> None:
        self._reader = reader

    @classmethod
    def from_inspection(cls, inspection: Any) -> "RepoGraph":
        # The inspection service owns the one configured reader, and says
        # "no artifact store is configured" when there is none.
        return cls(inspection._reader)

    def _manifest(self, tenant_id: str, repo_id: str, commit_sha: str, expected: str
                  ) -> tuple[str, dict[str, Any]]:
        if not isinstance(expected, str) or not _DIGEST.match(expected):
            raise InvalidGraph("the recorded graph digest is not a sha256 digest")
        key = manifest_key(tenant_id, repo_id, commit_sha)
        raw = _read_whole(self._reader(), key, MAX_MANIFEST_BYTES, what="graph manifest")
        if digest_of(raw) != expected:
            log.warning("repo graph tenant=%s repo_id=%s sha=%s manifest=mismatch",
                        tenant_id, repo_id, commit_sha[:12])
            raise GraphDigestMismatch(
                f"the graph manifest of commit {commit_sha} does not match the digest "
                f"recorded for it, so it is not served. Run the index again.",
                detail={"digest": expected},
            )
        return key, parse_manifest(raw, tenant_id=tenant_id, repo_id=repo_id,
                                   commit_sha=commit_sha)

    def verify(self, tenant_id: str, repo_id: str, commit_sha: str, expected: str) -> str:
        """For promotion: the manifest's key, once its bytes match `expected`."""
        key, _manifest = self._manifest(tenant_id, repo_id, commit_sha, expected)
        return key

    def verified_manifest(self, tenant_id: str, repo_id: str, commit_sha: str, expected: str
                          ) -> tuple[str, dict[str, Any]]:
        """For promotion: the manifest's key and parsed body, once its bytes match `expected`."""
        return self._manifest(tenant_id, repo_id, commit_sha, expected)

    def open(self, tenant_id: str, repo_id: str, version: Mapping[str, Any]) -> Graph:
        """A promoted version's graph, for `tenant_id`'s own registration only."""
        digest = version.get("graph_digest")
        if not digest:
            raise NoGraph(f"the index of commit {version.get('commit_sha')} has no graph")
        commit_sha = str(version.get("commit_sha") or "")
        expected_key = manifest_key(tenant_id, repo_id, commit_sha)
        if version.get("graph_manifest") != expected_key:
            raise InvalidGraph("the version's graph manifest is not under this repository's "
                               "own prefix")
        key, manifest = self._manifest(tenant_id, repo_id, commit_sha, digest)
        return Graph(self._reader(), tenant_id=tenant_id, repo_id=repo_id, key=key,
                     digest=digest, manifest=manifest)


__all__ = [
    "BLOB_SUFFIX", "COMPRESSION", "Graph", "GraphDigestMismatch", "GraphUnavailable",
    "InvalidGraph", "LAYERS", "MANIFEST_SCHEMA", "NoGraph", "RepoGraph", "blob_key",
    "digest_of", "graph_root", "manifest_key", "module_of", "parse_manifest",
]
