"""The account pool: who owns which Claude subscription, and who may use it.

An account is a Claude subscription the platform can run agents on. The pool is
per-tenant with explicit lending: an account belongs to exactly one tenant, and
may name other tenants it will lend spare capacity to. Isolation is the default
and sharing is a decision with a name on it -- CONTRACT.md invariant 9 says a
tenant's credentials must never be reachable from another tenant's pod, and
"unless its owner said otherwise, in writing, per account" is a narrowing of
that rule rather than a hole in it.

THE PROPERTY THIS MODULE EXISTS TO PROVIDE
------------------------------------------
No agent ever stops because its account ran out of quota. Two things are needed
and neither is sufficient alone:

  * pick well at admission -- assign the account with the most headroom, not
    merely one that is not yet exhausted;
  * keep every account alive -- including the ones nobody is using.

The second is the one that gets forgotten. A refresh token that is never
exchanged eventually dies, so a pool where three accounts are busy and two are
idle is a pool where the idle two quietly rot until the day they are needed.
The sweep therefore visits EVERY account, not only the ones with running
agents, and that is the whole reason `due_for_refresh()` takes no filter.

HEADROOM IS OBSERVED, NOT GUESSED
---------------------------------
Claude Code's `stream-json` output carries `rate_limit_event`, which reports
`utilization` as a 0-1 float per window with an exact `resetsAt`. Workers report
those readings; this module stores them. So "which account has room" is a
reading rather than an inference, and an exhausted account comes back at a known
instant rather than being probed.

Readings go stale. An account last observed an hour ago may have been used by
something else since -- another pod, or the operator's own laptop, which shares
these accounts. `Account.headroom()` therefore decays confidence with age and
`stale_after` exists to say when a reading stops counting at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable

#: Secret Manager holds the credential; Firestore holds everything else. The
#: secret name is derived, never supplied, for the same reason tenant secret
#: names are: a caller who could name the secret could name someone else's.
_SECRET_PREFIX = "swarm-account"

#: Labels become part of a secret name and a Kubernetes annotation, so they are
#: restricted to what both accept. Checked on the way IN, so a bad label is
#: refused at onboarding rather than at dispatch.
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")

#: A reading older than this says nothing useful. These accounts are also used
#: by a human at a laptop, so utilisation can move without the platform seeing
#: any of it.
DEFAULT_STALE_AFTER = timedelta(minutes=30)

#: Below this much remaining, an account is not handed to a NEW agent. It is
#: deliberately not zero: an agent assigned at 2% headroom will hit the wall
#: almost immediately and the swap machinery will have to rescue it, which is
#: more expensive than simply not starting it there.
DEFAULT_ASSIGN_FLOOR = 0.15


class AccountState(str, Enum):
    #: Usable, and may be assigned to new agents.
    AVAILABLE = "AVAILABLE"
    #: Usable by agents already on it; assigned to no new ones. An operator
    #: pausing an account, or the broker easing off one that is nearly spent.
    PAUSED = "PAUSED"
    #: No new assignments AND running agents are being moved off. The state that
    #: makes an account safely removable.
    DRAINING = "DRAINING"
    #: The refresh token is gone. Only a human can fix this, so the broker must
    #: stop trying rather than spend the endpoint's rate limit discovering the
    #: same answer every five minutes.
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


#: States from which an agent may still be STARTED.
ASSIGNABLE_STATES = frozenset({AccountState.AVAILABLE})

#: States a running agent may remain on. PAUSED is here and DRAINING is not:
#: pausing means "no new work", draining means "get off".
USABLE_STATES = frozenset({AccountState.AVAILABLE, AccountState.PAUSED})


class AccountError(RuntimeError):
    pass


def secret_name(owner_tenant: str, label: str) -> str:
    """`swarm-account-<tenant>-<label>`. Derived, never supplied."""
    validate_label(label)
    if not owner_tenant:
        raise AccountError("an account must have an owning tenant")
    return f"{_SECRET_PREFIX}-{owner_tenant}-{label}"


def validate_label(label: str) -> str:
    if not _LABEL.match(label or ""):
        raise AccountError(
            f"account label {label!r} must be lowercase letters, digits and "
            "dashes, starting and ending alphanumeric, at most 40 characters -- "
            "it becomes part of a Secret Manager name and a Kubernetes annotation"
        )
    return label


@dataclass(frozen=True)
class WindowReading:
    """One rate-limit window as the API reported it."""

    #: 0.0 = untouched, 1.0 = exhausted.
    utilization: float
    resets_at: datetime

    def remaining(self) -> float:
        return max(0.0, 1.0 - self.utilization)

    def is_reset(self, now: datetime) -> bool:
        return now >= self.resets_at


@dataclass(frozen=True)
class Account:
    account_id: str
    owner_tenant: str
    label: str
    provider: str = "anthropic"
    state: AccountState = AccountState.AVAILABLE

    #: Tenants this account's owner has explicitly lent it to. Empty is the
    #: default and the safe one.
    lend_to: tuple[str, ...] = ()

    #: Per-window readings, keyed by the API's own window name
    #: ("five_hour", "seven_day"). Keyed rather than fixed because the windows
    #: are the provider's to define, and a new one appearing must not need a
    #: schema change to be recorded.
    windows: dict[str, WindowReading] = field(default_factory=dict)
    observed_at: datetime | None = None

    #: How many agents currently hold this account. Advisory: the authoritative
    #: record is the lease, and this is what makes a listing readable.
    assigned: int = 0

    #: Why it is in its current state, for the human who has to act on it.
    reason: str = ""

    def may_serve(self, tenant_id: str) -> bool:
        """Whether this account is allowed to run `tenant_id`'s work at all.

        Ownership or an explicit loan, and nothing else. In particular NOT
        "the same project" or "the same domain": invariant 9 is about which pod
        can reach which credential, and a pod is not made safe by being nearby.
        """
        return tenant_id == self.owner_tenant or tenant_id in self.lend_to

    def headroom(
        self,
        now: datetime,
        *,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> float:
        """Fraction of capacity believed remaining, 0.0-1.0.

        The BINDING window decides, not the average: an account at 5% on its
        weekly and 90% on its five-hour has 5% of headroom, because the weekly
        one is what will refuse the next request. Averaging them would report
        47% and send agents at an account that is about to stop.

        A window that has passed its reset time is treated as full again, which
        is what `resetsAt` means, and is why an exhausted account can return to
        the pool without anyone probing it.
        """
        if not self.windows or self.observed_at is None:
            # Never observed. Optimistic ON PURPOSE: a new account has to be
            # assignable before it can report anything, and the alternative --
            # assuming empty -- makes onboarding a chicken-and-egg problem.
            return 1.0

        if now - self.observed_at > stale_after:
            # Too old to trust, but not evidence of exhaustion either. Halved
            # rather than zeroed: enough to prefer a freshly-observed account,
            # not so much that a quiet pool grinds to a halt.
            return self._binding_remaining(now) * 0.5

        return self._binding_remaining(now)

    def _binding_remaining(self, now: datetime) -> float:
        return min(
            (1.0 if w.is_reset(now) else w.remaining()) for w in self.windows.values()
        )

    def next_reset(
        self,
        now: datetime,
        *,
        floor: float = DEFAULT_ASSIGN_FLOOR,
    ) -> datetime | None:
        """When this account becomes assignable again, or None if it already is.

        Only windows that are actually CONSTRAINING count. An account with room
        has nothing to wait for, and reporting its next window rollover as a
        "reset" would schedule a wake-up for an account that never stopped --
        which reads, to whoever is looking at it, as though the account had been
        exhausted.

        This is what lets a drained account be scheduled back at a known instant
        instead of polled.
        """
        blocking = [
            w.resets_at
            for w in self.windows.values()
            if not w.is_reset(now) and w.remaining() < floor
        ]
        return min(blocking) if blocking else None

    def with_reading(
        self, windows: dict[str, WindowReading], observed_at: datetime
    ) -> "Account":
        """A newer reading, or this account unchanged.

        Older readings are DISCARDED rather than merged. Reports arrive from
        several pods at once and out of order, and letting a stale one overwrite
        a fresh one would make an exhausted account look available again --
        precisely the wrong direction to be wrong in.
        """
        if self.observed_at is not None and observed_at <= self.observed_at:
            return self
        return replace(self, windows=dict(windows), observed_at=observed_at)

    @property
    def secret(self) -> str:
        return secret_name(self.owner_tenant, self.label)

    def to_firestore(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "owner_tenant": self.owner_tenant,
            "label": self.label,
            "provider": self.provider,
            "state": self.state.value,
            "lend_to": list(self.lend_to),
            "assigned": self.assigned,
            "reason": self.reason,
            "observed_at": self.observed_at,
            "windows": {
                name: {"utilization": w.utilization, "resets_at": w.resets_at}
                for name, w in self.windows.items()
            },
        }

    @classmethod
    def from_firestore(cls, data: dict[str, Any]) -> "Account":
        windows: dict[str, WindowReading] = {}
        for name, raw in (data.get("windows") or {}).items():
            resets = raw.get("resets_at")
            if not isinstance(resets, datetime):
                continue
            windows[name] = WindowReading(
                utilization=float(raw.get("utilization", 0.0)),
                resets_at=_aware(resets),
            )
        observed = data.get("observed_at")
        return cls(
            account_id=data["account_id"],
            owner_tenant=data["owner_tenant"],
            label=data["label"],
            provider=data.get("provider", "anthropic"),
            state=AccountState(data.get("state", AccountState.AVAILABLE.value)),
            lend_to=tuple(data.get("lend_to") or ()),
            windows=windows,
            observed_at=_aware(observed) if isinstance(observed, datetime) else None,
            assigned=int(data.get("assigned", 0)),
            reason=data.get("reason", "") or "",
        )


def _aware(value: datetime) -> datetime:
    """Firestore hands back naive datetimes in some client versions."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def account_id_for(owner_tenant: str, label: str) -> str:
    validate_label(label)
    return f"{owner_tenant}:{label}"


