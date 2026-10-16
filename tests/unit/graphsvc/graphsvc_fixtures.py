"""What the swarm-graph tests share: a store in memory holding the hand-written
graph (test_graphmcp_source.build_manifest) under a tenant's graph prefix,
tickets minted by swarm-api's REFERENCE mint, and a service over both.

Every key is built at run time (`secrets.token_hex`), never written as a
literal: nothing here may look like a credential."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from test_graphmcp_source import COMMIT, build_manifest

from swarm_api import graph_ticket as reference
from swarm_graph.cache import ManifestCache, ResultCache, ShardCache
from swarm_graph.server import GraphService
from swarm_graph.store import ObjectMissing, StoreUnavailable
from swarm_graph.ticket import KeyRing
from swarm_graph.warm import Warmer

TENANT = "eng"
OTHER_TENANT = "ops"
REPO_ID = "repo_0123456789abcdef"
OTHER_REPO_ID = "repo_fedcba9876543210"


def new_key() -> str:
    return "k" + secrets.token_hex(24)


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class MemoryStore:
    """`ObjectStore` over a dict, counting reads, with switchable faults."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.down = False

    def read(self, name: str) -> bytes:
        self.reads.append(name)
        if self.down:
            raise StoreUnavailable("storage unreachable (test)")
        if name not in self.objects:
            raise ObjectMissing(name)
        return self.objects[name]

    def put_graph(self, tenant_id: str, repo_id: str, raw: bytes,
                  blobs: dict[str, bytes], commit: str = COMMIT) -> None:
        root = f"tenants/{tenant_id}/repos/{repo_id}/graph"
        self.objects[f"{root}/{commit}/manifest.json"] = raw
        for hexdigest, data in blobs.items():
            self.objects[f"{root}/blobs/{hexdigest}.jsonl.gz"] = data


def hand_graph() -> tuple[bytes, dict[str, bytes]]:
    return build_manifest()


def mint(key: str, *, raw: bytes, tenant_id: str = TENANT, repo_id: str = REPO_ID,
         index_sha: str = COMMIT, exp: int | None = None, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "tenant_id": tenant_id, "repo_id": repo_id, "task_id": "t-1", "attempt_id": "a-1",
        "generation": 3, "index_sha": index_sha, "manifest_digest": digest(raw),
        "exp": int(time.time()) + 300 if exp is None else exp}
    claims.update(overrides)
    return reference.mint_graph_ticket(key, **claims)


def inline_warmer() -> Warmer:
    """A warmer whose warms are recorded, not run, so a test decides when."""
    jobs: list = []
    warmer = Warmer(run=jobs.append)
    warmer.jobs = jobs  # type: ignore[attr-defined]
    return warmer


def service(key: str, store: MemoryStore, *, enabled: bool = True,
            shards: ShardCache | None = None, results: ResultCache | None = None,
            warmer: Warmer | None = None) -> GraphService:
    return GraphService(keys=KeyRing(key.encode()), store=store, enabled=lambda: enabled,
                        shards=ShardCache() if shards is None else shards,
                        results=ResultCache() if results is None else results,
                        manifests=ManifestCache(),
                        warmer=inline_warmer() if warmer is None else warmer)


def post(svc: GraphService, body: bytes, ticket: str | None, *,
         path: str = "/mcp", method: str = "POST"):
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    if ticket is not None:
        headers.append(("Authorization", f"Bearer {ticket}"))
    return svc.handle(method, path, headers, lambda n: body[:n])
