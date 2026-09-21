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

"STALE" AND "SPENT" ARE DIFFERENT CLAIMS
----------------------------------------
They have to stay different all the way out to the caller, because the caller
waits differently for each. An account that is spent comes back when the
provider's window rolls over -- hours, at a known instant. An account whose
reading has merely aged out says nothing about whether it has room; it says
nobody has looked lately, and the broker's own usage poll looks on every sweep.
Reporting the second as the first sends a task to sleep for a quarter of an
hour over a pool that will be readable again in five minutes; reporting either
as "waiting on a person" sends an operator to look at a pause that is not
there. `eligibility()` is the one place that decides which of these is true,
and `choose()` and the assign route both go through it so they cannot disagree
about what "unavailable" meant.

A HOLD IS HOW MANY AGENTS ARE REALLY ON AN ACCOUNT
--------------------------------------------------
`assigned` used to be a bare counter that went up on assign and down on
release. A worker that is SIGKILLed, OOM-killed or preempted never releases, so
the counter only ever drifted upward -- and since it is `choose()`'s
load-spreading tiebreak and the number an operator reads as "agents on this
account", it degraded quietly and permanently. Each assignment is therefore a
HOLD with an id and a deadline, `assigned` is the number of holds that have not
expired, and the sweep prunes the rest. That also gives release something to
prove: it names the hold it is giving back, so a tenant an account is lent to
cannot drive the owner's counter to zero with calls it never earned.
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

#: Separates the tenant from the label in a secret name. Two dashes because a
#: single one is ambiguous when both sides may contain dashes -- see
#: `secret_name`, which is where that cost invariant 9.
_SEPARATOR = "--"

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

#: How long a hold survives without being released.
#:
#: Longer than the longest runner profile's timeout (7200s) plus the setup and
#: teardown around it, so it can never expire under a worker that is still
#: running -- an expiring hold under a live agent would let the broker hand the
#: same subscription to a second one. Short enough that a worker killed without
#: warning costs a couple of hours of one slot on one account rather than that
#: slot forever.
DEFAULT_HOLD_TTL = timedelta(hours=3)