def choose(
    accounts: Iterable[Account],
    tenant_id: str,
    now: datetime,
    *,
    assign_floor: float = DEFAULT_ASSIGN_FLOOR,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> Account | None:
    """The account a new agent for `tenant_id` should get, or None.

    Ordering, in priority:
      1. the tenant's OWN accounts before borrowed ones -- a loan is a courtesy
         and should be spent last;
      2. most headroom first;
      3. fewest agents already on it, to spread load rather than stack it;
      4. label, so the choice is deterministic and a test can assert it.

    Returns None when nothing is eligible, which is not an error: the caller
    parks the task, which costs nothing and resumes by itself once an account
    frees up. Handing back an exhausted account instead would trade a free park
    for a failed dispatch.
    """
    candidates = [
        a
        for a in accounts
        if a.state in ASSIGNABLE_STATES
        and a.may_serve(tenant_id)
        and a.headroom(now, stale_after=stale_after) >= assign_floor
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda a: (
            0 if a.owner_tenant == tenant_id else 1,
            -a.headroom(now, stale_after=stale_after),
            a.assigned,
            a.label,
        ),
    )


def due_for_refresh(
    accounts: Iterable[Account],
    now: datetime,
    *,
    window: timedelta = timedelta(hours=3),
) -> list[Account]:
    """Every account whose credential should be exchanged now.

    Takes NO filter for "in use", and that omission is the point. A refresh
    token that is never exchanged eventually expires, so an idle account rots
    until the day it is wanted. Refreshing it on the same timer as a busy one is
    what makes "no human ever has to step in" true rather than aspirational.

    REAUTH_REQUIRED accounts are excluded: their refresh token is already gone,
    and retrying cannot bring it back. Spending the token endpoint's rate limit
    to rediscover that every five minutes would make a broken account into a
    problem for the working ones.
    """
    return [a for a in accounts if a.state is not AccountState.REAUTH_REQUIRED]


__all__ = [
    "Account",
    "AccountError",
    "AccountState",
    "WindowReading",
    "ASSIGNABLE_STATES",
    "USABLE_STATES",
    "DEFAULT_ASSIGN_FLOOR",
    "DEFAULT_STALE_AFTER",
    "account_id_for",
    "choose",
    "due_for_refresh",
    "secret_name",
    "validate_label",
]
