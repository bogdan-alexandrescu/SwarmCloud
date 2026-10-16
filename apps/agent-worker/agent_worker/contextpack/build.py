"""Building one step's pack: intake, retrieve, rank, deliver (§4.1).

    intake ──► retrieve ──► rank (local) ──► deliver
    type,      graph, rules, quotas,          CONTEXT.md (16 KiB),
    seeds      history, I, W  budget          pack.json, the artifact

Deterministic: the same snapshot, checkout and step give the same bytes, so a
replay (§6.1) can compare two arms' packs and a resumed attempt rebuilds the
pack it had.

TWO DELIVERIES, ONE PACK (docs/design/graph-service.md §7, lane GS-PACK).
A staged snapshot is read from `snapshot/`, as before. A pin (stage.json
`delivery: "service"`, GS-WORKER's §4.1) is read through `ServiceSource`, a
`graphmcp.snapshot.BlobSource` over the shared service's `index` tool: the
pack keeps its code and walks the same `Snapshot`, and only the shards the
seeds touch cross the network. Rewriting the pack as tool calls was the
alternative, and was not chosen: the pack walks shards row by row, and each
walk would be dozens of 4 KiB round trips (owner Q10). Either way the pack
records `delivery`, `shards_read` and `bytes_read`, so the claim that it
reads a small fraction of the graph is measured on every attempt.

WHAT A SERVED SHARD IS CHECKED AGAINST. A staged blob is checked against its
sha256 name before it is decoded (`snapshot.decode_blob`). The service
answers rows, not blob bytes, so that check is the service's, made under
the ticket's manifest digest (§3.3, §5.2). Here the pinned manifest is
checked against the pin's digest before it is parsed, and every shard must
arrive whole: contiguous pages, one `total`, and exactly the manifest's
`records` rows. A row over the service's 4 KiB answer cap comes back as a
placeholder, and is refused rather than silently dropped -- a pack missing
a caller would read as "no caller".
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Mapping

from ..graphmcp import tools
from ..graphmcp.freshness import Freshness
from ..graphmcp.snapshot import (
    ALL_LAYERS,
    BLOB_SUFFIX,
    BLOBS_DIR,
    CACHE_RAW_BYTES,
    MANIFEST_NAME,
    DirectorySource,
    Snapshot,
    SnapshotError,
    _Shard,
    blob_hexdigest,
    load_freshness_record,
)
from . import DELIVERY_SERVICE, PIN_FILE, PIN_MANIFEST_FILE, retrieve
from .layout import (
    ARTIFACT_NAME,
    BUDGET_BYTES,
    CONTEXT_FILE,
    INFLIGHT_FILE,
    PACK_FILE,
    SNAPSHOT_DIR,
    ContextPackError,
    read_json,
    read_stage,
)
from .rank import ordered, rerank, score, select
from .render import dumps, render_markdown, write_atomically
from .seeds import seeds_for
from .tasktype import classify

SCHEMA = "swarm.context-pack/v1"
#: Why Jev did not rank this pack, when the stager gave no reason of its own.
JEV_NOT_SET_UP = "Jev is not set up for this attempt"

#: `graphstage.DELIVERY_STAGED`: a snapshot copied into `work/`.
DELIVERY_STAGED = "staged"
#: The service's URL: a worker setting, never a caller's (graph-service.md
#: §4.2, invariant 10). The same name and default the shim's entry carries.
SERVICE_URL_ENV = "SWARM_GRAPH_URL"
DEFAULT_SERVICE_URL = "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"
#: The attempt's graph ticket, `private/graph-ticket` (§4.1 step 2): read per
#: request, so a renewal by the worker is picked up without a restart.
TICKET_FILE_ENV = "SWARM_GRAPH_TICKET_FILE"
#: The shim's per-answer timeout (§4.4). A pack that cannot be read in time is
#: one prompt line, never a late start.
SERVICE_TIMEOUT_SECONDS = 10.0
#: An answer is at most `tools.ANSWER_CAP` of text in a JSON-RPC envelope;
#: anything much larger is not the service's.
MAX_RESPONSE_BYTES = 64 * 1024
#: What each of the service's HTTP statuses means (§2.2), said as §10 says it.
#: A status is all that is said: never the ticket, never the body.
SERVICE_STATUS = {
    401: "the graph service refused this attempt's ticket",
    404: "the pinned index is no longer stored (pruned or missing)",
    409: "the stored index does not match its promoted digest; refused",
    503: "the graph service is switched off or cannot reach its store",
}

#: One JSON-RPC message in, its response out: `HttpService`, or a test's fake.
Transport = Callable[[dict], dict]


class HttpService:
    """swarm-graph over MCP streamable HTTP (§2.2): one POST per message, the
    attempt's ticket as the bearer credential, re-read from its file on every
    call. Never through a proxy: the ticket goes to the in-cluster Service
    and nowhere else."""

    def __init__(self, url: str, ticket_file: Path | str, *,
                 timeout: float = SERVICE_TIMEOUT_SECONDS) -> None:
        self.url = url
        self.ticket_file = Path(ticket_file)
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_env(cls) -> "HttpService":
        named = os.environ.get(TICKET_FILE_ENV, "").strip()
        if not named:
            raise SnapshotError("the graph is pinned to the service, but no graph ticket "
                                "file was named for this attempt")
        return cls(os.environ.get(SERVICE_URL_ENV, "").strip() or DEFAULT_SERVICE_URL, named)

    def _ticket(self) -> str:
        try:
            ticket = self.ticket_file.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            ticket = ""
        if not ticket:
            raise SnapshotError("this attempt's graph ticket is not readable")
        return ticket

    def __call__(self, message: dict) -> dict:
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self._ticket()}",
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise SnapshotError(SERVICE_STATUS.get(
                exc.code, f"the graph service answered HTTP {exc.code}")) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SnapshotError(f"the graph service is unreachable "
                                f"({type(exc).__name__})") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise SnapshotError(f"the graph service answered over {MAX_RESPONSE_BYTES} bytes")
        try:
            answer = json.loads(body)
        except ValueError:
            raise SnapshotError("the graph service answered something that is not "
                                "JSON") from None
        if not isinstance(answer, dict):
            raise SnapshotError("the graph service answered something that is not a "
                                "JSON-RPC response")
        return answer


def _raw_bytes(rows: list[dict]) -> int:
    """A served shard's size as `decode_blob` counts a staged one: its JSON
    lines, so the memory bound means the same thing on both sources."""
    return sum(len(json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                   .encode("utf-8")) + 1 for row in rows)


class ServiceSource:
    """A `BlobSource` whose shards come from the shared service (§7).

    `name_blobs(manifest)` maps each blob the verified manifest names to the
    `(layer, key)` the `index` tool reads it by; `decoded` pages that shard
    whole. What it fetched is held under `cache_raw_bytes` (the staged
    server's `CACHE_RAW_BYTES`) in this process and nowhere else: it is
    one attempt's, so one tenant's (invariant 9), and dropped with the pack.
    """

    def __init__(self, call: Transport, *, cache_raw_bytes: int = CACHE_RAW_BYTES) -> None:
        self._call = call
        self._names: dict[str, tuple[str, str, Any]] = {}
        self._cache: OrderedDict[str, _Shard] = OrderedDict()
        self._cache_bytes = 0
        self._cache_limit = cache_raw_bytes
        self._next_id = 0
        self._seen: set[str] = set()
        self.shards_read = 0
        self.bytes_read = 0

    def name_blobs(self, manifest: Mapping[str, Any]) -> None:
        for table in (manifest.get("shards"), manifest.get("index_shards")):
            for layer, keys in (table.items() if isinstance(table, Mapping) else ()):
                if layer not in ALL_LAYERS or not isinstance(keys, Mapping):
                    continue
                for key, entry in sorted(keys.items()):
                    if isinstance(entry, Mapping):
                        self._names.setdefault(blob_hexdigest(dict(entry)),
                                               (layer, str(key), entry.get("records")))

    def _index(self, layer: str, key: str, cursor: int) -> dict:
        self._next_id += 1
        arguments: dict[str, Any] = {"layer": layer, "key": key}
        if cursor:
            arguments["cursor"] = str(cursor)
        response = self._call({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                               "params": {"name": "index", "arguments": arguments}})
        where = f"index({layer}, {key})"
        error = response.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, Mapping) else error
            raise SnapshotError(f"the graph service refused {where}: {str(message)[:200]}")
        result = response.get("result")
        content = result.get("content") if isinstance(result, Mapping) else None
        text = content[0].get("text") if isinstance(content, list) and content \
            and isinstance(content[0], Mapping) else None
        if not isinstance(text, str):
            raise SnapshotError(f"the graph service's answer to {where} holds no text")
        self.bytes_read += len(text.encode("utf-8"))
        try:
            answer = json.loads(text)
        except ValueError:
            raise SnapshotError(f"the graph service's answer to {where} is not JSON") from None
        if not isinstance(answer, dict):
            raise SnapshotError(f"the graph service's answer to {where} is not an object")
        if "error" in answer or result.get("isError"):
            raise SnapshotError(f"the graph service refused {where}: "
                                f"{str(answer.get('error'))[:200]}")
        return answer

    def decoded(self, hexdigest: str) -> _Shard:
        cached = self._cache.get(hexdigest)
        if cached is not None:
            self._cache.move_to_end(hexdigest)
            return cached
        named = self._names.get(hexdigest)
        if named is None:
            raise SnapshotError(f"blob {hexdigest[:12]} is not named by the pinned manifest")
        layer, key, records = named
        rows: list[dict] = []
        total: int | None = None
        while True:
            answer = self._index(layer, key, len(rows))
            page, following, count = (answer.get("rows"), answer.get("next_cursor"),
                                      answer.get("total"))
            if not isinstance(page, list) or not isinstance(count, int) \
                    or (total is not None and count != total):
                raise SnapshotError(f"the graph service paged {layer}/{key} inconsistently")
            total = count
            if any(row == tools.ONE_ROW_OVER or not isinstance(row, dict) for row in page):
                raise SnapshotError(f"a row of {layer}/{key} is over the service's 4 KiB "
                                    "answer cap, so the shard cannot be read whole; "
                                    "it is refused rather than read short")
            rows.extend(page)
            if following is None:
                break
            if not page or str(following) != str(len(rows)):
                raise SnapshotError(f"the graph service paged {layer}/{key} out of order")
        if len(rows) != total or (isinstance(records, int) and records != total):
            raise SnapshotError(f"the graph service served {len(rows)} rows of {layer}/{key}; "
                                f"the manifest records {records}")
        shard = _Shard(rows, _raw_bytes(rows))
        if hexdigest not in self._seen:
            self._seen.add(hexdigest)
            self.shards_read += 1
        self._cache[hexdigest] = shard
        self._cache_bytes += shard.raw_bytes
        while self._cache_bytes > self._cache_limit and len(self._cache) > 1:
            _key, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= evicted.raw_bytes
        return shard

    def drop(self) -> None:
        """Release every shard: the pack is written, the agent reads the tools."""
        self._cache.clear()
        self._cache_bytes = 0


class _CountedDirectory:
    """A staged snapshot's `DirectorySource`, counting the blobs it reads and
    their stored bytes, so a staged pack is measured as a served one is."""

    def __init__(self, root: Path) -> None:
        self._inner = DirectorySource(root)
        self._blobs = Path(root) / BLOBS_DIR
        self._seen: set[str] = set()
        self.shards_read = 0
        self.bytes_read = 0

    def decoded(self, hexdigest: str) -> _Shard:
        shard = self._inner.decoded(hexdigest)
        if hexdigest not in self._seen:
            self._seen.add(hexdigest)
            self.shards_read += 1
            self.bytes_read += (self._blobs / f"{hexdigest}{BLOB_SUFFIX}").stat().st_size
        return shard

    def drop(self) -> None:
        return None


def _open(where: Path, stage: Mapping[str, Any],
          service: Transport | None) -> tuple[Snapshot, dict | None, Any, str]:
    """(snapshot, freshness record, its source, delivery) for this step."""
    if stage.get("delivery") == DELIVERY_SERVICE:
        pin = read_json(where / PIN_FILE)
        digest = pin.get("manifest_digest") if isinstance(pin, dict) else None
        if not isinstance(digest, str) or not digest:
            raise SnapshotError(f"{PIN_FILE} names no manifest digest")
        try:
            raw = (where / PIN_MANIFEST_FILE).read_bytes()
        except OSError as exc:
            raise SnapshotError(f"no pinned manifest: {exc.strerror}") from None
        source = ServiceSource(service if service is not None else HttpService.from_env())
        snap = Snapshot.from_manifest(raw, manifest_digest=digest, source=source)
        source.name_blobs(snap.manifest)
        return snap, pin, source, DELIVERY_SERVICE
    snapshot_dir = where / SNAPSHOT_DIR
    record = load_freshness_record(snapshot_dir)
    try:
        raw = (snapshot_dir / MANIFEST_NAME).read_bytes()
    except OSError as exc:
        raise SnapshotError(f"no manifest in the snapshot: {exc.strerror}") from None
    staged = _CountedDirectory(snapshot_dir)
    snap = Snapshot.from_manifest(raw, manifest_digest=(record or {}).get("manifest_digest")
                                  or None, source=staged)
    return snap, record, staged, DELIVERY_STAGED


def _arm(stage: Mapping[str, Any]) -> str | None:
    """The A/B arm: `graphstage.StageResult.record` nests it in `decision`;
    a stager that writes it at the top level is read too."""
    decision = stage.get("decision")
    arm = decision.get("arm") if isinstance(decision, Mapping) else None
    if not isinstance(arm, str):
        arm = stage.get("arm")
    return arm if isinstance(arm, str) else None


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return True


def _jev(stage: Mapping[str, Any], j1: Mapping[str, Any] | None = None) -> dict[str, str]:
    """What the pack says about Jev. It says `on` only for a pack J1 ranked: a
    call that failed or was skipped leaves exactly the local-only header."""
    if j1 is not None:
        return {"state": "on", "reason": f"J1 reranked the top {j1['candidates']} rows"}
    declared = stage.get("jev") if isinstance(stage.get("jev"), dict) else {}
    state = str(declared.get("state") or "off")
    reason = " ".join(str(declared.get("reason") or "").split())[:200]
    if state != "off":
        return {"state": "off", "reason": f"opted in, but {JEV_NOT_SET_UP}"}
    return {"state": "off", "reason": reason or JEV_NOT_SET_UP}


def build_pack(where: Path, *, checkout: Path, work_dir: Path, payload: Mapping[str, Any],
               task_type: str | None = None, judge: Any = None,
               service: Transport | None = None) -> dict[str, Any]:
    """Write CONTEXT.md and pack.json into `where`; return the pack.

    `judge` is the attempt's Jev client (`judge.attempt.load`) or None. With
    one, J1 reranks after the local scores (`rank.rerank`); whatever J1 does
    not answer in full leaves the pack the local-only run writes.

    `service` is how a pinned graph is read (`ServiceSource`); None is
    `HttpService.from_env()`, the attempt's ticket file and the worker's URL.
    """
    where, checkout, work_dir = Path(where), Path(checkout), Path(work_dir)
    if _inside(where, checkout):
        raise ContextPackError(f"the context directory {where} is inside the checkout; the "
                               "publish would commit it, so nothing is written there")
    stage = read_stage(where)
    kind = task_type or classify(payload, work_dir, stage)

    snap, record, source, delivery = _open(where, stage, service)
    try:
        return _build(where, snap, record, source, delivery, stage=stage, kind=kind,
                      checkout=checkout, work_dir=work_dir, payload=payload, judge=judge)
    finally:
        source.drop()


def _build(where: Path, snap: Snapshot, record: dict | None, source: Any, delivery: str, *,
           stage: Mapping[str, Any], kind: str, checkout: Path, work_dir: Path,
           payload: Mapping[str, Any], judge: Any) -> dict[str, Any]:
    fresh = Freshness.establish(snap.commit_sha, workdir=None if record else checkout,
                                record=record)
    observed = fresh.observe()
    freshness = {k: v for k, v in observed.block.items() if k != "dirty"}
    freshness["format_version"] = snap.format_version

    seeds = seeds_for(kind, payload, snap=snap, checkout=checkout, work_dir=work_dir)
    pool = retrieve.Pool()
    retrieve.graph(pool, snap, seeds, kind)
    retrieve.rules(pool, checkout, seeds, (), retrieve.tracked_files(checkout))
    blocked = retrieve.index_rules(pool, snap, seeds)
    retrieve.history(pool, snap, checkout, seeds)
    territory = list(dict.fromkeys(
        [*seeds.paths(), *(c.path for c in pool.rows() if c.kind == "caller" and c.hops == 1)]))
    in_flight = retrieve.in_flight(pool, read_json(where / INFLIGHT_FILE), territory)
    retrieve.cannot_push(pool, stage, blocked)
    retrieve.normalise_bm25(pool)

    rows = pool.rows()
    for cand in rows:
        cand.score = score(cand, kind)
        if cand.kind not in retrieve.ALWAYS:
            cand.stale = observed.stale(cand.path)
    brief = payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""
    j1 = rerank(rows, kind, brief, judge) if judge is not None else None
    ranked = select(rows, kind)
    header = {"task_type": kind, "freshness": freshness, "jev": _jev(stage, j1),
              "seeds": seeds.as_dict()}
    text, shown, paged = render_markdown(header, rows, ranked, in_flight)
    size = len(text.encode("utf-8"))
    tokens = -(-size // 4)
    arm = _arm(stage)
    always = [c for c in rows if c.kind in retrieve.ALWAYS]
    rest = ordered(c for c in rows if c.kind not in retrieve.ALWAYS)
    out_rows = [{**c.as_row(), "shown": c.kind in retrieve.ALWAYS or c.key() in shown}
                for c in [*sorted(always, key=lambda c: (c.kind, c.ref)), *rest]]
    shown_count = len(shown) + len(always)
    pack: dict[str, Any] = {
        "schema": SCHEMA,
        "task_type": kind,
        "arm": arm,
        "seeds": seeds.as_dict(),
        "freshness": freshness,
        "jev": header["jev"],
        "jev_calls": j1["calls"] if j1 else 0,
        "jev_input_tokens": j1["input_tokens"] if j1 else 0,
        "in_flight": in_flight,
        "rows": out_rows,
        "paged_out": paged,
        "bytes": size,
        "tokens": tokens,
        "budget_bytes": BUDGET_BYTES,
        "delivery": delivery,
        "shards_read": source.shards_read,
        "bytes_read": source.bytes_read,
        "summary": (f"Context pack: {f'arm {arm}, ' if arm else ''}{shown_count} rows, "
                    f"{tokens / 1000:.1f}k tokens"),
    }
    write_atomically(where / CONTEXT_FILE, text.encode("utf-8"))
    write_atomically(where / PACK_FILE, dumps(pack))
    return pack


def write_artifact(artifacts_dir: Path, pack: Mapping[str, Any]) -> Path:
    """pack.json again as the artifact §6 reads (`context-pack.json`)."""
    path = Path(artifacts_dir) / ARTIFACT_NAME
    write_atomically(path, dumps(dict(pack)))
    return path
