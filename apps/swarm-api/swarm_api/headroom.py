"""Every pool that refuses a runner profile, and what relaxing one would buy.

WHY THIS IS PYTHON ON THE SERVER AND NOT ARITHMETIC IN THE BROWSER
------------------------------------------------------------------
`swarm_common.admission.evaluate_capacity` already returns a LIST of blockers,
one entry per pool that refused. Two clients re-derived that list rather than
reading it, and both collapsed it to a single pool because both kept only a
running minimum:

  * `headroomFor` in `apps/swarm-ui/src/types.ts` -- `binding = name` overwrote
    on every new minimum, so the screen named one pool;
  * `binding_pool` in `apps/swarm-mcp/swarm_mcp/render.py` -- same shape, same
    single answer (left alone here; see the report).

When `resource:large` and `provider:anthropic` are at their ceilings at the
same moment, a screen that names one of them sends an operator to raise a limit
that changes nothing, and the pool that is actually still refusing never
appears. That is the specific trap the minimum-across-pools rule sets.

`docs/contract-change-requests.md`, "Why these requests keep arising", already
settled which remedy works for this repository. There are three restatement
surfaces: shell/jq is pinned by `scripts/lib/check-contract-parity.sh`,
Terraform's catalogue is pinned by `tests/terraform/catalogue.tftest.hcl`, and
TypeScript is pinned by **nothing** -- neither `make lint` nor `make test` runs
`tsc`, and `apps/swarm-ui` has no test runner at all. Its conclusion, verbatim:
"where one reader is TypeScript, nothing ends it today, and a route that serves
the value is the only remedy this repository has actually made work."

A parity check can hold a TABLE of constants to the Python. It cannot hold an
ALGORITHM to it, and the counterfactual below is an algorithm, not a table. So
this module computes the answer once, on the server, from the frozen function
itself, and `/v1/capacity` serves it. The browser renders what it is given.

HOW IT AVOIDS BEING A FOURTH RESTATEMENT
----------------------------------------
Nothing here re-implements "has capacity". Every question is answered by
calling `evaluate_capacity` -- the same function admission calls inside the
transaction -- and reading its answer:

  * "can one task of this profile start?"    -> evaluate_capacity(..., units)
  * "can n tasks start?"                     -> evaluate_capacity(..., n*units)
  * "what stops the next one?"               -> evaluate_capacity(..., (n+1)*units)

`has_capacity(u)` is `enabled and active + u <= effective_limit`, which is
monotone in `u`: once a pool refuses `u` it refuses everything larger. So
"n tasks fit" is monotone too, and the largest fitting `n` is found by binary
search over the predicate rather than by re-deriving the arithmetic. If
admission's rule ever changes, this follows it without being edited.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Sequence

from swarm_common.admission import evaluate_capacity
from swarm_common.models import SlotPool
from swarm_common.states import BlockedReason

# --------------------------------------------------------------------------
# The two groups
# --------------------------------------------------------------------------
# The split is by REMEDY, which is the only thing an operator can act on:
# somebody has to do something, versus waiting is a correct answer. Grouping by
# anything else (severity, scope, which subsystem raised it) produces a list
# where the item needing a human sits between two items that need nobody.

#: Somebody must act. Waiting does not clear any of these.
NEEDS_ACTION: frozenset[BlockedReason] = frozenset(
    {
        BlockedReason.MANUAL_PAUSE,       # resume the pool
        BlockedReason.DEPENDENCY,         # an upstream step has to finish or be fixed
        BlockedReason.BUDGET_LIMIT,       # raise the budget
        BlockedReason.QUOTA_EXHAUSTED,    # register a key, or buy quota
    }
)

#: Eligible, no room. Waiting clears every one of these on its own.
NO_ROOM: frozenset[BlockedReason] = frozenset(
    {
        BlockedReason.GLOBAL_CONCURRENCY_LIMIT,
        BlockedReason.TENANT_LIMIT,
        BlockedReason.PROVIDER_CONCURRENCY_LIMIT,
        BlockedReason.RESOURCE_CLASS_LIMIT,
        BlockedReason.RUNNER_LIMIT,
        BlockedReason.BACKEND_LIMIT,
        BlockedReason.COOLDOWN,
        BlockedReason.SCHEDULED_RETRY,
    }
)

GROUP_NEEDS_ACTION = "needs_action"
GROUP_NO_ROOM = "no_room"

_GROUP_BY_REASON: dict[str, str] = {
    **{r.value: GROUP_NEEDS_ACTION for r in NEEDS_ACTION},
    **{r.value: GROUP_NO_ROOM for r in NO_ROOM},
}


def group_for(reason: str) -> str | None:
    """Which group a blocker reason belongs to, or None when it is neither.

    None is a real answer and must not be collapsed into either group. A
    blocker entry can carry a string that is not a `BlockedReason` at all:
    `admission.py` raises `AdmissionDenied` with bare `task_missing`,
    `not_ready` and `cancel_requested` before it ever reaches the pools, and a
    worker park writes its own `ParkReason` values. Filing one of those under
    "waiting is a valid answer" would be a lie about the remedy, which is the
    whole reason the grouping exists.
    """
    return _GROUP_BY_REASON.get(reason)


def blocked_reason_groups() -> dict[str, list[str]]:
    """The grouping, as data, so no client has to restate it.

    Served on `/v1/capacity`. `tests/unit/control_plane/test_blocker_groups.py`
    asserts the two sets partition `BlockedReason` exactly, so a new member of
    the enum fails a test rather than quietly defaulting into a group.
    """
    return {
        GROUP_NEEDS_ACTION: sorted(r.value for r in NEEDS_ACTION),
        GROUP_NO_ROOM: sorted(r.value for r in NO_ROOM),
    }


# --------------------------------------------------------------------------
# Headroom, by asking the frozen function rather than by re-deriving it
# --------------------------------------------------------------------------

#: How the headroom number was arrived at. Three cases that a single integer
#: cannot tell apart, and that have three different remedies:
#:   measured  -- every required pool was read; the number is a measurement
#:   uncapped  -- not one required pool is configured, so nothing caps this at
#:                all. That is not zero and it is not a number either.
#:   unknown   -- at least one required pool could not be read. The true
#:                headroom may be anything, including zero.
BASIS_MEASURED = "measured"
BASIS_UNCAPPED = "uncapped"
BASIS_UNKNOWN = "unknown"


def _fits(pools: dict[str, SlotPool], required: Sequence[str], units: int) -> bool:
    return not evaluate_capacity(pools, required, units)


def _ceiling(pools: dict[str, SlotPool], required: Sequence[str], units: int) -> int | None:
    """An upper bound on how many tasks could ever fit. None means unbounded.

    For any configured pool p, admission needs `active_p + n*units <=
    effective_limit_p`, and `active_p >= 0`, so `n <= effective_limit_p //
    units`. The tightest such bound over the configured pools bounds the search
    below. When no required pool is configured there is no bound, which is the
    None case -- `evaluate_capacity` skips an absent pool because an
    unconfigured pool is unlimited by construction.
    """
    limits = [pools[name].effective_limit for name in required if name in pools]
    if not limits:
        return None
    return min(limits) // max(1, units)


def _headroom(pools: dict[str, SlotPool], required: Sequence[str], units: int) -> int | None:
    """How many tasks of this profile fit right now. None means unbounded.

    Binary search over `_fits`, which is `evaluate_capacity`. Linear counting
    would be wrong here rather than merely slow: `store.UNLIMITED_HARD_LIMIT`
    is 1_000_000, so a pool created without an explicit limit would make the
    loop run a million times per profile per request.
    """
    cap = _ceiling(pools, required, units)
    if cap is None:
        return None
    if cap == 0 or not _fits(pools, required, units):
        return 0
    low, high = 1, cap
    # A FIXED probe budget rather than `while low < high`. Bisection needs the
    # gap to shrink every pass, and the way that breaks is an off-by-one in
    # `mid` -- `(low + high) // 2` is the obvious-looking version and it stalls
    # at `low == high - 1`, because `mid` comes back as `low` and `low = mid`
    # changes nothing. In a synchronous request handler that is not a wrong
    # number, it is a worker that never returns. Bounding the loop converts
    # that mistake into a wrong answer, which a test can catch.
    for _ in range(cap.bit_length() + 2):
        if low >= high:
            break
        mid = (low + high + 1) // 2
        if _fits(pools, required, mid * units):
            low = mid
        else:
            high = mid - 1
    return low


def _stopping(
    pools: dict[str, SlotPool],
    required: Sequence[str],
    units: int,
    fitting: int | None,
) -> list[dict[str, Any]]:
    """Which pools refuse ONE MORE task than currently fits.

    At `fitting == 0` this is the list of pools refusing the task outright --
    the answer to "why is nothing starting". Above zero it is the set of
    ceilings that bind. They are the same question asked at different n, which
    is why they share an implementation: `evaluate_capacity` itself.
    """
    if fitting is None:
        return []
    return [_tag(b) for b in evaluate_capacity(pools, required, (fitting + 1) * units)]


def _tag(blocker: dict[str, Any]) -> dict[str, Any]:
    """A blocker from `evaluate_capacity`, plus which remedy group it is in."""
    out = dict(blocker)
    out["group"] = group_for(str(blocker.get("reason", "")))
    return out


def _counterfactual(
    pools: dict[str, SlotPool],
    required: Sequence[str],
    units: int,
    fitting: int | None,
) -> list[dict[str, Any]]:
    """For each required pool, what relaxing THAT ONE would have bought.

    The zeros are the valuable half. Under a minimum-across-pools rule, raising
    a ceiling that is not the binding one changes nothing at all, and there is
    no way to tell which ceilings those are by looking at the numbers -- which
    is exactly the mistake the rule invites.

    Two different remedies, so two different counterfactuals:

      * a PAUSED pool is relaxed by re-enabling it and leaving its limit alone,
        because "resume it" is the actual remedy and modelling it as unlimited
        would overstate what resuming buys;
      * any other pool is relaxed by removing it, which `evaluate_capacity`
        reads as unconfigured and therefore unlimited. That is the strongest
        possible form of "raise it": if the delta is still 0 here, then raising
        that ceiling to ANY value buys nothing, which is a much more useful
        statement than one about a particular target number.
    """
    out: list[dict[str, Any]] = []
    for name in required:
        pool = pools.get(name)
        if pool is None:
            # Nothing to raise: an unconfigured pool is already unlimited and
            # caps nothing, so a row for it would be noise with a 0 in it.
            continue
        if not pool.enabled:
            relaxed = dict(pools)
            relaxed[name] = replace(pool, enabled=True)
            action = "resume"
        else:
            relaxed = {k: v for k, v in pools.items() if k != name}
            action = "raise"
        after = _headroom(relaxed, required, units)
        out.append(
            {
                "pool": name,
                "action": action,
                "headroom_after": after,
                "basis_after": BASIS_UNCAPPED if after is None else BASIS_MEASURED,
                # None, not 0, when either side is unbounded: "unbounded minus
                # a number" is not a delta and must not render as one.
                "delta": None if (after is None or fitting is None) else after - fitting,
                # What would bind INSTEAD. This is the half that stops the
                # counterfactual reading as "raise this and you are done" when
                # a second pool is also at its ceiling.
                "next_binding": [b["pool"] for b in _stopping(relaxed, required, units, after)],
            }
        )
    return out


def analyse_profile(
    *,
    required: Sequence[str],
    pools: dict[str, SlotPool],
    units: int,
    unread: Iterable[str] = (),
) -> dict[str, Any]:
    """The full admission picture for one runner profile.

    `pools` holds only the required pools that were actually read. `unread`
    names required pools whose state could not be established -- not pools that
    do not exist. The difference decides everything:

      * absent AND readable  -> unconfigured, therefore unlimited by
        construction, therefore skipped. A fact, not a gap.
      * unread               -> the answer is not knowable. One unread pool
        could be the one refusing, so a headroom computed from the pools that
        did answer would be a confident number whose only support is that the
        pool which might have contradicted it stayed silent.

    The second case returns `headroom: None` with `complete: False` and keeps
    every blocker that WAS measured. Dropping them would shorten the list
    silently, which is worse than the single-name bug this module replaces: it
    would tell an operator they had cleared everything when they had not.
    """
    missing = [name for name in unread if name]
    present_absent = [name for name in required if name not in pools and name not in missing]

    if missing:
        return {
            "units": units,
            "headroom": None,
            "basis": BASIS_UNKNOWN,
            # Still reported. Partial is not the same as absent, and these are
            # real measurements -- they just are not the whole list.
            "blockers": _stopping(pools, [n for n in required if n in pools], units, 0),
            "binding": [],
            "counterfactual": [],
            "complete": False,
            "unread": sorted(missing),
            "uncapped": present_absent,
        }

    fitting = _headroom(pools, required, units)
    return {
        "units": units,
        "headroom": fitting,
        "basis": BASIS_UNCAPPED if fitting is None else BASIS_MEASURED,
        # Every pool refusing a task RIGHT NOW. The list, not its minimum.
        "blockers": _stopping(pools, required, units, 0),
        # Every pool that caps the next one beyond what fits. Identical to
        # `blockers` when nothing fits, which is the point: "why can nothing
        # start" and "what caps this" are one question asked at different n.
        "binding": [b["pool"] for b in _stopping(pools, required, units, fitting)],
        "counterfactual": _counterfactual(pools, required, units, fitting),
        "complete": True,
        "unread": [],
        "uncapped": present_absent,
    }
