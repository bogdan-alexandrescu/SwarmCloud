"""The context pack: what a step is told about its repository before it starts
(docs/design/context-quality.md §4.3-§4.5; lane CTXQ-P).

WHY. §3 of the design classified 69 failures of the last 150 lane PRs: 40 were
context failures -- a guard the agent never met, a covering test it never ran,
a parallel lane it could not see, a caller it never read -- and the
implementer, which writes most of the code, was given none of the graph. This
package turns the staged graph snapshot, the checkout's own docs and history,
and the stager's view of parallel work into ONE ranked file the agent reads
once, and one prompt line that names it.

THE LAYOUT, the one seam with the stager (lane CTXQ-S, `graphstage.py`), all
under `work/.swarm-context/` -- OUTSIDE the checkout (`work/repo`), beside
`.swarm-children/`, so the worker's publish never commits any of it:

    snapshot/          the KG5 snapshot graphmcp reads (manifest.json, blobs/,
                       optional freshness.json); written by the stager
    stage.json         optional, written by the stager: {"staged": false,
                       "reason"} when it staged nothing; else any of "type"
                       (a TASK_TYPES value it knows from the step's role),
                       "arm" (the A/B arm, §6.2), "jev" {"state", "reason"},
                       "hard_stop_paths" and "cannot_push" (globs)
    inflight.json      optional: {"rows": [{"title", "ref", "files"}]}, the
                       live in-flight view (layer I, phase 2). Absent is
                       UNKNOWN, never "nothing in flight"
    pin.json           instead of snapshot/, when stage.json says `delivery:
                       "service"` (graph-service.md §4.1, lane GS-WORKER):
                       freshness.json's record plus `manifest_digest`
    pin-manifest.json  beside it: the pinned manifest's bytes, checked
                       against the pin's digest before they are parsed
    CONTEXT.md         written here: the ranked pack, at most BUDGET_BYTES
    pack.json          written here: every row, ranked or paged out, and the
                       header; also copied to the artifact ARTIFACT_NAME

The directory is `$SWARM_CONTEXT` when the stager sets it, else
`work/.swarm-context` when a snapshot, a pin or a stage.json is there. No directory,
no line: a step without staging reads nothing about a feature it cannot use.

WHAT DECIDES STAGING is a stored setting the stager reads (owner decision
2026-10-14 Q1: the repository's `context` override over the workspace
default; no frozen-contract change), never a caller's input -- invariant 10.

OVER THE SHARED GRAPH SERVICE (graph-service.md §7, lane GS-PACK). With a
pin, `build.build_pack` reads the same `Snapshot` API through a
`build.ServiceSource`: only the shards the seeds touch, paged whole through
the service's `index` tool with the attempt's ticket, held in this process's
memory and dropped when the pack is written. Nothing is written to disk but
the pack. The pack is the staged pack, byte for byte; `pack.json` adds
`delivery`, `shards_read` and `bytes_read`, so what the pack costs is
measured on every attempt.

NEVER FAILS A STEP. `prepare` catches everything: a broken snapshot is one
prompt line saying the pack could not be built, and the agent starts as it
would have without it. Nothing here touches admission, leasing or capacity.

JEV. When the workspace opted in and the worker set Jev up for the attempt
(`judge.attempt`), J1 reranks the pack (`rank.rerank`, lane CTXQ-JW) and the
pack says `jev: on` with the call's spend. Otherwise, and whenever the call
fails, it records `jev` off with the stager's reason and spends nothing.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .layout import (
    ARTIFACT_NAME,
    BUDGET_BYTES,
    CONTEXT_DIR_NAME,
    CONTEXT_ENV,
    CONTEXT_FILE,
    INFLIGHT_FILE,
    PACK_FILE,
    SNAPSHOT_DIR,
    STAGE_FILE,
    TASK_TYPES,
    ContextPackError,
    read_stage,
)

#: The pin GS-WORKER's `graphstage.pin` writes in place of snapshot/ (§4.1),
#: and the pinned manifest beside it. Both hold no credential: the ticket is
#: in `private/`, never in `work/` (graph-service.md §4.1 step 2).
PIN_FILE = "pin.json"
PIN_MANIFEST_FILE = "pin-manifest.json"
#: stage.json's `delivery` for a pinned, served graph:
#: `graphstage.DELIVERY_SERVICE`, held equal by test_contextpack.py.
DELIVERY_SERVICE = "service"

if TYPE_CHECKING:
    from ..runners.base import RunnerContext

__all__ = [
    "ARTIFACT_NAME", "BUDGET_BYTES", "CONTEXT_DIR_NAME", "CONTEXT_ENV", "CONTEXT_FILE",
    "DELIVERY_SERVICE", "INFLIGHT_FILE", "PACK_FILE", "PIN_FILE", "PIN_MANIFEST_FILE",
    "SNAPSHOT_DIR", "STAGE_FILE", "TASK_TYPES",
    "GRAPH_LOOKUP_TOOL", "GRAPH_LOOKUP_TYPES",
    "ContextPackError", "context_dir", "failed_line", "lookup_line", "prepare", "prompt_line",
]

#: A stager's reason is shown in the prompt: one line, bounded.
_REASON_CHARS = 200


def prompt_line(context_file: Path | str) -> str:
    """The one line `run_cli_agent` adds when a pack was written (§4.4)."""
    return (
        f"Repository context for this step is in {os.fspath(context_file)}, outside the "
        "repository: callers, covering tests, guards, rules, similar past changes, parallel "
        "work and a checklist for this kind of step. Read it before editing. The "
        "swarm-graph tools answer anything it pages out."
    )


#: THE LOOKUP LINE (the owner's pick, 6 November 2026, after the 48-hour code
#: intelligence report; part of #2688). Implement steps called the graph most
#: (34% of its calls), reviews and fixes almost never, and when a step called
#: it the graph named a file it then changed in 35 of 56 answers. So a step of
#: these types that has the graph is told, as an instruction with its moment,
#: which tool answers "where is this defined or used". Other types get no line.
GRAPH_LOOKUP_TYPES = ("implement", "review", "fix")
#: The tool as the loaded-tools shape names it; a runner with the code-graph
#: skill passes its own wording (`claude_code.graph_lookup_line`).
GRAPH_LOOKUP_TOOL = "call the swarm-graph `context` tool"


def lookup_line(payload: Any, work_dir: Path | str, *, tool: str) -> str | None:
    """The one brief line naming the graph's lookup, or None for a step type
    not in `GRAPH_LOOKUP_TYPES`. The caller adds it only when the step has the
    graph. The type is the pack's own (`tasktype.classify`); never raises."""
    from .tasktype import classify

    try:
        where = context_dir(Path(work_dir))
        stage = read_stage(where) if where is not None else {}
    except Exception:  # noqa: BLE001 -- an unreadable stage.json types by the brief
        stage = {}
    try:
        kind = classify(payload if isinstance(payload, dict) else {}, Path(work_dir), stage)
    except Exception:  # noqa: BLE001 -- a line is never worth a failed step
        return None
    if kind not in GRAPH_LOOKUP_TYPES:
        return None
    return (f"Before you search widely for where a symbol is defined or used, {tool} with "
            "its name: one answer gives where it is defined and who calls it.")


