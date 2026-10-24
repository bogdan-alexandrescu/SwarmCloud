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
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable

from swarm_common.profiles import MODEL_CLASSES

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

#: A window's own floor, where it differs from DEFAULT_ASSIGN_FLOOR (BGD,
#: 2026-10-24, #2222). One 15% floor on every window stranded six accounts
#: with 2-14% of their WEEK left for one to four days, while 28 agents waited
#: on the one account whose 5-hour window would clear first. Five per cent of
#: a week is still hours of agent work, and an agent that does reach the limit
#: mid-run is swapped or parked like any other. The 5-hour window, and any
#: window this map does not name, keeps the default.
WINDOW_FLOORS: dict[str, float] = {"seven_day": 0.05}


def window_floor(window: str, floor: float | None = None) -> float:
    """The floor `window` is held to before a NEW agent is handed its account.

    `floor`, when a caller names one, is one floor for every window: the rule
    before #2222, kept so a caller (and every test written against it) that
    passes `assign_floor` gets exactly what it asked for. None, the default
    everywhere in the broker and the scheduler, is each window's own floor.
    """
    if floor is not None:
        return floor
    return WINDOW_FLOORS.get(window, DEFAULT_ASSIGN_FLOOR)


#: How many agents one account may hold at once, unless its owner sets
#: another number (BGD, 2026-10-24, #2222: "Per-account cap, staggered wake").
#:
#: `choose()` hands a new agent the account with the MOST headroom, and an
#: account's headroom does not move until its next reading arrives, minutes
#: later. With nothing capping that, every new agent went to the same account:
#: at 10:38Z on 2026-10-24, four of the six agents eng was allowed went to
#: aieng-claude1 the moment its 5-hour window reset, and the cap on the
#: tenant's provider pool (cut from 100 to 6 that morning) was the only thing
#: stopping the rest. Six is that same number per account: enough to keep a
#: subscription busy, few enough that a burst cannot spend a 5-hour window
#: before the next reading shows it.
DEFAULT_MAX_AGENTS = 6

#: The most an owner may set `max_agents` to. Only a bound on what the route
#: accepts -- no subscription runs fifty agents at once -- so a typo of a
#: thousand cannot quietly turn the cap off.
MAX_AGENTS_LIMIT = 50

#: How many agents an account NOBODY HAS MEASURED YET may hold (#2222; BGD,
#: 2026-10-24 ~10:55Z, "One until measured"). A never-observed account reads
#: as fully free (`Account.headroom`), so it won every pick until the poller
#: read it: at 10:38Z that day eng:saga-team was handed an agent before its
#: first reading, and the sign-in behind it had authorised the wrong
#: organisation -- one whose week was 98% spent. One agent keeps a new account
#: usable at once; its first reading decides the rest, up to `max_agents`.
MAX_AGENTS_BEFORE_READING = 1

#: How long a hold survives without being released.
#:
#: Longer than the longest runner profile's timeout (7200s) plus the setup and
#: teardown around it, so it can never expire under a worker that is still
#: running -- an expiring hold under a live agent would let the broker hand the
#: same subscription to a second one. Short enough that a worker killed without
#: warning costs a couple of hours of one slot on one account rather than that
#: slot forever.
DEFAULT_HOLD_TTL = timedelta(hours=3)

#: Where each hold's lifetime is recorded: `account_holds/{assignment_id}`.
#:
#: The account document's `holds` say who is on it NOW and are rewritten on
#: every change, so they cannot answer "who was on it at 14:00". This
#: collection can, and it is written INSIDE the same transaction as every
#: change to `holds` (acquire, release, prune) -- a record written after the
#: counter commits is one that can disagree with it.
HOLD_LOG_COLLECTION = "account_holds"

#: How long a closed record is kept: `expires_at = released_at + 90 days`, and
#: a Firestore TTL policy on `expires_at` deletes it (terraform/modules/firestore).
#: Ninety days covers a billing month with margin, which is the question the
#: history tab is for; past that a record is a liability that names tasks
#: nobody is asking about.
HOLD_LOG_RETENTION = timedelta(days=90)

#: How a hold ended. `released` is the worker's own exit path; `unusable` is a
#: release that reported the secret unreadable; `expired` is a hold whose
#: deadline passed without a release -- a worker killed outright.
HOLD_END_RELEASED = "released"
HOLD_END_UNUSABLE = "unusable"
HOLD_END_EXPIRED = "expired"
#: The hold was given up for a hold on ANOTHER account, in one transaction
#: (`main.swap_hold`): the attempt moved, it did not stop. Its record carries
#: `swapped_to`; the new hold's record carries `swapped_from`, so the 90-day
#: history serves the swap from both accounts' side with no second store.
HOLD_END_SWAPPED = "swapped"
#: The account was removed (`DELETE /v1/accounts/{id}`) while the hold was
#: open. Closed in the removal's own transaction with `released_at` = the
#: removal, so the span ends when the account stopped existing -- rather than
#: staying open until its TTL and then reading as `expired`, a worker killed
#: outright, which is not what happened.
HOLD_END_ACCOUNT_REMOVED = "account_removed"

#: Every end a record may carry. The broker's history read and swarm-api's
#: serve exactly these and nothing else.
HOLD_ENDS = (
    HOLD_END_RELEASED,
    HOLD_END_UNUSABLE,
    HOLD_END_EXPIRED,
    HOLD_END_SWAPPED,
    HOLD_END_ACCOUNT_REMOVED,
)

#: Why an attempt moved from one account to another mid-run (S13/S14).
#:
#:   exhausted  the account it was on ran out of quota;
#:   drain      an operator set the account DRAINING, which marks every live
#:              hold on it to move (`Hold.move`);
#:   unusable   the account it had just moved to could not start the agent --
#:              its secret could not be read -- so it moved again.
SWAP_EXHAUSTED = "exhausted"
SWAP_DRAIN = "drain"
SWAP_UNUSABLE = "unusable"
SWAP_REASONS = (SWAP_EXHAUSTED, SWAP_DRAIN, SWAP_UNUSABLE)

