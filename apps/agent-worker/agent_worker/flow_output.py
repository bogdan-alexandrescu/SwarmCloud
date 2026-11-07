"""A QA flow step's tar, normalised by the worker before the upload (QA-FLOW-CAPTURE, part of #2688).

Two legs of a peer application's QA re-run 8 (2026-11-06), told to bundle
their batch as `flows-1.tar`, wrote `flow-1.tar`. The flow report stages a
flow step's output by its step id (`swarm_api.qaspec.FLOW_LAYOUT`'s
`batch_tar`, `<step>.tar`), so the missing-output check failed the leg after
every flow had spent its browser time, and the report never started.

The owner's pick on PR #3521 (2026-11-06, `worker-normalise`): the check
runs HERE, in the worker, at the end of the step, where the files are, and
not only in the flow report's brief. Before the upload and the missing-output
check, a flow step whose `<step>.tar` is owed and was not written has a lone
stray top-level tar renamed to it, with one line saying so; several are
refused, named, and the missing-output check then fails the attempt naming
`<step>.tar`. A step that wrote its `<step>.tar` is left untouched.

ONE COPY OF THE RULE: the names and the rule are `swarm_rollup.flowlayout`'s,
which `swarm_api.qaspec.check_flow_output` reads too (this image does not ship
swarm-api; both build swarm_rollup). This module only lists the step's
top-level files, takes a flow's own tar from its `flow-<id>.json`, and does the
rename the rule picks.

Only a step whose `<step>.tar` is an expected output is touched: the owed
name is what says the step is a flow step, so no other step's files move.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from swarm_rollup import flowlayout

#: `result_summary`'s key for what the normalisation found.
SUMMARY_KEY = "flow_output"
#: The cause of the RUNNING event that records it on the step's events.
EVENT_CAUSE = "flow_output"


def owed_tar(step_id: Any, expected_outputs: Sequence[str]) -> str | None:
    """The `<step>.tar` this step owes, or None when it is no flow step."""
    if not isinstance(step_id, str) or not flowlayout.STEP_ID.match(step_id):
        return None
    expected = flowlayout.BATCH_TAR.format(step=step_id)
    return expected if expected in expected_outputs else None


def normalise(artifacts: Path, step_id: Any, expected_outputs: Sequence[str]) -> dict[str, Any] | None:
    """Rename a flow step's lone stray tar to its `<step>.tar`; None when nothing applies.

    The record is `{step, expected, ok, renamed, lines}`: `renamed` is
    `[written, expected]` when a stray was renamed, `ok` False when the owed
    tar is still missing, `lines` say it plainly.
    """
    expected = owed_tar(step_id, expected_outputs)
    if expected is None:
        return None
    try:
        names = sorted(entry.name for entry in os.scandir(artifacts) if entry.is_file(follow_symlinks=False))
    except OSError:
        return None
    own = {flowlayout.FLOW_TAR.format(id=fid) for fid in flowlayout.flow_ids_from_files(names)}
    pick = flowlayout.pick_flow_tar(step_id, names, own)
    if pick.line is None:
        return None
    record: dict[str, Any] = {"step": step_id, "expected": expected, "ok": False, "renamed": None, "lines": []}
    if pick.renamed is not None:
        stray = pick.renamed[0]
        try:
            os.replace(artifacts / stray, artifacts / expected)
        except OSError as exc:
            record["lines"].append(f"{step_id} packaged its flow output as {stray}, not {expected}, "
                                   f"and it could not be renamed ({type(exc).__name__})")
            return record
        record.update(ok=True, renamed=list(pick.renamed))
    record["lines"].append(pick.line)
    return record
