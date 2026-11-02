"""AIMD control for per-(provider, tenant) concurrency.

Additive increase, multiplicative decrease -- the same shape as TCP congestion
control, for the same reason: the safe operating point is unknown, changes
without warning, and the only feedback is failure. So the target creeps up while
things work and collapses the moment they do not.

The asymmetry is deliberate and is the whole design:

  INCREASE is SLOW and CONDITIONAL. One success proves nothing. The target rises
  by `additive_increase` only after `success_threshold` consecutive successes
  with no intervening 429, and the success run resets on every rate limit. Being
  wrong here costs a burst of 429s and a provider that starts throttling
  everything, which is far more expensive than a few minutes of under-use.

  DECREASE is FAST and UNCONDITIONAL. A single 429 multiplies the target by
  `multiplicative_decrease` immediately. Waiting for a second data point means
  the second data point arrives as a wall of 429s.

  BUT ONLY A TENANT'S 429 REACHES IT (#2302). A 429 from one pool account's
  window is that account's: the broker records it on the account's health
  record and the account is kept from new work by its own window readings
  (S15), so it never calls `record_rate_limit`/`record_exhausted` here. On
  2026-10-25 one account's 429 halved eng from 26 to 14 while every other
  account had room. A 429 still lands here when it names no account (API-key
  traffic), when its caller cannot prove it holds the account it names, and
  when every account the tenant may draw from is limited at once -- the pool
  itself is spent (`quota_broker.main._account_scope`).

  RECOVERY ALSO RUNS ON THE CLOCK (owner, 2026-11-02). Counting successes
  alone, a workspace halved from 40 to 7 needed some 660 clean task finishes
  to climb back, which is days. So the target ALSO rises by one for every
  `recovery_interval_seconds` (5 minutes) without a workspace-wide refusal,
  up to the ceiling (`recover_over_time`). There is no timer: the climb is
  worked out when the target is read, from the time of the last cut or the
  last climb, which the store keeps on the quota document beside the state
  (`recovery_clock`). A refusal still halves at once and restarts that
  clock; only the recovery got faster.

THE INVARIANT THAT MUST NEVER BREAK:

    adaptive_target <= ceiling = min(configured_hard_max, pool hard limit), always.

`configured_hard_max` is an admin's statement about what the platform is allowed
to do to a provider -- a contractual or budget ceiling, not a guess. The
per-tenant pool's own `hard_limit` (`provider:<p>:tenant:<t>`) is the same kind
of statement, and it is the number the console shows; on 2026-10-24 the pool
said 40 while the quota document said 50, and halving started from 50. So the
lower of the two is the ceiling (owner, 2026-11-02), and the routes that set
either one now set both. AIMD may only ever propose a number at or BELOW it.
Every function here clamps on the way out, and `_clamp` is the single place
that arithmetic happens, so a new signal cannot be added that forgets to clamp.
A caller that has not read the pool passes no `pool_hard_limit`, and the
quota document's maximum is the ceiling alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from swarm_common.models import ProviderState, QuotaState, utcnow


@dataclass(frozen=True)
class AimdConfig:
    #: Slots added per successful run. One: the point is that increase is slow.
    additive_increase: int = 1
    #: Multiplier applied on a 429. Halving is the classic choice and is
    #: aggressive enough to get under a provider's limit in a couple of steps.
    multiplicative_decrease: float = 0.5
    #: Consecutive successes required before a single additive increase.
    success_threshold: int = 20
    #: The target never drops below this, so a provider that recovers has
    #: something to grow from; zero would need an external nudge to restart.
    min_target: int = 1
    #: Cooldown applied after a 429 when the provider sent no Retry-After.
    default_cooldown_seconds: int = 30
    #: Ceiling on any provider-supplied Retry-After. A provider asking for an
    #: hour must not pin a tenant's tasks in a cooldown the platform cannot see
    #: the end of -- the tasks park and the window is re-checked.
    max_cooldown_seconds: int = 900
    #: Consecutive 429s that mean "stop entirely" rather than "slow down".
    exhaustion_threshold: int = 5
    #: Seconds without a workspace-wide refusal that raise the target by one,
    #: on top of the success-based climb (owner, 2026-11-02). 0 turns the
    #: clock off and leaves only the success-based climb.
    recovery_interval_seconds: int = 300

    def __post_init__(self) -> None:
        if self.additive_increase <= 0:
            raise ValueError("additive_increase must be positive")
        if not 0 < self.multiplicative_decrease < 1:
            raise ValueError("multiplicative_decrease must be a fraction in (0, 1)")
        if self.success_threshold <= 0:
            raise ValueError("success_threshold must be positive")
        if self.min_target < 0:
            raise ValueError("min_target cannot be negative")
        if self.recovery_interval_seconds < 0:
            raise ValueError("recovery_interval_seconds cannot be negative")


def ceiling_of(state: QuotaState, pool_hard_limit: int | None = None) -> int:
    """The highest target allowed: the lower of the quota document's
    `configured_hard_max` and the per-tenant pool's `hard_limit`, when the
    caller read the pool. Never below 0."""
    ceiling = max(0, int(state.configured_hard_max))
    if pool_hard_limit is not None:
        ceiling = min(ceiling, max(0, int(pool_hard_limit)))
    return ceiling


def _clamp(
    value: int | float,
    state: QuotaState,
    config: AimdConfig,
    pool_hard_limit: int | None = None,
) -> int:
    """The ONLY place an adaptive target is produced.

    Clamps into [min_target, ceiling], the ceiling being `ceiling_of`. If an
    admin sets a ceiling below min_target, the ceiling wins -- an operator's
    explicit ceiling outranks this module's preference for keeping a little
    headroom alive.
    """
    ceiling = ceiling_of(state, pool_hard_limit)
    floor = min(config.min_target, ceiling)
    target = int(math.floor(value))
    if target < floor:
        target = floor
    if target > ceiling:
        target = ceiling
    return target


def current_target(
    state: QuotaState, config: AimdConfig, *, pool_hard_limit: int | None = None
) -> int:
    """The working target, clamped. Never trusts a stored value."""
    if state.adaptive_target is None:
        return _clamp(state.configured_hard_max, state, config, pool_hard_limit)
    return _clamp(state.adaptive_target, state, config, pool_hard_limit)


def initial_state(provider: str, tenant_id: str, *, configured_hard_max: int,
                  now: datetime | None = None) -> QuotaState:
    moment = now or utcnow()
    return QuotaState(
        provider=provider,
        tenant_id=tenant_id,
        state=ProviderState.AVAILABLE,
        updated_at=moment,
        configured_hard_max=configured_hard_max,
        adaptive_target=configured_hard_max,
    )


def _run_count(state: QuotaState) -> int:
    """The stored count of the CURRENT rate-limit run, read honestly.

    `rate_limit_count` is the number of 429s in the run that is still going
    (CP-10, #85; owner decision 2026-09-25). A run starts at a 429 and ends
    when a success is reported (`record_success` writes 0) or when `refresh`
    retires the cooldown and the reset window (it writes 0 too). The
    exhaustion threshold therefore counts CONSECUTIVE 429s, which is what
    `AIMD_EXHAUSTION_THRESHOLD` is documented to count.

    A stored count with no `last_429_at` behind it is read as 0. Documents the
    worker used to create directly were seeded with a 1 nobody had observed;
    reading them this way corrects them on their next report, with no one-off
    Firestore write.
    """
    if state.last_429_at is None:
        return 0
    return max(0, int(state.rate_limit_count))


def record_success(
    state: QuotaState,
    *,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
    pool_hard_limit: int | None = None,
    requests_remaining: int | None = None,
    tokens_remaining: int | None = None,
    reset_at: datetime | None = None,
) -> QuotaState:
    """One successful provider call.

    Increases the target only once a full run of `success_threshold` successes
    has accumulated, then resets the run. A provider that is DISABLED by an
    admin is never raised here; only the admin path can leave that state.
    """
    moment = now or utcnow()
    if state.state is ProviderState.DISABLED:
        return replace(
            state,
            success_count=state.success_count + 1,
            rate_limit_count=0,
            updated_at=moment,
        )

    successes = state.success_count + 1
    target = current_target(state, config, pool_hard_limit=pool_hard_limit)
    if successes >= config.success_threshold:
        target = _clamp(target + config.additive_increase, state, config, pool_hard_limit)
        successes = 0

    return replace(
        state,
        state=ProviderState.AVAILABLE,
        adaptive_target=target,
        success_count=successes,
        # A success proves the window is open, so the cooldown and the
        # quota-derived cap are stale by definition.
        cooldown_until=None,
        quota_derived_limit=None,
        retry_after_seconds=None,
        # A clean call ends the rate-limit run. `refresh` never resets a state
        # that is already AVAILABLE, so without this the count would carry
        # 429s from runs that are over, and five 429s spread across a day of
        # successes would read as five consecutive ones and exhaust the tenant.
        # `last_429_at` is kept: it is when the last 429 was, not a run marker.
        rate_limit_count=0,
        requests_remaining=(
            requests_remaining if requests_remaining is not None else state.requests_remaining
        ),
        tokens_remaining=(
            tokens_remaining if tokens_remaining is not None else state.tokens_remaining
        ),
        reset_at=reset_at if reset_at is not None else state.reset_at,
        updated_at=moment,
    )


def record_rate_limit(
    state: QuotaState,
    *,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
    pool_hard_limit: int | None = None,
    retry_after_seconds: int | None = None,
    reset_at: datetime | None = None,
) -> QuotaState:
    """A 429. Cut hard and immediately.

    `retry_after` and `reset_at` are honoured when the provider sends them,
    because a provider's own statement about when the window reopens is better
    information than any backoff we could invent. Retry-After is capped at
    `max_cooldown_seconds` so a very long value parks work rather than pinning
    it in an invisible wait.
    """
    moment = now or utcnow()
    rate_limits = _run_count(state) + 1
    before = current_target(state, config, pool_hard_limit=pool_hard_limit)
    target = _clamp(before * config.multiplicative_decrease, state, config, pool_hard_limit)

    cooldown = retry_after_seconds if retry_after_seconds is not None else None
    if cooldown is None and reset_at is not None:
        cooldown = int(max(0, (reset_at - moment).total_seconds()))
    if cooldown is None:
        cooldown = config.default_cooldown_seconds
    cooldown = max(0, min(int(cooldown), config.max_cooldown_seconds))

    exhausted = rate_limits >= config.exhaustion_threshold
    next_state = ProviderState.EXHAUSTED if exhausted else ProviderState.THROTTLED
    if state.state is ProviderState.DISABLED:
        next_state = ProviderState.DISABLED

    return replace(
        state,
        state=next_state,
        adaptive_target=target,
        # While cooling down, nothing new may start on this provider for this
        # tenant. EXHAUSTED and COOLDOWN both drive effective_limit to 0 in the
        # frozen model, which is what makes an exhausted provider stop the work
        # rather than merely slow it.
        quota_derived_limit=0,
        cooldown_until=moment + timedelta(seconds=cooldown),
        retry_after_seconds=cooldown,
        reset_at=reset_at if reset_at is not None else state.reset_at,
        last_429_at=moment,
        rate_limit_count=rate_limits,
        success_count=0,          # the success run is broken; start it over
        updated_at=moment,
    )


def record_exhausted(
    state: QuotaState,
    *,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
    pool_hard_limit: int | None = None,
    reset_at: datetime | None = None,
    retry_after_seconds: int | None = None,
) -> QuotaState:
    """The provider says the quota window is spent, not merely busy."""
    moment = now or utcnow()
    before = current_target(state, config, pool_hard_limit=pool_hard_limit)
    cooldown = retry_after_seconds
    if cooldown is None and reset_at is not None:
        cooldown = int(max(0, (reset_at - moment).total_seconds()))
    if cooldown is None:
        cooldown = config.max_cooldown_seconds
    cooldown = max(0, min(int(cooldown), config.max_cooldown_seconds))
    return replace(
        state,
        state=ProviderState.EXHAUSTED if state.state is not ProviderState.DISABLED
        else ProviderState.DISABLED,
        adaptive_target=_clamp(
            before * config.multiplicative_decrease, state, config, pool_hard_limit
        ),
        quota_derived_limit=0,
        cooldown_until=moment + timedelta(seconds=cooldown),
        retry_after_seconds=cooldown,
        reset_at=reset_at if reset_at is not None else state.reset_at,
        last_429_at=moment,
        rate_limit_count=_run_count(state) + 1,
        success_count=0,
        updated_at=moment,
    )


class TargetResetRefused(ValueError):
    """A reset an admin asked for that this module will not apply as asked."""


def reset_target(
    state: QuotaState,
    target: int,
    *,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
    pool_hard_limit: int | None = None,
    clear_cooldown: bool = False,
) -> QuotaState:
    """An admin's reset of the working target. The ONLY path that raises it
    other than additive increase (QUOTA-RESET, owner 2026-10-14).

    It exists because one 429 halves the target and the climb back is
    `additive_increase` per `success_threshold` successes: a tenant halved to 3
    under a hard max of 50 needs some 740 clean calls to get back to 40, and
    an operator who has just lent it more accounts knows something AIMD
    cannot. It is deliberately narrow:

      * the result goes through `_clamp`, so it lands in
        [min_target, configured_hard_max] and the invariant holds;
        `configured_hard_max` itself is never touched -- raising the ceiling
        is `set_hard_max`'s decision, not this one's;
      * the success run and the rate-limit run restart from the new target,
        so the next 429 halves FROM it and the exhaustion threshold counts
        afresh;
      * a cooldown is cleared only when asked. A raised target under a live
        cooldown still caps at 0 until the window reopens;
      * EXHAUSTED is refused unless the cooldown is cleared too: the provider
        has said the window is spent, and a raised target that opens nothing
        would read as if it had;
      * DISABLED stays DISABLED. Only the admin's enable switch leaves it.
    """
    moment = now or utcnow()
    if state.state is ProviderState.EXHAUSTED and not clear_cooldown:
        raise TargetResetRefused(
            f"{state.provider} is EXHAUSTED for tenant {state.tenant_id}: the "
            "provider said the quota window is spent, so a raised target would "
            "open nothing. Send clear_cooldown=true to reopen it as well"
        )
    changed = replace(
        state,
        adaptive_target=_clamp(int(target), state, config, pool_hard_limit),
        success_count=0,
        rate_limit_count=0,
        updated_at=moment,
    )
    if clear_cooldown:
        changed = replace(
            changed,
            state=(
                ProviderState.DISABLED
                if state.state is ProviderState.DISABLED
                else ProviderState.AVAILABLE
            ),
            cooldown_until=None,
            retry_after_seconds=None,
            quota_derived_limit=None,
            # `refresh` holds a state until BOTH the cooldown and the reset
            # window have passed; a cleared cooldown with a future `reset_at`
            # left behind would be reopened here and nowhere else.
            reset_at=None,
        )
    return changed


def refresh(
    state: QuotaState,
    *,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
    pool_hard_limit: int | None = None,
) -> QuotaState:
    """Expire a cooldown or a reset window that has passed.

    Called before every read of the effective limit. Without it an EXHAUSTED
    provider would stay at zero until the next live call happened to arrive --
    and no call can arrive while the limit is zero, which is a deadlock.
    """
    moment = now or utcnow()
    if state.state is ProviderState.DISABLED:
        return state

    cooled = state.cooldown_until is None or state.cooldown_until <= moment
    reset = state.reset_at is None or state.reset_at <= moment
    if not (cooled and reset):
        return state
    if state.state in (ProviderState.AVAILABLE, ProviderState.UNKNOWN):
        return state

    return replace(
        state,
        state=ProviderState.AVAILABLE,
        quota_derived_limit=None,
        cooldown_until=None,
        retry_after_seconds=None,
        reset_at=None if reset else state.reset_at,
        # The rate-limit run is over; keep the target where AIMD left it and let
        # additive increase climb back. Jumping straight back to the hard max is
        # how a provider gets a second wall of 429s.
        rate_limit_count=0,
        adaptive_target=current_target(state, config, pool_hard_limit=pool_hard_limit),
        updated_at=moment,
    )


def recover_over_time(
    state: QuotaState,
    *,
    clock_at: datetime | None,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
    pool_hard_limit: int | None = None,
) -> tuple[QuotaState, datetime | None, int]:
    """The time-based climb, worked out at the moment the target is read.

    Returns (state, clock, intervals): the target raised by one for every whole
    `recovery_interval_seconds` since `clock_at` -- the last cut or the last
    climb -- up to the ceiling; the clock moved on by the intervals spent, so a
    part-interval carries to the next read; and how many intervals were spent
    (0 when nothing rose). No timer or loop runs this: the store calls it on
    every read-modify-write and on the sweep's tick, which already runs every
    five minutes to retire cooldowns.

    `clock_at` None is a document written before the clock existed: its last
    429 starts the clock, else its last write. DISABLED never climbs; only the
    admin's enable switch leaves it. A target at or above the ceiling has
    nothing to climb and is returned as it is, the clock with it.
    """
    moment = now or utcnow()
    interval = int(config.recovery_interval_seconds)
    if interval <= 0 or state.state is ProviderState.DISABLED:
        return state, clock_at, 0
    start = clock_at or state.last_429_at or state.updated_at
    target = current_target(state, config, pool_hard_limit=pool_hard_limit)
    ceiling = ceiling_of(state, pool_hard_limit)
    if target >= ceiling or start is None or moment <= start:
        return state, clock_at, 0
    intervals = int((moment - start).total_seconds() // interval)
    if intervals <= 0:
        return state, start, 0
    raised = _clamp(target + intervals, state, config, pool_hard_limit)
    spent = min(intervals, raised - target)
    if spent <= 0:
        return state, clock_at, 0
    return (
        replace(state, adaptive_target=raised, updated_at=moment),
        start + timedelta(seconds=spent * interval),
        spent,
    )


def recovery_clock(
    before: QuotaState,
    after: QuotaState,
    *,
    clock_at: datetime | None,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
    pool_hard_limit: int | None = None,
) -> datetime | None:
    """The clock to store once a signal has turned `before` into `after`.

    A refusal (a new `last_429_at`) or any move of the target -- a halving, a
    success-based climb, an admin's reset or a lowered ceiling -- restarts it
    at `now`, so the next time-based climb is a full interval after the cut or
    the climb. Anything else keeps it.
    """
    moved = current_target(before, config, pool_hard_limit=pool_hard_limit) != current_target(
        after, config, pool_hard_limit=pool_hard_limit
    )
    if moved or after.last_429_at != before.last_429_at:
        return now or utcnow()
    return clock_at


def quota_derived_limit_for(
    state: QuotaState,
    config: AimdConfig = AimdConfig(),
    *,
    now: datetime | None = None,
    pool_hard_limit: int | None = None,
) -> int:
    """The cap this quota state imposes on the matching slot pool.

    This is the bridge between provider health and platform concurrency: the
    number returned here is written to `SlotPool.quota_derived_limit`, and
    `SlotPool.effective_limit` is the minimum of hard limit, adaptive target and
    this. An EXHAUSTED or cooling-down provider therefore drives the pool's
    effective limit to 0 and the scheduler stops admitting work against it --
    without anything having to remember to pause anything.

    This function does NOT expire anything. It reports what the state it was
    handed implies, as of `now` (default: when that state was written). Applying
    `refresh` first is the caller's decision, because silently re-dating a state
    against the wall clock here would make "an exhausted provider caps the pool
    at zero" depend on how long the document had been sitting around.
    """
    reference = now or state.updated_at
    if state.state in (
        ProviderState.EXHAUSTED,
        ProviderState.DISABLED,
        ProviderState.COOLDOWN,
    ):
        return 0
    if state.cooldown_until is not None and state.cooldown_until > reference:
        return 0
    return current_target(state, config, pool_hard_limit=pool_hard_limit)
