"""A step's session limits, by its ROLE, when its caller named none.

CONTRACT REQUEST 158, accepted by the owner 2026-11-24: the claude-code
runner family declares `context_ceiling`, an optional object input
(`{"type": "limits", "tokens": 1..200000, "tool_calls": 1..150}`) that
LOWERS the step's session limits -- the context, in tokens, past which a
session is ended at a turn boundary and a fresh one started from a handoff,
and the top-level tool calls past which the same happens
(`agent_worker.runners.ceiling`, `resolve_limits`). Nothing raises them past
the owner's 200,000 and 150: the declared bounds stop there and the worker
clamps again.

`ROLE_CEILINGS` is the per-role table, beside `stepclass.ROLE_CLASSES`, and
every site that calls `stepclass.choose` for one of these roles also calls
`stamp` (plus the first review at workflow submission, `service`), so a later
change is one edit here.

THE RULES (`stamp`), in order:

  * a value the step already carries -- its CALLER's -- always wins; it was
    or will be checked against the declared bounds at submission
    (`validation.validate_runner_input`), and refused there naming the field;
  * a role with no row (`build`) is not stamped: it keeps today's limits;
  * a profile that does not declare the input (`codex`, `mock`, the
    control-plane profiles) is not stamped: it would refuse the step;
  * otherwise the role's row, written in the shape the declaration accepts.

WHERE THE ROLE COMES FROM. Never from a metadata field a caller writes
(request 158's caution): each call site passes the role constant it already
resolved its model class from, or, for a first review at submission, the
workflow's own graph (a step another step's `when` reads). A stamp only ever
LOWERS a limit, and a caller may set its own value inside the bounds anyway,
so naming a role buys a caller nothing it could not ask for directly.

THE SPEC SIGNATURE COVERS IT. The value is inside `input`, which every spec
format signs (`swarm_common.specsign`), and every stamp happens before the
task is built and signed, so a worker cannot be handed another value: the
spec would not verify.

INVARIANT 10: two bounded integers and a fixed shape name. No image, command,
resource spec or backend parameter is read from a caller.

WHY THESE VALUES. The scorecard (`scripts/lib/scorecard.py`,
docs/scorecard.md) defines the per-role session measures these are to be
tuned from (tool calls and turns per session, sessions ended at the ceiling,
fresh sessions per attempt), but no run of it is recorded in the repository,
so no per-role session distribution was available on 2026-11-24. What is
recorded is `plan_costs.json` (measured 2026-11-23 over 30 days): a build step
63 turns and $3.60 at the median, a review $1.10, a fix $1.40, a turn count
past which a step costs disproportionately of about 100, and docs/agent-output.md's
finding that a session's cost grows with roughly the SQUARE of its length (each
turn re-reads the context). Read under that square law, a review's median
session is about 63 x sqrt(1.10 / 3.60) = 35 turns and a fix's about
63 x sqrt(1.40 / 3.60) = 39. Each limit below sits about two and a half times
over its role's estimated median, so it ends only the long tail -- the
sessions that cost the most -- and never a typical one, which would only buy
a handoff. Re-tune from the scorecard's "sessions ended at the ceiling %, by
role" once a run of it is read: a role whose stops pass a few percent is set
too low.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from swarm_common.profiles import RUNNER_PROFILES, RunnerProfile

from . import stepclass

#: The date of the owner's acceptance of contract request 158.
ACCEPTED = "2026-11-24"

#: The step input the claude-code runner reads (`ceiling.PAYLOAD_KEY`).
INPUT_KEY = "context_ceiling"
#: The declaration's one shape (`profiles._CONTEXT_CEILING_INPUTS`): the
#: value of the object's `type` key. The worker ignores it.
SHAPE = "limits"

#: Every review role: a first review, whatever the gate later decides about
#: its CLASS, a security review. One row for all of them, because a first
#: review is stamped at submission, before the gate reads its diff and
#: re-signs it onto a class (`stepclass.review_role`); the gate changes only
#: the class, so the limits must not depend on which review role it picks.
#: 90 tool calls: about 2.5 x a review's estimated median of 35 turns (the
#: module doc). 150,000 tokens: a review reads the diff it was given (up to
#: `stepclass.REVIEW_CHEAP_MAX_LINES` lines and past it) and the files around
#: it, so it keeps three quarters of the owner's context; past that each turn
#: re-reads at least 150k tokens to write one verdict, and a handoff with the
#: notes is the cheaper way on.
_REVIEW = MappingProxyType({"tokens": 150_000, "tool_calls": 90})

#: Role -> limits, applied only when the step carries none. No `build` row:
#: a build step keeps today's 200,000 / 150 (its median is already 63 turns,
#: and it is the role the owner's ceiling was sized on).
ROLE_CEILINGS: Mapping[str, Mapping[str, int]] = MappingProxyType({
    # The planner explores the repository and writes one plan; a handoff
    # mid-plan loses the picture the plan is made from, so it keeps the
    # owner's full context. 120 tool calls: no planner cost is recorded, but
    # a planner that needs more than the knee of ~100 calls to read an issue
    # is looping, and 120 leaves a fifth over the knee for a large issue.
    stepclass.ROLE_PLANNER: MappingProxyType({"tokens": 200_000, "tool_calls": 120}),
    stepclass.ROLE_REVIEW: _REVIEW,
    stepclass.ROLE_REVIEW_RISK: _REVIEW,
    stepclass.ROLE_REVIEW_UNREAD: _REVIEW,
    stepclass.ROLE_REVIEW_LARGE: _REVIEW,
    stepclass.ROLE_SECURITY_REVIEW: _REVIEW,
    # The re-review reads the previous verdict and what the fix changed: no
    # more than the review it follows, and no separate measure is recorded,
    # so it takes the review's row rather than a guess below it.
    stepclass.ROLE_REREVIEW: _REVIEW,
    # A fix: about 2.5 x its estimated median of 39 turns is 98, so 100
    # tool calls. 150,000 tokens, as a review: it reads the findings and the
    # code they name, edits, and runs the related tests.
    stepclass.ROLE_FIX: MappingProxyType({"tokens": 150_000, "tool_calls": 100}),
    # A CI fix is a fix whose findings are a failing check's excerpt: the
    # fix's row. No separate cost is recorded for it.
    stepclass.ROLE_CI_FIX: MappingProxyType({"tokens": 150_000, "tool_calls": 100}),
    # A conflict fix merges the base in, resolves each conflict and runs the
    # tests the conflicts touched: a fix's work over a merge's files, so the
    # fix's row. No separate cost is recorded for it.
    stepclass.ROLE_CONFLICT_FIX: MappingProxyType({"tokens": 150_000, "tool_calls": 100}),
})

#: The role a first review is stamped with at workflow submission: the role
#: it has until the review gate reads its diff (`stepclass.review_role`
#: returns it for a review whose diff was never read). Every review role
#: shares `_REVIEW`, so the gate's later pick leaves the stamp right.
SUBMITTED_REVIEW_ROLE = stepclass.ROLE_REVIEW_UNREAD


def _profile(profile: str | RunnerProfile | None) -> RunnerProfile | None:
    if profile is None or isinstance(profile, RunnerProfile):
        return profile
    return RUNNER_PROFILES.get(str(profile))


def default_for(role: str) -> dict[str, Any] | None:
    """The `context_ceiling` value `role` is stamped with, or None (no row)."""
    limits = ROLE_CEILINGS.get(role)
    return None if limits is None else {"type": SHAPE, **limits}


def stamp(role: str, profile: str | RunnerProfile | None,
          step_input: Mapping[str, Any] | None) -> dict[str, Any]:
    """`step_input` with `role`'s `context_ceiling` added (the rules in the module doc).

    Always a new dict; the caller's own value, a role with no row and a
    profile that does not declare the input all leave it as it was.
    """
    out = dict(step_input or {})
    if INPUT_KEY in out:
        return out
    value = default_for(role)
    resolved = _profile(profile)
    declared = (resolved.inputs or {}) if resolved is not None else {}
    if value is None or INPUT_KEY not in declared:
        return out
    # Written as the declaration normalises it, so the stored and signed
    # bytes are what the worker's re-check (`check_inputs`) hands its runner.
    out[INPUT_KEY] = declared[INPUT_KEY].check(INPUT_KEY, value)
    return out


def review_steps(steps: Any) -> frozenset[str]:
    """The steps of a workflow that are reviews: those another step's `when`
    reads a verdict from (the definition `validation.default_review_gate_steps`
    uses). Read from the graph, never from a step's metadata."""
    return frozenset(
        step.when.step for step in steps if getattr(step, "when", None) is not None
    )
