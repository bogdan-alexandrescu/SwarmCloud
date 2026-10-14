"""The staged directory's names, the budget and the task types (§4.4, §4.5, §8).

Kept apart from the builder so the runner's import of the package stays cheap:
`run_cli_agent` imports this module on every start, and the graph reader only
when there is a snapshot to read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: `work/.swarm-context/`: beside `.swarm-children/`, outside the checkout.
CONTEXT_DIR_NAME = ".swarm-context"
#: Set by the stager (CTXQ-S) when it staged a directory somewhere else.
CONTEXT_ENV = "SWARM_CONTEXT"
SNAPSHOT_DIR = "snapshot"
STAGE_FILE = "stage.json"
INFLIGHT_FILE = "inflight.json"
CONTEXT_FILE = "CONTEXT.md"
PACK_FILE = "pack.json"
#: The artifact §6's replay and A/B read, and CTXQ-G's gate appends to.
ARTIFACT_NAME = "context-pack.json"
#: CONTEXT.md's cap: 16 KiB, about 4k tokens (owner decision 2026-10-14, Q2).
#: The agent re-reads its context on every turn, so every byte here is paid
#: turns x times; the rest is paged through the swarm-graph tools.
BUDGET_BYTES = 16 * 1024
#: The owner's five task types (2026-10-14), with fix and test-writing as the
#: design's two pipelines of the owner's "fix/test-writing". The names are
#: J0's choices (§5.2), so a Jev classification maps onto them unchanged.
TASK_TYPES = ("implement", "review", "fix", "test", "design", "plan")
#: A stage.json or inflight.json larger than this is not the stager's.
MAX_STAGED_JSON_BYTES = 1024 * 1024


class ContextPackError(Exception):
    """The pack cannot be built as staged. Said in one prompt line, never raised
    out of `prepare`."""


def read_json(path: Path) -> Any:
    """A staged JSON file, or None when it is absent. Never read through a link:
    `work/` travels in checkpoints, so an earlier attempt's agent could have
    planted one at this name."""
    if path.is_symlink():
        raise ContextPackError(f"{path.name} is a link; it is not read")
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_STAGED_JSON_BYTES:
        raise ContextPackError(f"{path.name} is over {MAX_STAGED_JSON_BYTES} bytes")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ContextPackError(f"{path.name} is not JSON") from None


def read_stage(where: Path) -> dict[str, Any]:
    value = read_json(Path(where) / STAGE_FILE)
    return value if isinstance(value, dict) else {}
