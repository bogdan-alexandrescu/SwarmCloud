"""Overview's workflow and parked checks, at the seams Python can hold.

THE DEFECT THESE EXIST FOR. `overview/now` ran six derived checks -- dispatch,
leases, provider quota, accounts, pools, failures -- and none of them could see
a workflow. With a three-step workflow in the tenant holding one READY step and
two PARKED ones, the panel said:

    Nothing is running -- No task on the 7 most recently created is in LEASED,
    DISPATCHED, STARTING or RUNNING. The state counts agree: zero.

Every sentence true; the conclusion false. The word "workflow" appeared nowhere
on the screen, re-verified on 2026-09-22 against the live deployment with a
COMPLETED three-step workflow present.

WHAT IS TESTED WHERE, because this file is deliberately only half the coverage.
The BEHAVIOUR -- a stalled workflow produces a problem, parked steps produce
one with the reason, a healthy workflow stays silent -- is exercised by
`apps/swarm-ui/test/checks.test.mjs`, which imports `src/checks.ts` and runs it
under `node --test`. That suite needs node and `npm ci`, and `make test` is
offline by contract, so it is NOT in the gate; running it is one command
(`cd apps/swarm-ui && npm test`) and the report for this change names that gap.

What was here and is now `contract.constants.test.ts`'s, against the constants
generated from the Python objects (part of #72): the park vocabulary and its
three groups, the stall threshold against `dispatch_timeout_seconds` and the
measured cold start, and the in-flight gate against the frozen
CONCURRENCY_STATES. What is left reads the console's source alone: where the
threshold's number came from, and copy that never sends a reader of a PARKED
task, which holds no capacity (CONTRACT invariant 1), to raise a ceiling.

The TypeScript is read as TEXT, the same technique `test_dispatch_ui_surface`
uses and for the same reason: no node, no network, no credentials, and a mock
of the UI would agree with whatever the UI happens to do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing api.ts seam test: this suite has to pass in a tree that holds only
#: the Python services.
pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui/src is not present")

def _src(name: str) -> str:
    return (UI / name).read_text()


def _code(name: str) -> str:
    """The module with its comments removed.

    Every rule below that forbids a SPELLING has to read code rather than
    prose, because this house style explains the trap it is avoiding directly
    above the line that avoids it -- `checks.ts` contains the sentence
    "`w.rollup?.complete || true` is true for every input there is" three lines
    above the comparison that does not do that. A scanner that cannot tell the
    warning from the defect fails on the warning.

    Block comments go entirely; a line comment is only stripped when it starts
    the line, so a `//` inside a string literal survives.
    """
    src = re.sub(r"/\*.*?\*/", "", (UI / name).read_text(), flags=re.S)
    return "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("//"))


# --------------------------------------------------------------------------
# 1. The stall threshold, and where its number came from
# --------------------------------------------------------------------------

def test_the_stall_threshold_says_where_its_number_came_from():
    """CLAUDE.md: if you changed a default, say WHY next to the value."""
    src = _src("checks.ts")
    head = src[: src.index("const WORKFLOW_STALL_SECONDS")]
    comment = head[head.rindex("/**") :]
    assert "122.6" in comment and "159.0" in comment, (
        "the measured cold-start distribution is not cited beside the threshold"
    )
    assert "dispatch_timeout_seconds" in comment, (
        "the platform setting that bounds the threshold is not cited beside it"
    )


def test_the_stall_threshold_is_crossed_with_an_inclusive_comparison():
    """`>=`, so the boundary second is inside the window it names.

    A `>` here makes "has not advanced in 10 minutes" false at exactly ten
    minutes, which is the off-by-one that makes a threshold unexplainable.
    """
    assert re.search(r"quiet\s*>=\s*WORKFLOW_STALL_SECONDS", _code("checks.ts")), (
        "the stall comparison is not `quiet >= WORKFLOW_STALL_SECONDS`"
    )


# --------------------------------------------------------------------------
# 2. Invariant 1: a parked step is a progress problem, not a capacity one
# --------------------------------------------------------------------------

def test_the_parked_copy_never_sends_a_reader_to_raise_a_ceiling():
    """A parked task holds no capacity, so the copy must not imply it does.

    An operator told their work is parked because the platform is full raises a
    pool ceiling that was never binding, and the work still does not move. This
    scans the parked check's own strings, not the whole file: `poolCheck` above
    it is legitimately about ceilings.
    """
    src = _code("checks.ts")
    body = src[src.index("function parkedCheck(") :]
    body = body[: body.index("\nfunction unitFor(")]
    forbidden = ("at its limit", "ceiling", "over capacity", "the platform is full")
    for phrase in forbidden:
        assert phrase not in body, (
            f"parkedCheck's copy contains {phrase!r}; a parked task holds no "
            "capacity (CONTRACT invariant 1) and this would send a reader to "
            "the wrong screen"
        )
    assert "hold no capacity" in body or "holds no capacity" in body, (
        "parkedCheck never states that parked work holds no capacity, which is "
        "the one thing a reader needs in order not to go looking at pools"
    )


def test_the_stall_copy_says_it_is_a_progress_problem():
    src = _code("checks.ts")
    body = src[src.index("function workflowCheck(") : src.index("\nfunction describeSteps(")]
    assert "not a full pool" in body


# --------------------------------------------------------------------------
# 3. The panel says "workflow" at all
# --------------------------------------------------------------------------

def test_derive_checks_registers_a_workflow_check_and_a_parked_check():
    """The audit's finding, pinned.

    `deriveChecks` is the whole list; the panel prints every check's label in
    its provenance line, so a check that is not in this list cannot appear on
    the screen in any state -- not as a problem, not as a clear, not as blind.
    """
    src = _code("checks.ts")
    body = src[src.index("export function deriveChecks(") :]
    body = body[: body.index("\n}")]
    for call in ("workflowCheck(s.workflows, now)", "parkedCheck(s.tasks, now)"):
        assert call in body, f"deriveChecks does not call {call}"


def test_overview_reads_the_workflow_route():
    """The screen has to FETCH workflows, not only be able to judge them."""
    assert "loadWorkflows" in _src("Overview.tsx"), (
        "Overview.tsx does not read the workflow route, so its workflow check "
        "can only ever report `reading`"
    )
    # `read()` takes the value `route()` builds, never a bare string (CH-18,
    # swarm-ui fetch.ts): the registry is keyed by the route template, and a
    # string could key it by a concrete URL. The path literal is the same.
    assert re.search(r"read<WorkflowPage>\((?:route\()?'/v1/workflows\?limit=\d+'", _src("api.ts")), (
        "api.ts has no loadWorkflows reading GET /v1/workflows"
    )


def test_the_checks_take_their_clock_as_an_argument():
    """Two checks measure an age; a function that reads the clock itself cannot
    be tested against a threshold at all."""
    assert re.search(
        r"export function deriveChecks\(s: CheckInputs, now: number\)", _code("checks.ts")
    )


def test_the_rollup_completeness_read_avoids_the_alternative_operator_trap():
    """`false // true` is TRUE in jq, and `false || true` is TRUE in TypeScript.

    CLAUDE.md records the jq form: `.enabled // true` reports a PAUSED pool as
    open. The same shape here -- `w.rollup?.complete || true` -- is true for
    every input there is, so an INCOMPLETE rollup would be judged as complete
    and this screen would report a workflow state derived from steps the API
    could not read. `?? true` has the subtler half: it keeps `false` but invents
    agreement for a missing rollup. The fix is an explicit comparison, exactly
    as the shell rule says (`.enabled == false`).
    """
    src = _code("checks.ts")
    assert "w.rollup.complete === false" in src, (
        "the rollup completeness read is not an explicit `=== false` comparison"
    )
    assert not re.search(r"complete\s*(\|\||\?\?)\s*true", src), (
        "checks.ts defaults `rollup.complete` with `||` or `??`; both report an "
        "incomplete rollup as complete, which is the jq `false // true` trap"
    )
