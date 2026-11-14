"""The console's Cache section, swarm-api's half (docs/design/graph-answer-cache.md
§5.2-§5.5; lane CACHE-3-ROUTE, part of #1835).

    GET /v1/repositories/{repo_id}/graph-cache[?shards=1&page=N]
    GET /v1/repositories/{repo_id}/graph-delivery[?days=7]   (#4030, `graph_delivery`)

One read of the shared graph service's answer cache for one of the caller's own
registered repositories: the repository facts (§5.2's four tiles), one panel per
swarm-graph replica, and, with `shards=1`, a page of the shard list.

WHY EACH RULE:

  * THE TENANT IS THE CALLER'S TOKEN'S (invariant 10). `tenant_scope` derives
    it; the request names only a `repo_id`, and a repo_id the tenant has not
    registered is the 404 every repository route answers
    (`Repositories.get`), one sentence for "no such" and "another tenant's".
  * THE DIGESTS COME FROM FIRESTORE, NEVER FROM THE REQUEST OR GCS. The current
    graph is `index.current_sha`'s version's `graph_digest`, which only
    promotion writes (graph_ticket.py says why); the previous one is the
    version the current one was built from (its `base_sha`), or else the newest
    version recorded before it that carries a graph.
  * ONE STATS TICKET, ONE CALL (§5.3). `mint_stats_ticket` MACs the tenant, the
    repository and both digests under the stats domain, never the attempt
    ticket's, for 60 s, and the route calls `settings.graph_stats_url`
    (SWARM_GRAPH_STATS_URL) + `/v1/stats`
    once with it as the bearer credential, as the pod's shim presents a graph
    ticket to `/mcp`. The receiving replica fans out to its peers (lane 1).
  * SWARM-GRAPH NOT ANSWERING IS A 200, NOT AN ERROR (§5.3). Refused, a 3 s
    timeout, a non-200, a body that is not the document, or no
    `graph_stats_url` at all (SWARM_GRAPH_STATS_URL unset: the stats are not
    served in this deployment) is
    `replicas: []` with `stats: {state: "not_read", reason}`. The latency tile,
    read from Firestore, still shows, and nothing is shown as zero (§5.4).
  * NOTHING OF ANOTHER TENANT LEAVES (§5.4). The service already filters on the
    ticket's tenant; this route also passes on only the fields §5.3 names, a
    warm record only for one of the ticket's two digests, and a blob's marks
    only for a blob the current manifest names. A replica's two MiB totals are
    numbers across every tenant and are shown because a full cache is why this
    repository misses (§5.2).
  * THE SERVED ANSWER LATENCY IS MERGED, NEVER INVENTED (lane
    graph-cache-panel-latency, part of #2688). Each replica serves its own
    `answer_latency` (swarm_graph/stats.py): count, p50, p95, p99 and max over
    its window, by outcome, by temperature and by both. `facts.answer_latency`
    sums the counts and takes the slowest max. Each replica also serves its
    histogram (`bounds_ms` and each group's `counts`), so when every replica
    that answered served counts over the same bounds the histograms add and
    each percentile is read off the sum, exactly as one replica reads its own
    (`exact: true`; the owner's pick, 3 November 2026). When one did not (an
    older swarm-graph during a roll-out) a percentile is taken as it is when
    one replica answered and as the slowest replica's otherwise: never below
    the merged figure, so it is an upper bound and says so (`exact: false`).
    The counts stay in this route; the console gets the figures. A replica that did not answer, or answered without
    the figure, is named in `not_read`, never counted as zero answers; with no
    replica's figure at all the fact is `{state: "not_read", reason}`. It sits
    beside `latency`, the indexer's uncached measurement, which is unchanged.
  * THE MANIFEST COMPARISON IS CACHED PER DIGEST PAIR. A promoted graph is
    immutable per digest (repograph.py), so the comparison of two is too; it is
    kept in `RepoGraph.view`'s bounded cache under a key that begins with the
    tenant and repository and names both digests. The manifests are still
    opened, and so digest-checked, on every request, as the graph routes do.

Read-only: nothing here writes Firestore or the bucket, and nothing logs or
returns the ticket or the key.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from ..deps import AppContext, get_context, tenant_scope
from ..errors import ApiError
from ..graph_ticket import STATS_TICKET_SECONDS, mint_stats_ticket
from ..repoindex import VERSIONS_COLLECTION, RepoIndex
from ..repograph import RepoGraph
from ..repositories import COLLECTION as REPOSITORIES

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/repositories", tags=["repositories"])

#: §5.3: the internal load balancer refusing or not answering in 3 s is
#: `not_read`, so the section never waits longer than its own refresh allows.
STATS_TIMEOUT_SECONDS = 3.0
#: The service's document is a few KB per replica plus one mark per blob of
#: one repository's digest; 8 MiB bounds the read, not the use.
MAX_STATS_BYTES = 8 * 1024 * 1024
#: §5.2: the shard list is read a page of 100 rows at a time.
SHARD_PAGE_ROWS = 100
#: §5.2: the facts cover 24 h, or less when a replica restarted inside it.
WINDOW_HOURS = 24

NOT_READ = "not_read"
#: `stats.reason` values this route gives; lane 1's per-peer reasons are
#: `timeout`, `refused`, `disabled`, `ticket_refused` and `bad_response`.
NOT_CONFIGURED = "not_configured"
NO_KEY = "no_ticket_key"
NO_INDEX = "no_index"
NO_GRAPH = "no_graph"
#: `facts.answer_latency.reason` when swarm-graph answered: no replica was
#: read, or every replica read served no `answer_latency`.
NO_REPLICA_READ = "no_replica_read"
NOT_SERVED = "not_served"
TIMEOUT = "timeout"
REFUSED = "refused"
TICKET_REFUSED = "ticket_refused"
BAD_RESPONSE = "bad_response"

#: The replica fields §5.3 names, passed on as they are when they are numbers.
_REPLICA_NUMBERS = ("result_bytes", "result_bound", "shard_bytes", "shard_bound")
_PEER_REASONS = frozenset({TIMEOUT, REFUSED, "disabled", TICKET_REFUSED, BAD_RESPONSE})
#: `answer_latency`'s groups as swarm-graph serves them (stats.LATENCY_OUTCOMES,
#: stats.TEMPERATURES); a name outside these is not passed on.
LATENCY_OUTCOMES = ("hit", "carried", "miss", "partial", "uncached")
TEMPERATURES = ("warm", "cold")
_PERCENTILES = ("p50_ms", "p95_ms", "p99_ms")

Send = Callable[[str, Mapping[str, str], float], "tuple[int, bytes]"]


def _urllib_send(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET", headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - our own internal URL
            return response.status, response.read(MAX_STATS_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def stats_url(settings: Any) -> str:
    """`settings.graph_stats_url` (ApiSettings, from SWARM_GRAPH_STATS_URL), or
    "" when this deployment has none: then the section is `not_read`.

    The setting is the one source; nothing here reads the environment.
    """
    url = str(getattr(settings, "graph_stats_url", "") or "").strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else ""


class StatsReader:
    """One `GET <url>/v1/stats` with a stats ticket; `send` is the transport."""

    def __init__(self, url: str, *, send: Send = _urllib_send,
                 timeout: float = STATS_TIMEOUT_SECONDS) -> None:
        self.url = url
        self._send = send
        self._timeout = timeout

    def read(self, ticket: str, *, shards: bool) -> tuple[dict[str, Any] | None, str | None]:
        """(the document, None) or (None, the reason it was not read)."""
        if not self.url:
            return None, NOT_CONFIGURED
        url = f"{self.url}/v1/stats" + ("?shards=1" if shards else "")
        try:
            status, raw = self._send(url, {"Authorization": f"Bearer {ticket}",
                                           "Accept": "application/json"}, self._timeout)
        except (TimeoutError, socket.timeout):
            return None, TIMEOUT
        except urllib.error.URLError as exc:
            timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
            return None, TIMEOUT if timed_out else REFUSED
        except OSError:
            return None, REFUSED
        if status == 401:
            return None, TICKET_REFUSED
        if status != 200 or len(raw) > MAX_STATS_BYTES:
            return None, BAD_RESPONSE
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, BAD_RESPONSE
        if not isinstance(document, dict) or not isinstance(document.get("replicas"), list):
            return None, BAD_RESPONSE
        return document, None


def stats_reader(ctx: AppContext = Depends(get_context)) -> StatsReader:
    return StatsReader(stats_url(ctx.settings))


# --------------------------------------------------------------------------
# reading the registration's two versions
# --------------------------------------------------------------------------

def _versions(ctx: AppContext, repo_id: str) -> Any:
    return ctx.db.collection(REPOSITORIES).document(repo_id).collection(VERSIONS_COLLECTION)


def _recorded(row: Mapping[str, Any]) -> datetime:
    value = row.get("recorded_at")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def previous_version(rows: Sequence[Mapping[str, Any]], current: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
    """The version the current one replaced, for "changed by the last merge".

    The one it was built from (`base_sha`, an incremental run's previous
    index) when that is kept and has a graph; otherwise the newest version
    recorded before it that has one. None when there is none: a first index
    has nothing to compare with.
    """
    commit = current.get("commit_sha")
    graphed = [dict(r) for r in rows if r.get("graph_digest") and r.get("commit_sha")
               and r.get("commit_sha") != commit]
    base = current.get("base_sha")
    for row in graphed:
        if base and row.get("commit_sha") == base:
            return row
    before = [r for r in graphed if _recorded(r) < _recorded(current)]
    before.sort(key=_recorded, reverse=True)
    return before[0] if before else None


# --------------------------------------------------------------------------
# the manifest comparison (§5.2's "last merge" column)
# --------------------------------------------------------------------------

def compare_manifests(current: Mapping[str, Any], previous: Mapping[str, Any] | None
                      ) -> list[dict[str, Any]]:
    """One row per shard of `current`: `changed` when its key's blob differs
    from `previous`'s or the key is new, `unchanged` when it is the same blob,
    and None for every row when there is no previous manifest."""
    before = (previous or {}).get("shards") or {}
    rows = []
    for layer, modules in sorted((current.get("shards") or {}).items()):
        prior = before.get(layer) or {}
        for module, entry in sorted((modules or {}).items()):
            blob = str(entry.get("blob") or "")
            if previous is None:
                last_merge = None
            else:
                was = (prior.get(module) or {}).get("blob")
                last_merge = "unchanged" if was == blob else "changed"
            raw = entry.get("raw_bytes")
            rows.append({"layer": layer, "module": module, "blob": blob,
                         "raw_bytes": raw if isinstance(raw, int) else None,
                         "last_merge": last_merge})
    return rows


def _hex(blob: Any) -> str:
    text = str(blob or "")
    return text.split(":", 1)[1] if text.startswith("sha256:") else text


# --------------------------------------------------------------------------
# what the replicas said
# --------------------------------------------------------------------------

def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 \
        else None


def _str(value: Any, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _outcomes(value: Any) -> dict[str, int | None]:
    """The 24 hourly buckets summed: hit, carried (None when no bucket counts
    it: carry forward is not built, §5.2), miss."""
    hit = miss = 0
    carried: int | None = None
    for bucket in value if isinstance(value, list) else []:
        if not isinstance(bucket, dict):
            continue
        hit += _int(bucket.get("hit")) or 0
        miss += _int(bucket.get("miss")) or 0
        if _int(bucket.get("carried")) is not None:
            carried = (carried or 0) + bucket["carried"]
    return {"hit": hit, "carried": carried, "miss": miss}


def _histogram(value: Any) -> dict[float, int]:
    """The served-ms histogram as `{upper ms: count}`: from swarm-graph's
    `{"bounds_ms": [...], "counts": [len + 1]}` (the last count is above the
    last bound, kept at that bound) or from `{"<upper ms>": count}`."""
    out: dict[float, int] = {}
    if isinstance(value, dict) and isinstance(value.get("bounds_ms"), list) \
            and isinstance(value.get("counts"), list):
        bounds = [b for b in value["bounds_ms"] if isinstance(b, (int, float))
                  and not isinstance(b, bool)]
        if bounds and len(bounds) == len(value["bounds_ms"]):
            for index, count in enumerate(value["counts"]):
                bound = float(bounds[min(index, len(bounds) - 1)])
                if _int(count):
                    out[bound] = out.get(bound, 0) + count
        return out
    for upper, count in (value.items() if isinstance(value, dict) else []):
        try:
            bound = float(upper)
        except (TypeError, ValueError):
            continue
        if _int(count):
            out[bound] = out.get(bound, 0) + count
    return out


def p95(histogram: Mapping[float, int]) -> float | None:
    total = sum(histogram.values())
    if not total:
        return None
    seen = 0
    for upper in sorted(histogram):
        seen += histogram[upper]
        if seen >= 0.95 * total:
            return upper
    return max(histogram)


def _ms(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value >= 0 and value == value and value != float("inf") else None


def _bounds(value: Any) -> tuple[float, ...] | None:
    """A replica's latency `bounds_ms`: positive numbers, strictly rising."""
    if not isinstance(value, list) or not value:
        return None
    bounds = [_ms(b) for b in value]
    if any(b is None or b <= 0 for b in bounds) \
            or any(a >= b for a, b in zip(bounds, bounds[1:])):
        return None
    return tuple(bounds)  # type: ignore[arg-type]


