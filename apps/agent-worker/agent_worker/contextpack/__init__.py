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
    CONTEXT.md         written here: the ranked pack, at most BUDGET_BYTES
    pack.json          written here: every row, ranked or paged out, and the
                       header; also copied to the artifact ARTIFACT_NAME

The directory is `$SWARM_CONTEXT` when the stager sets it, else
`work/.swarm-context` when a snapshot or a stage.json is there. No directory,
no line: a step without staging reads nothing about a feature it cannot use.

WHAT DECIDES STAGING is a stored setting the stager reads (owner decision
2026-10-14 Q1: the repository's `context` override over the workspace
default; no frozen-contract change), never a caller's input -- invariant 10.

NEVER FAILS A STEP. `prepare` catches everything: a broken snapshot is one
prompt line saying the pack could not be built, and the agent starts as it
would have without it. Nothing here touches admission, leasing or capacity.

JEV. The pack records `jev` off with the stager's reason and spends nothing:
the J1 rerank goes into `rank.py` in lane CTXQ-JW, after the client (CTXQ-J).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from ..runners.base import RunnerContext

__all__ = [
    "ARTIFACT_NAME", "BUDGET_BYTES", "CONTEXT_DIR_NAME", "CONTEXT_ENV", "CONTEXT_FILE",
    "INFLIGHT_FILE", "PACK_FILE", "SNAPSHOT_DIR", "STAGE_FILE", "TASK_TYPES",
    "ContextPackError", "context_dir", "prepare", "prompt_line",
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


def _one_line(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:_REASON_CHARS]


def context_dir(work_dir: Path) -> Path | None:
    """Where this step's context is staged, or None when nothing is."""
    named = os.environ.get(CONTEXT_ENV, "").strip()
    if named:
        return Path(named)
    default = Path(work_dir) / CONTEXT_DIR_NAME
    if (default / SNAPSHOT_DIR).is_dir() or (default / STAGE_FILE).is_file():
        return default
    return None


def prepare(ctx: "RunnerContext", checkout: Path) -> str | None:
    """Build the pack for this step and return its prompt line; never raises.

    None when nothing was staged. A stager that decided to stage nothing (an
    index-less or stale-beyond-ancestry repository) gets its one line, with
    its reason. Any failure to build is one line too, and the step goes on.
    """
    try:
        where = context_dir(ctx.work_dir)
        if where is None:
            return None
        stage = read_stage(where)
        if stage.get("staged") is False or not (where / SNAPSHOT_DIR).is_dir():
            reason = _one_line(str(stage.get("reason") or "no graph snapshot was staged"))
            return f"No repository context was staged for this step: {reason}."
        from .build import build_pack, write_artifact

        pack = build_pack(where, checkout=Path(checkout), work_dir=Path(ctx.work_dir),
                          payload=ctx.payload)
        write_artifact(Path(ctx.artifacts_dir), pack)
        return prompt_line(where / CONTEXT_FILE)
    except Exception as exc:  # noqa: BLE001 -- a pack is never worth a failed step
        return (f"Repository context could not be built for this step "
                f"({type(exc).__name__}: {_one_line(str(exc))[:120]}); work from the "
                "checkout as usual.")
