"""Provider quota: deciding between a short wait and giving the slot back.

Invariant 4 in one sentence: **a worker never sleeps through a long provider
wait.** A worker blocked on a 20-minute rate limit is 8 GiB of memory and 4 vCPU
doing nothing while holding a concurrency slot that another tenant could use, and
on Cloud Run it is also being billed. So the rule is mechanical:

    wait <= max_in_worker_retry_delay_seconds   ->  sleep and retry in place
    wait >  max_in_worker_retry_delay_seconds   ->  checkpoint, upload, publish
                                                    the quota state, PARK with
                                                    next_eligible_at, release the
                                                    lease, exit

An unknown wait is treated as a long one. Guessing "probably short" costs a slot
for as long as the guess is wrong; guessing "probably long" costs one requeue,
which the scheduler performs the moment `next_eligible_at` passes.

Two sources feed this. The runner child writes `quota.json` when it sees a 429
(it is the only thing that sees the provider's response headers), and the control
plane publishes the aggregated provider state that the quota broker maintains --
which is how a worker learns that a *different* worker on the same tenant key
already exhausted the quota.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from swarm_common.models import ProviderState, utcnow
from swarm_common.states import ParkReason

#: Used when a provider says "slow down" without saying for how long.
UNKNOWN_WAIT_SECONDS = 300


@dataclass(frozen=True)
class QuotaSignal:
    provider: str
    state: ProviderState
    source: str                      # "runner" | "control_plane"
    retry_after_seconds: int | None = None
    reset_at: datetime | None = None
    detail: str = ""

    def wait_seconds(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        candidates: list[int] = []
        if self.retry_after_seconds is not None:
            candidates.append(int(self.retry_after_seconds))
        if self.reset_at is not None:
            reset = self.reset_at
            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=timezone.utc)
            candidates.append(int(max(0.0, (reset - now).total_seconds())))
        if not candidates:
            return UNKNOWN_WAIT_SECONDS
        return max(candidates)

    @property
    def park_reason(self) -> ParkReason:
        if self.state is ProviderState.COOLDOWN:
            return ParkReason.PROVIDER_COOLDOWN
        if self.state is ProviderState.DISABLED:
            return ParkReason.PROVIDER_OUTAGE
        return ParkReason.PROVIDER_QUOTA_EXHAUSTED


@dataclass(frozen=True)
class QuotaDecision:
    park: bool
    wait_seconds: int
    next_eligible_at: datetime
    reason: ParkReason
    detail: dict[str, Any]


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def read_runner_signal(quota_path: Path, default_provider: str | None) -> QuotaSignal | None:
    """Parse the `quota.json` a runner writes when the provider rate-limits it.

    Shape (all fields optional except that the file exists at all):

        {"provider": "anthropic", "retry_after_seconds": 1800,
         "reset_at": "2026-09-15T21:00:00Z", "state": "EXHAUSTED",
         "detail": "429 from /v1/messages"}
    """
    path = Path(quota_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        # A runner that crashed mid-write still told us something happened.
        return QuotaSignal(
            provider=default_provider or "unknown",
            state=ProviderState.THROTTLED,
            source="runner",
            detail="unparseable quota signal file",
        )
    if not isinstance(data, dict):
        return None
    try:
        state = ProviderState(str(data.get("state", ProviderState.EXHAUSTED.value)).upper())
    except ValueError:
        state = ProviderState.EXHAUSTED
    retry_after = data.get("retry_after_seconds")
    return QuotaSignal(
        provider=str(data.get("provider") or default_provider or "unknown"),
        state=state,
        source="runner",
        retry_after_seconds=int(retry_after) if retry_after is not None else None,
        reset_at=_parse_dt(data.get("reset_at")),
        detail=str(data.get("detail", ""))[:500],
    )


def signal_from_control(signals: Any, provider: str | None) -> QuotaSignal | None:
    """Turn a control-plane poll into a quota signal, if it carries one."""
    if not provider or not getattr(signals, "provider_paused", False):
        return None
    return QuotaSignal(
        provider=provider,
        state=signals.provider_state or ProviderState.EXHAUSTED,
        source="control_plane",
        retry_after_seconds=signals.retry_after_seconds,
        reset_at=signals.quota_reset_at,
        detail="provider marked unavailable by the quota broker",
    )


def decide(
    signal: QuotaSignal,
    *,
    max_in_worker_retry_delay_seconds: int,
    now: datetime | None = None,
    remaining_task_seconds: float | None = None,
) -> QuotaDecision:
    """Short wait -> stay. Long wait -> park. Unknown wait -> park."""
    now = now or utcnow()
    wait = signal.wait_seconds(now)
    park = wait > max_in_worker_retry_delay_seconds
    if not park and remaining_task_seconds is not None and wait >= remaining_task_seconds:
        # Waiting here would burn the whole remaining timeout and then fail.
        park = True
    return QuotaDecision(
        park=park,
        wait_seconds=wait,
        next_eligible_at=now + timedelta(seconds=wait),
        reason=signal.park_reason,
        detail={
            "provider": signal.provider,
            "provider_state": signal.state.value,
            "source": signal.source,
            "wait_seconds": wait,
            "signal_detail": signal.detail,
        },
    )