def _counts(value: Any, count: int, bounds: tuple[float, ...] | None) -> list[int] | None:
    """A group's histogram `counts`, trusted only as len(bounds) + 1 whole
    numbers that add up to the group's own `count`; None otherwise."""
    if bounds is None or not isinstance(value, list) or len(value) != len(bounds) + 1:
        return None
    counts = [_int(n) for n in value]
    if any(n is None for n in counts) or sum(counts) != count:  # type: ignore[arg-type]
        return None
    return counts  # type: ignore[return-value]


def _summary(value: Any, bounds: tuple[float, ...] | None = None) -> dict[str, Any] | None:
    """One `answer_latency` group (`count`, the percentiles, `max_ms`) held to
    numbers, or None when it is not one. A count of 0 has no percentile.
    `counts` is the group's histogram over `bounds` when the replica served
    one that adds up, else None (a swarm-graph from before the counts)."""
    if not isinstance(value, dict):
        return None
    count = _int(value.get("count"))
    if count is None:
        return None
    out: dict[str, Any] = {"count": count}
    for name in (*_PERCENTILES, "max_ms"):
        out[name] = _ms(value.get(name)) if count else None
    out["counts"] = _counts(value.get("counts"), count, bounds)
    return out


def replica_latency(value: Any) -> dict[str, Any] | None:
    """A replica's `answer_latency`: the overall group, `by_outcome`,
    `by_temperature` and `cells`, only the groups swarm-graph names, and the
    histogram's `bounds_ms`; None when the replica served none (an older
    swarm-graph, or not the document)."""
    bounds = _bounds(value.get("bounds_ms")) if isinstance(value, dict) else None
    overall = _summary(value, bounds)
    if overall is None or not isinstance(value, dict):
        return None

    def groups(field: str, names: Sequence[str]) -> dict[str, dict[str, Any]]:
        table = value.get(field)
        table = table if isinstance(table, dict) else {}
        return {n: g for n in names if (g := _summary(table.get(n), bounds)) is not None}

    cells = [f"{o}/{t}" for o in LATENCY_OUTCOMES for t in TEMPERATURES]
    window = _int(value.get("window_hours"))
    return {**overall, "window_hours": window, "bounds_ms": bounds,
            "by_outcome": groups("by_outcome", LATENCY_OUTCOMES),
            "by_temperature": groups("by_temperature", TEMPERATURES),
            "cells": groups("cells", cells)}


