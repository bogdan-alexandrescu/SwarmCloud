"""A QA flow step's tar names and its stray-tar rule, stated once for the brief and the worker.

Two legs of the AI Studio session's QA re-run 8 (2026-11-06), told to bundle
their batch as `flows-1.tar`, wrote `flow-1.tar`; the flow report stages a
flow step's output by its step id, so the missing-output check failed the leg
after every flow had spent its browser time. PR #3521 wrote the rule that reads
such a tar twice, in `swarm_api.qaspec.check_flow_output` and in the worker's
`agent_worker.flow_output`, which restated the layout. The owner's pick on
6 November 2026: the worker normalises the step's output before its
expected-output check, by ONE copy of the rule (a value stated twice caused
three outages here).

THE RULE. A flow step `flows-<k>` (a batch) or `flow-<id>` (a mutating flow)
owes `<step>.tar`. When it is missing and exactly one other top-level tar is
there that is none of its flows' own `flow-<id>.tar`, that one is taken in its
place, said in one line; several are refused, each named; none is refused,
naming the owed tar. A step that wrote its `<step>.tar` is untouched.

WHY HERE. swarm-api and the agent worker are installed in different images,
neither imports the other, and `apps/common/swarm_common/` is frozen; this
package is the one both images already build, as `agentimage` is. Pure: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

#: `swarm_api.qaspec.FLOW_LAYOUT`'s `batch_tar`, `flow_tar` and `json`.
BATCH_TAR = "{step}.tar"
FLOW_TAR = "flow-{id}.tar"
FLOW_JSON = "flow-{id}.json"
#: A flow step's id: a batch `flows-<k>` or a mutating flow's `flow-<id>`.
STEP_ID = re.compile(r"^flows?-[A-Za-z0-9][A-Za-z0-9._-]*$")
_FLOW_JSON = re.compile(r"^flow-(.+)\.json$")


@dataclass(frozen=True)
class FlowTarPick:
    """What the rule found. `tar` is the name to read as `expected` (itself,
    or the lone stray), None when refused; `renamed` is `(stray, expected)`
    when a stray stands in; `line` says it, None when nothing needs saying."""

    expected: str
    tar: str | None
    renamed: tuple[str, str] | None
    line: str | None
    ok: bool


def flow_ids_from_files(files: Iterable[str]) -> list[str]:
    """The flow ids whose `flow-<id>.json` sits at the top level of `files`."""
    return [m.group(1) for name in files if isinstance(name, str) and "/" not in name
            and (m := _FLOW_JSON.match(name))]


def pick_flow_tar(step_id: str, files: Collection[str], own: Collection[str]) -> FlowTarPick:
    """Apply the rule to a flow step's top-level `files`; `own` are its flows' own tars."""
    expected = BATCH_TAR.format(step=step_id)
    own = set(own) - {expected}
    if expected in files:
        return FlowTarPick(expected, expected, None, None, True)
    strays = sorted(n for n in files if isinstance(n, str) and n.endswith(".tar") and "/" not in n
                    and n != expected and n not in own)
    if len(strays) == 1:
        return FlowTarPick(expected, strays[0], (strays[0], expected),
                           f"{step_id} packaged its flow output as {strays[0]}, not {expected}: read as {expected}",
                           True)
    if strays:
        return FlowTarPick(expected, None, None,
                           f"{step_id} wrote {', '.join(strays)} in place of {expected}: refused, as the layout "
                           f"names one tar, {expected}", False)
    return FlowTarPick(expected, None, None, f"{step_id} wrote no {expected}", False)
