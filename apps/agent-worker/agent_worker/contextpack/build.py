"""Building one step's pack: intake, retrieve, rank, deliver (§4.1).

    intake ──► retrieve ──► rank (local) ──► deliver
    type,      graph, rules, quotas,          CONTEXT.md (16 KiB),
    seeds      history, I, W  budget          pack.json, the artifact

Deterministic: the same snapshot, checkout and step give the same bytes, so a
replay (§6.1) can compare two arms' packs and a resumed attempt rebuilds the
pack it had.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..graphmcp.freshness import Freshness
from ..graphmcp.snapshot import Snapshot, load_freshness_record
from . import retrieve
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
               task_type: str | None = None, judge: Any = None) -> dict[str, Any]:
    """Write CONTEXT.md and pack.json into `where`; return the pack.

    `judge` is the attempt's Jev client (`judge.attempt.load`) or None. With
    one, J1 reranks after the local scores (`rank.rerank`); whatever J1 does
    not answer in full leaves the pack the local-only run writes."""
    where, checkout, work_dir = Path(where), Path(checkout), Path(work_dir)
    if _inside(where, checkout):
        raise ContextPackError(f"the context directory {where} is inside the checkout; the "
                               "publish would commit it, so nothing is written there")
    stage = read_stage(where)
    kind = task_type or classify(payload, work_dir, stage)

    snapshot_dir = where / SNAPSHOT_DIR
    record = load_freshness_record(snapshot_dir)
    snap = Snapshot(snapshot_dir,
                    manifest_digest=(record or {}).get("manifest_digest") or None)
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
    arm = stage.get("arm") if isinstance(stage.get("arm"), str) else None
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
