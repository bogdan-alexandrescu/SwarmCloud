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

THE INVARIANT THAT MUST NEVER BREAK:

    adaptive_target <= configured_hard_max, always.

`configured_hard_max` is an admin's statement about what the platform is allowed
to do to a provider -- a contractual or budget ceiling, not a guess. AIMD may
only ever propose a number BELOW it. Every function here clamps on the way out,
and `_clamp` is the single place that arithmetic happens, so a new signal cannot
be added that forgets to clamp.
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

    def __post_init__(self) -> None:
        if self.additive_increase <= 0:
            raise ValueError("additive_increase must be positive")
        if not 0 < self.multiplicative_decrease < 1:
            raise ValueError("multiplicative_decrease must be a fraction in (0, 1)")
        if self.success_threshold <= 0:
            raise ValueError("success_threshold must be positive")
        if self.min_target < 0:
            raise ValueError("min_target cannot be negative")


def _clamp(value: int | float, state: QuotaState, config: AimdConfig) -> int:
    """The ONLY place an adaptive target is produced.

    Clamps into [min_target, configured_hard_max]. If an admin sets a hard max
    below min_target, the hard max wins -- an operator's explicit ceiling
    outranks this module's preference for keeping a little headroom alive.
    """
    ceiling = max(0, int(state.configured_hard_max))
    floor = min(config.min_target, ceiling)
    target = int(math.floor(value))
    if target < floor:
        target = floor
    if target > ceiling:
        target = ceiling
    return target


def current_target(state: QuotaState, config: AimdConfig) -> int:
    """The working target, clamped. Never trusts a stored value."""
    if state.adaptive_target is None:
        return _clamp(state.configured_hard_max, state, config)
    return _clamp(state.adaptive_target, state, config)


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


def record_success(
    state: QuotaState,
    *,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
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
        return replace(state, success_count=state.success_count + 1, updated_at=moment)

    successes = state.success_count + 1
    target = current_target(state, config)
    if successes >= config.success_threshold:
        target = _clamp(target + config.additive_increase, state, config)
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
    rate_limits = state.rate_limit_count + 1
    target = _clamp(
        current_target(state, config) * config.multiplicative_decrease, state, config
    )

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
    reset_at: datetime | None = None,
    retry_after_seconds: int | None = None,
) -> QuotaState:
    """The provider says the quota window is spent, not merely busy."""
    moment = now or utcnow()
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
            current_target(state, config) * config.multiplicative_decrease, state, config
        ),
        quota_derived_limit=0,
        cooldown_until=moment + timedelta(seconds=cooldown),
        retry_after_seconds=cooldown,
        reset_at=reset_at if reset_at is not None else state.reset_at,
        last_429_at=moment,
        rate_limit_count=state.rate_limit_count + 1,
        success_count=0,
        updated_at=moment,
    )


def refresh(
    state: QuotaState,
    *,
    now: datetime | None = None,
    config: AimdConfig = AimdConfig(),
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
        adaptive_target=current_target(state, config),
        updated_at=moment,
    )


def quota_derived_limit_for(
    state: QuotaState,
    config: AimdConfig = AimdConfig(),
    *,
    now: datetime | None = None,
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
    return _clamp(current_target(state, config), state, config)