def _public(group: Mapping[str, Any]) -> dict[str, Any]:
    """A group as the console reads it: the histogram stays in this route."""
    return {k: v for k, v in group.items() if k != "counts"}


def _read_off(counts: Sequence[int], bounds: Sequence[float], slowest: float
              ) -> dict[str, Any]:
    """Each percentile read off a merged histogram as swarm-graph reads its
    own (stats._percentiles): the bound of the bucket the rank falls in, the
    slowest answer above the last bound, never above the slowest answer."""
    total = sum(counts)
    out: dict[str, Any] = {}
    for name, q in zip(_PERCENTILES, (0.50, 0.95, 0.99)):
        rank = max(1, math.ceil(round(q * total, 6)))
        seen = 0
        for index, n in enumerate(counts):
            seen += n
            if seen >= rank:
                bound = bounds[index] if index < len(bounds) else slowest
                value = min(bound, slowest)
                out[name] = int(value) if float(value).is_integer() else value
                break
    return out


def merge_summaries(groups: Sequence[Mapping[str, Any]],
                    bounds: Sequence[float] | None = None) -> dict[str, Any]:
    """Several replicas' summaries of one group as one. The counts add and
    the max is the slowest. When every replica that answered served its
    histogram over the same `bounds`, the histograms add and each percentile
    is read off the sum: exact, as one replica's own (the owner's pick,
    3 November 2026). Otherwise (a replica from before the counts, mixed
    versions during a roll-out) a percentile is exact when one replica
    answered and else the slowest replica's, an upper bound (`exact` false)."""
    answered = [g for g in groups if g.get("count")]
    out: dict[str, Any] = {"count": sum(g.get("count") or 0 for g in groups)}
    for name in (*_PERCENTILES, "max_ms"):
        values = [g[name] for g in answered if g.get(name) is not None]
        out[name] = max(values) if values else None
    counted = bounds is not None and len(answered) > 1 and out["max_ms"] is not None \
        and all(g.get("counts") is not None and g.get("max_ms") is not None for g in answered)
    if counted:
        summed = [sum(column) for column in zip(*(g["counts"] for g in answered))]
        out.update(_read_off(summed, bounds, out["max_ms"]))  # type: ignore[arg-type]
    out["exact"] = counted or len(answered) <= 1
    return out


