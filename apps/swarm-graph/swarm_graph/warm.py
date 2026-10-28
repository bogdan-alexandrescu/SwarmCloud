"""Warming on first sight (docs/design/graph-service.md §5.4; owner decision Q5).

DECIDED 2026-10-15 BY THE OWNER: warm on first sight, with no new IAM. The
first ticket naming a manifest digest this replica has not seen starts a
background fetch of the hot layers (HOT_LAYERS), bounded by the stored bytes
a staged start would have downloaded (`graphstage.MAX_SNAPSHOT_BYTES`). The
query that caused it is not held back: it reads what it needs at once, and the
shard cache's single-flight load means it and the warmer never read one blob
twice. Only the first query after a promotion, per replica, pays GCS reads.

NOT BUILT: Pub/Sub warming. It needed a `pubsub.subscriber` grant beyond the
owner's "graph/** only", and the owner declined it. Revisit it only if the
first-query cost after a promotion measures as a problem: every warm writes a
`graph_warmed` line with its shards, bytes and milliseconds, and every answer
a `graph_answer` line with `cache: hit|miss`.

The warmer reads through the snapshot's own `TenantSource`, so what it warms is
keyed by the ticket's tenant like everything else (§3.3 step 5).

THE LAST WARM (docs/design/graph-answer-cache.md §5.3). Each finished warm is
also remembered per `(tenant_id, repo_id)`, with its digest, milliseconds and
shards, for the console's "Last promotion warm-up" tile: `last_warm` returns it
to a stats read of that same tenant only. `replayed` is None: this image
replays no questions (§2.3 a, pre-warming, is a later lane), and None says
"not built" where 0 would say "replayed nothing".
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from agent_worker.graphmcp.snapshot import Snapshot, SnapshotError
from agent_worker.graphstage import MAX_SNAPSHOT_BYTES

#: §5.4's hot layers, in the order they are read.
HOT_LAYERS = ("symbols", "callers", "files", "terms")
#: A warm reads at most this many STORED bytes: what one staged start
#: downloads at most, so warming never costs more than staging did.
WARM_BYTES = MAX_SNAPSHOT_BYTES
#: Warms running at once per replica; the rest wait their turn.
WARM_WORKERS = 2
#: Digests remembered as seen; the oldest is forgotten first, and a forgotten
#: one is warmed again on its next sight (its shards are likely still cached).
SEEN_LIMIT = 4096


@dataclass(frozen=True)
class Warmed:
    shards: int
    stored_bytes: int
    cause: str


@dataclass(frozen=True)
class LastWarm:
    """One repository's most recent finished warm on this replica."""

    manifest_digest: str
    ms: int
    shards: int
    cause: str
    #: When it finished, epoch seconds.
    at: float
    #: Questions replayed (§2.3 a); None until pre-warming is built.
    replayed: int | None = None


def warm(snapshot: Snapshot, *, budget: int = WARM_BYTES) -> Warmed:
    """Read the hot layers' shards through the snapshot's source, until the
    budget of stored bytes is spent. Stops at the first unreadable shard."""
    shards = 0
    spent = 0
    for layer in HOT_LAYERS:
        for key in snapshot.keys(layer):
            entry = snapshot.entry(layer, key) or {}
            size = entry.get("bytes")
            size = size if isinstance(size, int) and size >= 0 else 0
            if spent + size > budget:
                return Warmed(shards, spent, "budget")
            try:
                snapshot.shard(layer, key)
            except SnapshotError as exc:
                return Warmed(shards, spent, type(exc).__name__)
            shards += 1
            spent += size
    return Warmed(shards, spent, "done")


class Warmer:
    """Starts one warm per (tenant, repository, digest) the first time it is seen."""

    def __init__(self, *, budget: int = WARM_BYTES, workers: int = WARM_WORKERS,
                 on_done: Callable[[str, str, Warmed, int], None] | None = None,
                 run: Callable[[Callable[[], None]], None] | None = None) -> None:
        self._budget = budget
        self._lock = threading.Lock()
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._last: OrderedDict[tuple[str, str], LastWarm] = OrderedDict()
        self._on_done = on_done
        self._pool: ThreadPoolExecutor | None = None
        if run is None:
            self._pool = ThreadPoolExecutor(max_workers=workers,
                                            thread_name_prefix="swarm-graph-warm")
            pool = self._pool

            def run(job: Callable[[], None]) -> None:
                pool.submit(job)
        self._run = run

    def saw(self, tenant_id: str, repo_id: str, manifest_digest: str,
            snapshot: Snapshot) -> bool:
        """Note a sighting; True when it was the first and a warm was started."""
        key = (tenant_id, repo_id, manifest_digest)
        with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                return False
            self._seen[key] = None
            while len(self._seen) > SEEN_LIMIT:
                self._seen.popitem(last=False)

        def job() -> None:
            started = time.monotonic()
            try:
                result = warm(snapshot, budget=self._budget)
            except Exception as exc:  # noqa: BLE001 - a warm never takes the replica down
                result = Warmed(0, 0, type(exc).__name__)
            ms = int((time.monotonic() - started) * 1000)
            self._remember(tenant_id, repo_id,
                           LastWarm(manifest_digest=manifest_digest, ms=ms, shards=result.shards,
                                    cause=result.cause, at=time.time()))
            if self._on_done is not None:
                self._on_done(tenant_id, repo_id, result, ms)

        self._run(job)
        return True

    def _remember(self, tenant_id: str, repo_id: str, last: LastWarm) -> None:
        with self._lock:
            self._last[(tenant_id, repo_id)] = last
            self._last.move_to_end((tenant_id, repo_id))
            while len(self._last) > SEEN_LIMIT:
                self._last.popitem(last=False)

    def last_warm(self, tenant_id: str, repo_id: str) -> LastWarm | None:
        """This tenant's repository's last finished warm here, or None."""
        with self._lock:
            return self._last.get((tenant_id, repo_id))

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["HOT_LAYERS", "SEEN_LIMIT", "WARM_BYTES", "LastWarm", "Warmed", "Warmer", "warm"]