def failed_line(error: str, *, graph: bool) -> str:
    """The one line when the pack could not be built (lane REVIEW-STARTUP-WAIT).

    It names the PACK, never the graph: in five batch-one steps the line
    "Repository context could not be built ...; work from the checkout as
    usual" made the agent report the graph unavailable while swarm-graph was
    connected. With a graph delivered (`graph`), the line says the tools are
    there; the runner's own graph line stays as it is."""
    head = f"The context pack for this step could not be built ({error})."
    if graph:
        return (f"{head} Only the pack (CONTEXT.md) is missing: the swarm-graph tools are "
                "connected and answer callers, covering tests and rules directly.")
    return f"{head} Work from the checkout as usual."


def _one_line(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:_REASON_CHARS]


def context_dir(work_dir: Path) -> Path | None:
    """Where this step's context is staged, or None when nothing is."""
    named = os.environ.get(CONTEXT_ENV, "").strip()
    if named:
        return Path(named)
    default = Path(work_dir) / CONTEXT_DIR_NAME
    if (default / SNAPSHOT_DIR).is_dir() or (default / PIN_FILE).is_file() \
            or (default / STAGE_FILE).is_file():
        return default
    return None


def prepare(ctx: "RunnerContext", checkout: Path, judge: Any = None) -> str | None:
    """Build the pack for this step and return its prompt line; never raises.

    None when nothing was staged. A stager that decided to stage nothing (an
    index-less or stale-beyond-ancestry repository) gets its one line, with
    its reason. Any failure to build is one line too (`failed_line`), and the
    step goes on, its artifact saying so. The pack is bounded as a whole
    (`build.PACK_DEADLINE_SECONDS`) and its seconds are in the artifact.
    """
    started = time.monotonic()
    graph = False
    stage: dict[str, Any] = {}
    try:
        where = context_dir(ctx.work_dir)
        if where is None:
            return None
        stage = read_stage(where)
        served = stage.get("delivery") == DELIVERY_SERVICE and (where / PIN_FILE).is_file()
        if not served and (stage.get("staged") is False or not (where / SNAPSHOT_DIR).is_dir()):
            reason = _one_line(str(stage.get("reason") or "no graph snapshot was staged"))
            return f"No repository context was staged for this step: {reason}."
        # From here a graph was delivered, staged or served: a failure below
        # is the pack's alone.
        graph = True
        from .build import PACK_DEADLINE_SECONDS, Deadline, build_pack, write_artifact
        from .tasktype import classify_with_jev

        # J0 types the step only where the local rules fall back to implement,
        # and only with a Jev client (Jev for code on): `classify_with_jev`.
        # One deadline for the whole of it, J0's classification included.
        deadline = Deadline(PACK_DEADLINE_SECONDS)
        kind = classify_with_jev(ctx.payload, Path(ctx.work_dir), stage, judge)
        pack = build_pack(where, checkout=Path(checkout), work_dir=Path(ctx.work_dir),
                          payload=ctx.payload, task_type=kind, judge=judge,
                          deadline=deadline)
        # The seconds go in the artifact only: pack.json stays the same
        # bytes for the same step.
        write_artifact(Path(ctx.artifacts_dir), {
            **pack, "built": True, "seconds": round(time.monotonic() - started, 3),
            "deadline_seconds": deadline.seconds})
        return prompt_line(where / CONTEXT_FILE)
    except Exception as exc:  # noqa: BLE001 -- a pack is never worth a failed step
        error = f"{type(exc).__name__}: {_one_line(str(exc))[:160]}"
        _record_failure(ctx, stage, error, round(time.monotonic() - started, 3), graph)
        return failed_line(error, graph=graph)


def _record_failure(ctx: "RunnerContext", stage: dict[str, Any], error: str,
                    seconds: float, graph: bool) -> None:
    """`context-pack.json` for a pack that was not built: the error, its
    seconds and the arm, so §6 reads the step's arm and not a missing pack.
    Never raises."""
    if not graph:
        return
    try:
        from .build import PACK_DEADLINE_SECONDS, SCHEMA, _arm, write_artifact

        write_artifact(Path(ctx.artifacts_dir), {
            "schema": SCHEMA, "built": False, "error": error, "seconds": seconds,
            "deadline_seconds": PACK_DEADLINE_SECONDS, "arm": _arm(stage),
            "delivery": stage.get("delivery"), "graph_files": 0})
    except Exception:  # noqa: BLE001 -- a record is never worth a failed step
        return
