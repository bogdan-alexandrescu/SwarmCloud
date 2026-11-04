"""The read-only stats a replica serves on its stats port
(docs/design/graph-answer-cache.md §5.3; lane CACHE-1-STATS, part of #1835).

The console's Cache section (BGD's pick C, 2026-10-27) reads every replica
live. swarm-api mints a STATS TICKET (ticket.py) and calls one replica's
`GET /v1/stats`; that replica answers for itself and reads every peer's
`GET /v1/stats/local` with the same ticket. The routes are server.py's, on
STATS_PORT, which never serves `/mcp`: a NetworkPolicy admits by port, not
path, so swarm-api's range is admitted to 8081 and never to 8080.

WHAT ONE REPLICA RETURNS (`local_document`), the interface lanes 3 and 4 build
against:

    {"replica": <pod name>, "state": "read", "started_at": <ISO-8601 Z>,
     "window_hours": <1..24: the hours this replica's counts cover>,
     "result_bytes", "result_bound", "shard_bytes", "shard_bound",
     -- for the ticket's (tenant_id, repo_id) only:
     "outcomes": [{"hour": <ISO Z>, "hit": n, "miss": n}, ... 24, oldest first],
     "served_ms": {"bounds_ms": MS_BOUNDS, "counts": [len(MS_BOUNDS) + 1]},
     "answer_latency": {"window_hours": 24, "bounds_ms": LATENCY_BOUNDS_MS,
                        "count", "p50_ms", "p95_ms", "p99_ms", "max_ms",
                        "counts": [len(LATENCY_BOUNDS_MS) + 1],
                        "by_outcome": {<LATENCY_OUTCOMES>: {"count", "p50_ms", ..., "counts"}},
                        "by_temperature": {"warm"|"cold": {...}},
                        "cells": {"<outcome>/<temperature>": {...}, only those seen}},
     "answers_hot": n,
     "shards_hot": n | null,
     "last_warm": {"digest", "ms", "shards", "replayed": null, "cause", "at"} | null,
     -- with `?shards=1` only:
     "shards": {<blob sha256 hex>: {"hot", "hits_24h", "last_read": <ISO Z> | null}}}

and `GET /v1/stats` is `{"replicas": [this replica, then each peer],
"peers_resolved": bool}`. A peer that does not answer is exactly
`{"replica": <address>, "state": "not_read", "reason": <NOT_READ_REASONS>}`,
NEVER a panel of zeros (§5.4); `peers_resolved` false says the headless
Service did not resolve, so a missing peer is never silent either.

THE FIGURES, and why each is what it is:

  * the four byte figures are the replica's totals across every tenant. They
    are numbers, not keys or names, and a full cache is what makes this
    repository's answers miss (§5.2);
  * `outcomes` and `served_ms` are AnswerCounters, recorded by
    `GraphService._log` right after the `graph_answer` line, so the tile and
    the log-based metric of §3.1 count the same events. `none` (no cache key)
    is not an outcome (§3.2). Each bucket carries a `carried` count: carry
    forward (§2.3 b) is built (CARRY_FORWARD);
  * `answer_latency` is every answer's milliseconds, recorded with the same
    line, by outcome (`hit`, `carried`, `miss`, `partial` for one cut at the
    deadline, `uncached` for a question with no cache key) and by temperature
    (`cold` when the request read the manifest or a shard from storage,
    `warm` when it was answered from memory). The percentiles are read off a
    histogram whose bounds step about 25%, each capped at the slowest answer
    seen, and are null with a count of 0, never a 0 ms that was not measured.
    Every group also serves its histogram's `counts` over `bounds_ms`, so
    swarm-api merges the replicas' percentiles exactly (the owner's pick,
    3 November 2026; graph_cache.py `merge_summaries`);
  * `answers_hot` counts this tenant's result-cache entries for the ticket's
    current digest;
  * `shards_hot` and `shards` are over the blobs of the ticket's CURRENT
    digest's manifest, read from this replica's manifest cache. A replica
    that does not hold that manifest has never answered for this digest since
    it was last opened; it does not know the blobs, and says so with
    `shards_hot: null` and an empty map rather than a count it cannot make;
  * `window_hours` is how much of the 24 h this replica saw: everything here is
    memory, and a restart starts it at zero (§5.2 labels a short window).

TENANT-SCOPED (invariant 9, §5.4). Every per-repository figure is read under
the ticket's tenant: the counters, the result keys, the manifest key and the
shard keys all lead with `tenant_id`, and nothing here enumerates another
tenant's. Nothing returned is a key, a repository id, a digest or a name of
any other tenant; the ticket's own ids are not echoed either.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any, Callable

from agent_worker.graphmcp.snapshot import ALL_LAYERS, Snapshot, SnapshotError, blob_hexdigest

from .cache import HOUR_SECONDS, ManifestCache, ResultCache, ShardCache
from .ticket import StatsTicket
from .warm import Warmer

#: The stats port. Never `/mcp` here, never stats on server.PORT (§5.3).
STATS_PORT = 8081
STATS_PATH = "/v1/stats"
STATS_LOCAL_PATH = "/v1/stats/local"
#: Each peer is read with this timeout (§5.3), all of them at once.
PEER_TIMEOUT_SECONDS = 2.0
#: The headless Service (kubernetes/swarm-system/swarm-graph.yaml): its name
#: resolves to every ready pod's address, which is how peers find each other.
PEERS_HOST = "swarm-graph.swarm-system.svc.cluster.local"
PEERS_HOST_ENV = "SWARM_GRAPH_PEERS_HOST"
#: A peer's document is a few KB, or about 100 bytes a blob with the shard
#: map; anything past this is not one.
MAX_PEER_BYTES = 16 * 1024 * 1024
#: The answer outcomes counted; `none` is not one (§3.2).
OUTCOMES = ("hit", "carried", "miss")
#: Whether carry forward (§2.3 b) is built: it is (cache.ResultCache's carry
#: key, the owner's 2026-11-03 request), so every bucket carries `carried`.
CARRY_FORWARD = True
HOURS = 24
#: The served-ms histogram's upper bounds, exponential from 1 ms like the
#: log-based metric's buckets (§3.1); counts[i] is answers with
#: bounds[i-1] < ms <= bounds[i], and the last count is everything above.
MS_BOUNDS = tuple(2 ** i for i in range(17))
#: `answer_latency`'s finer bounds, so a percentile is read to within about
#: 25%: 1 ms to a minute. A percentile is the bound of the bucket it falls
#: in, never above the slowest answer seen.
LATENCY_BOUNDS_MS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100,
                     120, 150, 200, 250, 300, 400, 500, 600, 800, 1000, 1200, 1500, 2000,
                     2500, 3000, 4000, 5000, 6000, 7000, 8000, 10000, 15000, 20000, 30000,
                     60000)
#: An answer's outcome for `answer_latency`: the cache's word, `partial` for
#: one cut at the deadline, `uncached` for a question that has no cache key.
LATENCY_OUTCOMES = ("hit", "carried", "miss", "partial", "uncached")
#: Cold: the request read the manifest or a shard from storage; warm: memory only.
TEMPERATURES = ("warm", "cold")
PERCENTILES = (("p50_ms", 0.50), ("p95_ms", 0.95), ("p99_ms", 0.99))
NOT_READ = "not_read"
NOT_READ_REASONS = ("timeout", "refused", "disabled", "ticket_refused", "bad_response")


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bucket_of(ms: int, bounds: tuple[int, ...] = MS_BOUNDS) -> int:
    for index, bound in enumerate(bounds):
        if ms <= bound:
            return index
    return len(bounds)


def _percentiles(counts: list[int], slowest: int) -> dict[str, Any]:
    """`count`, `max_ms`, each PERCENTILES' bound over one latency histogram,
    and the histogram's `counts` (len(LATENCY_BOUNDS_MS) + 1), so swarm-api
    can merge replicas exactly rather than take the slowest one's figure."""
    total = sum(counts)
    out: dict[str, Any] = {"count": total}
    for name, q in PERCENTILES:
        if not total:
            out[name] = None
            continue
        rank = max(1, math.ceil(round(q * total, 6)))
        seen = 0
        for index, n in enumerate(counts):
            seen += n
            if seen >= rank:
                bound = LATENCY_BOUNDS_MS[index] if index < len(LATENCY_BOUNDS_MS) else slowest
                out[name] = min(bound, slowest)
                break
    out["max_ms"] = slowest if total else None
    out["counts"] = list(counts)
    return out