#: What `CredentialRefresher` can answer for one account, which the sweep
#: records as `last_refresh_reason` (docs/web-ui/06-accounts.md P2). Named so
#: a reader of the document knows the closed set; the refresher is the writer.
REFRESH_REASONS = (
    "no_refresh_credential",
    "store_unavailable",
    "unreadable",
    "still_valid",
    "published",
    "published_unverified",
    "unverified_skipped",
    "publish_failed",
    "reauth_required",
    "refresh_failed",
    "refreshed",
    # An agent held the account and its token was not yet within the held
    # margin, so nothing was exchanged (#626, `CredentialRefresher._refresh`).
    "held_deferred",
    "error",
)


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
    #: At least one account has room by its last reading, but that reading is
    #: too old to act on. The broker's usage poll, minutes away -- NOT a clock,
    #: and emphatically not "they are spent". It wins over any window instant
    #: another account carries, because the poll is the sooner of the two
    #: (#1885); an instant still travels with it when a window clears sooner.
    NO_RECENT_READING = "no_recent_reading"
    #: Accounts have room by their windows, and every one of them already
    #: holds its `max_agents` agents (#2222). NOT a clock: a slot frees when
    #: an agent on it finishes, which no instant predicts, so the worker
    #: re-asks within minutes, as for a stale reading. An instant still
    #: travels with it when another account's window clears sooner.
    ACCOUNTS_FULL = "accounts_full"


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

    The change was not free: accounts were already registered under the
    single-dash name, and it orphaned three of them, each deriving a secret
    name that did not exist. That is why an account records its `secret_ref`
    and why the recorded name wins over the one derived here
    (`Account.secret`, `AccountStore.secret_for`). Another rename orphans
    every account that has no `secret_ref` recorded, so it needs a migration.
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
    #: When THIS window was read. Per window because readings now arrive per
    #: window -- a worker's stream reports `five_hour` and `seven_day` in
    #: separate events -- and `Account.with_reading` merges them, so the
    #: account's `observed_at` (the newest of them) no longer dates each one.
    #: None on a window stored before this field, which is dated by the
    #: account's `observed_at` as it always was. Not part of equality: two
    #: readings of the same figures are the same reading.
    observed_at: datetime | None = field(default=None, compare=False)

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

    #: What the worker said it was running, from its own config (#379). OPTIONAL
    #: in every sense: a worker image older than this field sends neither, and
    #: a hold written before it existed has neither. They are a CLAIM by the
    #: worker, not a fact the broker checked -- swarm-api verifies that the
    #: hold's tenant owns the task before it shows a link to anyone.
    task_id: str | None = None
    attempt_id: str | None = None

    #: When the hold was taken. None on a hold from before this field.
    assigned_at: datetime | None = None

    #: Set when this hold must MOVE to another account at the worker's next
    #: turn boundary (S14): `drain` when an operator set the account DRAINING,
    #: written in the same transaction as the state change. None means stay.
    move: str | None = None

    #: Set on a hold that was taken by a SWAP rather than an assign: the
    #: account the attempt came from and why it moved. None on every other.
    swapped_from: str | None = None
    swap_reason: str | None = None

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

    #: The state this account held when something marked it REAUTH_REQUIRED,
    #: so that recovery can give it back rather than promote it.
    #:
    #: REAUTH_REQUIRED is written by a BACKGROUND SWEEP and cleared by a
    #: PERSON, and those two facts together are why this exists. Without it,
    #: the only sensible state to recover to is AVAILABLE -- so an account an
    #: operator had deliberately PAUSED or DRAINING would come back in service
    #: the moment its credential was replaced, with nothing saying the pause
    #: had been overruled. `AccountStore.register` carries the same reasoning
    #: for the same reason: a sweep must not undo a decision nobody asked it
    #: to touch.
    #:
    #: None means "nothing is remembered", which is both a document written
    #: before this field existed and an account that reached REAUTH_REQUIRED
    #: by an operator typing it. Recovery from that is AVAILABLE.
    state_before_reauth: AccountState | None = None

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

    #: What the refresh sweep last did with this account's credential, written
    #: back on EVERY sweep (docs/web-ui/06-accounts.md P2). The sweep had each
    #: account's `RefreshOutcome.expires_at` in hand and threw it away, so the
    #: Accounts screen could show a token's expiry only after a manual refresh.
    #: None on an account no sweep has visited since this field existed.
    #: Metadata only: an instant and a reason, never the token or its length.
    last_refresh_at: datetime | None = None
    token_expires_at: datetime | None = None
    last_refresh_reason: str = ""

    #: The model classes (the frozen `MODEL_CLASSES` names) this account's plan
    #: can run, in the vocabulary's order. EMPTY MEANS EVERY CLASS: that is
    #: every document written before this field, and every account nobody has
    #: narrowed, so nothing changes for an existing pool until an owner says
    #: otherwise. Owner decision R5 (2026-10-17, PR 1509): a plan that cannot
    #: serve a class -- Fable is the expected one -- must never be handed a
    #: step of it. `serves_class` is the one statement of the rule, and
    #: `choose`, `eligibility` and `accounts_serving` all go through it.
    model_classes: tuple[str, ...] = ()

    #: The account's NAME (#1913; owner decision 2026-10-22, BGD, "Editable
    #: display name"): free text its owner chose, shown wherever the console
    #: names the account in place of its label. None is "no Name", which is
    #: every document written before this field, and the label shows.
    #:
    #: DISPLAY TEXT, NEVER AN ID. The label cannot change because it is built
    #: into the account's id, its Secret Manager secret name (`secret_name`)
    #: and a Kubernetes annotation; renaming is this field instead, so nothing
    #: here may feed a secret name, an annotation, a filter or `choose()`.
    #: `validate_display_name` is the one statement of what it may hold.
    display_name: str | None = None

    #: How many agents may hold this account at once (#2222). DEFAULT_MAX_AGENTS
    #: on every document written before this field, and on every account its
    #: owner has not set. Counted against LIVE holds (`is_full`), never the
    #: `assigned` projection, so a killed worker's expired hold frees its slot
    #: without anyone releasing it.
    max_agents: int = DEFAULT_MAX_AGENTS

    #: WHICH SUBSCRIPTION THIS IS (#2222): the opaque identity of the
    #: provider ORGANISATION its credential runs on (`oauth.Organisation`),
    #: recorded at add and at every refresh. Never the login -- one login can
    #: belong to two organisations, and those are two subscriptions. None is
    #: an account the provider has not yet said this about, which is every
    #: document written before this field.
    organisation_id: str | None = None
    #: The organisation's display name and plan ("Personal", "Max"), as the
    #: provider gave them. Display text, not secret, and never an identity.
    organisation_name: str | None = None
    organisation_plan: str | None = None

    #: When this account was first registered. None is a document written
    #: before this field, which is older than any that carries one. Decides
    #: which of two registrations of one organisation is kept (`duplicates`).
    registered_at: datetime | None = None

    #: Another account registered on the SAME organisation (#2222), or None.
    #: Both of a pair carry it, each naming the other side; `duplicate_withheld`
    #: is True on the newer one, which `choose()` never hands out -- one
    #: subscription is one account's worth of agents, however many labels it
    #: has.
    duplicate_of: str | None = None
    duplicate_withheld: bool = False

    def serves_class(self, model_class: str | None) -> bool:
        """Whether a step of `model_class` may run on this account.

        None is a step whose class is NOT KNOWN -- an older worker that sent
        no task id, or a task id that does not name one of the caller's own
        tasks. Only an account that serves every class takes it: a narrowed
        account was narrowed because some class fails on it, and "unknown"
        may be that class.
        """
        if not self.model_classes:
            return True
        return model_class is not None and model_class in self.model_classes

    def live_holds(self, now: datetime) -> tuple[Hold, ...]:
        return tuple(h for h in self.holds if not h.is_expired(now))

    def is_full(self, now: datetime) -> bool:
        """Whether this account already holds as many agents as it may (#2222).

        LIVE holds, not `assigned`: the projection is maintained by the same
        transactions, but a hold whose worker was killed only stops counting
        once it expires, and that is the rule the hold transactions apply when
        they re-check the cap (`quota_broker.main.acquire_hold`).
        """
        return len(self.live_holds(now)) >= self.max_agents

    def awaits_first_reading(self, now: datetime) -> bool:
        """Whether this account has no reading yet AND already holds the one
        agent such an account may (`MAX_AGENTS_BEFORE_READING`).

        LIVE holds, for the reason `is_full` gives. Once a reading arrives
        this is False for good, and the account takes agents up to its
        `max_agents` like any other.
        """
        return (
            self.observed_at is None
            and len(self.live_holds(now)) >= MAX_AGENTS_BEFORE_READING
        )

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
        until = self.unreadable_until(tenant_id, unreadable_after=unreadable_after)
        return until is not None and now <= until

    def unreadable_until(
        self,
        tenant_id: str,
        *,
        unreadable_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> datetime | None:
        """When this tenant's unreadable mark lapses, or None if it has none.

        The mark is time-limited (`is_unreadable_for`), so the instant it lapses
        is an instant the account can be handed to this tenant again -- one
        `eligibility()` must count, or a pool whose only other account is spent
        for the week parks for the week over a mark that ends in half an hour.
        """
        reported = self.unreadable_by.get(tenant_id)
        if reported is None:
            return None
        return _aware(reported) + unreadable_after

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

        A NEVER-OBSERVED ACCOUNT READS AS FULL, AND TAKES ONE AGENT. Optimistic
        on purpose: a new account has to be assignable before it can report
        anything, and assuming it empty makes onboarding a chicken-and-egg
        problem. But optimism alone made it win EVERY pick until the poller
        read it: on 2026-10-24 at 10:38Z eng:saga-team was handed an agent
        before its first reading, and a sign-in that had authorised the wrong
        organisation would have run that agent -- and every one after it --
        against a week already 98% spent. So `choose()` hands a never-observed
        account at most `MAX_AGENTS_BEFORE_READING` agent
        (`awaits_first_reading`); its first reading decides the rest (BGD,
        "One until measured").
        """
        if not self.windows or self.observed_at is None:
            # Never observed: full, and capped at one agent by `choose()`.
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

    def blocking_windows(
        self,
        now: datetime,
        *,
        floor: float | None = None,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> dict[str, WindowReading]:
        """The windows that keep this account from a NEW agent, by name.

        A window blocks while it has not reset and its remaining share is under
        its floor (`window_floor`). An aged reading counts at half, exactly as
        `headroom()` halves it, so with one `floor` for every window this is
        `headroom() < floor` window by window. A never-observed account has no
        reading to block on, and is assignable for the reason `headroom()`
        gives.
        """
        if not self.windows or self.observed_at is None:
            return {}
        factor = 0.5 if now - self.observed_at > stale_after else 1.0
        return {
            name: w
            for name, w in self.windows.items()
            if not w.is_reset(now) and w.remaining() * factor < window_floor(name, floor)
        }

    def assignable(
        self,
        now: datetime,
        *,
        floor: float | None = None,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> bool:
        """Whether a NEW agent may be handed this account: no window blocks it."""
        return not self.blocking_windows(now, floor=floor, stale_after=stale_after)

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
        floor: float | None = None,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> datetime | None:
        """When this account becomes assignable again, or None if nothing is a clock.

        Only windows that are actually CONSTRAINING count. An account with room
        has nothing to wait for, and reporting its next window rollover as a
        "reset" would schedule a wake-up for an account that never stopped --
        which reads, to whoever is looking at it, as though the account had been
        exhausted.

        THE ELIGIBILITY RULE HERE IS THE ONE `choose()` USES (`assignable()`,
        each window against its own floor since #2222), staleness included,
        which it did not used to be. `choose()` rejects on a window's remaining
        share, which `headroom()` and `blocking_windows()` HALVE for an aged
        reading; this compared the raw
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
        if self.assignable(now, floor=floor, stale_after=stale_after):
            return None
        blocking = [
            w.resets_at
            for name, w in self.windows.items()
            if not w.is_reset(now) and w.remaining() < window_floor(name, floor)
        ]
        return min(blocking) if blocking else None

    def with_reading(
        self, windows: dict[str, WindowReading], observed_at: datetime
    ) -> "Account":
        """This account with a reading folded in PER WINDOW, or unchanged.

        A MERGE, NOT A REPLACEMENT (docs/web-ui/06-accounts.md P4, audit
        finding 4). Readings now arrive one window at a time -- a worker's
        stream reports `five_hour` and `seven_day` in separate
        `rate_limit_event`s -- and a replacement let a reading carrying only
        `seven_day` erase a binding `five_hour`, which would make a spent
        account look available.

        Per window, an OLDER reading is DISCARDED. Reports arrive from several
        pods at once and out of order, and letting a stale one overwrite a
        fresh one would make an exhausted account look available again --
        precisely the wrong direction to be wrong in. Each window is dated by
        its own `observed_at`, or by the account's for a window stored before
        windows carried one.

        A window no reading mentions is KEPT, never dropped: a window the
        provider stops reporting stays stored until its `resets_at` passes,
        after which `choose()` no longer counts it. That is harmless, and it is
        the price of the merge -- absence from one reading is not evidence the
        window is gone.
        """
        merged = dict(self.windows)
        changed = False
        for name, reading in windows.items():
            current = merged.get(name)
            dated = None
            if current is not None:
                dated = current.observed_at or self.observed_at
            if current is not None and dated is not None and observed_at <= dated:
                continue
            merged[name] = replace(reading, observed_at=observed_at)
            changed = True
        if not changed:
            return self
        newest = observed_at
        if self.observed_at is not None and self.observed_at > newest:
            newest = self.observed_at
        return replace(self, windows=merged, observed_at=newest)

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
            # "" rather than None: the same empty-means-nothing-remembered
            # spelling `secret` uses, so a document never carries a null that
            # a reader has to tell apart from a missing key.
            "state_before_reauth": (
                self.state_before_reauth.value if self.state_before_reauth else ""
            ),
            "secret": self.secret_ref,
            "observed_at": self.observed_at,
            "windows": windows_to_firestore(self.windows),
            "last_refresh_at": self.last_refresh_at,
            "token_expires_at": self.token_expires_at,
            "last_refresh_reason": self.last_refresh_reason,
            "model_classes": list(self.model_classes),
            "display_name": self.display_name,
            "max_agents": self.max_agents,
            "organisation_id": self.organisation_id,
            "organisation_name": self.organisation_name,
            "organisation_plan": self.organisation_plan,
            "registered_at": self.registered_at,
            "duplicate_of": self.duplicate_of,
            "duplicate_withheld": self.duplicate_withheld,
        }

    @classmethod
    def from_firestore(cls, data: dict[str, Any]) -> "Account":
        windows: dict[str, WindowReading] = {}
        for name, raw in (data.get("windows") or {}).items():
            resets = raw.get("resets_at")
            if not isinstance(resets, datetime):
                continue
            seen = raw.get("observed_at")
            windows[name] = WindowReading(
                utilization=float(raw.get("utilization", 0.0)),
                resets_at=_aware(resets),
                observed_at=_aware(seen) if isinstance(seen, datetime) else None,
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
            state_before_reauth=_optional_state(data.get("state_before_reauth")),
            # Descriptive, so a malformed value reads as absent rather than
            # making the account unreadable and taking it out of the pool.
            last_refresh_at=_optional_instant(data.get("last_refresh_at")),
            token_expires_at=_optional_instant(data.get("token_expires_at")),
            last_refresh_reason=_optional_text(data.get("last_refresh_reason")) or "",
            # NOT descriptive like the three above: it narrows who may be
            # assigned this account, so a malformed value raises and the
            # document is unreadable -- out of the pool -- rather than read as
            # absent, which would mean "every class".
            model_classes=_model_classes_from_firestore(data.get("model_classes")),
            # Descriptive, like the refresh record: a malformed Name reads as
            # no Name -- the label shows -- rather than taking the account out
            # of the pool over a piece of display text.
            display_name=_display_name_from_firestore(data.get("display_name")),
            # NOT descriptive, like `model_classes`: it limits how many agents
            # the account takes, so a malformed value raises and the document
            # is unreadable -- out of the pool -- rather than read as some
            # number nobody chose.
            max_agents=max_agents_from_firestore(data.get("max_agents")),
            # Descriptive: a malformed identity reads as none, which is how
            # every account read before #2222 -- never as some other one.
            organisation_id=_optional_text(data.get("organisation_id")),
            organisation_name=_optional_text(data.get("organisation_name")),
            organisation_plan=_optional_text(data.get("organisation_plan")),
            registered_at=_optional_instant(data.get("registered_at")),
            duplicate_of=_optional_text(data.get("duplicate_of")),
            # Only a stored True withholds: anything else is the default.
            duplicate_withheld=data.get("duplicate_withheld") is True,
        )


def validate_model_classes(classes: Iterable[Any]) -> tuple[str, ...]:
    """`classes` as an account stores them: known names, once each, in the
    vocabulary's order. Empty stays empty, which means every class.

    Raises AccountError naming the vocabulary on an unknown or repeated name,
    so a typo is refused at the admin route rather than quietly narrowing an
    account to nothing that exists.
    """
    names = list(classes)
    unknown = [n for n in names if not isinstance(n, str) or n not in MODEL_CLASSES]
    if unknown:
        raise AccountError(
            f"model_classes names {unknown!r}, which is not in MODEL_CLASSES "
            f"{tuple(MODEL_CLASSES)}; an empty list means every class"
        )
    if len(set(names)) != len(names):
        raise AccountError("model_classes names a class twice")
    return tuple(n for n in MODEL_CLASSES if n in names)


#: The longest Name, in characters, once trimmed. The owner's number
#: (2026-10-22): long enough for "Team Research -- Opus seat 2", short enough
#: that the list row's heading and the pane's never have to be cut mid-word.
DISPLAY_NAME_MAX = 60

#: Unicode categories a Name may not hold: control characters (Cc -- a newline,
#: a tab, NUL, an escape) and the line and paragraph separators (Zl, Zp), each
#: of which breaks the one line every screen draws a Name on.
_NAME_FORBIDDEN = frozenset({"Cc", "Zl", "Zp"})


def validate_display_name(name: Any) -> str:
    """`name` as an account stores it: trimmed, 1 to DISPLAY_NAME_MAX
    characters, capitals and spaces allowed, no control character.

    Raises AccountError, in words a person renaming can act on, for anything
    else. An EMPTY Name is refused rather than stored: clearing is its own
    request (None), so an empty field never silently turns into "no Name".
    """
    if not isinstance(name, str):
        raise AccountError("an account's Name is text; send null to clear it")
    trimmed = name.strip()
    if not trimmed:
        raise AccountError(
            "an account's Name is 1 to 60 characters once trimmed, and this one "
            "is empty; send null to clear it so the label shows"
        )
    if len(trimmed) > DISPLAY_NAME_MAX:
        raise AccountError(
            f"an account's Name is at most {DISPLAY_NAME_MAX} characters once "
            f"trimmed; this one is {len(trimmed)}"
        )
    bad = next((c for c in trimmed if unicodedata.category(c) in _NAME_FORBIDDEN), None)
    if bad is not None:
        raise AccountError(
            f"an account's Name may not hold a control character or a line "
            f"break, and this one holds U+{ord(bad):04X}"
        )
    return trimmed


def _display_name_from_firestore(raw: Any) -> str | None:
    if raw is None:
        return None
    try:
        return validate_display_name(raw)
    except AccountError:
        return None


def validate_max_agents(value: Any) -> int:
    """`value` as an account stores it: a whole number from 1 to MAX_AGENTS_LIMIT.

    Raises AccountError, in words the owner setting it can act on, for anything
    else. Zero is refused rather than read as "take no agents": that is what
    pausing the account says (`AccountState.PAUSED`), and one switch for it is
    enough.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise AccountError("an account's max_agents is a whole number")
    if not 1 <= value <= MAX_AGENTS_LIMIT:
        raise AccountError(
            f"an account's max_agents is 1 to {MAX_AGENTS_LIMIT}, and this is {value}; "
            "pause the account to stop it taking new agents"
        )
    return value


def max_agents_from_firestore(raw: Any) -> int:
    """A stored `max_agents`, DEFAULT_MAX_AGENTS when absent.

    Raises ValueError on a malformed value, which `accountstore._decode` turns
    into a malformed document. Public because the hold transactions in
    `quota_broker.main` read the raw document and must apply the same number.
    """
    if raw is None:
        return DEFAULT_MAX_AGENTS
    try:
        return validate_max_agents(raw)
    except AccountError as exc:
        raise ValueError(str(exc)) from None


def _model_classes_from_firestore(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        # A string iterated would be letters. TypeError is one of the
        # malformed-document errors `accountstore._decode` turns into one.
        raise TypeError("model_classes is not a list")
    try:
        return validate_model_classes(raw)
    except AccountError as exc:
        raise ValueError(str(exc)) from None


def windows_to_firestore(windows: dict[str, WindowReading]) -> dict[str, Any]:
    """`Account.windows` as stored. One spelling, for every writer of the map."""
    out: dict[str, Any] = {}
    for name, w in windows.items():
        item: dict[str, Any] = {"utilization": w.utilization, "resets_at": w.resets_at}
        if w.observed_at is not None:
            item["observed_at"] = w.observed_at
        out[name] = item
    return out


def _optional_instant(raw: Any) -> datetime | None:
    return _aware(raw) if isinstance(raw, datetime) else None


def holds_to_firestore(holds: Iterable[Hold]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in holds:
        item: dict[str, Any] = {
            "assignment_id": h.assignment_id,
            "tenant_id": h.tenant_id,
            "expires_at": h.expires_at,
        }
        # Only when present, so a hold with no stamp is written back in exactly
        # the shape it was read in rather than growing three nulls.
        if h.task_id:
            item["task_id"] = h.task_id
        if h.attempt_id:
            item["attempt_id"] = h.attempt_id
        if h.assigned_at is not None:
            item["assigned_at"] = h.assigned_at
        if h.move:
            item["move"] = h.move
        if h.swapped_from:
            item["swapped_from"] = h.swapped_from
        if h.swap_reason:
            item["swap_reason"] = h.swap_reason
        out.append(item)
    return out


def _optional_text(raw: Any) -> str | None:
    """A non-empty string, or None. Never an exception: the stamp is descriptive."""
    return raw if isinstance(raw, str) and raw else None


def hold_log_entry(
    hold: Hold,
    account_id: str,
    *,
    end: str | None = None,
    released_at: datetime | None = None,
    swapped_to: str | None = None,
    swap_reason: str | None = None,
) -> dict[str, Any]:
    """The `account_holds/{assignment_id}` document for one hold.

    Built from the HOLD every time, open or closed, so a close is a complete
    write rather than a merge onto a record that may not exist: a hold taken
    before this collection existed has no open record, and closing it must
    still leave a whole one. Writing in full also keeps the transaction free of
    a read of the record, so the only document a hold transaction reads is
    still the account.

    `expires_at` is the TTL field. A closed record keeps `HOLD_LOG_RETENTION`
    from when it closed; an OPEN one keeps it from when its hold would lapse,
    so a record whose hold is never closed still ages out rather than living
    forever. (Removing the account closes its open records as
    `account_removed`; see `main.remove_account_and_close_holds`.)

    A SWAP IS TWO RECORDS (S13/S14). The hold that was left closes with
    `end: swapped` and `swapped_to` naming the account the attempt moved to;
    the hold that was taken opens with `swapped_from` (from `Hold`). Each side
    carries the swap's reason (`swapped_to_reason`, `swapped_from_reason`). Written in the swap's own transaction, so either
    account's history read serves the swap.

    NO SECRET NAME, and nothing else from the account: the record says who
    held what, when, and how it ended.
    """
    closed_at = released_at if end is not None else None
    anchor = closed_at if closed_at is not None else hold.expires_at
    return {
        "account_id": account_id,
        "tenant_id": hold.tenant_id,
        "task_id": hold.task_id,
        "attempt_id": hold.attempt_id,
        "assigned_at": hold.assigned_at,
        "released_at": closed_at,
        "end": end,
        "hold_expires_at": hold.expires_at,
        "expires_at": anchor + HOLD_LOG_RETENTION,
        # How this hold BEGAN, when a swap began it -- carried on every write
        # of the record, open or closed, so a hold that came in by a swap and
        # later left by one says both.
        "swapped_from": hold.swapped_from,
        "swapped_from_reason": hold.swap_reason if hold.swapped_from else None,
        # How it ENDED, when a swap ended it.
        "swapped_to": swapped_to if end == HOLD_END_SWAPPED else None,
        "swapped_to_reason": swap_reason if end == HOLD_END_SWAPPED else None,
    }


def holds_from_firestore(raw: Any) -> tuple[Hold, ...]:
    """Read holds back, dropping any that are not shaped like one.

    Skipping a malformed entry rather than raising, for the reason
    `AccountStore.list` gives about malformed documents: one bad hold must not
    make the whole account unreadable and take the pool down with it. A dropped
    hold under-counts by one, which `choose()` survives; an unreadable account
    is one nothing can be assigned from at all.
    """
    # A `holds` that is not a list at all reads as no holds, for the same
    # reason: the release and prune paths read this raw (#243), and `for item
    # in 5` would raise inside the one transaction that could repair it.
    if not isinstance(raw, (list, tuple)):
        return ()
    holds: list[Hold] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        expires = item.get("expires_at")
        assignment_id = item.get("assignment_id")
        if not isinstance(expires, datetime) or not assignment_id:
            continue
        assigned = item.get("assigned_at")
        holds.append(
            Hold(
                assignment_id=str(assignment_id),
                tenant_id=str(item.get("tenant_id") or ""),
                expires_at=_aware(expires),
                # Absent on a hold from before #379, and malformed reads as
                # absent rather than dropping the hold: a bad stamp must not
                # cost the COUNT an agent.
                task_id=_optional_text(item.get("task_id")),
                attempt_id=_optional_text(item.get("attempt_id")),
                assigned_at=_aware(assigned) if isinstance(assigned, datetime) else None,
                move=_optional_text(item.get("move")),
                swapped_from=_optional_text(item.get("swapped_from")),
                swap_reason=_optional_text(item.get("swap_reason")),
            )
        )
    return tuple(holds)


def _aware(value: datetime) -> datetime:
    """Firestore hands back naive datetimes in some client versions."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _optional_state(raw: Any) -> AccountState | None:
    """A remembered state, or None -- never an exception.

    Deliberately more forgiving than the `state` field above, which is allowed
    to raise and take the document out of `AccountStore.list` with it. This one
    is ADVISORY: it only says where recovery should put the account back. An
    unrecognised value here must not make an account unreadable and remove it
    from the pool, because the account itself is fine.
    """
    if not raw:
        return None
    try:
        return AccountState(str(raw))
    except ValueError:
        return None


def account_id_for(owner_tenant: str, label: str) -> str:
    validate_label(label)
    return f"{owner_tenant}:{label}"


def accounts_serving(
    accounts: Iterable[Account],
    tenant_id: str,
    provider: str,
    *,
    model_class: str | None = "",
) -> list[Account]:
    """Every account the pool could hand `tenant_id` for `provider`, in any state.

    THE POOL'S SCOPE, STATED ONCE: owned by the tenant or lent to it
    (`may_serve`, which is invariant 9), and of the provider asked for. Two
    things ask it:

      * the assign route, which narrows it further -- state, headroom, what
        this attempt already tried -- through `choose()` and `eligibility()`;
      * the scheduler's admission (`scheduler.credentials`), which asks only
        whether it is EMPTY. Empty is exactly the broker's
        `no_accounts_registered`: the pool is not how this tenant runs, the
        worker falls back to the tenant's own key, and a tenant with no key
        then has nothing to run on. Anything else means the pool IS how it
        runs, and a spent or paused account is a wait the worker parks on.

    Before 2026-09-25 the route wrote this out inline and nothing else asked,
    so admission could not tell a tenant the pool serves from one it does not
    (#169).

    AND THE STEP'S MODEL CLASS (owner decision R5), when one is asked about:
    an account that does not serve it is not in the pool FOR THAT STEP, so a
    class no account serves is `no_accounts_registered` to the worker -- it
    falls back to the tenant's own key, which serves every class -- and
    admission parks a keyless tenant's step before any lease. The default,
    "", asks about no class: the scope as it was, for the callers (the
    listing, the provider count) that are not placing a step.
    """
    return [
        a
        for a in accounts
        if a.provider == provider
        and a.may_serve(tenant_id)
        and (model_class == "" or a.serves_class(model_class))
    ]


def _registration_order(account: Account) -> tuple[int, datetime, str]:
    """Older first: a document with no `registered_at` predates the field."""
    return (
        0 if account.registered_at is None else 1,
        account.registered_at or datetime.min.replace(tzinfo=timezone.utc),
        account.account_id,
    )


def duplicates(accounts: Iterable[Account]) -> dict[str, tuple[str, bool]]:
    """Every account sharing an organisation with another: `(other, withheld)`.

    ONE ORGANISATION IS ONE SUBSCRIPTION (#2222), whatever its labels, owners
    or logins. Of the accounts on one organisation the OLDEST registration is
    kept: it names the next one and is not withheld. Every newer one names the
    kept one and is withheld, so `choose()` hands the subscription out once.
    An account with no recorded organisation is in no pair: absence is not
    sameness. An account in no pair is absent from the answer.
    """
    groups: dict[tuple[str, str], list[Account]] = {}
    for a in accounts:
        identity = getattr(a, "organisation_id", None)
        if identity:
            groups.setdefault((a.provider, identity), []).append(a)
    out: dict[str, tuple[str, bool]] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        kept, *newer = sorted(group, key=_registration_order)
        out[kept.account_id] = (newer[0].account_id, False)
        for a in newer:
            out[a.account_id] = (kept.account_id, True)
    return out


def _withheld(accounts: list[Account]) -> set[str]:
    """Accounts `choose()` must not hand out because an older registration of
    the same organisation is the one in use: the stored mark, or a pair in
    this very list that the sweep has not marked yet."""
    marked = {a.account_id for a in accounts if getattr(a, "duplicate_withheld", False) is True}
    return marked | {i for i, (_, held) in duplicates(accounts).items() if held}


def choose(
    accounts: Iterable[Account],
    tenant_id: str,
    now: datetime,
    *,
    assign_floor: float | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    exclude: Iterable[str] = (),
    model_class: str | None = "",
) -> Account | None:
    """The account a new agent for `tenant_id` should get, or None.

    Ordering, in priority:
      1. the tenant's OWN accounts before borrowed ones -- a loan is a courtesy
         and should be spent last;
      2. most headroom first;
      3. fewest agents already on it, to spread load rather than stack it;
      4. label, so the choice is deterministic and a test can assert it.

    AN ACCOUNT ALREADY HOLDING ITS `max_agents` IS NO CANDIDATE (#2222).
    Headroom does not move until the next reading, so "most headroom first"
    alone stacked every new agent on one account; the cap is what makes the
    ordering spread them.

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

    `model_class` is the step's class (`Account.serves_class`); "" asks about
    none. An account that cannot run the step's model is never handed it,
    however much headroom it has (owner decision R5).

    A DUPLICATE IS HANDED OUT ONCE (#2222): of two accounts on one
    organisation only the older registration is a candidate (`duplicates`).
    AND A NEVER-MEASURED ACCOUNT TAKES ONE AGENT (`awaits_first_reading`):
    it is skipped while it holds one and has no reading, and is an ordinary
    candidate from its first reading on.
    """
    skip = {str(a) for a in exclude}
    accounts = list(accounts)
    withheld = _withheld(accounts)
    candidates = [
        a
        for a in accounts
        if a.state in ASSIGNABLE_STATES
        and a.account_id not in withheld
        and not a.awaits_first_reading(now)
        and a.may_serve(tenant_id)
        and (model_class == "" or a.serves_class(model_class))
        and a.account_id not in skip
        and not a.is_unreadable_for(tenant_id, now, unreadable_after=stale_after)
        and a.assignable(now, floor=assign_floor, stale_after=stale_after)
        and not a.is_full(now)
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
    assign_floor: float | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    exclude: Iterable[str] = (),
    model_class: str | None = "",
) -> tuple[Unavailable, datetime | None]:
    """WHY nothing can be assigned, and when that changes.

    Call it only when `choose()` returned None, with the same arguments, so the
    two cannot disagree about what "unavailable" meant. That coupling is the
    whole reason this exists as a function rather than as a few lines in the
    route: the route used to answer "when" with a rule that did not match the
    rule `choose()` had just answered "no" with, and the two disagreed in
    exactly the band where it mattered.

    The second element is an INSTANT and only ever that: the EARLIEST moment
    any candidate could take the agent (#1885). A pool waiting only on a
    person, or only on the next usage poll, has no instant, and inventing one
    would wake a task up to the same answer. The reason is what the caller
    picks its wait from.

    EVERY CANDIDATE COUNTS, WHATEVER BLOCKS IT. This used to take the minimum
    over accounts blocked by a WINDOW and nothing else, so an account blocked
    by something with no window -- a reading too old to trust, a tenant's
    unreadable mark -- dropped out of the minimum, and the answer became
    another account's seven-day clear. Measured 2026-10-21: a step parked
    with `next_check_at` 46 h out, an account was assigned to another step 53
    minutes later, and the park was still waiting 2 h 48 min after that. Each
    kind of block now says when it can end:

      * a window under the floor: `Account.next_reset`, the provider's
        `resetsAt`;
      * a tenant's unreadable mark: when it lapses (`unreadable_until`), or
        the account's own window clear if that is later;
      * a reading too old to trust, with no window under the floor: the
        broker's next usage poll. Undated, so the reason becomes
        `NO_RECENT_READING` -- whose wait is minutes -- even when another
        account carries a far window instant; the earliest dated instant still
        travels with it, for a window that clears sooner than the poll;
      * paused, draining or needing re-authentication: a person. The one block
        with no instant at all -- when somebody acts is not a time the broker
        can know -- so it shortens nothing here.

      * no reading yet, and already holding the one agent such an account
        may (`Account.awaits_first_reading`, #2222): the broker's next usage
        poll, which reads a never-observed account first. Undated, so the
        reason is `NO_RECENT_READING`, as for an aged reading.

      * a later registration of an organisation another account already
        holds (`duplicates`, #2222): a person, who removes one of the two.
        Undated and no candidate at all, so a pool of nothing else is
        `POOL_PAUSED`.

      * every slot taken (`Account.is_full`, #2222), with its windows clear:
        an agent on it finishing. Undated -- no instant predicts a finish --
        so the reason becomes `ACCOUNTS_FULL`, which the worker re-asks within
        minutes; the earliest dated instant still travels with it. The
        scheduler re-asks the moment an agent finishes anyway
        (`scheduler.loop.Scheduler.release_dependants`), because a finishing
        agent frees a slot and brings the freshest readings the pool has.

    `exclude` is this ATTEMPT's list and dies with it, so an account it names
    is no candidate for this answer: the worker parks such an attempt as
    unreadable (`lifecycle._pool_credential_env`), and the tenant's unreadable mark,
    which outlives the attempt, is what dates it.
    """
    skip = {str(a) for a in exclude}
    accounts = list(accounts)
    withheld = _withheld(accounts)
    serves = [
        a
        for a in accounts
        if a.may_serve(tenant_id) and (model_class == "" or a.serves_class(model_class))
    ]
    if not serves:
        # The pool is not how this tenant runs at all. The ONE answer that
        # means "fall back to your tenant secret" rather than "wait", and the
        # reason every deployment that has never registered an account is
        # unaffected by any of this.
        return Unavailable.NO_ACCOUNTS_REGISTERED, None

    untried = [a for a in serves if a.account_id not in skip]

    def window_clear(a: Account) -> datetime | None:
        return a.next_reset(now, floor=assign_floor, stale_after=stale_after)

    # Accounts this tenant recently could not read, which can take the agent
    # once the mark lapses -- and once their own window clears, if one blocks.
    marks: list[datetime] = []
    for a in untried:
        if a.state not in ASSIGNABLE_STATES or not a.is_unreadable_for(
            tenant_id, now, unreadable_after=stale_after
        ):
            continue
        lapse = a.unreadable_until(tenant_id, unreadable_after=stale_after)
        if lapse is None:
            continue
        clear = window_clear(a)
        marks.append(max(lapse, clear) if clear is not None else lapse)

    mine = [
        a
        for a in untried
        if not a.is_unreadable_for(tenant_id, now, unreadable_after=stale_after)
    ]
    if not mine:
        # Accounts exist and this attempt has ruled every one of them out --
        # it tried them and could not read them. A wait, not a fallback: a
        # tenant whose pool is broken must not quietly go back to sharing one
        # subscription, which is the contention the pool exists to remove.
        return Unavailable.NO_ACCOUNT_AVAILABLE, min(marks) if marks else None

    assignable = [
        a for a in mine if a.state in ASSIGNABLE_STATES and a.account_id not in withheld
    ]
    if not assignable and not marks:
        # Paused, draining or needing re-authentication. Every one of those is
        # a person's decision or a person's job, so there is nothing to wait
        # for and no point waking up sooner.
        return Unavailable.POOL_PAUSED, None

    instants = list(marks)
    waiting_on_poll = False
    full = False
    for a in assignable:
        clear = window_clear(a)
        if clear is not None:
            instants.append(clear)
        elif a.is_stale(now, stale_after=stale_after):
            # Blocked by the staleness halving alone. Not evidence of
            # exhaustion: the broker's own usage poll refreshes this reading
            # on its next sweep, which is sooner than any window but undated.
            waiting_on_poll = True
        elif a.awaits_first_reading(now):
            # Holding the one agent a never-measured account may: its first
            # reading, the usage poll's to take, decides the rest. Before
            # `is_full` so a never-measured account whose cap is one reads as
            # waiting on that reading, not as full.
            waiting_on_poll = True
        elif a.is_full(now):
            # Room by every window, and every slot taken: it frees when an
            # agent on it finishes, sooner than any window but undated.
            full = True
    earliest = min(instants) if instants else None

    if waiting_on_poll:
        return Unavailable.NO_RECENT_READING, earliest
    if full:
        return Unavailable.ACCOUNTS_FULL, earliest

    # Every candidate is spent by a window or held off by a mark, and
    # `earliest` is the first of them to clear. None only for an account
    # assignable, fresh and under the floor with no unreset window: genuinely
    # spent, with nothing that can say when it clears.
    return Unavailable.NO_ACCOUNT_AVAILABLE, earliest


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
    "DEFAULT_MAX_AGENTS",
    "MAX_AGENTS_LIMIT",
    "MAX_AGENTS_BEFORE_READING",
    "WINDOW_FLOORS",
    "window_floor",
    "DEFAULT_HOLD_TTL",
    "DEFAULT_STALE_AFTER",
    "HOLD_END_ACCOUNT_REMOVED",
    "HOLD_END_EXPIRED",
    "HOLD_END_RELEASED",
    "HOLD_END_SWAPPED",
    "HOLD_END_UNUSABLE",
    "HOLD_ENDS",
    "REFRESH_REASONS",
    "SWAP_DRAIN",
    "SWAP_EXHAUSTED",
    "SWAP_REASONS",
    "SWAP_UNUSABLE",
    "HOLD_LOG_COLLECTION",
    "HOLD_LOG_RETENTION",
    "account_id_for",
    "accounts_serving",
    "choose",
    "due_for_refresh",
    "duplicates",
    "eligibility",
    "holds_from_firestore",
    "holds_to_firestore",
    "hold_log_entry",
    "max_agents_from_firestore",
    "secret_name",
    "validate_label",
    "validate_max_agents",
    "windows_to_firestore",
]