def merge_latency(latencies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The replicas' `replica_latency` merged, group by group, over their
    histograms when every replica served the same bounds."""
    served = {lat.get("bounds_ms") for lat in latencies}
    bounds = next(iter(served)) if len(served) == 1 else None

    def merged(field: str, names: Sequence[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for name in names:
            groups = [lat[field][name] for lat in latencies if name in lat[field]]
            if groups:
                out[name] = merge_summaries(groups, bounds)
        return out

    cells = [f"{o}/{t}" for o in LATENCY_OUTCOMES for t in TEMPERATURES]
    return {**merge_summaries(latencies, bounds),
            "by_outcome": merged("by_outcome", LATENCY_OUTCOMES),
            "by_temperature": merged("by_temperature", TEMPERATURES),
            "cells": merged("cells", cells)}


def latency_not_read(reason: str) -> dict[str, Any]:
    return {"state": NOT_READ, "reason": reason}


class Replicas:
    """The service's document, held to what §5.3 names and this ticket's digests."""

    def __init__(self, document: Mapping[str, Any], *, digests: Sequence[str | None],
                 now: datetime) -> None:
        self._digests = {d for d in digests if d}
        self._now = now
        self.raw = [r for r in document.get("replicas") or [] if isinstance(r, dict)]
        self.panels = [self._panel(r) for r in self.raw]

    def _repository(self, replica: Mapping[str, Any]) -> Mapping[str, Any]:
        # swarm-graph serves the ticket's repository's figures at the top of
        # each replica's document (stats.local_document); a `repository`
        # object holding them is read the same way.
        repository = replica.get("repository")
        return repository if isinstance(repository, dict) else replica

    def _panel(self, replica: Mapping[str, Any]) -> dict[str, Any]:
        name = _str(replica.get("replica"), 120) or "unknown"
        if replica.get("state") == NOT_READ:
            reason = replica.get("reason")
            return {"replica": name, "state": NOT_READ,
                    "reason": reason if reason in _PEER_REASONS else BAD_RESPONSE}
        started = _parse_time(replica.get("started_at"))
        panel: dict[str, Any] = {
            "replica": name, "state": "read",
            "started_at": started.isoformat() if started else None,
            "uptime_seconds": int((self._now - started).total_seconds()) if started else None,
        }
        for field in _REPLICA_NUMBERS:
            panel[field] = _int(replica.get(field))
        repository = self._repository(replica)
        panel["answers_hot"] = _int(repository.get("answers_hot"))
        panel["shards_hot"] = _int(repository.get("shards_hot"))
        panel["last_warm"] = self._warm(repository.get("last_warm"))
        latency = replica_latency(repository.get("answer_latency"))
        panel["answer_latency"] = None if latency is None else {
            **{k: latency[k] for k in ("count", *_PERCENTILES, "max_ms")},
            "by_temperature": {t: _public(g) for t, g in latency["by_temperature"].items()}}
        return panel

    def _warm(self, warm: Any) -> dict[str, Any] | None:
        # Only a warm of this repository's own two digests is named back.
        if not isinstance(warm, dict) or warm.get("digest") not in self._digests:
            return None
        return {"digest": warm["digest"], "ms": _int(warm.get("ms")),
                "shards": _int(warm.get("shards")), "replayed": _int(warm.get("replayed"))}

    def _read(self) -> list[tuple[dict[str, Any], Mapping[str, Any]]]:
        return [(p, r) for p, r in zip(self.panels, self.raw) if p["state"] == "read"]

    def facts(self, current_digest: str) -> dict[str, Any]:
        read = self._read()
        totals = {"hit": 0, "carried": None, "miss": 0}
        histogram: dict[float, int] = {}
        for _panel, raw in read:
            repository = self._repository(raw)
            counts = _outcomes(repository.get("outcomes"))
            totals["hit"] += counts["hit"] or 0
            totals["miss"] += counts["miss"] or 0
            if counts["carried"] is not None:
                totals["carried"] = (totals["carried"] or 0) + counts["carried"]
            for upper, count in _histogram(repository.get("served_ms")).items():
                histogram[upper] = histogram.get(upper, 0) + count
        carried = totals["carried"]
        answers = totals["hit"] + (carried or 0) + totals["miss"]
        hits = totals["hit"] + (carried or 0)
        uptimes = [p["uptime_seconds"] for p, _r in read if p["uptime_seconds"] is not None]
        window = min([WINDOW_HOURS] + [max(0, u) // 3600 for u in uptimes]) if read else None
        warms = [p["last_warm"] for p, _r in read
                 if p["last_warm"] and p["last_warm"]["digest"] == current_digest]
        slowest = max(warms, key=lambda w: w["ms"] or 0) if warms else None
        return {
            "answer_latency": self.answer_latency(),
            "window_hours": window,
            "answers": answers if read else None,
            "hit": totals["hit"] if read else None,
            "carried": carried,
            "miss": totals["miss"] if read else None,
            "hit_rate": (hits / answers) if answers else None,
            # A dash until carry forward exists (the owner Q4), never 0%.
            "reused_share": (carried / hits) if carried is not None and hits else None,
            "served_p95_ms": p95(histogram),
            "last_warm": slowest,
        }

    def answer_latency(self) -> dict[str, Any]:
        """`facts.answer_latency`: every read replica's figure merged, the
        replicas whose figure was not read named, never counted as zero."""
        latencies, missing = [], []
        for panel, raw in zip(self.panels, self.raw):
            latency = replica_latency(self._repository(raw).get("answer_latency")) \
                if panel["state"] == "read" else None
            if latency is None:
                missing.append(panel["replica"])
            else:
                latencies.append(latency)
        if not latencies:
            reason = NOT_SERVED if self._read() else NO_REPLICA_READ
            return {**latency_not_read(reason), "not_read": missing}
        windows = [lat["window_hours"] for lat in latencies if lat["window_hours"] is not None]
        return {"state": "read", "replicas_read": len(latencies), "not_read": missing,
                "window_hours": min(windows) if windows else None,
                **merge_latency(latencies)}

    def marks(self) -> list[tuple[str, Mapping[str, Any]]]:
        """(replica, its `{blob hex: mark}`) for each replica read."""
        out = []
        for panel, raw in self._read():
            repository = self._repository(raw)
            blobs = repository.get("blobs", repository.get("shards"))
            table = {_hex(k): v for k, v in (blobs.items() if isinstance(blobs, dict) else [])
                     if isinstance(v, dict)}
            out.append((panel["replica"], table))
        return out


def shard_rows(rows: Sequence[Mapping[str, Any]], replicas: Replicas | None
               ) -> list[dict[str, Any]]:
    """§5.2's rows: each blob's marks merged in, changed first, then by hits."""
    marks = replicas.marks() if replicas is not None else []
    merged = []
    for row in rows:
        blob = _hex(row["blob"])
        per_replica, hits, last = [], 0, None
        for name, table in marks:
            mark = table.get(blob)
            if mark is None:
                per_replica.append({"replica": name, "hot": False})
                continue
            per_replica.append({"replica": name, "hot": mark.get("hot") is True})
            hits += _int(mark.get("hits_24h")) or 0
            read_at = _parse_time(mark.get("last_read"))
            if read_at and (last is None or read_at > last):
                last = read_at
        merged.append({**row, "replicas": per_replica, "hits_24h": hits if marks else None,
                       "last_read": last.isoformat() if last else None})
    merged.sort(key=lambda r: (r["last_merge"] != "changed", -(r["hits_24h"] or 0),
                               r["layer"], r["module"]))
    return merged


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

def _not_read(reason: str) -> dict[str, Any]:
    return {"state": NOT_READ, "reason": reason}


def _no_facts(reason: str) -> dict[str, Any]:
    """The facts when no replica was read: every figure null, never zero, and
    the served latency not read for the same reason as the section."""
    return {"window_hours": None, "answers": None, "hit": None, "carried": None,
            "miss": None, "hit_rate": None, "reused_share": None, "served_p95_ms": None,
            "last_warm": None, "answer_latency": latency_not_read(reason)}


def graph_cache(ctx: AppContext, tenant_id: str, repo_id: str, *, shards: bool, page: int,
                reader: StatsReader, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    index = RepoIndex.from_context(ctx)
    record = index.registrations.get(tenant_id, repo_id)  # another tenant's: 404
    body: dict[str, Any] = {"repo_id": repo_id, "index": None, "facts": None, "replicas": [],
                            "stats": None, "latency": None}
    if shards:
        body["shards"] = None
    current_sha = (record.get("index") or {}).get("current_sha")
    current = index.version(tenant_id, repo_id) if current_sha else None
    if current is None:
        body["stats"] = _not_read(NO_INDEX)
        body["facts"] = _no_facts(NO_INDEX)
        return body
    latency = current.get("answer_latency")
    body["latency"] = dict(latency) if isinstance(latency, dict) else {
        "state": "unmeasured", "cause": "not_recorded"}
    current_digest = current.get("graph_digest")
    if not current_digest:
        body["stats"] = _not_read(NO_GRAPH)
        body["facts"] = _no_facts(NO_GRAPH)
        return body
    rows = [{"commit_sha": s.id, **(s.to_dict() or {})} for s in _versions(ctx, repo_id).stream()]
    previous = previous_version(rows, current)
    previous_digest = previous.get("graph_digest") if previous else None
    body["index"] = {"commit_sha": current.get("commit_sha"), "manifest_digest": current_digest,
                     "previous_sha": previous.get("commit_sha") if previous else None,
                     "previous_digest": previous_digest}

    key = getattr(ctx.settings, "graph_ticket_key", "")
    replicas: Replicas | None = None
    if not reader.url:
        body["stats"] = _not_read(NOT_CONFIGURED)
    elif not key:
        body["stats"] = _not_read(NO_KEY)
    else:
        ticket = mint_stats_ticket(key, tenant_id=tenant_id, repo_id=repo_id,
                                   manifest_digest=current_digest,
                                   previous_digest=previous_digest,
                                   exp=int(clock()) + STATS_TICKET_SECONDS)
        document, reason = reader.read(ticket, shards=shards)
        ticket = ""
        if document is None:
            log.info("graph cache stats tenant=%s repo_id=%s outcome=not_read reason=%s",
                     tenant_id, repo_id, reason)
            body["stats"] = _not_read(reason or BAD_RESPONSE)
        else:
            replicas = Replicas(document, digests=(current_digest, previous_digest),
                                now=ctx.now())
            body["stats"] = {"state": "read"}
            body["replicas"] = replicas.panels
            body["facts"] = replicas.facts(current_digest)
    if body["facts"] is None:
        body["facts"] = _no_facts(body["stats"]["reason"])
    if shards:
        body["shards"] = _shards(ctx, tenant_id, repo_id, current, previous, replicas, page)
    return body


def _shards(ctx: AppContext, tenant_id: str, repo_id: str, current: Mapping[str, Any],
            previous: Mapping[str, Any] | None, replicas: Replicas | None,
            page: int) -> dict[str, Any]:
    graphs = RepoGraph.from_inspection(ctx.inspection)
    try:
        now_graph = graphs.open(tenant_id, repo_id, current)
    except ApiError as failed:
        return {"state": NOT_READ, "reason": failed.code, "total": None, "changed": None,
                "page": page, "rows": []}
    comparison = "read"
    before = None
    if previous is not None:
        try:
            before = graphs.open(tenant_id, repo_id, previous)
        except ApiError as failed:
            # The previous graph swept or rewritten: the rows still show,
            # their "last merge" unknown rather than guessed.
            comparison = failed.code
    cache_key = (tenant_id, repo_id, "graph-cache-compare", now_graph.digest,
                 before.digest if before is not None else None)
    rows = graphs.view(cache_key, lambda: compare_manifests(
        now_graph.manifest, before.manifest if before is not None else None))
    merged = shard_rows(rows, replicas)
    start = (page - 1) * SHARD_PAGE_ROWS
    return {"state": "read", "comparison": comparison, "total": len(merged),
            "changed": (sum(1 for r in merged if r["last_merge"] == "changed")
                        if before is not None else None),
            "page": page, "page_rows": SHARD_PAGE_ROWS,
            "rows": merged[start:start + SHARD_PAGE_ROWS]}


@router.get("/{repo_id}/graph-cache")
async def get_graph_cache(
    repo_id: str,
    shards: int = Query(default=0, ge=0, le=1),
    page: int = Query(default=1, ge=1, le=10_000),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    reader: StatsReader = Depends(stats_reader),
) -> dict:
    """The Cache section of one of the caller's own repositories (§5.2): the
    facts, one panel per swarm-graph replica, and with `shards=1` a page of
    the shard list. swarm-graph not answering is a 200 with `replicas: []`
    and `stats.state` `not_read`; another tenant's repo_id is a 404."""
    return await run_in_threadpool(
        lambda: graph_cache(ctx, tenant_id, repo_id, shards=bool(shards), page=page,
                            reader=reader))


def graph_delivery(ctx: AppContext, tenant_id: str, repo_id: str, *, days: int,
                   reader: StatsReader) -> dict[str, Any]:
    """#4030: one of the caller's own repositories' last `days` of graph
    delivery (attempts by delivery, fallbacks by reason, from the attempts'
    `graph_ready` marks) and the replicas' merged served p50/p95 against the
    300 ms target. The same figures `graph-latency-report` records per firing."""
    from ..graphreports import delivery_report, read_served_latency, window_until

    index = RepoIndex.from_context(ctx)
    record = index.registrations.get(tenant_id, repo_id)  # another tenant's: 404
    repository = f"{record['owner']}/{record['repo']}"
    since, until = window_until(ctx.now(), days)
    return {"repo_id": repo_id, "repository": repository, "days": days,
            "delivery": delivery_report(ctx.store, ctx.db, tenant_id, since, until,
                                        repository=repository),
            "served_latency": read_served_latency(ctx, tenant_id, repo_id, reader)}


@router.get("/{repo_id}/graph-delivery")
async def get_graph_delivery(
    repo_id: str,
    days: int = Query(default=7, ge=1, le=30),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    reader: StatsReader = Depends(stats_reader),
) -> dict:
    """How the graph reached one of the caller's own repositories' attempts
    over `days` (default 7), the fallbacks by reason, and the served answer
    latency against 300 ms; another tenant's repo_id is a 404."""
    return await run_in_threadpool(
        lambda: graph_delivery(ctx, tenant_id, repo_id, days=days, reader=reader))


__all__ = [
    "LATENCY_OUTCOMES", "MAX_STATS_BYTES", "NOT_READ", "SHARD_PAGE_ROWS",
    "STATS_TIMEOUT_SECONDS", "TEMPERATURES", "Replicas", "StatsReader", "compare_manifests", "graph_cache",
    "graph_delivery", "merge_latency",
    "merge_summaries", "p95", "previous_version", "replica_latency",
    "router", "shard_rows", "stats_reader", "stats_url",
]