class AnswerCounters:
    """Answer outcomes and served milliseconds per `(tenant_id, repo_id)`, in
    hourly buckets kept HOURS. A repository with nothing in the window is
    dropped at the next hourly sweep."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
        self._swept = 0

    def record(self, tenant_id: str, repo_id: str, cache: str | None, ms: int, *,
               now: float, partial: bool = False, cold: bool = False) -> None:
        """One answer: its cache outcome (hit, carried or miss; `none` is
        not one), and its latency by outcome and temperature."""
        outcome = "partial" if partial else ("uncached" if cache == "none" else cache)
        if outcome not in LATENCY_OUTCOMES:
            return
        ms = max(0, ms)
        hour = int(now // HOUR_SECONDS)
        with self._lock:
            hours = self._by.setdefault((tenant_id, repo_id), {})
            bucket = hours.get(hour)
            if bucket is None:
                bucket = hours[hour] = {"hit": 0, "carried": 0, "miss": 0,
                                        "ms": [0] * (len(MS_BOUNDS) + 1), "latency": {}}
            if cache in OUTCOMES:
                bucket[cache] += 1
                bucket["ms"][_bucket_of(ms)] += 1
            cell = (outcome, "cold" if cold else "warm")
            counts, slowest = bucket["latency"].get(cell) or (
                [0] * (len(LATENCY_BOUNDS_MS) + 1), 0)
            counts[_bucket_of(ms, LATENCY_BOUNDS_MS)] += 1
            bucket["latency"][cell] = (counts, max(slowest, ms))
            if hour != self._swept:
                self._swept = hour
                oldest = hour - HOURS + 1
                for key in list(self._by):
                    kept = {h: b for h, b in self._by[key].items() if h >= oldest}
                    if kept:
                        self._by[key] = kept
                    else:
                        del self._by[key]

    def view(self, tenant_id: str, repo_id: str, *, now: float) -> dict[str, Any]:
        """`outcomes` (HOURS buckets, oldest first) and `served_ms` for one
        tenant's repository."""
        hour = int(now // HOUR_SECONDS)
        names = OUTCOMES if CARRY_FORWARD else tuple(o for o in OUTCOMES if o != "carried")
        counts = [0] * (len(MS_BOUNDS) + 1)
        outcomes = []
        cells: dict[tuple[str, str], tuple[list[int], int]] = {}
        with self._lock:
            hours = self._by.get((tenant_id, repo_id), {})
            for h in range(hour - HOURS + 1, hour + 1):
                bucket = hours.get(h)
                row: dict[str, Any] = {"hour": iso(h * HOUR_SECONDS)}
                row.update({name: (bucket[name] if bucket else 0) for name in names})
                outcomes.append(row)
                if bucket:
                    counts = [a + b for a, b in zip(counts, bucket["ms"])]
                    for cell, (seen, slowest) in bucket["latency"].items():
                        held, top = cells.get(cell) or ([0] * (len(LATENCY_BOUNDS_MS) + 1), 0)
                        cells[cell] = ([a + b for a, b in zip(held, seen)], max(top, slowest))
        return {"outcomes": outcomes,
                "served_ms": {"bounds_ms": list(MS_BOUNDS), "counts": counts},
                "answer_latency": _latency(cells)}


def _latency(cells: dict[tuple[str, str], tuple[list[int], int]]) -> dict[str, Any]:
    """`answer_latency`: percentiles over every answer in the window, then by
    outcome, by temperature, and by both."""
    width = len(LATENCY_BOUNDS_MS) + 1

    def over(chosen: list[tuple[list[int], int]]) -> dict[str, Any]:
        counts = [0] * width
        slowest = 0
        for seen, top in chosen:
            counts = [a + b for a, b in zip(counts, seen)]
            slowest = max(slowest, top)
        return _percentiles(counts, slowest)

    return {
        "window_hours": HOURS, "bounds_ms": list(LATENCY_BOUNDS_MS),
        **over(list(cells.values())),
        "by_outcome": {o: over([v for (co, _t), v in cells.items() if co == o])
                       for o in LATENCY_OUTCOMES},
        "by_temperature": {t: over([v for (_o, ct), v in cells.items() if ct == t])
                           for t in TEMPERATURES},
        "cells": {f"{o}/{t}": _percentiles(*cells[(o, t)])
                  for o in LATENCY_OUTCOMES for t in TEMPERATURES if (o, t) in cells},
    }


def manifest_blobs(snap: Snapshot) -> list[str]:
    """Every blob a manifest names, by its sha256 hex, each once."""
    seen: dict[str, None] = {}
    for layer in ALL_LAYERS:
        for key in snap.keys(layer):
            entry = snap.entry(layer, key)
            if entry is None:
                continue
            try:
                seen.setdefault(blob_hexdigest(entry), None)
            except SnapshotError:
                continue
    return list(seen)


def local_document(ticket: StatsTicket, *, replica: str, started_at: float, now: float,
                   results: ResultCache, shards: ShardCache, manifests: ManifestCache,
                   warmer: Warmer, counters: AnswerCounters,
                   with_shards: bool) -> dict[str, Any]:
    """This replica's figures for the ticket's tenant and repository (the
    module docstring's shape)."""
    tenant = ticket.tenant_id
    document: dict[str, Any] = {
        "replica": replica, "state": "read", "started_at": iso(started_at),
        "window_hours": max(1, min(HOURS, int((now - started_at) // HOUR_SECONDS) + 1)),
        "result_bytes": results.bytes, "result_bound": results.bound,
        "shard_bytes": shards.bytes, "shard_bound": shards.bound,
        **counters.view(tenant, ticket.repo_id, now=now),
        "answers_hot": results.held_for(tenant, ticket.manifest_digest),
    }
    snap = manifests.peek((tenant, ticket.repo_id, ticket.manifest_digest))
    blobs = manifest_blobs(snap) if snap is not None else []
    document["shards_hot"] = sum(1 for b in blobs if shards.hot(tenant, b)) \
        if snap is not None else None
    last = warmer.last_warm(tenant, ticket.repo_id)
    document["last_warm"] = None if last is None else {
        "digest": last.manifest_digest, "ms": last.ms, "shards": last.shards,
        "replayed": last.replayed, "cause": last.cause, "at": iso(last.at)}
    if with_shards:
        rows = {}
        for blob in blobs:
            hits, last_read = shards.reads(tenant, blob)
            rows[blob] = {"hot": shards.hot(tenant, blob), "hits_24h": hits,
                          "last_read": None if last_read is None else iso(last_read)}
        document["shards"] = rows
    return document


# --- the peers ---------------------------------------------------------------------

def replica_name() -> str:
    """The pod's name: Kubernetes sets HOSTNAME to it."""
    return os.environ.get("HOSTNAME") or socket.gethostname()


def own_addresses() -> set[str]:
    """This pod's addresses, so the fan-out does not read itself twice."""
    mine = {os.environ.get("POD_IP", "")} - {""}
    try:
        mine.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return mine


class Peers:
    """Every other replica's `host:port`, from the headless Service's
    addresses; None when the name does not resolve."""

    def __init__(self, host: str | None = None, port: int = STATS_PORT,
                 own: Callable[[], set[str]] = own_addresses) -> None:
        self.host = host or os.environ.get(PEERS_HOST_ENV) or PEERS_HOST
        self.port = port
        self.own = own

    def __call__(self) -> list[str] | None:
        try:
            infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        except OSError:
            return None
        mine = self.own()
        peers: list[str] = []
        for info in infos:
            address = str(info[4][0])
            at = f"[{address}]:{self.port}" if ":" in address else f"{address}:{self.port}"
            if address not in mine and at not in peers:
                peers.append(at)
        return peers


def not_read(address: str, reason: str) -> dict[str, str]:
    return {"replica": address, "state": NOT_READ, "reason": reason}


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _refusal_code(body: bytes) -> str | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed.get("error") if isinstance(parsed, dict) else None


def read_peer(address: str, ticket: str, *, with_shards: bool, timeout: float) -> dict[str, Any]:
    """One peer's local document, or its `not_read` entry. Never raises."""
    url = f"http://{address}{STATS_LOCAL_PATH}" + ("?shards=1" if with_shards else "")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {ticket}"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(MAX_PEER_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = _refusal_code(exc.read(4096))
        if exc.code == 401 and code == "ticket_refused":
            return not_read(address, "ticket_refused")
        if exc.code == 503 and code == "disabled":
            return not_read(address, "disabled")
        return not_read(address, "bad_response")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return not_read(address, "timeout")
        return not_read(address, "refused")
    except TimeoutError:
        return not_read(address, "timeout")
    except ConnectionRefusedError:
        return not_read(address, "refused")
    except (OSError, http.client.HTTPException, ValueError):
        return not_read(address, "bad_response")
    if len(body) > MAX_PEER_BYTES:
        return not_read(address, "bad_response")
    try:
        document = json.loads(body)
    except ValueError:
        return not_read(address, "bad_response")
    if not (isinstance(document, dict) and document.get("state") == "read"
            and isinstance(document.get("replica"), str) and "outcomes" in document):
        return not_read(address, "bad_response")
    return document


def read_peers(peers: list[str], ticket: str, *, with_shards: bool,
               timeout: float) -> list[dict[str, Any]]:
    """Every peer read at once, in `peers` order. A read still running past
    its timeout (a peer trickling bytes) is `timeout`, not awaited."""
    if not peers:
        return []
    pool = ThreadPoolExecutor(max_workers=min(len(peers), 16),
                              thread_name_prefix="swarm-graph-stats")
    try:
        futures = [pool.submit(read_peer, p, ticket, with_shards=with_shards, timeout=timeout)
                   for p in peers]
        deadline = time.monotonic() + timeout + 0.5
        out = []
        for address, future in zip(peers, futures):
            try:
                out.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
            except FutureTimeout:
                out.append(not_read(address, "timeout"))
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "CARRY_FORWARD",
    "HOURS",
    "LATENCY_BOUNDS_MS",
    "LATENCY_OUTCOMES",
    "MS_BOUNDS",
    "NOT_READ_REASONS",
    "OUTCOMES",
    "PEERS_HOST",
    "PEER_TIMEOUT_SECONDS",
    "STATS_LOCAL_PATH",
    "STATS_PATH",
    "STATS_PORT",
    "AnswerCounters",
    "Peers",
    "local_document",
    "manifest_blobs",
    "own_addresses",
    "read_peer",
    "read_peers",
    "replica_name",
]
