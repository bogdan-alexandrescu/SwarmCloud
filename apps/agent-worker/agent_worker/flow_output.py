"""A QA flow step's tar, normalised by the worker before the upload (QA-FLOW-CAPTURE, part of #2688).

Two legs of the AI Studio session's QA re-run 8 (2026-11-06), told to bundle
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

THE LAYOUT IS `swarm_api.qaspec.FLOW_LAYOUT`'s, read the same way as
`qaspec.check_flow_output` reads it (this image does not ship swarm-api, so
the three names below are restated from it and the unit tests hold the two
equal): a flow step's id is `flows-<k>` (a batch) or `flow-<id>` (a mutating
flow), its tar `<step>.tar`, and a flow's own tar `flow-<id>.tar`, beside its
`flow-<id>.json`. A tar named by one of the step's flows' jsons is that
flow's own and is never read as the batch's.

Only a step whose `<step>.tar` is an expected output is touched: the owed
name is what says the step is a flow step, so no other step's files move.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: `qaspec.FLOW_LAYOUT["batch_tar"]`, `["flow_tar"]` and `["json"]`.
BATCH_TAR = "{step}.tar"
FLOW_TAR = "flow-{id}.tar"
FLOW_JSON = "flow-{id}.json"
#: A flow step's id: a batch `flows-<k>` or a mutating flow's `flow-<id>`.
STEP_ID = re.compile(r"^flows?-[A-Za-z0-9][A-Za-z0-9._-]*$")
_FLOW_JSON = re.compile(r"^flow-(.+)\.json$")
#: `result_summary`'s key for what the normalisation found.
SUMMARY_KEY = "flow_output"


def owed_tar(step_id: Any, expected_outputs: Sequence[str]) -> str | None:
    """The `<step>.tar` this step owes, or None when it is no flow step."""
    if not isinstance(step_id, str) or not STEP_ID.match(step_id):
        return None
    expected = BATCH_TAR.format(step=step_id)
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
    if expected in names:
        return None
    own = {FLOW_TAR.format(id=m.group(1)) for n in names if (m := _FLOW_JSON.match(n))}
    strays = [n for n in names if n.endswith(".tar") and n != expected and n not in own]
    record: dict[str, Any] = {"step": step_id, "expected": expected, "ok": False, "renamed": None, "lines": []}
    if len(strays) == 1:
        try:
            os.replace(artifacts / strays[0], artifacts / expected)
        except OSError as exc:
            record["lines"].append(f"{step_id} packaged its flow output as {strays[0]}, not {expected}, "
                                   f"and it could not be renamed ({type(exc).__name__})")
            return record
        record.update(ok=True, renamed=[strays[0], expected])
        record["lines"].append(f"{step_id} packaged its flow output as {strays[0]}, not {expected}: "
                               f"renamed to {expected}")
    elif strays:
        record["lines"].append(f"{step_id} wrote {', '.join(strays)} in place of {expected}: refused, as the "
                               f"layout names one tar, {expected}")
    else:
        record["lines"].append(f"{step_id} wrote no {expected}")
    return record