class Unavailable(str, Enum):
    """Why no account could be assigned. Each one is a different wait.

    Kept as an enum rather than a string at the call site because these travel
    to a worker that picks a park duration from them, and a typo in a literal
    would silently become the long fallback.
    """

    #: This tenant may use no account at all. Not a wait: the pool is not how
    #: this deployment runs, and the caller falls back to its tenant secret.
    NO_ACCOUNTS_REGISTERED = "no_accounts_registered"
    #: Accounts exist, the binding window on every one of them is below the
    #: assign floor, and at least one says when it clears. A clock.
    NO_ACCOUNT_AVAILABLE = "no_account_available"
    #: Every account is paused, draining or needs re-authentication. A person.
    POOL_PAUSED = "pool_paused"
    #: Accounts have room by their last reading, but every reading is too old
    #: to act on. The broker's usage poll, minutes away -- NOT a clock, and
    #: emphatically not "they are spent".
    NO_RECENT_READING = "no_recent_reading"


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
    """`swarm-account-<tenant>--<label>`. Derived, never supplied.

    TWO DASHES, and that is a security boundary rather than a style choice.

    A single dash made the name AMBIGUOUS, because a tenant id and a label may
    both contain one: `("acme-prod", "x")` and `("acme", "prod-x")` produced the
    identical secret. Registering the second would bind the second tenant's
    worker service account as an accessor on a secret holding the FIRST
    tenant's live refresh and access tokens -- CONTRACT.md invariant 9, broken
    with no attacker and no malice, by two ordinary onboardings. Reported in
    docs/audits/2026-09-18/06-quota-broker-accounts.md and unfixed until
    account registration moved into the Settings page, where the label is
    chosen by whoever is signed in rather than by an operator running a script.

    `--` is unambiguous because the name splits at the FIRST occurrence and the
    TENANT cannot contain one -- the check below refuses that. A label may
    contain `--` harmlessly: everything after the first separator is the label
    by definition, so there is still exactly one way to read the name.
    (`validate_label` does not forbid a repeated dash, and an earlier draft of
    this comment claimed it did.)

    Changed at the only moment it was free: no account had yet been registered
    anywhere, so there was nothing to migrate.
    """
    validate_label(label)
    if not owner_tenant:
        raise AccountError("an account must have an owning tenant")
    if _SEPARATOR in owner_tenant:
        raise AccountError(
            f"tenant id {owner_tenant!r} contains {_SEPARATOR!r}, which separates "
            "the tenant from the label in a secret name and must appear in neither"
        )
    return f"{_SECRET_PREFIX}-{owner_tenant}{_SEPARATOR}{label}"


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
class Hold:
    """One agent's claim on an account, with a deadline.

    The deadline is what makes the count self-correcting. A worker releases on
    its own exit path and that is the normal case; a worker that is killed
    outright cannot, and nothing else in this repository knows the assignment
    existed -- `apps/reconciler/` has no account code. So the hold carries its
    own expiry and the quota sweep prunes it.
    """

    assignment_id: str
    tenant_id: str
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


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

    #: The live claims on this account. The authoritative answer to "how many
    #: agents are on it", and the reason `assigned` cannot drift upward forever.
    holds: tuple[Hold, ...] = ()

    #: How many agents currently hold this account -- the projection of `holds`
    #: that a listing reads and `choose()` sorts on, maintained by the same
    #: transaction that changes them. Advisory in the sense that the lease is
    #: still the authoritative record of a running attempt.
    assigned: int = 0

    #: Tenants that were handed this account and could not read its secret,
    #: with when they said so. Per TENANT rather than global, on purpose: a
    #: borrower reporting that it cannot read a lent secret says nothing about
    #: the owner, and letting one tenant's report pause an account for everyone
    #: would be a griefing tool. `choose()` skips an account for the tenant
    #: that reported it, for as long as `unreadable_after` says.
    unreadable_by: dict[str, datetime] = field(default_factory=dict)

    #: Why it is in its current state, for the human who has to act on it.
    reason: str = ""

    #: The secret this account's credential actually lives in, RECORDED at
    #: registration rather than re-derived on every read.
    #:
    #: Derivation was fine until the naming rule had to change. When
    #: `secret_name` moved from one dash to two -- to stop
    #: ("acme-prod","x") and ("acme","prod-x") colliding -- every account
    #: already registered started deriving a name that does not exist, and
    #: nothing said so: the refresh sweep reports "no_refresh_credential",
    #: which is also what a healthy API-key tenant reports, and the usage
    #: poller simply skips. Three live accounts were orphaned that way and the
    #: platform looked fine.
    #:
    #: A name that is stored cannot be invalidated by a change to how names are
    #: made. Empty means "derive it", which is what a document written before
    #: this field existed needs.
    secret_ref: str = ""

    #: When an agent was last assigned this account, ever. None means NEVER,
    #: and that is the field's whole purpose: a pool with accounts in it and
    #: nothing ever assigned from it is a pool no worker can reach, which is
    #: the one failure mode of this feature that is otherwise completely
    #: silent -- no error, no log, a healthy-looking listing, and every agent
    #: quietly back on the one shared per-tenant subscription. The sweep says
    #: so out loud.
    last_assigned_at: datetime | None = None

    def live_holds(self, now: datetime) -> tuple[Hold, ...]:
        return tuple(h for h in self.holds if not h.is_expired(now))

    def is_unreadable_for(
        self,
        tenant_id: str,
        now: datetime,
        *,
        unreadable_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> bool:
        """Whether this tenant recently found this account's secret unreadable.

        TIME-LIMITED rather than sticky, because both causes fix themselves or
        get fixed: a freshly onboarded account gets its access token published
        on the broker's next sweep, and a missing `secretAccessor` grant on a
        lent secret is something an operator adds. A permanent mark would turn
        a five-minute onboarding window into an account that never came back.
        """
        reported = self.unreadable_by.get(tenant_id)
        if reported is None:
            return False
        return now - _aware(reported) <= unreadable_after

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

    def is_stale(
        self,
        now: datetime,
        *,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> bool:
        """Whether the last reading is too old to act on.

        A never-observed account is NOT stale: it has no reading to age out,
        and `headroom()` deliberately treats it as full so a new account can be
        assigned before it can report anything.
        """
        if self.observed_at is None:
            return False
        return now - self.observed_at > stale_after

    def next_reset(
        self,
        now: datetime,
        *,
        floor: float = DEFAULT_ASSIGN_FLOOR,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> datetime | None:
        """When this account becomes assignable again, or None if nothing is a clock.

        Only windows that are actually CONSTRAINING count. An account with room
        has nothing to wait for, and reporting its next window rollover as a
        "reset" would schedule a wake-up for an account that never stopped --
        which reads, to whoever is looking at it, as though the account had been
        exhausted.

        THE ELIGIBILITY RULE HERE IS THE ONE `headroom()` USES, staleness
        included, which it did not used to be. `choose()` rejects on
        `headroom()`, which HALVES an aged reading; this compared the raw
        remaining against the floor and applied no staleness at all. So an
        account blocked only by the halving -- raw remaining between the floor
        and twice it, last observed over `stale_after` ago -- was rejected by
        `choose()` and reported no blocking window, and the route answered "no
        account available, and no idea when". The caller then fell back to its
        long poll for exactly the case the "park until a known instant" design
        was written for. Taking the same `stale_after` makes the two agree by
        construction.

        A stale reading that is otherwise fine still yields None here, and that
        is correct: nothing is waiting on a window. It is waiting on the next
        usage poll, which is not a reset instant and must not be dressed up as
        one -- see `eligibility()`, which says so in words the caller can act
        on.
        """
        if self.headroom(now, stale_after=stale_after) >= floor:
            return None
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
        """Where this account's credential actually lives.

        The RECORDED name wins over a derived one. Deriving was safe until the
        naming rule changed: when `secret_name` moved from one dash to two, to
        stop ("acme-prod","x") and ("acme","prod-x") colliding, every account
        already registered began pointing at a secret that had never existed --
        silently, because a missing refresh secret and a tenant that simply
        uses an API key report the identical thing. Three live accounts were
        orphaned that way and the platform looked healthy.

        A name that is stored cannot be invalidated by a change to how names
        are made. Empty means "derive it", which is what a document written
        before this field existed needs.
        """
        return self.secret_ref or secret_name(self.owner_tenant, self.label)

    def to_firestore(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "owner_tenant": self.owner_tenant,
            "label": self.label,
            "provider": self.provider,
            "state": self.state.value,
            "lend_to": list(self.lend_to),
            "assigned": self.assigned,
            "last_assigned_at": self.last_assigned_at,
            "holds": holds_to_firestore(self.holds),
            "unreadable_by": dict(self.unreadable_by),
            "reason": self.reason,
            "secret": self.secret_ref,
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
            secret_ref=str(data.get("secret") or ""),
            windows=windows,
            observed_at=_aware(observed) if isinstance(observed, datetime) else None,
            holds=holds_from_firestore(data.get("holds")),
            last_assigned_at=(
                _aware(data["last_assigned_at"])
                if isinstance(data.get("last_assigned_at"), datetime)
                else None
            ),
            unreadable_by={
                str(t): _aware(v)
                for t, v in (data.get("unreadable_by") or {}).items()
                if isinstance(v, datetime)
            },
            assigned=int(data.get("assigned", 0)),
            reason=data.get("reason", "") or "",
        )


def holds_to_firestore(holds: Iterable[Hold]) -> list[dict[str, Any]]:
    return [
        {
            "assignment_id": h.assignment_id,
            "tenant_id": h.tenant_id,
            "expires_at": h.expires_at,
        }
        for h in holds
    ]


def holds_from_firestore(raw: Any) -> tuple[Hold, ...]:
    """Read holds back, dropping any that are not shaped like one.

    Skipping a malformed entry rather than raising, for the reason
    `AccountStore.list` gives about malformed documents: one bad hold must not
    make the whole account unreadable and take the pool down with it. A dropped
    hold under-counts by one, which `choose()` survives; an unreadable account
    is one nothing can be assigned from at all.
    """
    holds: list[Hold] = []
    for item in raw or ():
        if not isinstance(item, dict):
            continue
        expires = item.get("expires_at")
        assignment_id = item.get("assignment_id")
        if not isinstance(expires, datetime) or not assignment_id:
            continue
        holds.append(
            Hold(
                assignment_id=str(assignment_id),
                tenant_id=str(item.get("tenant_id") or ""),
                expires_at=_aware(expires),
            )
        )
    return tuple(holds)


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
    exclude: Iterable[str] = (),
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

    `exclude` is how a caller says "not that one, I already tried it". It
    exists because everything above is deterministic: without it, a worker
    handed an account whose secret it cannot read would be handed the same
    account on its next ask, and on the retry after that, until the task ran
    out of attempts. `unreadable_by` does the same job across attempts and for
    this tenant only -- a borrower that cannot read a lent secret says nothing
    about the owner, so its report must not take the account away from anyone
    else.
    """
    skip = {str(a) for a in exclude}
    candidates = [
        a
        for a in accounts
        if a.state in ASSIGNABLE_STATES
        and a.may_serve(tenant_id)
        and a.account_id not in skip
        and not a.is_unreadable_for(tenant_id, now, unreadable_after=stale_after)
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


def eligibility(
    accounts: Iterable[Account],
    tenant_id: str,
    now: datetime,
    *,
    assign_floor: float = DEFAULT_ASSIGN_FLOOR,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    exclude: Iterable[str] = (),
) -> tuple[Unavailable, datetime | None]:
    """WHY nothing can be assigned, and when that changes.

    Call it only when `choose()` returned None, with the same arguments, so the
    two cannot disagree about what "unavailable" meant. That coupling is the
    whole reason this exists as a function rather than as a few lines in the
    route: the route used to answer "when" with a rule that did not match the
    rule `choose()` had just answered "no" with, and the two disagreed in
    exactly the band where it mattered.

    The second element is a reset INSTANT and only ever that. A pool waiting on
    a person, or on the next usage poll, has no instant, and inventing one
    would wake a task up to the same answer. The reason is what the caller
    picks its wait from.
    """
    skip = {str(a) for a in exclude}
    serves = [a for a in accounts if a.may_serve(tenant_id)]
    if not serves:
        # The pool is not how this tenant runs at all. The ONE answer that
        # means "fall back to your tenant secret" rather than "wait", and the
        # reason every deployment that has never registered an account is
        # unaffected by any of this.
        return Unavailable.NO_ACCOUNTS_REGISTERED, None

    mine = [
        a
        for a in serves
        if a.account_id not in skip
        and not a.is_unreadable_for(tenant_id, now, unreadable_after=stale_after)
    ]
    if not mine:
        # Accounts exist and this attempt has ruled every one of them out --
        # it tried them and could not read them. A wait, not a fallback: a
        # tenant whose pool is broken must not quietly go back to sharing one
        # subscription, which is the contention the pool exists to remove.
        return Unavailable.NO_ACCOUNT_AVAILABLE, None

    assignable = [a for a in mine if a.state in ASSIGNABLE_STATES]
    if not assignable:
        # Paused, draining or needing re-authentication. Every one of those is
        # a person's decision or a person's job, so there is nothing to wait
        # for and no point waking up sooner.
        return Unavailable.POOL_PAUSED, None

    resets = [
        r
        for r in (
            a.next_reset(now, floor=assign_floor, stale_after=stale_after)
            for a in assignable
        )
        if r is not None
    ]
    if resets:
        return Unavailable.NO_ACCOUNT_AVAILABLE, min(resets)

    if any(a.is_stale(now, stale_after=stale_after) for a in assignable):
        # Nothing is waiting on a window: every account that is blocked is
        # blocked by the staleness halving alone. That is not evidence of
        # exhaustion and must not be reported as it -- the broker's own usage
        # poll refreshes these readings on its next sweep.
        return Unavailable.NO_RECENT_READING, None

    # Assignable, fresh, and still under the floor with no unreset window:
    # genuinely spent, with nothing that can say when it clears.
    return Unavailable.NO_ACCOUNT_AVAILABLE, None


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
    "Hold",
    "Unavailable",
    "WindowReading",
    "ASSIGNABLE_STATES",
    "USABLE_STATES",
    "DEFAULT_ASSIGN_FLOOR",
    "DEFAULT_HOLD_TTL",
    "DEFAULT_STALE_AFTER",
    "account_id_for",
    "choose",
    "due_for_refresh",
    "eligibility",
    "holds_from_firestore",
    "holds_to_firestore",
    "secret_name",
    "validate_label",
]
