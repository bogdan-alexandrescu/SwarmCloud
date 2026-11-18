"""Carry forward: reuse an answer whose inputs did not change
(docs/design/graph-answer-cache.md §2.3 b, §2.5).

An answer is a function of what its handler read from the `Snapshot`, and a
handler reads the index only through it (graph-service.md §5.2,
"Determinism"). So the service hands `tools.compute` a `Recorder`: the same
snapshot, whose every manifest-level read is written into a READ SET:

  * `entry(layer, key)` -- and so every `shard(layer, key)` and every lookup
    built on one -- as `("entry", layer, key)` -> the digest of the manifest's
    entry, or ABSENT. Absent is recorded too: "no such symbol" depends on the
    key being missing, and a merge that adds the symbol must miss;
  * `keys(layer)`, a listing, as `("keys", layer, "")` -> the digest of that
    layer's whole key -> entry table, so a listing misses when any key of the
    layer moved;
  * `has_layer`, `has_terms` and `term_stats` as `("has", layer, "")` and
    `("term_stats", "", "")`.

An entry's digest covers the whole entry (`blob`, `records`, `bytes`), not
just its blob, because `index` answers with the counts.

`unchanged(reads, snap)` re-resolves every read against another digest's
snapshot, which is in memory (the manifest cache): no GCS read and no decode.
Equal reads mean equal output, the same determinism an exact hit relies on
(§2.1), so a candidate that passes is served as that digest's answer; any
difference is a miss and the answer is computed.

A `Recorder` is a per-request VIEW of the cached snapshot: it shares the
manifest tables and the blob source (or takes a request's own source, the
deadline's) and never writes to the cached object. Its community memo starts
empty, so `community_of` always reads, and records, the communities shard.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_worker.graphmcp.snapshot import BlobSource, Snapshot

ReadKey = tuple[str, str, str]
ReadSet = dict[ReadKey, str]

ABSENT = "-"
#: What one recorded read costs against the result cache's bound, beyond its
#: strings: a dictionary slot and a tuple (§2.4 measured about 70 bytes a read).
READ_OVERHEAD_BYTES = 64
_DIGESTS = "_swarm_carry_digests"


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode("ascii")).hexdigest()[:32]


def _memo(snap: Snapshot) -> dict[ReadKey, str]:
    """The per-snapshot memo of layer and manifest-field digests. It lives on
    the cached snapshot (a manifest's tables never change), so a check against
    one digest hashes each listed layer once, not once per candidate."""
    memo = snap.__dict__.get(_DIGESTS)
    if memo is None:
        memo = snap.__dict__.setdefault(_DIGESTS, {})
    return memo


def value_of(snap: Snapshot, read: ReadKey) -> str:
    """What `snap` answers for one recorded read."""
    kind, layer, key = read
    if kind == "entry":
        entry = Snapshot.entry(snap, layer, key)
        return ABSENT if entry is None else _digest(entry)
    if kind == "has":
        return "1" if Snapshot.has_layer(snap, layer) else "0"
    memo = _memo(snap)
    held = memo.get(read)
    if held is None:
        if kind == "keys":
            table = {k: Snapshot.entry(snap, layer, k) for k in Snapshot.keys(snap, layer)}
            held = _digest(table)
        elif kind == "term_stats":
            held = _digest(snap.manifest.get("term_stats") or {})
        else:
            raise ValueError(f"no read of kind {kind!r}")
        memo[read] = held
    return held


def unchanged(reads: ReadSet, snap: Snapshot) -> bool:
    """Whether every read resolves in `snap` exactly as it did when recorded."""
    return all(value_of(snap, read) == value for read, value in reads.items())


def read_set_bytes(reads: ReadSet) -> int:
    return sum(len(a) + len(b) + len(c) + len(v) + READ_OVERHEAD_BYTES
               for (a, b, c), v in reads.items())


class Recorder(Snapshot):
    """A view of a snapshot that records its reads (the module docstring)."""

    reads: ReadSet

    @classmethod
    def over(cls, snap: Snapshot, source: BlobSource | None = None) -> "Recorder":
        _memo(snap)
        view = cls.__new__(cls)
        view.__dict__.update(snap.__dict__)
        view._communities = None
        if source is not None:
            view.source = source
        view.reads = {}
        return view

    def _record(self, read: ReadKey) -> None:
        if read not in self.reads:
            self.reads[read] = value_of(self, read)

    def keys(self, layer: str) -> list[str]:
        self._record(("keys", layer, ""))
        return super().keys(layer)

    def entry(self, layer: str, key: str) -> dict | None:
        self._record(("entry", layer, key))
        return super().entry(layer, key)

    def has_layer(self, layer: str) -> bool:
        self._record(("has", layer, ""))
        return super().has_layer(layer)

    @property
    def has_terms(self) -> bool:
        self._record(("has", "terms", ""))
        return bool(self._tables.get("terms"))

    @property
    def term_stats(self) -> dict:
        self._record(("term_stats", "", ""))
        return dict(self.manifest.get("term_stats") or {})


__all__ = [
    "ABSENT",
    "READ_OVERHEAD_BYTES",
    "ReadSet",
    "Recorder",
    "read_set_bytes",
    "unchanged",
    "value_of",
]
